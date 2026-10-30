from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from typer.testing import CliRunner

from soota.cli import app
from soota.content.weir import capture
from soota.errors import AuthBudgetError, ContractError, InputError, SourceError, StorageError
from soota.providers.weir_keys import inventory, load_keys, save_key
from soota.storage.credit_budget import CreditBudget
from soota.storage.task_budget import TaskBudget, task_budget_scope
from soota.tasks.entry import ingest
from soota.tasks.runner import resume

URL = "https://x.com/example/status/123"
RESET = (datetime.now(UTC) + timedelta(days=20)).isoformat()


def free_usage(reset=RESET):
    return {
        "balance": 5000,
        "reserved": 0,
        "available": 5000,
        "tier": "free",
        "spendingPaused": False,
        "freeGrant": {"granted": 5000, "remaining": 5000, "resetsAt": reset},
    }


@pytest.fixture
def pool(settings, monkeypatch):
    monkeypatch.delenv("WEIR_API_KEY", raising=False)
    settings.weir.enabled = True
    root = settings.config_dir / "credentials"
    root.mkdir(parents=True)
    (root / "weir.key").write_text("synthetic-primary")
    save_key(settings, "backup", "synthetic-backup")
    calls = []
    original = httpx.Client

    def install(handler):
        def reply(request):
            key = request.headers["authorization"].removeprefix("Bearer ")
            calls.append((key, request.url.path))
            return handler(key, request)

        monkeypatch.setattr(httpx, "Client", lambda **kw: original(**kw, transport=httpx.MockTransport(reply)))
        return calls

    return install


def response(request, *, charged="2"):
    return httpx.Response(
        200,
        json={"data": {"id": request.url.path.rsplit("/", 1)[-1], "text": "Full source body."}},
        headers={"x-credits-charged": charged},
    )


def budget(settings):
    return CreditBudget(settings.data_dir / "billing" / "weir")


@pytest.mark.parametrize("stage", ["usage", "data"])
def test_401_moves_to_backup_and_records_shared_task_credits(settings, pool, stage):
    def reply(key, request):
        usage = request.url.path == "/v1/usage"
        if key == "synthetic-primary" and (usage if stage == "usage" else not usage):
            return httpx.Response(401)
        return httpx.Response(200, json={"data": free_usage()}) if usage else response(request)

    calls = pool(reply)
    result = ingest(settings, URL, provider="weir", want="context", weir_max_credits=100)
    assert result.ok
    assert ("synthetic-backup", "/v1/x/posts/123") in calls
    usage = result.data["weir_credits"]
    assert usage["charged_credits"] == 2 and usage["pending_credits"] == 0
    assert next(k for k in usage["keys"] if k["name"] == "backup")["charged_credits"] == 2
    with budget(settings).locked() as ledger:
        assert len({r.task for r in ledger.state.requests}) == 1
    before = len(calls)
    assert capture(settings, URL, allow_credits=False).body == "Full source body."
    assert len(calls) == before


def test_round_robin_advances_after_each_successful_data_request(settings, pool):
    calls = pool(
        lambda key, r: httpx.Response(200, json={"data": free_usage()})
        if r.url.path == "/v1/usage"
        else response(r)
    )
    assert capture(settings, URL).body == "Full source body."
    assert capture(settings, "https://x.com/example/status/124").body == "Full source body."
    assert [key for key, path in calls if path.startswith("/v1/x/")] == [
        "synthetic-primary",
        "synthetic-backup",
    ]
    with budget(settings).locked() as ledger:
        assert {r.key_name for r in ledger.state.requests} == {"default", "backup"}


@pytest.mark.parametrize("status", [402, 403, 404, 429, 500, 503])
def test_non_auth_errors_do_not_rotate_accounts(settings, pool, status):
    calls = pool(
        lambda key, r: (
            httpx.Response(200, json={"data": free_usage()}) if r.url.path == "/v1/usage" else httpx.Response(status)
        )
    )
    with pytest.raises((AuthBudgetError, SourceError)):
        capture(settings, URL)
    assert all(key == "synthetic-primary" for key, _ in calls)
    with budget(settings).locked() as ledger:
        assert ledger.summary()["counted_credits"] == 0


@pytest.mark.parametrize("reason", ["low_balance", "purchased", "paid_tier", "paused"])
def test_unusable_free_workspace_does_not_rotate(settings, pool, reason):
    usage = free_usage()
    if reason == "low_balance":
        usage.update(balance=0, available=0, freeGrant={"granted": 5000, "remaining": 0, "resetsAt": RESET})
    elif reason == "purchased":
        usage.update(balance=5001, available=5001)
    elif reason == "paid_tier":
        usage["tier"] = "t1"
    else:
        usage["spendingPaused"] = True
    calls = pool(lambda key, r: httpx.Response(200, json={"data": usage}))
    with pytest.raises(AuthBudgetError):
        capture(settings, URL)
    assert calls == [("synthetic-primary", "/v1/usage")]


@pytest.mark.parametrize("failure", ["timeout", "missing_receipt", "overcharge"])
def test_uncertain_or_excessive_charge_stops_without_backup(settings, pool, failure):
    def reply(key, request):
        if request.url.path == "/v1/usage":
            return httpx.Response(200, json={"data": free_usage()})
        if failure == "timeout":
            raise httpx.ReadTimeout("synthetic", request=request)
        return response(request, charged="" if failure == "missing_receipt" else "3")

    calls = pool(reply)
    with pytest.raises((SourceError, ContractError, AuthBudgetError)):
        capture(settings, URL)
    assert all(key == "synthetic-primary" for key, _ in calls)
    with budget(settings).locked() as ledger:
        assert ledger.summary()["pending_credits"] == (0 if failure == "overcharge" else 2)
        assert ledger.state.blocked == (failure == "overcharge")


def test_each_key_has_its_own_calendar_monthly_cap(settings, pool):
    primary_valid = True

    def reply(key, request):
        if key == "synthetic-primary" and not primary_valid:
            return httpx.Response(401)
        reset = RESET if key == "synthetic-primary" else (datetime.now(UTC) + timedelta(days=50)).isoformat()
        return (
            httpx.Response(200, json={"data": free_usage(reset)})
            if request.url.path == "/v1/usage"
            else response(request)
        )

    calls = pool(reply)
    settings.weir.monthly_credits = 2
    capture(settings, URL)
    capture(settings, "https://x.com/example/status/124")
    assert len([p for _, p in calls if p.startswith("/v1/x")]) == 2
    with budget(settings).locked() as ledger:
        assert ledger.summary()["monthly_key_counted_credits"] == {"default": 2, "backup": 2}


def test_legacy_ledger_counts_before_rotation_without_rewriting(settings, pool):
    def reply(key, request):
        if key == "synthetic-primary":
            return httpx.Response(401)
        return (
            httpx.Response(200, json={"data": free_usage()})
            if request.url.path == "/v1/usage"
            else response(request)
        )

    pool(reply)
    ledger = budget(settings)
    ledger.root.mkdir(parents=True)
    legacy = json.dumps(
        {
            "version": 1,
            "blocked": False,
            "requests": [{"id": "old", "period": RESET, "task": "original-task", "reserved": 2, "charged": None}],
        }
    )
    ledger.path.write_text(legacy)
    settings.weir.monthly_credits = 2
    capture(settings, URL)
    updated = json.loads(ledger.path.read_text())
    assert updated["requests"][0] == {
        **json.loads(legacy)["requests"][0],
        "key_name": None,
        "month": None,
    }
    with budget(settings).locked() as ledger:
        assert ledger.summary()["monthly_key_counted_credits"]["legacy"] == 2


def test_task_limit_and_concurrent_monthly_limit_survive_failover(settings, pool):
    calls = pool(
        lambda key, r: (
            httpx.Response(401)
            if key == "synthetic-primary"
            else httpx.Response(200, json={"data": free_usage()})
            if r.url.path == "/v1/usage"
            else response(r)
        )
    )
    settings.weir.monthly_credits = 4
    settings.weir.task_credits = 2
    task = TaskBudget(settings.data_dir / "task", None)
    with task_budget_scope(task):
        capture(settings, URL)
        with pytest.raises(AuthBudgetError, match="任务累计"):
            capture(settings, "https://x.com/example/status/124")

    def read(i):
        try:
            capture(settings, f"https://x.com/example/status/{200 + i}")
            return True
        except AuthBudgetError:
            return False

    with ThreadPoolExecutor(max_workers=3) as executor:
        assert sum(executor.map(read, range(3))) == 1
    assert sum(p.startswith("/v1/x") for _, p in calls) == 2


def test_partial_article_survives_replacing_key_and_resume(settings, pool):
    ready = False

    def reply(key, r):
        if key.startswith("synthetic-primary") and ready:
            return httpx.Response(401)
        if r.url.path == "/v1/usage":
            return httpx.Response(200, json={"data": free_usage()})
        if r.url.path.endswith("/article"):
            if not ready:
                return httpx.Response(503)
            data = {"id": "456", "postId": "123", "title": "Article", "text": "Complete article."}
        else:
            data = {"id": "123", "text": "https://x.com/i/article/456", "article": {"id": "456", "text": None}}
        return httpx.Response(200, json={"data": data}, headers={"x-credits-charged": "2"})

    calls = pool(reply)
    first = ingest(settings, URL, provider="weir", want="context", weir_max_credits=4)
    assert not first.ok and first.data["weir_credits"]["charged_credits"] == 2
    (settings.config_dir / "credentials" / "weir.key").write_text("synthetic-primary-replaced")
    ready = True
    done = resume(settings, first.data["job_id"])
    assert done.ok and done.data["weir_credits"]["charged_credits"] == 4
    assert sum(p == "/v1/x/posts/123" for _, p in calls) == 1
    assert ("synthetic-backup", "/v1/x/posts/123/article") in calls


def test_private_named_key_cli_preserves_primary_and_supports_disable(settings, monkeypatch, tmp_path):
    monkeypatch.delenv("WEIR_API_KEY", raising=False)
    monkeypatch.setenv("SOOTA_CONFIG_DIR", str(settings.config_dir))
    file = tmp_path / "input.key"
    runner = CliRunner()
    file.write_text("synthetic-first")
    assert runner.invoke(app, ["providers", "auth", "weir", "--key-file", str(file)]).exit_code == 0
    file.write_text("synthetic-second")
    result = runner.invoke(
        app, ["providers", "auth", "weir", "--key-name", "backup", "--key-file", str(file), "--json"]
    )
    assert result.exit_code == 0 and "synthetic-second" not in result.output
    assert [k.name for k in load_keys(settings)] == ["default", "backup"]
    result = runner.invoke(app, ["providers", "keys", "weir", "--disable", "backup", "--json"])
    assert result.exit_code == 0 and "synthetic" not in result.output
    assert [k.name for k in load_keys(settings)] == ["default"]
    assert inventory(settings, enable="backup")["effective_order"] == ["default", "backup"]
    assert (settings.config_dir / "credentials" / "weir.key").read_text().strip() == "synthetic-first"
    assert "synthetic-second" not in repr(load_keys(settings))
    if __import__("os").name != "nt":
        assert (settings.config_dir / "credentials" / "weir-keys.json").stat().st_mode & 0o777 == 0o600


def test_environment_override_does_not_silently_fall_back(settings, pool, monkeypatch):
    monkeypatch.setenv("WEIR_API_KEY", "synthetic-environment")
    calls = pool(lambda key, r: httpx.Response(401))
    with pytest.raises(AuthBudgetError):
        capture(settings, URL)
    assert calls == [("synthetic-environment", "/v1/usage")]
    monkeypatch.setenv("WEIR_API_KEY", "invalid key")
    with pytest.raises(InputError):
        capture(settings, URL)
    assert len(calls) == 1


def test_round_robin_401_uses_next_key_and_persists_success_position(settings, pool):
    def reply(key, request):
        if request.url.path != "/v1/usage":
            return response(request)
        return (
            httpx.Response(200, json={"data": free_usage()})
            if key == "synthetic-backup"
            else httpx.Response(401)
        )

    calls = pool(reply)
    assert capture(settings, URL).body == "Full source body."
    assert calls[0][0] == "synthetic-primary"
    assert capture(settings, "https://x.com/example/status/124").body == "Full source body."
    assert calls[-2][0] == "synthetic-backup"
    assert json.loads((settings.config_dir / "credentials" / "weir-cursor.json").read_text())["next_index"] == 0


def test_corrupt_pool_and_invalid_names_never_expose_secrets(settings, pool):
    calls = pool(lambda key, r: pytest.fail("No HTTP should be sent"))
    path = settings.config_dir / "credentials" / "weir-keys.json"
    path.write_text('{"version":2,"keys":[{"name":"backup","key":"synthetic-secret"}]}')
    with pytest.raises(StorageError) as error:
        capture(settings, URL)
    assert "synthetic-secret" not in str(error.value)
    assert not calls
    for name in ("../outside", "default", "environment", ""):
        with pytest.raises(InputError):
            save_key(settings, name, "synthetic-secret")


def test_exhausted_pool_stops_after_one_auth_check_per_key(settings, pool):
    calls = pool(lambda key, r: httpx.Response(401))
    with pytest.raises(AuthBudgetError, match="所有"):
        capture(settings, URL)
    assert calls == [("synthetic-primary", "/v1/usage"), ("synthetic-backup", "/v1/usage")]
    with budget(settings).locked() as ledger:
        assert ledger.summary()["counted_credits"] == 0


def test_monthly_limit_applies_only_to_selected_key(settings, pool):
    def reply(key, request):
        if request.url.path == "/v1/usage":
            return httpx.Response(200, json={"data": free_usage()})
        return httpx.Response(401) if key == "synthetic-primary" else response(request)

    calls = pool(reply)
    settings.weir.monthly_credits = 2
    capture(settings, URL)
    with pytest.raises(AuthBudgetError, match="每月"):
        capture(settings, "https://x.com/example/status/124")
    data_calls = [key for key, path in calls if path.startswith("/v1/x/") and "not-sent" not in path]
    assert data_calls == ["synthetic-primary", "synthetic-backup", "synthetic-primary"]
    with budget(settings).locked() as ledger:
        assert ledger.summary()["monthly_key_counted_credits"] == {"backup": 2, "default": 0}


def test_backup_must_also_pass_free_only_guard(settings, pool):
    usage = free_usage()
    usage.update(balance=5100, available=5100)
    calls = pool(
        lambda key, r: httpx.Response(401) if key == "synthetic-primary" else httpx.Response(200, json={"data": usage})
    )
    with pytest.raises(AuthBudgetError, match="额外余额"):
        capture(settings, URL)
    assert not any(p.startswith("/v1/x") for _, p in calls)


def test_calendar_rollover_releases_monthly_cap_but_preserves_task_total(settings, pool, monkeypatch):
    import soota.storage.credit_budget as credit_module

    now = datetime.now(UTC)
    next_month = (now.replace(day=1) + timedelta(days=32)).replace(day=1)

    class NextMonth(datetime):
        @classmethod
        def now(cls, tz=None):
            return next_month

    pool(lambda key, r: httpx.Response(200, json={"data": free_usage()}) if r.url.path == "/v1/usage" else response(r))
    settings.weir.monthly_credits = 2
    settings.weir.task_credits = 2
    task = TaskBudget(settings.data_dir / "task", None)
    with task_budget_scope(task):
        capture(settings, URL)
        monkeypatch.setattr(credit_module, "datetime", NextMonth)
        with pytest.raises(AuthBudgetError, match="任务累计"):
            capture(settings, "https://x.com/example/status/124")
    capture(settings, "https://x.com/example/status/125")
    with budget(settings).locked() as ledger:
        assert ledger.summary()["counted_credits"] == 4
        assert ledger.summary()["monthly_counted_credits"] == 2


def test_duplicate_keys_are_not_retried_and_parallel_registration_keeps_all(settings, pool):
    save_key(settings, "duplicate", "synthetic-primary")
    assert [k.name for k in load_keys(settings)] == ["default", "backup"]
    with ThreadPoolExecutor(max_workers=3) as executor:
        list(executor.map(lambda i: save_key(settings, f"other-{i}", f"synthetic-other-{i}"), range(3)))
    assert len(inventory(settings)["backups"]) == 5
