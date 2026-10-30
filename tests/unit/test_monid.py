"""Synthetic transport tests: budget safety, resumability and caption contracts."""

import json

import httpx
import pytest
from typer.testing import CliRunner

from soota.billing import actual_cost
from soota.cli import app
from soota.config import PaymentConfig, PriorityRule, load_settings
from soota.errors import AuthBudgetError, ContractError, SourceError
from soota.models import CaptionAcquisition, MediaItem, TranscriptMode, TranscriptStatus
from soota.pipeline import ingest
from soota.providers.monid.client import APIFY_CAPTIONS, MonidClient
from soota.providers.monid.provider import MonidProvider
from soota.providers.priority import ordered_names, quote_rows
from soota.routing import recognize
from soota.storage.task_budget import TaskBudget, task_budget_scope
from soota.transcripts.acquisition import acquire_captions

URL = "https://www.youtube.com/watch?v=Example0001"


def install(settings, monkeypatch, handler, cap=0.005):
    settings.monid.enabled = True
    settings.monid.poll_seconds = 0
    monkeypatch.setenv("MONID_API_KEY", "synthetic-test-key")
    return MonidClient(settings, cap, transport=httpx.MockTransport(handler))


def quote(value=0.005, kind="PER_RESULT"):
    return {"price": {"type": kind, "amount": {"value": value, "currency": "USD"}}}


def final(row=None):
    return {
        "runId": "abc123",
        "status": "COMPLETED",
        "providerResponse": {"httpStatus": 200},
        "cost": {"value": 0.005, "currency": "USD"},
        "output": [
            row
            or {
                "video_id": "Example0001",
                "title": "Synthetic example",
                "duration_seconds": 10,
                "status": "success",
                "language": "en",
                "is_auto_generated": False,
                "transcript": [{"start": 1.2, "duration": 2, "text": "Example sentence."}],
            }
        ],
    }


def test_caption_chain_once_and_shared_conversion(settings, monkeypatch):
    calls = []
    budget = TaskBudget(settings.data_dir / "task", 0.005)

    def handler(request):
        calls.append(request.url.path)
        if request.url.path == "/v1/inspect":
            return httpx.Response(200, json=quote())
        assert budget.snapshot()["reserved_cost_usd"] == "0.005"
        assert json.loads(request.content)["input"]["body"]["youtube_url"] == URL
        return httpx.Response(200, json=final())

    client = install(settings, monkeypatch, handler)
    with task_budget_scope(budget):
        provider = MonidProvider(settings, client=client)
        item = provider.inspect(recognize(URL))
        captions = acquire_captions(provider, item)
    assert captions.status == TranscriptStatus.READY
    assert captions.segments[0].start_ms == 1200 and captions.segments[0].end_ms == 3200
    assert item.provenance["upstream_provider"] == "apify"
    assert calls == ["/v1/inspect", "/v1/run"]
    assert actual_cost(client.records) == 0.005

    # A second invocation reuses the receipt and the converted cache without new spending.
    other = MonidProvider(settings, client=install(settings, monkeypatch, handler, cap=0))
    with task_budget_scope(budget):
        assert acquire_captions(other, other.inspect(recognize(URL))).segments
    assert len(calls) == 2 and actual_cost(other.records) == 0


@pytest.mark.parametrize("category,expected", [("transcripts_disabled", "missing"), ("upstream_error", "unknown")])
def test_empty_results_billed_but_only_explicit_absence_is_missing(settings, monkeypatch, category, expected):
    row = {"video_id": "Example0001", "status": "error", "error_category": category, "transcript": []}
    client = install(
        settings,
        monkeypatch,
        lambda r: httpx.Response(200, json=quote() if r.url.path.endswith("inspect") else final(row)),
    )
    provider = MonidProvider(settings, client=client)
    result = acquire_captions(provider, provider.inspect(recognize(URL)))
    assert result.status == expected and actual_cost(client.records) == 0.005


@pytest.mark.parametrize("cost,cap", [(0.006, 0.005), (0.005, 0)])
def test_live_quote_blocks_dispatch_above_cap(settings, monkeypatch, cost, cap):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, json=quote(cost))

    client = install(settings, monkeypatch, handler, cap)
    with pytest.raises(AuthBudgetError):
        client.run("apify", APIFY_CAPTIONS, {"body": {"youtube_url": URL}})
    assert calls == ["/v1/inspect"]


def test_timeout_resume_polls_original_run_without_new_reservation(settings, monkeypatch):
    settings.monid.max_polls = 1
    calls = []
    done = False

    def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith("inspect"):
            data = quote()
        else:
            data = final() if done else {"runId": "abc123", "status": "RUNNING"}
        return httpx.Response(200, json=data)

    client = install(settings, monkeypatch, handler)
    inputs = {"body": {"youtube_url": URL}}
    budget = TaskBudget(settings.data_dir / "task", 0.005)
    with task_budget_scope(budget), pytest.raises(SourceError):
        client.run("apify", APIFY_CAPTIONS, inputs)
    done = True
    second = install(settings, monkeypatch, handler)
    with task_budget_scope(budget):
        assert second.run("apify", APIFY_CAPTIONS, inputs)["status"] == "COMPLETED"
    assert calls.count("/v1/run") == 1
    assert budget.snapshot()["reserved_cost_usd"] == "0.005"
    assert second.records[0].cache_hit


def test_lost_post_response_blocks_duplicate_charge(settings, monkeypatch):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith("inspect"):
            return httpx.Response(200, json=quote())
        raise httpx.ReadTimeout("simulated lost response")

    inputs = {"body": {"youtube_url": URL}}
    with pytest.raises(SourceError):
        install(settings, monkeypatch, handler).run("apify", APIFY_CAPTIONS, inputs)
    with pytest.raises(SourceError, match="阻止重复扣费"):
        install(settings, monkeypatch, handler).run("apify", APIFY_CAPTIONS, inputs)
    assert calls.count("/v1/run") == 1


def test_wrong_video_rejected_and_not_cached_as_valid_caption(settings, monkeypatch):
    response = final()
    response["output"][0]["video_id"] = "WrongVideo1"
    provider = MonidProvider(
        settings,
        client=install(
            settings,
            monkeypatch,
            lambda r: httpx.Response(200, json=quote() if r.url.path.endswith("inspect") else response),
        ),
    )
    with pytest.raises(ContractError):
        provider.inspect(recognize(URL))
    assert not provider.cache.has(recognize(URL), "captions")


def test_price_order_platform_rules_and_unknown_coverage(settings):
    settings.monid.enabled = True
    youtube, bili = recognize(URL), recognize("https://www.bilibili.com/video/BV1M1421t7hT")
    names = ["tikhub", "monid", "yt-dlp", "opencli"]
    assert ordered_names(settings, youtube, "captions", names) == ["opencli", "yt-dlp", "monid", "tikhub"]
    assert ordered_names(settings, bili, "captions", names) == ["opencli", "yt-dlp", "tikhub", "monid"]
    settings.routing.priorities["youtube"] = {"captions": PriorityRule(mode="manual", order=["tikhub", "monid"])}
    assert ordered_names(settings, youtube, "captions", names)[0] == "tikhub"
    assert ordered_names(settings, youtube, "captions", names, max_cost=0) == ["opencli", "yt-dlp"]
    assert not quote_rows(settings, bili, "api", ["monid"], endpoint="/api/v1/bilibili/huahuo/not_supported")[0][
        "supported"
    ]


def test_manual_order_used_by_pipeline_and_absence_stops_paid_fallback(settings, monkeypatch):
    settings.monid.enabled = True
    settings.routing.priorities["youtube"] = {"captions": PriorityRule(mode="manual", order=["monid"])}
    client = install(settings, monkeypatch, lambda request: httpx.Response(200, json=quote()))
    client.inspect("apify", APIFY_CAPTIONS)
    calls = []

    class MissingProvider:
        def __init__(self, *args):
            self.records = []

        def inspect(self, source):
            calls.append("monid")
            return MediaItem(source=source, provenance={"provider": "monid"})

        def get_captions(self, item):
            return CaptionAcquisition(status=TranscriptStatus.MISSING)

    monkeypatch.setattr("soota.pipeline.MonidProvider", MissingProvider)
    result = ingest(settings, URL, transcript=TranscriptMode.CAPTIONS_ONLY)
    assert result.data["routing"]["attempted"] == ["monid"]
    assert result.data["transcript_status"] == "missing" and calls == ["monid"]


def test_cli_priority_setup_roundtrip_and_secret_not_printed(settings, monkeypatch, tmp_path):
    for name, value in [
        ("SOOTA_CONFIG_DIR", settings.config_dir),
        ("SOOTA_DATA_DIR", settings.data_dir),
        ("SOOTA_CACHE_DIR", settings.cache_dir),
    ]:
        monkeypatch.setenv(name, str(value))
    keyfile = tmp_path / "key"
    keyfile.write_text("synthetic-private-key")
    runner = CliRunner()
    for args in [
        ["providers", "auth", "monid", "--key-file", str(keyfile)],
        ["providers", "enable", "monid"],
        ["providers", "priority", "set", "youtube", "captions", "--mode", "manual", "--order", "monid,yt-dlp"],
    ]:
        result = runner.invoke(app, [*args, "--json"])
        assert result.exit_code == 0, result.stdout
        assert "synthetic-private-key" not in result.stdout
    loaded = load_settings()
    assert loaded.payment == PaymentConfig()
    assert loaded.monid.enabled and loaded.routing.priorities["youtube"]["captions"].order == ["monid", "yt-dlp"]
    assert (loaded.config_dir / "credentials/monid.key").stat().st_mode & 0o777 == 0o600
    result = runner.invoke(app, ["providers", "priority", "show", "youtube", "captions", "--json"])
    assert json.loads(result.stdout)["data"]["order"][0] == "monid"


def test_completed_native_run_reused_in_same_task_but_not_new_task(settings, monkeypatch):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, json=quote(0.005, "PER_CALL") if request.url.path.endswith("inspect") else final())

    client = install(settings, monkeypatch, handler)
    budget = TaskBudget(settings.data_dir / "original", 0.005)
    with task_budget_scope(budget):
        client.run("tikhub", "/api/v1/example/read", {"queryParams": {"id": "1"}})
        client.run("tikhub", "/api/v1/example/read", {"queryParams": {"id": "1"}})
    assert calls.count("/v1/run") == 1
    with task_budget_scope(TaskBudget(settings.data_dir / "new-task", 0.005)):
        install(settings, monkeypatch, handler).run("tikhub", "/api/v1/example/read", {"queryParams": {"id": "1"}})
    assert calls.count("/v1/run") == 2


def test_native_relay_preserves_post_body_and_uses_manual_priority(settings, monkeypatch, tmp_path):
    from soota.api import call_endpoint
    from soota.providers.tikhub.registry import load_registry

    endpoint = load_registry().resolve("wechat_mp.fetch_article_detail_h5")
    settings.routing.priorities["wechat_mp"] = {"api": PriorityRule(mode="manual", order=["monid"])}
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith("inspect"):
            return httpx.Response(200, json=quote(0.001, "PER_CALL"))
        payload = json.loads(request.content)
        assert payload["provider"] == "tikhub" and payload["endpoint"] == endpoint.path
        assert payload["input"] == {
            "queryParams": {},
            "body": {"url": "https://mp.weixin.qq.com/s/example", "raw": False},
        }
        response = final()
        response["cost"]["value"] = 0.001
        response["output"] = {"code": 200, "data": {"content": "Synthetic article"}}
        return httpx.Response(200, json=response)

    client = install(settings, monkeypatch, handler, 0.001)
    monkeypatch.setattr("soota.providers.monid.provider.MonidClient", lambda *args, **kwargs: client)
    client.inspect("tikhub", endpoint.path)
    body = tmp_path / "body.json"
    body.write_text(json.dumps({"url": "https://mp.weixin.qq.com/s/example", "raw": False}))
    result = call_endpoint(settings, endpoint.key, body_file=body, provider_name="auto", max_cost=0.001)
    assert result.ok and result.data["request"]["provider"] == "monid"
    assert result.data["actual_cost_usd"] == 0.001 and calls.count("/v1/run") == 1


def test_audio_budget_resolved_after_caption_spending(settings, monkeypatch):
    from soota.models import RequestRecord
    from soota.providers.audio import audio_provider
    from soota.providers.router import remaining_budget

    spent = []
    caps = []

    class Audio:
        def __init__(self, settings, cap, **kwargs):
            caps.append(cap)
            self.records = []

        def get_audio(self, item):
            raise AuthBudgetError("too expensive")

    settings.monid.enabled = True
    monkeypatch.setattr("soota.providers.audio.MonidProvider", Audio)
    provider = audio_provider(settings, "monid", lambda: None, remaining_cost=lambda: remaining_budget(0.01, spent))
    spent.append(RequestRecord(endpoint="caption", billed=True, estimated_cost_usd=0.009))
    with pytest.raises(AuthBudgetError):
        provider.get_audio(MediaItem(source=recognize(URL)))
    assert caps == [0.001]


def test_cli_bilibili_preview_includes_free_sources(settings, monkeypatch):
    monkeypatch.setenv("SOOTA_CONFIG_DIR", str(settings.config_dir))
    result = CliRunner().invoke(app, ["providers", "priority", "show", "bilibili", "captions", "--json"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["data"]["order"][:2] == ["opencli", "yt-dlp"]


@pytest.mark.parametrize("content", ["[]", "{bad", '{"tikhub": []}'])
def test_bad_price_cache_is_structured_error(settings, monkeypatch, content):
    monkeypatch.setenv("SOOTA_CONFIG_DIR", str(settings.config_dir))
    settings.config_dir.mkdir(parents=True)
    (settings.config_dir / "provider-prices.json").write_text(content)
    result = CliRunner().invoke(app, ["providers", "priority", "show", "youtube", "--json"])
    assert result.exit_code == 3
    assert json.loads(result.stdout)["code"] == "input"


def test_feature_alias_respects_manual_monid_priority(settings):
    from soota.features import call_feature

    settings.monid.enabled = True
    settings.routing.priorities["twitter"] = {"api": PriorityRule(mode="manual", order=["monid"])}
    result = call_feature(settings, "twitter.profile", params=["username=example"], dry_run=True)
    assert result.data["provider"] == "monid"
    assert result.data["request"]["provider"] == "monid"
    assert result.data["feature"]["plans"][0]["provider"] == "monid"
