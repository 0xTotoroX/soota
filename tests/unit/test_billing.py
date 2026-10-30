import io
import json

import pytest
from typer.testing import CliRunner

from soota.api import call_endpoint
from soota.billing import BalancePaused, PaymentGate, PaymentRequired, confirm_payment
from soota.catalog import catalog, resume_catalog
from soota.cli import app
from soota.config import PaymentConfig, load_settings
from soota.errors import AuthBudgetError, ContractError, InputError
from soota.models import RequestRecord
from soota.pipeline import estimate
from soota.policy_cli import set_policy
from soota.providers.tikhub.client import ENDPOINT_INFO, TikHubProvider
from soota.providers.tikhub.registry import load_registry
from soota.storage.cache import CacheStore
from tests.conftest import load_fixture

INFO = "/api/v1/tikhub/user/get_user_info"
ENDPOINT = load_registry().resolve("bilibili.web.fetch_one_video")
URL = "https://www.bilibili.com/video/BV1M1421t7hT"


def fake_requester(events, *, balance=10):
    def request(method, path, params):
        events.append(path)
        if path == ENDPOINT_INFO:
            return {"data": {"endpoint_uri": params["endpoint"], "endpoint_cost": 0.001}}
        if path == INFO:
            return {"user_data": {"balance": balance, "free_credit": 10000}}
        return {"code": 200, "data": {"ok": True}}

    return request


def test_default_noninteractive_requires_payment_before_dispatch(settings, monkeypatch):
    settings.payment = PaymentConfig()
    monkeypatch.setattr("sys.stdin", io.StringIO("yes\n"))
    calls = []
    result = call_endpoint(settings, ENDPOINT.key, params=[f"url={URL}"], requester=fake_requester(calls))
    assert result.code == "payment_required"
    assert result.data["reserved_cost_usd"] == 0
    assert result.data["details"]["estimated_cost_usd"] == 0.001
    assert calls == [ENDPOINT_INFO]
    assert not settings.ledger_path.exists()


@pytest.mark.parametrize("answer,accepted", [("y\n", True), ("yes\n", True), ("\n", False), ("n\n", False)])
def test_terminal_prompt_uses_stderr_and_defaults_no(answer, accepted, monkeypatch, capsys):
    class Terminal(io.StringIO):
        def isatty(self):
            return True

    monkeypatch.setattr("sys.stdin", Terminal(answer))
    assert confirm_payment({"endpoint": "fixture", "estimated_cost_usd": 0.001}) is accepted
    output = capsys.readouterr()
    assert output.out == "" and "[y/N]" in output.err


def test_gate_prompts_each_actual_request_and_skips_cache_and_free(settings, monkeypatch):
    settings.payment = PaymentConfig()
    prompts = []
    monkeypatch.setattr("soota.billing.confirm_payment", lambda data: prompts.append(data) or len(prompts) == 1)
    gate = PaymentGate(settings)
    record = RequestRecord(endpoint="fixture", billed=True, estimated_cost_usd=0.001)
    gate.authorize(record, lambda: None)
    gate.authorize(record.model_copy(update={"cache_hit": True}), lambda: None)
    gate.authorize(record.model_copy(update={"billed": False}), lambda: None)
    with pytest.raises(PaymentRequired):
        gate.authorize(record, lambda: None)
    assert len(prompts) == 2 and float(gate.reserved) == 0.001


@pytest.mark.parametrize("balance", [5, 5.0005, 5.001])
def test_floor_pauses_before_paid_dispatch_ignoring_free_credit(settings, balance):
    settings.payment = PaymentConfig(mode="auto", min_balance_usd=5)
    calls = []
    result = call_endpoint(
        settings, ENDPOINT.key, params=[f"url={URL}"], requester=fake_requester(calls, balance=balance)
    )
    assert result.code == "balance_paused"
    assert calls == [ENDPOINT_INFO, INFO]
    assert not settings.ledger_path.exists()


def test_balance_is_rechecked_each_request_and_failure_stops(settings, monkeypatch):
    settings.payment = PaymentConfig(mode="auto", min_balance_usd=5)
    monkeypatch.setattr("soota.billing.time.sleep", lambda delay: None)
    wallets = iter(
        [
            {"user_data": {"balance": "5.01", "free_credit": "999"}},
            {"user_data": {"balance": "5.001", "free_credit": "999"}},
        ]
    )
    calls = []

    def request(method, path, params):
        calls.append(path)
        if path == ENDPOINT_INFO:
            return {"data": {"endpoint_uri": params["endpoint"], "endpoint_cost": 0.001}}
        return next(wallets) if path == INFO else {"data": {}}

    client = TikHubProvider(cache=CacheStore(settings.cache_dir), requester=request, payment_gate=PaymentGate(settings))
    client._call(ENDPOINT.path, {}, billed=True)
    with pytest.raises(BalancePaused):
        client._call(ENDPOINT.path, {}, billed=True)
    assert calls == [ENDPOINT_INFO, INFO, ENDPOINT.path, INFO]
    assert client.max_retries == 0


@pytest.mark.parametrize("wallet", [None, {}, {"balance": float("nan")}, {"balance": True}])
def test_unknown_balance_never_allows_charge(settings, wallet):
    settings.payment = PaymentConfig(mode="auto", min_balance_usd=0)
    gate = PaymentGate(settings)
    with pytest.raises(BalancePaused):
        gate.authorize(RequestRecord(endpoint="test", billed=True, estimated_cost_usd=0.001), lambda: wallet)


def test_balance_error_is_a_pause(settings):
    settings.payment = PaymentConfig(mode="auto", min_balance_usd=0)

    def failed():
        raise ContractError("bad wallet")

    with pytest.raises(BalancePaused):
        PaymentGate(settings).authorize(RequestRecord(endpoint="x", billed=True, estimated_cost_usd=0.001), failed)


def test_no_balance_limit_skips_wallet_and_cap_precedes_prompt(settings, monkeypatch):
    calls = []
    result = call_endpoint(settings, ENDPOINT.key, params=[f"url={URL}"], requester=fake_requester(calls))
    assert result.ok and calls == [ENDPOINT_INFO, ENDPOINT.path]
    settings.payment = PaymentConfig()
    monkeypatch.setattr("soota.billing.confirm_payment", lambda data: pytest.fail("over-cap must not prompt"))
    record = RequestRecord(endpoint="x", billed=True, estimated_cost_usd=0.001)
    with pytest.raises(AuthBudgetError):
        PaymentGate(settings, 0).authorize(record, lambda: None)


def test_policy_requires_explicit_auto_threshold_and_preserves_toml(settings, monkeypatch):
    monkeypatch.setattr("soota.pipeline.StdioMCPClient.available", lambda config: True)
    settings.config_dir.mkdir(parents=True)
    settings.config_path.write_text('[soota]\ndefault_language="en"\n[opencli_mcp]\nenabled=true\n')
    monkeypatch.setenv("SOOTA_CONFIG_DIR", str(settings.config_dir))
    monkeypatch.setenv("SOOTA_DATA_DIR", str(settings.data_dir))
    monkeypatch.setenv("SOOTA_CACHE_DIR", str(settings.cache_dir))
    with pytest.raises(InputError):
        set_policy(settings, "auto", None, False)
    with pytest.raises(InputError):
        set_policy(settings, "auto", 5, True)
    set_policy(settings, "auto", 5, False)
    loaded = load_settings()
    assert loaded.routing.default_provider == "auto" and loaded.payment.min_balance_usd == 5
    assert loaded.default_language == "en"
    assert estimate(loaded, URL).data["provider"] == "opencli-mcp"
    assert all(
        path.startswith("/api/")
        for path in estimate(loaded, URL, provider="tikhub", requester=fake_requester([])).data["endpoints"]
    )
    set_policy(settings, "auto", None, True)
    assert load_settings().payment.min_balance_usd is None
    set_policy(settings, "ask", None, False)
    assert load_settings().payment == PaymentConfig()


def test_legacy_auto_payment_does_not_override_independent_routing(settings, monkeypatch):
    settings.config_dir.mkdir(parents=True)
    monkeypatch.setenv("SOOTA_CONFIG_DIR", str(settings.config_dir))
    (settings.config_dir / "payment-policy.json").write_text(
        json.dumps({"mode": "auto", "min_balance_usd": 5, "default_provider": "tikhub"})
    )
    loaded = load_settings()
    assert loaded.payment.mode == "auto" and loaded.payment.min_balance_usd == 5
    assert loaded.routing.default_provider == "auto"
    settings.config_path.write_text('[routing]\ndefault_provider="tikhub"\n')
    assert load_settings().routing.default_provider == "tikhub"


def test_bad_routing_config_returns_structured_cli_error(settings, monkeypatch):
    settings.config_dir.mkdir(parents=True)
    settings.config_path.write_text('[routing]\ndefault_provider="invalid"\n')
    monkeypatch.setenv("SOOTA_CONFIG_DIR", str(settings.config_dir))
    result = CliRunner().invoke(app, ["policy", "show", "--json"])
    assert result.exit_code == 3
    assert json.loads(result.stdout)["code"] == "input"


@pytest.mark.usefixtures("cached_tikhub_prices")
def test_catalog_asks_each_page_and_resumes_without_double_reservation(settings, monkeypatch):
    settings.payment = PaymentConfig()
    prompts = iter([True, False])
    monkeypatch.setattr("soota.billing.confirm_payment", lambda details: next(prompts))
    fixture = load_fixture("youtube_catalog.json")
    calls = []

    def request(method, path, params):
        calls.append(path)
        return fixture

    url = "https://www.youtube.com/channel/UCAAAAAAAAAAAAAAAAAAAAAA"
    first = catalog(settings, url, requester=request)
    assert first.data["error"]["code"] == "payment_required"
    assert len(calls) == 1 and first.data["reserved_cost_usd"] == 0.001
    monkeypatch.setattr("soota.billing.confirm_payment", lambda details: True)
    fixture["data"]["continuation_token"] = None
    fixture["data"]["videos"] = [{"video_id": "Example0003", "title": "Next"}]
    second = resume_catalog(settings, first.data["job_id"], requester=request)
    assert second.ok and len(calls) == 2
    assert second.data["reserved_cost_usd"] == 0.002


def test_cli_noninteractive_is_structured_and_policy_writes_only_isolated_config(settings, monkeypatch):
    for name, value in [
        ("SOOTA_CONFIG_DIR", settings.config_dir),
        ("SOOTA_DATA_DIR", settings.data_dir),
        ("SOOTA_CACHE_DIR", settings.cache_dir),
    ]:
        monkeypatch.setenv(name, str(value))
    runner = CliRunner()
    result = runner.invoke(app, ["policy", "set", "--mode", "auto", "--json"])
    assert result.exit_code == 3
    assert not (settings.config_dir / "payment-policy.json").exists()
    result = runner.invoke(app, ["policy", "set", "--mode", "auto", "--min-balance", "5", "--json"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["data"]["routing"]["default_provider"] == "auto"


@pytest.mark.parametrize("content", ["{bad", '{"mode":"unexpected"}', "[]"])
def test_corrupt_policy_is_structured_and_recoverable(settings, monkeypatch, content):
    monkeypatch.setenv("SOOTA_CONFIG_DIR", str(settings.config_dir))
    settings.config_dir.mkdir(parents=True)
    (settings.config_dir / "payment-policy.json").write_text(content)
    runner = CliRunner()
    broken = runner.invoke(app, ["policy", "show", "--json"])
    assert broken.exit_code == 3
    assert json.loads(broken.stdout)["code"] == "input"
    blocked = runner.invoke(app, ["inspect", URL, "--offline", "--json"])
    assert blocked.exit_code == 3 and json.loads(blocked.stdout)["code"] == "input"
    recovered = runner.invoke(app, ["policy", "set", "--mode", "ask", "--json"])
    assert recovered.exit_code == 0
    assert json.loads(recovered.stdout)["data"]["policy"]["mode"] == "ask"
