from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from soota.cli import app
from soota.config import WeirConfig, load_settings
from soota.content.weir import capture, target
from soota.errors import AuthBudgetError, ContractError, InputError, SourceError, StorageError
from soota.storage.credit_budget import CreditBudget
from soota.storage.task_budget import TaskBudget, task_budget_scope
from soota.tasks.entry import ingest
from soota.tasks.runner import resume

URL = "https://x.com/example/status/123"
RESET = (datetime.now(UTC) + timedelta(days=20)).isoformat()


def balance(**changes):
    data = dict(
        balance=5000,
        reserved=0,
        available=5000,
        tier="free",
        spendingPaused=False,
        freeGrant=dict(granted=5000, remaining=5000, resetsAt=RESET),
    )
    data.update(changes)
    return data


@pytest.fixture
def weir(settings, monkeypatch):
    settings.weir.enabled = True
    monkeypatch.setenv("WEIR_API_KEY", "synthetic-test-key")
    original = httpx.Client
    calls = []

    def install(handler=None, usage=None, charged="2"):
        def reply(request):
            calls.append(request.url.path)
            assert request.url.host == "api.weirapp.io"
            if request.url.path == "/v1/usage":
                return httpx.Response(200, json={"data": balance() if usage is None else usage})
            if handler:
                return handler(request)
            return httpx.Response(
                200,
                json={"data": {"id": request.url.path.rsplit("/", 1)[-1], "text": "First.\n\nRepeated.\n\nRepeated."}},
                headers={"x-credits-charged": charged},
            )

        monkeypatch.setattr(httpx, "Client", lambda **kw: original(**kw, transport=httpx.MockTransport(reply)))
        return calls

    return install


def ledger(settings):
    return CreditBudget(settings.data_dir / "billing" / "weir")


def test_ingest_preserves_body_cache_and_credit_receipt(settings, weir):
    calls = weir()
    result = ingest(settings, URL, provider="weir", want="context,data", weir_max_credits=2)
    assert result.ok
    context = Path(result.data["context"]).read_text()
    assert context.count("Repeated.") == 2 and context.index("First.") < context.index("Repeated.")
    assert result.data["weir_credits"]["counted_credits"] == 2
    before = len(calls)
    capture(settings, URL, allow_credits=False)
    assert len(calls) == before
    data = json.loads((Path(result.data["output"]) / "data.json").read_text())
    assert data["id"] == "123"


@pytest.mark.parametrize(
    "usage",
    [
        balance(balance=5001, available=5001),
        balance(tier="t1"),
        balance(freeGrant=None),
        balance(spendingPaused=True),
        balance(available=1),
        balance(balance=True),
        balance(reserved=5000, available=0),
        balance(freeGrant=dict(granted=5000, remaining=5000, resetsAt="bad")),
    ],
)
def test_free_only_refuses_before_data(settings, weir, usage):
    calls = weir(usage=usage)
    with pytest.raises((AuthBudgetError, ContractError)):
        capture(settings, URL)
    assert calls == ["/v1/usage"]


def test_global_monthly_cap_is_shared_between_tasks_and_keys(settings, weir, monkeypatch):
    calls = weir()
    settings.weir.monthly_credits = 2
    capture(settings, URL)
    monkeypatch.setenv("WEIR_API_KEY", "another-synthetic-key")
    with pytest.raises(AuthBudgetError, match="每月"):
        capture(settings, "https://x.com/example/status/124")
    assert sum(p.startswith("/v1/x") for p in calls) == 1


def test_task_limit_covers_batch_and_resume(settings, weir, tmp_path):
    calls = weir()
    path = tmp_path / "inputs.txt"
    path.write_text(URL + "\nhttps://x.com/example/status/124\n")
    result = ingest(settings, inputs_file=path, provider="weir", want="context", weir_max_credits=2, workers=2)
    assert not result.ok
    assert result.data["weir_credits"]["counted_credits"] == 2
    again = resume(settings, result.data["job_id"])
    assert not again.ok
    assert sum(p.startswith("/v1/x") for p in calls) == 1
    done = resume(settings, result.data["job_id"], weir_max_credits=4)
    assert done.ok
    assert done.data["weir_credits"]["counted_credits"] == 4
    assert sum(p.startswith("/v1/x") for p in calls) == 2


def test_month_reset_does_not_reset_task(settings, weir):
    weir()
    settings.weir.task_credits = 2
    budget = TaskBudget(settings.data_dir / "synthetic-job", None)
    with task_budget_scope(budget):
        capture(settings, URL)
        other = balance(
            freeGrant=dict(granted=5000, remaining=5000, resetsAt=(datetime.now(UTC) + timedelta(days=30)).isoformat())
        )
        weir(usage=other)
        with pytest.raises(AuthBudgetError, match="任务累计"):
            capture(settings, "https://x.com/example/status/124")


def test_timeout_keeps_reservation_on_resume(settings, weir):
    def timeout(request):
        raise httpx.ReadTimeout("synthetic", request=request)

    weir(handler=timeout)
    settings.weir.monthly_credits = 2
    with pytest.raises(SourceError):
        capture(settings, URL)
    with ledger(settings).locked() as budget:
        assert budget.summary()["pending_credits"] == 2
    calls = weir()
    with pytest.raises(AuthBudgetError):
        capture(settings, URL)
    assert calls.count("/v1/x/posts/123") == 1


@pytest.mark.parametrize("status", [400, 401, 402, 403, 404, 429, 503])
def test_confirmed_http_failure_releases_reservation(settings, weir, status):
    weir(handler=lambda _: httpx.Response(status, json={"error": "synthetic"}))
    with pytest.raises((AuthBudgetError, SourceError)):
        capture(settings, URL)
    with ledger(settings).locked() as budget:
        assert budget.summary()["counted_credits"] == 0


def test_unknown_billing_retains_reservation(settings, weir):
    weir(charged="invalid")
    with pytest.raises(ContractError):
        capture(settings, URL)
    with ledger(settings).locked() as budget:
        assert budget.summary()["pending_credits"] == 2


def test_unexpected_price_blocks_future_requests(settings, weir):
    calls = weir(charged="3")
    with pytest.raises(AuthBudgetError):
        capture(settings, URL)
    with pytest.raises(AuthBudgetError):
        capture(settings, URL)
    assert calls.count("/v1/x/posts/123") == 1


def test_concurrent_tasks_do_not_exceed_monthly_cap(settings, weir):
    calls = weir()
    settings.weir.monthly_credits = 4

    def read(i):
        try:
            capture(settings, f"https://x.com/example/status/{i + 1}")
            return True
        except AuthBudgetError:
            return False

    with ThreadPoolExecutor(max_workers=4) as executor:
        assert sum(executor.map(read, range(4))) == 2
    assert sum(p.startswith("/v1/x") for p in calls) == 2


def test_wrong_post_is_charged_but_never_saved(settings, weir):
    weir(
        handler=lambda _: httpx.Response(
            200, json={"data": {"id": "999", "text": "wrong"}}, headers={"x-credits-charged": "2"}
        )
    )
    with pytest.raises(ContractError):
        capture(settings, URL)
    with ledger(settings).locked() as budget:
        assert budget.summary()["charged_credits"] == 2
    assert not list(settings.cache_dir.rglob("*.json"))


def test_article_body_never_uses_preview(settings, weir):
    def reply(request):
        if request.url.path.endswith("/article"):
            data = {
                "id": "456",
                "postId": "123",
                "title": "Article",
                "markdown": "Full article.",
                "text": "Full article.",
            }
        else:
            data = {
                "id": "123",
                "text": "https://x.com/i/article/456",
                "article": {
                    "id": "456",
                    "title": "Article",
                    "previewText": "Preview only",
                    "text": None,
                    "markdown": None,
                },
            }
        return httpx.Response(200, json={"data": data}, headers={"x-credits-charged": "2"})

    calls = weir(handler=reply)
    result = ingest(settings, URL, provider="weir", want="context", weir_max_credits=4)
    assert result.ok and "Full article." in Path(result.data["context"]).read_text()
    assert result.data["weir_credits"]["counted_credits"] == 4
    assert calls.count("/v1/x/posts/123/article") == 1


@pytest.mark.parametrize("body", ["", "[removed]", "[deleted]"])
def test_reddit_link_or_removed_post_is_not_full_text(settings, weir, body):
    weir(
        handler=lambda _: httpx.Response(
            200, json={"data": {"id": "abc123", "title": "Title", "selftext": body}}, headers={"x-credits-charged": "4"}
        )
    )
    with pytest.raises(SourceError):
        capture(settings, "https://www.reddit.com/r/example/comments/abc123/title/")


def test_reddit_text_is_preserved(settings, weir):
    weir(
        handler=lambda _: httpx.Response(
            200,
            json={"data": {"id": "abc123", "title": "Title", "selftext": "First\n\nSecond"}},
            headers={"x-credits-charged": "4"},
        )
    )
    result = capture(settings, "https://redd.it/abc123")
    assert result.body == "First\n\nSecond" and result.title == "Title"


def test_corrupted_ledger_blocks_send(settings, weir):
    calls = weir()
    budget = ledger(settings)
    budget.root.mkdir(parents=True)
    budget.path.write_text('{"version": 1, "requests": [{"reserved": -2}]}')
    with pytest.raises(StorageError):
        capture(settings, URL)
    assert not calls


def test_cli_configuration_is_private_and_roundtrips(settings, monkeypatch):
    monkeypatch.setenv("SOOTA_CONFIG_DIR", str(settings.config_dir))
    monkeypatch.setenv("SOOTA_DATA_DIR", str(settings.data_dir))
    monkeypatch.setenv("SOOTA_CACHE_DIR", str(settings.cache_dir))
    runner = CliRunner()
    assert runner.invoke(app, ["providers", "enable", "weir", "--json"]).exit_code == 0
    result = runner.invoke(
        app, ["providers", "limits", "weir", "--monthly-credits", "4000", "--task-credits", "100", "--json"]
    )
    assert result.exit_code == 0, result.output
    configured = load_settings()
    assert configured.weir.enabled and configured.weir.free_only
    assert configured.weir.monthly_credits == 4000 and configured.weir.task_credits == 100
    plan = runner.invoke(app, ["ingest", URL, "--provider", "weir", "--weir-max-credits", "7", "--dry-run", "--json"])
    assert plan.exit_code == 0, plan.output
    assert json.loads(plan.output)["data"]["plan"]["weir_max_credits"] == 7
    assert runner.invoke(app, ["providers", "usage", "weir", "--json"]).exit_code == 0
    assert runner.invoke(app, ["providers", "limits", "weir", "--task-credits", "-1"]).exit_code != 0
    assert runner.invoke(app, ["providers", "limits", "weir", "--clear-task", "--json"]).exit_code == 0
    assert load_settings().weir.task_credits is None


def test_explicit_free_only_cannot_be_disabled():
    with pytest.raises(ValueError):
        WeirConfig(free_only=False)


@pytest.mark.parametrize(
    "url",
    [
        "https://x.com/i/article/456",
        "https://x.com/example/status/123/photo/1",
        "https://reddit.com/r/example/comments/abc123/title/comment123",
        "https://reddit.com.evil.test/comments/abc123",
        "https://a:b@x.com/example/status/123",
    ],
)
def test_unsupported_inputs_fail_before_network(url):
    with pytest.raises(InputError):
        target(url)


def test_zero_credit_limit_blocks_data_but_cache_remains_free(settings, weir):
    calls = weir()
    capture(settings, URL)
    settings.weir.monthly_credits = 0
    capture(settings, URL)
    with pytest.raises(AuthBudgetError):
        capture(settings, "https://x.com/example/status/124")
    assert sum(p.startswith("/v1/x") for p in calls) == 1


def test_zero_dollar_budget_blocks_data_before_usage(settings, weir):
    calls = weir()
    result = ingest(settings, URL, provider="weir", want="context", max_cost=0)
    assert not result.ok and not calls


def test_disabled_provider_cannot_reuse_cache(settings, weir):
    calls = weir()
    capture(settings, URL)
    settings.weir.enabled = False
    with pytest.raises(InputError):
        capture(settings, URL)
    assert calls == ["/v1/usage", "/v1/x/posts/123"]


def test_auto_can_select_weir_before_dispatch(settings, weir, monkeypatch, tmp_path):
    from soota.content.native import capture_content

    calls = weir()
    monkeypatch.setattr("soota.content.native.opencli_commands.commands", lambda settings: [])
    result = capture_content(settings, URL, tmp_path)
    assert result.provider == "weir"
    assert calls == ["/v1/usage", "/v1/x/posts/123"]


def test_auto_does_not_retry_dispatched_opencli_failure(settings, weir, monkeypatch, tmp_path):
    from soota.content.native import capture_content

    calls = weir()
    monkeypatch.setattr(
        "soota.content.native.opencli_commands.commands",
        lambda settings: [{"command": "twitter/article", "access": "read"}],
    )

    def fail(*args):
        raise SourceError("upstream unavailable")

    monkeypatch.setattr("soota.content.native._dispatch", fail)
    with pytest.raises(SourceError):
        capture_content(settings, URL, tmp_path)
    assert not calls


def test_auth_never_prints_key_and_creates_private_file(settings, monkeypatch, tmp_path):
    monkeypatch.setenv("SOOTA_CONFIG_DIR", str(settings.config_dir))
    key = tmp_path / "input.key"
    key.write_text("synthetic-auth-key\n")
    result = CliRunner().invoke(app, ["providers", "auth", "weir", "--key-file", str(key), "--json"])
    assert result.exit_code == 0 and "synthetic-auth-key" not in result.output
    saved = settings.config_dir / "credentials" / "weir.key"
    assert saved.read_text().strip() == "synthetic-auth-key"
    if __import__("os").name != "nt":
        assert saved.stat().st_mode & 0o777 == 0o600


def test_article_resume_reuses_successful_carrier(settings, weir):
    attempts = 0

    def reply(request):
        nonlocal attempts
        if request.url.path.endswith("/article"):
            attempts += 1
            if attempts == 1:
                return httpx.Response(503, json={"error": "temporary"})
            data = {"id": "456", "postId": "123", "title": "Article", "text": "Complete body"}
        else:
            data = {
                "id": "123",
                "text": "https://x.com/i/article/456",
                "article": {"id": "456", "title": "Article", "text": None},
            }
        return httpx.Response(200, json={"data": data}, headers={"x-credits-charged": "2"})

    calls = weir(handler=reply)
    result = ingest(settings, URL, provider="weir", want="context", weir_max_credits=4)
    assert not result.ok
    done = resume(settings, result.data["job_id"])
    assert done.ok, done.model_dump()
    assert done.data["weir_credits"]["counted_credits"] == 4
    assert calls.count("/v1/x/posts/123") == 1
    assert calls.count("/v1/x/posts/123/article") == 2


def test_retweeted_article_uses_nested_full_body(settings, weir):
    data = {
        "id": "123",
        "text": "RT @example: https://x.com/i/article/456",
        "retweetedPost": {
            "id": "124",
            "text": "https://x.com/i/article/456",
            "article": {"id": "456", "title": "Article", "markdown": "Full nested article"},
        },
    }
    calls = weir(handler=lambda _: httpx.Response(200, json={"data": data}, headers={"x-credits-charged": "2"}))
    result = capture(settings, URL)
    assert "Full nested article" in result.body
    assert result.title == "Article" and len(calls) == 2


def test_retweeted_article_link_without_body_is_not_success(settings, weir):
    data = {
        "id": "123",
        "text": "RT @example: https://x.com/i/article/456",
        "retweetedPost": {"id": "124", "text": "https://x.com/i/article/456"},
    }
    weir(handler=lambda _: httpx.Response(200, json={"data": data}, headers={"x-credits-charged": "2"}))
    with pytest.raises(SourceError):
        capture(settings, URL)


def test_optional_failed_text_does_not_fabricate_required_data(settings, weir):
    weir(handler=lambda _: httpx.Response(404, json={"error": "missing"}))
    result = ingest(settings, URL, provider="weir", want="data", optional="context")
    assert not result.ok
    assert "data" in result.data["items"][0]["data"]["missing_outputs"]
    assert not (Path(result.data["output"]) / "data.json").exists()
