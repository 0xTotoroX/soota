from pathlib import Path

import pytest

from soota.models import TranscriptMode, TranscriptSegment, TranscriptSource
from soota.pipeline import ingest
from tests.conftest import load_fixture

pytestmark = pytest.mark.usefixtures("cached_tikhub_prices")


@pytest.fixture(autouse=True)
def fake_audio_decode_for_routing_contracts(monkeypatch):
    # These tests use synthetic byte placeholders and fake ASR. Actual decoding
    # and final media acceptance are covered by test_media_* and content tasks.
    monkeypatch.setattr("soota.transcripts.resolver.ensure_audio", lambda path, **kwargs: path)
    monkeypatch.setattr("soota.transcripts.quality.audio_duration_ms", lambda path: 1000)


class FakeAsr:
    name = "fake"

    def transcribe(self, audio: Path, *, language: str | None):
        return [
            TranscriptSegment(
                segment_id="seg-000001",
                start_ms=0,
                end_ms=1000,
                text="asr text",
                language=language,
                source=TranscriptSource.ASR,
            )
        ]


def _youtube_requester(missing: bool = False):
    calls: list[str] = []

    def requester(method: str, path: str, params: dict):
        calls.append(path)
        if path.endswith("get_video_info_v2"):
            return load_fixture("youtube_info.json")
        if path.endswith("get_video_captions"):
            if missing:
                return load_fixture("youtube_captions_missing.json")
            if not params.get("language_code"):
                return load_fixture("youtube_captions_list.json")
            return load_fixture("youtube_captions_srt.json")
        if path.endswith("get_video_streams_v2"):
            return load_fixture("youtube_streams.json")
        raise AssertionError(path)

    requester.calls = calls  # type: ignore[attr-defined]
    return requester


def test_captions_preferred_and_no_asr(settings, tmp_path) -> None:
    requester = _youtube_requester(missing=False)

    def http_get(url: str):  # pragma: no cover
        raise AssertionError("cdn not used for captions")

    result = ingest(
        settings,
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        provider="tikhub",
        transcript=TranscriptMode.AUTO,
        requester=requester,
        asr=FakeAsr(),
        audio_provider="tikhub",
    )
    assert result.exit_code == 0
    assert result.data["transcript_source"] == TranscriptSource.OFFICIAL
    assert not any("streams" in c for c in requester.calls)


def test_missing_captions_runs_asr(settings, monkeypatch) -> None:
    requester = _youtube_requester(missing=True)

    class Resp:
        def __init__(self, content: bytes):
            self.content = content

        def json(self):
            return {}

        def raise_for_status(self) -> None:
            return None

    def fake_get(url: str) -> Resp:
        return Resp(b"audio-bytes")

    from soota.providers.tikhub import client as client_mod

    original_init = client_mod.TikHubProvider.__init__

    def wrapped(self, *args, **kwargs):
        kwargs.setdefault("http_get", fake_get)
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(client_mod.TikHubProvider, "__init__", wrapped)
    result = ingest(
        settings,
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        provider="tikhub",
        transcript=TranscriptMode.AUTO,
        requester=requester,
        asr=FakeAsr(),
        audio_provider="tikhub",
    )
    assert result.data["transcript_source"] == TranscriptSource.ASR
    assert any("streams" in c for c in requester.calls)


def test_second_run_is_cache_hit(settings) -> None:
    requester = _youtube_requester()
    ingest(
        settings,
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        requester=requester,
        asr=FakeAsr(),
        audio_provider="tikhub",
        max_cost=0.1,
        provider="tikhub",
    )
    first = list(requester.calls)
    requester.calls.clear()
    result = ingest(
        settings,
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        requester=requester,
        asr=FakeAsr(),
        audio_provider="tikhub",
        max_cost=0.1,
        provider="tikhub",
    )
    assert result.data["cache_hit"] is True
    assert requester.calls == []
    assert first


def _bili_requester(*, empty_subtitles: bool = False):
    from tests.conftest import load_fixture

    calls: list[str] = []
    info = load_fixture("bilibili_info.json")
    if empty_subtitles:
        info = {
            "code": 200,
            "data": {
                "View": {
                    **info["data"]["View"],
                    "subtitle": {"list": []},
                }
            },
        }

    def requester(method: str, path: str, params: dict):
        calls.append(path)
        if path.endswith("fetch_one_video_v3"):
            assert "url" in params and "bv_id" not in params
            return info
        if path.endswith("fetch_video_subtitle"):
            return {"code": 200, "data": {"subtitles": []}}
        if path.endswith("fetch_video_playurl"):
            return load_fixture("youtube_streams.json")
        raise AssertionError(path)

    requester.calls = calls  # type: ignore[attr-defined]
    return requester


def test_bili_inspect_tracks_skip_paid_subtitle(settings, monkeypatch) -> None:
    from tests.conftest import load_fixture

    requester = _bili_requester()

    class Resp:
        def json(self):
            return load_fixture("bilibili_subtitle_body.json")

        def raise_for_status(self) -> None:
            return None

        content = b"{}"

    from soota.providers.tikhub import client as client_mod

    original_init = client_mod.TikHubProvider.__init__

    def wrapped(self, *args, **kwargs):
        kwargs.setdefault("http_get", lambda url: Resp())
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(client_mod.TikHubProvider, "__init__", wrapped)
    result = ingest(
        settings,
        "https://www.bilibili.com/video/BV1M1421t7hT",
        provider="tikhub",
        transcript=TranscriptMode.AUTO,
        requester=requester,
        asr=FakeAsr(),
        audio_provider="tikhub",
    )
    assert result.data["transcript_source"] == TranscriptSource.OFFICIAL
    assert not any("subtitle" in c for c in requester.calls)
    content = Path(result.data["content"]).read_text(encoding="utf-8")
    assert "[seg-000001] 你好" in content
    assert "[seg-000002] 世界" in content
    assert "## 摘要" not in content
    assert "你好世界" in content or "你好" in content


def test_bili_empty_inspect_still_probes_subtitle(settings, monkeypatch) -> None:
    requester = _bili_requester(empty_subtitles=True)

    class Resp:
        def __init__(self, content: bytes = b"audio-bytes"):
            self.content = content

        def json(self):
            return {}

        def raise_for_status(self) -> None:
            return None

    from soota.providers.tikhub import client as client_mod

    original_init = client_mod.TikHubProvider.__init__

    def wrapped(self, *args, **kwargs):
        kwargs.setdefault("http_get", lambda url: Resp())
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(client_mod.TikHubProvider, "__init__", wrapped)
    result = ingest(
        settings,
        "https://www.bilibili.com/video/BV1M1421t7hT",
        provider="tikhub",
        transcript=TranscriptMode.AUTO,
        requester=requester,
        asr=FakeAsr(),
        audio_provider="tikhub",
    )
    assert any("fetch_video_subtitle" in c for c in requester.calls)
    assert result.data["transcript_status"] == "unknown"
    assert not any("playurl" in c for c in requester.calls)


def test_explicit_tikhub_audio_failure_does_not_change_provider(settings, monkeypatch) -> None:
    from soota.errors import SourceError

    requester = _youtube_requester(missing=True)
    monkeypatch.setattr(
        "soota.providers.ytdlp.YtDlpProvider.get_audio", lambda *a: pytest.fail("implicit source switch")
    )
    monkeypatch.setattr(
        "soota.providers.tikhub.client.TikHubProvider._download",
        lambda *args: (_ for _ in ()).throw(SourceError("cdn failure")),
    )
    with pytest.raises(SourceError, match="cdn failure"):
        ingest(
            settings,
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            provider="tikhub",
            transcript=TranscriptMode.AUTO,
            requester=requester,
            asr=FakeAsr(),
            audio_provider="tikhub",
        )
    assert not list(settings.cache_dir.rglob("streams*.json"))
