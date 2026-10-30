from pathlib import Path

import pytest

from soota.errors import AuthBudgetError, CaptionUnavailable
from soota.models import (
    AudioAsset,
    CaptionAcquisition,
    CaptionPayload,
    CaptionResult,
    MediaItem,
    RequestRecord,
    TranscriptMode,
    TranscriptStatus,
)
from soota.pipeline import ingest
from soota.providers.opencli import OpenCLIProvider
from soota.providers.opencli_runtime import OpenCLIRuntime
from soota.providers.process import ProcessResult
from soota.providers.tikhub.normalize import youtube_caption_content
from soota.providers.ytdlp import YtDlpProvider
from soota.routing import recognize
from soota.transcripts.conversion import convert_captions
from tests.unit.test_opencli import REAL_RUN
from tests.unit.test_resolver import FakeAsr
from tests.unit.test_ytdlp_provider import INFO, URL, stub_process


@pytest.fixture(autouse=True)
def fake_audio_decode_for_routing_contracts(monkeypatch):
    # These tests use synthetic byte placeholders and fake ASR. Actual decoding
    # and final media acceptance are covered by test_media_* and content tasks.
    monkeypatch.setattr("soota.transcripts.resolver.ensure_audio", lambda path, **kwargs: path)
    monkeypatch.setattr("soota.transcripts.quality.audio_duration_ms", lambda path: 1000)


BILI = "https://www.bilibili.com/video/BV1xx411c7mD"


def setup_media(monkeypatch, status):
    monkeypatch.setattr(
        OpenCLIProvider,
        "inspect",
        lambda self, source: MediaItem(source=source, title="Example", provenance={"provider": "opencli"}),
    )
    monkeypatch.setattr(OpenCLIProvider, "get_captions", lambda *a: CaptionResult(status=status))
    monkeypatch.setattr("soota.pipeline._provider", lambda *a, **k: pytest.fail("paid provider constructed"))


def stub_audio(monkeypatch, events):
    def audio(self, item):
        events.append("audio")
        path = self.cache.audio_path(item.source)
        path.write_bytes(b"fake-audio")
        self.records.append(RequestRecord(endpoint="yt-dlp:audio", billed=False))
        return AudioAsset(path=path)

    monkeypatch.setattr(YtDlpProvider, "get_audio", audio)


@pytest.mark.parametrize("mode", [TranscriptMode.AUTO, TranscriptMode.CAPTIONS_ONLY])
def test_confirmed_absent_never_queries_next_caption_provider(settings, monkeypatch, mode):
    setup_media(monkeypatch, TranscriptStatus.MISSING)
    monkeypatch.setattr(YtDlpProvider, "inspect", lambda *a: pytest.fail("next metadata/caption query"))
    monkeypatch.setattr(YtDlpProvider, "get_captions", lambda *a: pytest.fail("next caption query"))
    events = []
    stub_audio(monkeypatch, events)
    result = ingest(settings, BILI, transcript=mode, max_cost=0, asr=FakeAsr())
    assert result.data["routing"]["attempted"] == ["opencli"]
    if mode == TranscriptMode.AUTO:
        assert result.ok and result.data["transcript_source"] == "asr" and events == ["audio"]
        assert result.data["audio_provider"] == "yt-dlp"
        assert result.data["asr_engine"] == "fake"
    else:
        assert not result.ok and result.data["transcript_status"] == "missing" and not events


def test_unknown_is_not_transcribed_without_confirmation(settings, monkeypatch):
    setup_media(monkeypatch, TranscriptStatus.UNKNOWN)
    result = ingest(settings, BILI, provider="opencli", max_cost=0, asr=FakeAsr())
    assert not result.ok and result.code == "transcript_unknown"
    assert result.data["transcript_status"] == "unknown"
    assert "unknown" in Path(result.data["content"]).read_text()


def test_ytdlp_confirms_no_tracks_then_runs_free_asr(settings, monkeypatch):
    setup_media(monkeypatch, TranscriptStatus.UNKNOWN)
    calls = stub_process(monkeypatch, info={**INFO, "subtitles": {}, "automatic_captions": {}})
    events = []
    stub_audio(monkeypatch, events)
    result = ingest(settings, URL, max_cost=0, asr=FakeAsr())
    assert result.ok and events == ["audio"] and len(calls) == 1
    assert result.data["routing"]["attempted"] == ["opencli", "yt-dlp"]
    assert not any(r["billed"] for r in result.data["requests"])


def test_explicit_asr_never_probes_captions(settings, monkeypatch):
    setup_media(monkeypatch, TranscriptStatus.UNKNOWN)
    monkeypatch.setattr(OpenCLIProvider, "get_captions", lambda *a: pytest.fail("caption query"))
    events = []
    stub_audio(monkeypatch, events)
    result = ingest(settings, BILI, transcript=TranscriptMode.ASR, max_cost=0, asr=FakeAsr())
    assert result.ok and events == ["audio"]


def test_downie_only_runs_when_explicitly_selected(settings, monkeypatch):
    setup_media(monkeypatch, TranscriptStatus.MISSING)
    events = []
    stub_audio(monkeypatch, events)

    class Downie:
        name = "downie"

        def __init__(self, settings):
            self.records = []

        def get_audio(self, item):
            events.append("downie")
            path = settings.cache_dir / "downie-test.m4a"
            path.write_bytes(b"fake-audio")
            return AudioAsset(path=path)

    monkeypatch.setattr("soota.providers.audio.DownieProvider", Downie)
    ingest(settings, BILI, max_cost=0, asr=FakeAsr())
    assert events == ["audio"]
    result = ingest(settings, BILI, max_cost=0, asr=FakeAsr(), audio_provider="downie", force=True)
    assert result.ok and result.data["audio_provider"] == "downie" and events == ["audio", "downie"]


@pytest.mark.parametrize(
    "platform,stderr,expected",
    [
        ("bilibili", "code: EMPTY_RESULT\n此视频没有发现外挂或智能字幕。", "missing"),
        ("bilibili", "code: EMPTY_RESULT\n字幕文件中没有字幕片段。", "unavailable"),
        ("youtube", "code: EMPTY_RESULT\n该视频没有字幕", "unavailable"),
    ],
)
def test_opencli_only_trusts_specific_empty_track_signal(settings, monkeypatch, platform, stderr, expected):
    monkeypatch.setattr(OpenCLIProvider, "_run", REAL_RUN)
    monkeypatch.setattr(OpenCLIRuntime, "resolve", lambda self: ("opencli", "external"))
    monkeypatch.setattr("soota.providers.opencli.preflight", lambda *a: None)
    monkeypatch.setattr(
        "soota.providers.opencli.run_process",
        lambda argv, **kw: ProcessResult(0, "1.8.8", "") if "--version" in argv else ProcessResult(66, "", stderr),
    )
    provider = OpenCLIProvider(settings)
    item = MediaItem(source=recognize(BILI if platform == "bilibili" else URL))
    if expected == "missing":
        assert provider.get_captions(item).status == TranscriptStatus.MISSING
    else:
        with pytest.raises(CaptionUnavailable):
            provider.get_captions(item)
    assert (
        convert_captions(
            CaptionAcquisition(status="ready", payload=CaptionPayload(format=f"opencli-{platform}", body=[]))
        ).status
        == TranscriptStatus.UNKNOWN
    )


@pytest.mark.parametrize(
    "info,status",
    [
        ({**INFO, "subtitles": {}, "automatic_captions": {}}, "missing"),
        ({k: v for k, v in INFO.items() if k not in {"subtitles", "automatic_captions"}}, "unknown"),
        ({**INFO, "subtitles": {"zh": [{"ext": "unsupported-format"}]}, "automatic_captions": {}}, "unknown"),
    ],
)
def test_ytdlp_absent_is_not_missing_metadata_or_unsupported_format(settings, monkeypatch, info, status):
    stub_process(monkeypatch, info=info)
    provider = YtDlpProvider(settings)
    item = provider.inspect(recognize(URL))
    assert provider.get_captions(item).status.value == status


def test_failed_tikhub_job_with_empty_list_is_not_absent():
    from soota.models import TranscriptSource

    result = youtube_caption_content(
        {"code": 200, "data": {"status": "failed", "captions": []}},
        language="zh",
        source_kind=TranscriptSource.OFFICIAL,
    )
    assert result.status == TranscriptStatus.UNKNOWN


def test_old_missing_cache_does_not_short_circuit_new_probe(settings, monkeypatch):
    from soota.storage.cache import CacheStore
    from soota.transcripts.resolver import TranscriptResolver

    source = recognize(URL)
    cache = CacheStore(settings.cache_dir)
    cache.put_json(source, "transcript", {"status": "missing", "segments": []})
    calls = []

    class Captions:
        def get_captions(self, item):
            calls.append("probe")
            return CaptionResult(status=TranscriptStatus.UNKNOWN)

    result = TranscriptResolver(Captions(), cache, settings, asr=FakeAsr()).resolve(
        MediaItem(source=source), TranscriptMode.AUTO
    )
    assert calls == ["probe"] and result.status == TranscriptStatus.UNKNOWN


def test_confirmed_counterpart_absence_does_not_query_ytdlp(settings, monkeypatch):
    from soota.counterpart import youtube_counterpart
    from tests.unit.test_content_selection import Client, media

    monkeypatch.setattr(YtDlpProvider, "inspect", lambda *a: pytest.fail("next caption provider"))
    original = media("bilibili")
    item, captions, notes = youtube_counterpart(Client(missing=True), original, YtDlpProvider(settings))
    assert item.source == original.source and captions is None
    assert item.provenance["youtube_check"]["status"] == "youtube_captions_missing"


@pytest.mark.usefixtures("cached_tikhub_prices")
def test_audio_budget_deducts_paid_metadata_already_requested(settings, monkeypatch):
    from soota.providers.opencli_runtime import RuntimeUnavailable
    from soota.providers.tikhub.client import YOUTUBE_INFO, YOUTUBE_STREAMS, TikHubProvider, load_price_snapshot
    from tests.unit.test_resolver import _youtube_requester

    def unavailable(*args, **kwargs):
        raise RuntimeUnavailable("not installed")

    monkeypatch.setattr(OpenCLIProvider, "inspect", unavailable)
    monkeypatch.setattr(YtDlpProvider, "inspect", unavailable)
    monkeypatch.setattr(TikHubProvider, "_download", lambda self, url, dest: dest.write_bytes(b"audio"))
    prices = load_price_snapshot()["endpoints"]
    cap = max(prices[YOUTUBE_INFO], prices[YOUTUBE_STREAMS])
    requester = _youtube_requester()
    with pytest.raises(AuthBudgetError, match="超过"):
        ingest(
            settings,
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            transcript=TranscriptMode.ASR,
            audio_provider="tikhub",
            max_cost=cap,
            requester=requester,
            asr=FakeAsr(),
        )
    assert requester.calls == [YOUTUBE_INFO]


def test_auto_estimate_includes_conditional_paid_audio_without_network(settings, monkeypatch):
    from soota.pipeline import estimate

    monkeypatch.setattr("soota.pipeline._provider", lambda *a, **k: pytest.fail("network client created"))
    result = estimate(settings, BILI, provider="opencli", audio_provider="tikhub")
    assert result.data["caption_estimated_cost_usd"] == 0
    assert result.data["estimated_cost_usd"] > 0
    assert result.data["conditional_audio"]["condition"] == "confirmed_no_captions"
    assert all("subtitle" not in path for path in result.data["conditional_audio"]["endpoints"])


def test_explicit_ytdlp_asr_skips_subtitle_discovery(settings, monkeypatch):
    calls = stub_process(monkeypatch)
    events = []
    stub_audio(monkeypatch, events)
    monkeypatch.setattr(YtDlpProvider, "get_captions", lambda *a: pytest.fail("caption query"))
    result = ingest(settings, URL, provider="yt-dlp", transcript=TranscriptMode.ASR, max_cost=0, asr=FakeAsr())
    assert result.ok and events == ["audio"]
    assert "--no-write-subs" in calls[0] and "--write-subs" not in calls[0]


def test_none_mode_is_not_reported_as_confirmed_absence(settings, monkeypatch):
    setup_media(monkeypatch, TranscriptStatus.MISSING)
    monkeypatch.setattr(OpenCLIProvider, "get_captions", lambda *a: pytest.fail("caption query"))
    result = ingest(settings, BILI, transcript=TranscriptMode.NONE, max_cost=0)
    assert result.ok and result.data["transcript_status"] == "not_requested"


@pytest.mark.usefixtures("cached_tikhub_prices")
def test_pending_downie_resume_does_not_repay_caption_query(settings, monkeypatch):
    from soota.providers.downie import DownloadPending
    from tests.unit.test_resolver import _youtube_requester

    attempts = []
    requester = _youtube_requester(missing=True)

    class Downie:
        name = "downie"

        def __init__(self, settings):
            self.records = []

        def get_audio(self, item):
            attempts.append("audio")
            if len(attempts) == 1:
                raise DownloadPending("waiting")
            path = settings.cache_dir / "downie-test.m4a"
            path.write_bytes(b"fake-audio")
            return AudioAsset(path=path, provider="downie")

    monkeypatch.setattr("soota.providers.audio.DownieProvider", Downie)
    args = dict(provider="tikhub", audio_provider="downie", requester=requester, asr=FakeAsr())
    url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    with pytest.raises(DownloadPending):
        ingest(settings, url, **args)
    already_requested = list(requester.calls)
    result = ingest(settings, url, max_cost=0, **args)
    assert result.ok and result.data["audio_provider"] == "downie"
    assert requester.calls == already_requested
    assert result.data["estimated_cost_usd"] == 0


@pytest.mark.parametrize("login_flag,expected", [(False, "missing"), (None, "unknown"), (True, "auth")])
def test_tikhub_bili_empty_list_requires_visibility_evidence(settings, login_flag, expected):
    from soota.providers.tikhub.client import TikHubProvider
    from soota.storage.cache import CacheStore

    payload = {"code": 200, "data": {"subtitles": []}}
    if login_flag is not None:
        payload["data"]["need_login_subtitle"] = login_flag
    client = TikHubProvider(cache=CacheStore(settings.cache_dir), api_key="test", requester=lambda *a: payload)
    media = MediaItem(source=recognize(BILI), extras={"aid": "12345", "cid": "67890"})
    if expected == "auth":
        with pytest.raises(AuthBudgetError):
            client.get_captions(media)
    else:
        assert client.get_captions(media).status.value == expected
