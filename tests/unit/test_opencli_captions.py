import json
from pathlib import Path

import pytest

from soota.errors import AuthBudgetError, ContractError, SourceError
from soota.models import CaptionAcquisition, CaptionPayload, MediaItem, TranscriptMode, TranscriptSource
from soota.pipeline import estimate, ingest
from soota.providers.opencli import OpenCLIProvider
from soota.providers.opencli_runtime import OpenCLIRuntime
from soota.providers.process import ProcessResult
from soota.routing import recognize
from soota.transcripts.conversion import convert_captions
from tests.unit.test_opencli import BILI, REAL_RUN, fail, video_rows

YOUTUBE = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
YOUTUBE_DASH_ID = "https://www.youtube.com/watch?v=--Example01&t=16s"


def test_youtube_id_starting_with_dash_uses_canonical_url_for_opencli(settings, monkeypatch):
    source = recognize(YOUTUBE_DASH_ID)
    calls = []

    def command(self, site, name, args):
        calls.append((site, name, args))
        return (
            [{"field": "videoId", "value": source.id}, {"field": "title", "value": "Example"}]
            if name == "video"
            else [{"start": "0.0s", "end": "1.0s", "text": "Caption text"}]
        )

    monkeypatch.setattr(OpenCLIProvider, "_run", command)
    result = ingest(settings, YOUTUBE_DASH_ID, provider="opencli", transcript=TranscriptMode.CAPTIONS_ONLY, max_cost=0)

    assert result.ok
    assert [args[0] for _, _, args in calls] == [source.canonical_url, source.canonical_url]


@pytest.mark.parametrize(
    "stderr,expected",
    [
        ("Navigation rejected", "浏览器导航被拒绝"),
        ("Caption URL returned empty response", "字幕地址返回空响应"),
    ],
)
def test_opencli_caption_failures_report_specific_errors(settings, monkeypatch, stderr, expected):
    monkeypatch.setattr(OpenCLIProvider, "_run", REAL_RUN)
    monkeypatch.setattr(OpenCLIRuntime, "resolve", lambda self: ("opencli", "external"))
    monkeypatch.setattr("soota.providers.opencli.preflight", lambda *args: None)

    def process(argv, **kwargs):
        return ProcessResult(0, "1.8.3", "") if "--version" in argv else ProcessResult(1, "", stderr)

    monkeypatch.setattr("soota.providers.opencli.run_process", process)
    item = MediaItem(source=recognize(YOUTUBE_DASH_ID), title="Example")
    with pytest.raises(SourceError, match=expected):
        OpenCLIProvider(settings).get_captions(item)


@pytest.mark.parametrize("mode", [TranscriptMode.AUTO, TranscriptMode.CAPTIONS_ONLY])
@pytest.mark.parametrize("platform,url", [("bilibili", BILI), ("youtube", YOUTUBE)])
def test_ingest_defaults_to_opencli_captions_without_tikhub(settings, monkeypatch, mode, platform, url):
    monkeypatch.setattr("soota.pipeline.StdioMCPClient.available", lambda config: False)
    calls = []

    def command(self, site, name, args):
        calls.append((site, name, args))
        self.version = "1.8.3"
        if name == "video":
            return (
                video_rows()
                if site == "bilibili"
                else [{"field": "videoId", "value": "dQw4w9WgXcQ"}, {"field": "title", "value": "Example"}]
            )
        if site == "bilibili":
            return [{"index": 1, "from": "0.25s", "to": "1.50s", "content": "字幕原文"}]
        return [{"index": 1, "start": "0.25s", "end": "1.50s", "text": "Caption text"}]

    monkeypatch.setattr(OpenCLIProvider, "_run", command)
    monkeypatch.setattr("soota.pipeline._provider", fail)
    monkeypatch.setattr("soota.providers.router.TikHubProvider", fail)
    result = ingest(settings, url, transcript=mode, max_cost=0)
    manifest = json.loads(Path(result.data["manifest"]).read_text())
    assert result.ok and result.data["provider"] == "opencli"
    assert manifest["transcript"]["source"] == "unknown" and manifest["transcript"]["language"] is None
    assert "seg-000001" in Path(result.data["transcript"]).read_text()
    assert ("字幕原文" if platform == "bilibili" else "Caption text") in Path(result.data["content"]).read_text()
    assert calls[-1][1] == ("subtitle" if platform == "bilibili" else "transcript")
    if platform == "youtube":
        assert calls[-1][2][-2:] == ["--mode", "raw"]
    calls.clear()
    cached = ingest(settings, url, transcript=mode, max_cost=0, force=True)
    assert cached.ok and cached.data["cache_hit"] and not calls


def test_default_estimate_is_free_offline_and_never_checks_tikhub_wallet(settings, monkeypatch):
    monkeypatch.setattr("soota.pipeline.StdioMCPClient.available", lambda config: False)
    monkeypatch.setattr("soota.pipeline._provider", fail)
    result = estimate(settings, BILI)
    assert result.data["provider"] == "opencli" and result.data["estimated_cost_usd"] == 0
    assert len(result.data["endpoints"]) == 2 and not settings.data_dir.exists()


def test_empty_captions_preserve_metadata_without_paid_fallback_and_do_not_poison_cache(settings, monkeypatch):
    from soota.providers.opencli_runtime import RuntimeUnavailable
    from soota.providers.ytdlp import YtDlpProvider

    def unavailable(*args):
        raise RuntimeUnavailable("yt-dlp absent")

    monkeypatch.setattr(YtDlpProvider, "inspect", unavailable)
    monkeypatch.setattr("soota.pipeline.StdioMCPClient.available", lambda config: False)
    rows = []
    monkeypatch.setattr(
        OpenCLIProvider, "_run", lambda self, site, name, args: video_rows() if name == "video" else rows
    )
    monkeypatch.setattr("soota.pipeline._provider", fail)
    result = ingest(settings, BILI, max_cost=0)
    assert result.exit_code == 2 and Path(result.data["manifest"]).is_file()
    rows.append({"from": "0.0s", "to": "1.0s", "content": "Now available"})
    result = ingest(settings, BILI, max_cost=0, force=True)
    assert result.ok


@pytest.mark.parametrize("error", [AuthBudgetError("login or 429"), ContractError("invalid captions")])
def test_caption_restrictions_and_contract_errors_never_trigger_paid_fallback(settings, monkeypatch, error):
    monkeypatch.setattr("soota.pipeline.StdioMCPClient.available", lambda config: False)

    def command(self, site, name, args):
        if name == "video":
            return video_rows()
        raise error

    monkeypatch.setattr(OpenCLIProvider, "_run", command)
    monkeypatch.setattr("soota.pipeline._provider", fail)
    with pytest.raises(type(error)):
        ingest(settings, BILI, fallback="tikhub", max_cost=0.1)


@pytest.mark.usefixtures("cached_tikhub_prices")
def test_explicit_tikhub_skips_opencli(settings, monkeypatch):
    from tests.unit.test_resolver import _youtube_requester

    monkeypatch.setattr(OpenCLIProvider, "__init__", fail)
    result = ingest(
        settings,
        YOUTUBE,
        provider="tikhub",
        transcript=TranscriptMode.CAPTIONS_ONLY,
        requester=_youtube_requester(),
        max_cost=0.1,
    )
    assert result.ok and result.data["transcript_source"] != TranscriptSource.UNKNOWN


@pytest.mark.usefixtures("cached_tikhub_prices")
def test_authorized_caption_fallback_runs_only_after_opencli_unknown(settings, monkeypatch):
    from tests.unit.test_resolver import _youtube_requester

    events = []

    def command(self, site, name, args):
        events.append("opencli:" + name)
        return (
            [{"field": "videoId", "value": "dQw4w9WgXcQ"}, {"field": "title", "value": "Example"}]
            if name == "video"
            else []
        )

    from soota.providers.opencli_runtime import RuntimeUnavailable
    from soota.providers.ytdlp import YtDlpProvider

    def unavailable(*args):
        events.append("yt-dlp")
        raise RuntimeUnavailable("yt-dlp unavailable")

    monkeypatch.setattr(YtDlpProvider, "inspect", unavailable)
    paid = _youtube_requester()

    def requester(*args, **kwargs):
        events.append("tikhub")
        return paid(*args, **kwargs)

    monkeypatch.setattr(OpenCLIProvider, "_run", command)
    result = ingest(
        settings, YOUTUBE, fallback="tikhub", max_cost=0.1, transcript=TranscriptMode.CAPTIONS_ONLY, requester=requester
    )
    assert result.ok and events[:2] == ["opencli:video", "opencli:transcript"]
    assert "tikhub" in events[2:] and result.data["routing"]["attempted"] == ["opencli", "yt-dlp", "tikhub"]


@pytest.mark.parametrize(
    "row",
    [
        {"from": "NaNs", "to": "1s", "content": "x"},
        {"from": "2s", "to": "1s", "content": "x"},
        {"from": "0s", "to": "1s", "content": 42},
    ],
)
def test_invalid_caption_segments_are_not_silently_accepted(row):
    with pytest.raises(ContractError):
        convert_captions(
            CaptionAcquisition(status="ready", payload=CaptionPayload(format="opencli-bilibili", body=[row]))
        )
