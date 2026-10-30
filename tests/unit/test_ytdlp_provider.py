import json
import re
from pathlib import Path

import pytest

from soota.errors import AuthBudgetError, CaptionUnavailable, ContractError, SourceError
from soota.models import CaptionPayload, CaptionResult, MediaItem, TranscriptMode, TranscriptSource
from soota.pipeline import estimate, ingest
from soota.providers.opencli import OpenCLIProvider
from soota.providers.opencli_runtime import RuntimeUnavailable
from soota.providers.process import ProcessResult
from soota.providers.router import catalog_provider, inspect_item
from soota.providers.ytdlp import YtDlpProvider, select_track
from soota.routing import recognize
from soota.transcripts.conversion import convert_captions, parse_payload

URL = "https://www.youtube.com/watch?v=--Example01&t=16s"
INFO = {
    "id": "--Example01",
    "title": "Example",
    "channel": "Sample channel",
    "duration": 2,
    "subtitles": {"zh": [{"ext": "json3", "url": "https://example.invalid/?secret=hidden"}]},
    "automatic_captions": {"en": [{"ext": "json3"}]},
}
BODY = {"events": [{"tStartMs": 100, "dDurationMs": 900, "segs": [{"utf8": "字幕"}, {"utf8": "内容"}]}]}


def stub_process(monkeypatch, *, info=None, body=None, error=None):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if error:
            return ProcessResult(1, "", error)
        if "--dump-single-json" in argv:
            return ProcessResult(0, json.dumps(INFO if info is None else info), "")
        template = argv[argv.index("-o") + 1]
        Path(template.replace("%(ext)s", "zh.json3")).write_text(json.dumps(BODY if body is None else body))
        return ProcessResult(0, "", "")

    monkeypatch.setattr("soota.providers.ytdlp.shutil.which", lambda _: "/test/yt-dlp")
    monkeypatch.setattr("soota.providers.ytdlp.run_process", run)
    return calls


def test_explicit_soota_downloads_only_selected_captions_and_reuses_cache(settings, monkeypatch):
    calls = stub_process(monkeypatch)
    result = ingest(settings, URL, provider="yt-dlp", transcript=TranscriptMode.CAPTIONS_ONLY, max_cost=0)
    manifest = json.loads(Path(result.data["manifest"]).read_text())
    assert result.ok and result.data["provider"] == "yt-dlp"
    assert manifest["transcript"]["language"] == "zh" and manifest["transcript"]["source"] == "official"
    assert manifest["processing"]["estimated_cost_usd"] == 0
    assert result.data["routing"]["attempted"] == ["yt-dlp"]
    assert len(calls) == 2
    for call in calls:
        assert "--ignore-config" in call and "--skip-download" in call and "--no-playlist" in call
        assert call[-2:] == ["--", recognize(URL).canonical_url]
    assert "--write-subs" in calls[1] and "--no-write-auto-subs" in calls[1]
    assert all("hidden" not in file.read_text() for file in settings.cache_dir.rglob("*.json"))
    cached = ingest(settings, URL, provider="yt-dlp", max_cost=0, force=True)
    assert cached.ok and cached.data["cache_hit"] and len(calls) == 2


@pytest.mark.parametrize("failure", [CaptionUnavailable("empty response"), RuntimeUnavailable("bridge absent"), None])
def test_auto_tries_soota_after_opencli_before_any_paid_request(settings, monkeypatch, failure):
    calls = stub_process(monkeypatch)

    def command(self, site, command, args):
        if command == "video":
            return [{"field": "videoId", "value": "--Example01"}, {"field": "title", "value": "Example"}]
        if failure:
            raise failure
        return []

    monkeypatch.setattr(OpenCLIProvider, "_run", command)
    result = ingest(settings, URL, max_cost=0)
    assert result.ok and result.data["routing"]["attempted"] == ["opencli", "yt-dlp"]
    assert result.data["provider"] == "yt-dlp" and len(calls) == 2


@pytest.mark.parametrize("failure", [AuthBudgetError("429"), ContractError("bad data"), SourceError("unknown")])
def test_access_and_unknown_errors_do_not_switch_providers(settings, monkeypatch, failure):
    calls = stub_process(monkeypatch)

    def fail(*args):
        raise failure

    monkeypatch.setattr(OpenCLIProvider, "inspect", fail)
    with pytest.raises(type(failure)):
        ingest(settings, URL)
    assert not calls


def test_explicit_opencli_does_not_switch_to_soota(settings, monkeypatch):
    calls = stub_process(monkeypatch)
    monkeypatch.setattr(OpenCLIProvider, "inspect", lambda self, source: MediaItem(source=source))

    def fail(*args):
        raise CaptionUnavailable("empty response")

    monkeypatch.setattr(OpenCLIProvider, "get_captions", fail)
    with pytest.raises(CaptionUnavailable):
        ingest(settings, URL, provider="opencli")
    assert not calls


@pytest.mark.parametrize("body", [{}, {"events": []}, {"events": [{"tStartMs": -1, "segs": [{"utf8": "text"}]}]}])
def test_bad_caption_payload_is_not_cached_as_missing(settings, monkeypatch, body):
    stub_process(monkeypatch, body=body)
    provider = YtDlpProvider(settings)
    item = provider.inspect(recognize(URL))
    with pytest.raises(ContractError):
        convert_captions(provider.get_captions(item))
    assert provider.cache.get_json(item.source, "captions") is None


def test_wrong_video_metadata_rejected(settings, monkeypatch):
    stub_process(monkeypatch, info={**INFO, "id": "OtherID0001"})
    with pytest.raises(ContractError):
        YtDlpProvider(settings).inspect(recognize(URL))


def test_restrictions_are_sanitized(settings, monkeypatch):
    stub_process(monkeypatch, error="HTTP Error 429 signed-secret-url")
    with pytest.raises(AuthBudgetError) as exc:
        YtDlpProvider(settings).inspect(recognize(URL))
    assert "secret" not in str(exc.value)


def test_missing_captions_is_partial_and_no_negative_cache(settings, monkeypatch):
    stub_process(monkeypatch, info={**INFO, "subtitles": {}, "automatic_captions": {}})
    result = ingest(settings, URL, provider="yt-dlp", transcript=TranscriptMode.CAPTIONS_ONLY, max_cost=0)
    assert result.exit_code == 2
    provider = YtDlpProvider(settings)
    assert provider.cache.get_json(recognize(URL), "captions") is None


def test_language_selection_respects_requested_language_then_official_track():
    tracks = {"official": ["en", "zh-Hans"], "auto": ["zh"]}
    assert select_track(tracks, "zh") == ("zh", TranscriptSource.AUTO)
    assert select_track(tracks, "en") == ("en", TranscriptSource.OFFICIAL)
    assert select_track({"official": ["zh"], "auto": ["zh"]}, "zh") == ("zh", TranscriptSource.OFFICIAL)


def test_unsupported_catalog_and_audio_are_not_silently_paid(settings):
    with pytest.raises(SourceError):
        catalog_provider(recognize("https://www.youtube.com/@Example"), "yt-dlp")
    asr_plan = estimate(settings, URL, provider="yt-dlp", transcript=TranscriptMode.ASR)
    assert asr_plan.data["estimated_cost_usd"] == 0
    assert asr_plan.data["audio_provider"] == "yt-dlp"
    result = estimate(settings, URL, provider="yt-dlp")
    assert result.data["provider"] == "yt-dlp" and result.data["estimated_cost_usd"] == 0


def test_metadata_fallback_preserves_previous_records(settings, monkeypatch):
    from soota.models import RequestRecord

    stub_process(monkeypatch)

    def unavailable(self, source):
        self.records.append(RequestRecord(endpoint="opencli:attempt", billed=False))
        raise RuntimeUnavailable("absent")

    monkeypatch.setattr(OpenCLIProvider, "inspect", unavailable)
    item, records, warnings = inspect_item(settings, recognize(URL), max_cost=0)
    assert item.provenance["provider"] == "yt-dlp"
    assert [r.endpoint for r in records] == ["opencli:attempt", "yt-dlp:youtube/video"]
    assert warnings


def test_feature_output_is_written(settings, monkeypatch, tmp_path):
    from soota.features import call_feature, list_features

    stub_process(monkeypatch)
    target = tmp_path / "detail.json"
    result = call_feature(settings, "youtube.video.detail", params=[f"url={URL}"], provider="yt-dlp", output=target)
    assert result.ok and Path(result.data["output"]) == target
    assert json.loads(target.read_text())["metadata"]["title"] == "Example"
    plans = list_features(settings, provider="yt-dlp").data["features"]
    assert [r["id"] for r in plans] == ["bilibili.video.detail", "youtube.video.detail"]


@pytest.mark.usefixtures("cached_tikhub_prices")
def test_paid_confirmation_still_required_after_both_free_unavailable(settings, monkeypatch):
    from soota.billing import PaymentRequired
    from soota.config import PaymentConfig
    from tests.unit.test_resolver import _youtube_requester

    settings.payment = PaymentConfig(mode="ask")

    def unavailable(*args):
        raise RuntimeUnavailable("unavailable")

    monkeypatch.setattr(OpenCLIProvider, "inspect", unavailable)
    monkeypatch.setattr(YtDlpProvider, "inspect", unavailable)
    with pytest.raises(PaymentRequired):
        ingest(settings, URL, max_cost=0.1, transcript=TranscriptMode.CAPTIONS_ONLY, requester=_youtube_requester())


def test_counterpart_can_use_soota_and_preserves_original_identity(settings, monkeypatch):
    from soota.counterpart import youtube_counterpart
    from tests.unit.test_content_selection import Client, media

    original = media("bilibili")
    info = {**INFO, "id": media("youtube").source.id}
    stub_process(monkeypatch, info=info)
    fallback = YtDlpProvider(settings)
    client = Client(missing=True)
    from soota.models import CaptionResult, TranscriptStatus

    monkeypatch.setattr(client, "get_captions", lambda item: CaptionResult(status=TranscriptStatus.UNKNOWN))
    item, captions, _ = youtube_counterpart(client, original, fallback)
    assert captions and captions.language == "zh"
    assert item.provenance["provider"] == "yt-dlp"
    assert item.provenance["selection"]["requested_source"]["id"] == original.source.id
    assert item.provenance["youtube_check"]["status"] == "selected"
    assert len(fallback.records) == 2


def test_zero_budget_with_both_free_unavailable_never_constructs_paid_provider(settings, monkeypatch):
    def unavailable(*args):
        raise RuntimeUnavailable("unavailable")

    monkeypatch.setattr(OpenCLIProvider, "inspect", unavailable)
    monkeypatch.setattr(YtDlpProvider, "inspect", unavailable)
    monkeypatch.setattr("soota.pipeline._provider", lambda *a, **k: pytest.fail("paid provider constructed"))
    with pytest.raises(SourceError):
        ingest(settings, URL, max_cost=0)


BILI_URL = "https://www.bilibili.com/video/BV1xx411c7mD"
BILI_INFO = {
    "id": "BV1xx411c7mD_p1",
    "title": "Bilibili sample",
    "uploader": "Example creator",
    "subtitles": {
        "danmaku": [{"ext": "xml"}],
        "ai-en": [{"ext": "srt"}],
        "ai-zh": [{"ext": "srt"}],
    },
}
SRT = "1\n00:00:01,200 --> 00:00:02,300\n第一行\n第二行\n\n"


def stub_bili(monkeypatch, *, info=None, body=SRT, warning=""):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if "--dump-single-json" in argv:
            return ProcessResult(0, json.dumps(BILI_INFO if info is None else info), warning)
        template = argv[argv.index("-o") + 1]
        language = argv[argv.index("--sub-langs") + 1].replace("\\", "")
        Path(template.replace("%(ext)s", f"{language}.srt")).write_text(body)
        return ProcessResult(0, "", "")

    monkeypatch.setattr("soota.providers.ytdlp.shutil.which", lambda _: "/test/yt-dlp")
    monkeypatch.setattr("soota.providers.ytdlp.run_process", run)
    return calls


def test_bili_ai_subtitle_download_preserves_source_and_timestamps(settings, monkeypatch):
    calls = stub_bili(monkeypatch)
    result = ingest(settings, BILI_URL, provider="yt-dlp", transcript=TranscriptMode.CAPTIONS_ONLY, max_cost=0)
    manifest = json.loads(Path(result.data["manifest"]).read_text())
    assert result.ok and result.data["provider"] == "yt-dlp"
    assert manifest["transcript"]["source"] == "auto"
    assert manifest["transcript"]["language"] == "ai-zh"
    doc = convert_captions(YtDlpProvider(settings).get_captions(MediaItem(source=recognize(BILI_URL))))
    assert [(s.start_ms, s.end_ms, s.text) for s in doc.segments] == [(1200, 2300, "第一行\n第二行")]
    assert "--write-subs" in calls[1] and "--no-write-auto-subs" in calls[1]
    assert re.fullmatch(calls[1][calls[1].index("--sub-langs") + 1], "ai-zh")
    assert "--simulate" in calls[0] and all("--skip-download" in call for call in calls)
    assert len(calls) == 2


def test_bili_prefers_uploaded_chinese_over_ai_chinese(settings, monkeypatch):
    stub_bili(monkeypatch, info={**BILI_INFO, "subtitles": {**BILI_INFO["subtitles"], "zh": [{"ext": "srt"}]}})
    provider = YtDlpProvider(settings)
    result = provider.get_captions(provider.inspect(recognize(BILI_URL)))
    assert result.language == "zh" and result.source == TranscriptSource.OFFICIAL


def test_bili_auto_routing_uses_ytdlp_before_paid_provider(settings, monkeypatch):
    stub_bili(monkeypatch)
    monkeypatch.setattr(OpenCLIProvider, "inspect", lambda self, source: MediaItem(source=source))
    from soota.models import CaptionResult, TranscriptStatus

    monkeypatch.setattr(OpenCLIProvider, "get_captions", lambda *a: CaptionResult(status=TranscriptStatus.UNKNOWN))
    monkeypatch.setattr("soota.pipeline._provider", lambda *a, **k: pytest.fail("paid provider constructed"))
    result = ingest(settings, BILI_URL, max_cost=0)
    assert result.ok and result.data["routing"]["attempted"] == ["opencli", "yt-dlp"]


def test_bili_login_warning_stops_without_paid_fallback(settings, monkeypatch):
    stub_bili(monkeypatch, warning="WARNING: Subtitles are only available when logged in. secret-url")
    monkeypatch.setattr("soota.pipeline._provider", lambda *a, **k: pytest.fail("paid provider constructed"))
    with pytest.raises(AuthBudgetError) as exc:
        ingest(settings, BILI_URL, provider="yt-dlp", fallback="tikhub", max_cost=0.1)
    assert "secret" not in str(exc.value)


def test_danmaku_alone_is_not_a_transcript(settings, monkeypatch):
    calls = stub_bili(monkeypatch, info={**BILI_INFO, "subtitles": {"danmaku": [{"ext": "xml"}]}})
    result = ingest(settings, BILI_URL, provider="yt-dlp", transcript=TranscriptMode.CAPTIONS_ONLY, max_cost=0)
    assert result.exit_code == 2 and len(calls) == 1


def test_bili_wrong_part_and_invalid_srt_are_rejected(settings, monkeypatch):
    stub_bili(monkeypatch, info={**BILI_INFO, "id": "BV1xx411c7mD_p2"})
    with pytest.raises(ContractError):
        YtDlpProvider(settings).inspect(recognize(BILI_URL))
    stub_bili(monkeypatch, body="not a subtitle")
    provider = YtDlpProvider(settings)
    with pytest.raises(ContractError):
        convert_captions(provider.get_captions(provider.inspect(recognize(BILI_URL))))
    assert provider.cache.get_json(recognize(BILI_URL), "captions") is None


def test_legacy_provider_name_and_config_use_canonical_output(settings, monkeypatch):
    from soota.config import RoutingConfig
    from soota.features import list_features

    stub_process(monkeypatch)
    result = ingest(settings, URL, provider="soota", max_cost=0)
    assert result.data["provider"] == "yt-dlp"
    assert result.data["routing"]["attempted"] == ["yt-dlp"]
    assert RoutingConfig(default_provider="soota").default_provider == "yt-dlp"
    assert list_features(settings, provider="soota").data == list_features(settings, provider="yt-dlp").data


def test_bili_feature_alias_uses_ytdlp(settings, monkeypatch):
    from soota.features import call_feature

    stub_bili(monkeypatch)
    result = call_feature(settings, "bilibili.video.detail", params=[f"url={BILI_URL}"], provider="yt-dlp")
    assert result.ok and result.data["provider"] == "yt-dlp"
    assert result.data["operation"] == "bilibili/video"


def test_legacy_cache_is_read_without_relabeling_existing_files(settings, monkeypatch):
    import time

    monkeypatch.setattr("soota.providers.ytdlp.run_process", lambda *a, **k: pytest.fail("unexpected request"))
    provider = YtDlpProvider(settings)
    source = recognize(URL)
    item = MediaItem(source=source, provenance={"provider": "soota", "caption_tracks": {"official": ["zh"]}})
    caption = CaptionResult(
        status="ready",
        language="zh",
        source=TranscriptSource.OFFICIAL,
        segments=parse_payload(
            CaptionPayload(format="json3", body=BODY), language="zh", source=TranscriptSource.OFFICIAL
        ),
    )
    for name, value in (("inspect", item), ("captions", caption)):
        provider.legacy_cache.put_json(
            source, name, {"timestamp": time.time(), "result": value.model_dump(mode="json")}
        )
    assert provider.inspect(source).provenance["provider"] == "yt-dlp"
    assert provider.get_captions(item).language == "zh"
    assert all(r.cache_hit for r in provider.records)
    assert provider.legacy_cache.get_json(source, "inspect")["result"]["provenance"]["provider"] == "soota"


def test_bili_av_link_resolves_to_stable_bv_identity(settings, monkeypatch):
    url = "https://www.bilibili.com/video/av12345"
    stub_bili(monkeypatch, info={**BILI_INFO, "original_url": url, "webpage_url": BILI_URL})
    result = ingest(settings, url, provider="yt-dlp", max_cost=0)
    assert result.ok
    assert json.loads(Path(result.data["manifest"]).read_text())["source"]["id"] == recognize(BILI_URL).id


def test_audio_download_skips_captions_and_reuses_only_completed_file(settings, monkeypatch):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        template = argv[argv.index("-o") + 1]
        Path(template.replace("%(ext)s", "m4a")).write_bytes(b"audio")
        return ProcessResult(0, "", "")

    monkeypatch.setattr("soota.providers.ytdlp.shutil.which", lambda _: "/test/binary")
    monkeypatch.setattr("soota.providers.ytdlp.run_process", run)
    provider = YtDlpProvider(settings)
    item = MediaItem(source=recognize(URL))
    result = provider.get_audio(item)
    assert result.path.read_bytes() == b"audio" and result.provider == "yt-dlp"
    assert "--skip-download" not in calls[0] and "--no-write-subs" in calls[0]
    assert calls[0][-2:] == ["--", item.source.canonical_url]
    provider.get_audio(item)
    assert len(calls) == 1 and provider.records[-1].cache_hit


@pytest.mark.parametrize(
    "formats,chosen,body",
    [
        (["srt", "vtt"], "vtt", "WEBVTT\n\n00:00.123 --> 00:01.234\n获取格式测试"),
        (["ttml"], "ttml", '<tt><body><p begin=".123s" dur="1.111s">获取格式测试</p></body></tt>'),
        (["srv3"], "srv3", '<timedtext><body><p t="123" d="1111">获取格式测试</p></body></timedtext>'),
    ],
)
def test_available_formats_are_selected_before_shared_conversion(settings, monkeypatch, formats, chosen, body):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if "--dump-single-json" in argv:
            info = {**INFO, "subtitles": {"zh": [{"ext": f} for f in formats]}, "automatic_captions": {}}
            return ProcessResult(0, json.dumps(info), "")
        assert argv[argv.index("--sub-format") + 1] == chosen
        template = argv[argv.index("-o") + 1]
        Path(template.replace("%(ext)s", f"zh.{chosen}")).write_text(body)
        return ProcessResult(0, "", "")

    monkeypatch.setattr("soota.providers.ytdlp.shutil.which", lambda _: "/test/yt-dlp")
    monkeypatch.setattr("soota.providers.ytdlp.run_process", run)
    result = ingest(settings, URL, provider="yt-dlp", transcript=TranscriptMode.CAPTIONS_ONLY, max_cost=0)
    assert result.ok
    assert "获取格式测试" in Path(result.data["context"]).read_text()
    assert "00:00:00.123-00:00:01.234" in Path(result.data["transcript"]).read_text()
    assert len(calls) == 2


def test_old_metadata_with_filtered_tracks_is_refreshed(settings, monkeypatch):
    import time

    calls = stub_process(monkeypatch)
    provider = YtDlpProvider(settings)
    source = recognize(URL)
    old = MediaItem(
        source=source,
        provenance={
            "caption_probe_complete": True,
            "caption_availability": "present",
            "caption_tracks": {"official": [], "auto": []},
        },
    )
    provider.cache.put_json(source, "inspect", {"timestamp": time.time(), "result": old.model_dump(mode="json")})
    assert provider.inspect(source).provenance["caption_formats"]["official"]["zh"] == ["json3"]
    assert len(calls) == 1


def test_invalid_caption_encoding_is_a_contract_error(settings, monkeypatch):
    provider = YtDlpProvider(settings)
    item = MediaItem(source=recognize(URL), provenance={"caption_tracks": {"official": ["zh"]}})

    def run(source, args, operation):
        Path(args[args.index("-o") + 1].replace("%(ext)s", "zh.json3")).write_bytes(b"\xff\xfeinvalid")

    monkeypatch.setattr(provider, "_run", run)
    with pytest.raises(ContractError, match="UTF-8"):
        provider.get_captions(item)
