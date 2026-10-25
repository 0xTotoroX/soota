import json
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from soota.auto_updates import automatic_update, should_update
from soota.billing import PaymentGate, PaymentRequired
from soota.cli import app
from soota.config import PaymentConfig, load_settings
from soota.errors import ContractError, SourceError
from soota.pipeline import ingest, inspect, render
from soota.platform_support import npm_entry, venv_python
from soota.providers.managed import WORKER, MCPRuntime, TikHubRuntime, bind_runtimes
from soota.providers.opencli_runtime import OpenCLIRuntime
from soota.providers.process import ProcessResult, run_process
from soota.providers.tikhub.client import TikHubProvider
from soota.storage.cache import CacheStore


def fail(*args, **kwargs):
    raise AssertionError("Unexpected network, browser or paid request")


def install_fake(runtime, version):
    path = runtime.managed_binary(version)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    old = runtime.state()
    (runtime.root / "state.json").write_text(json.dumps({"active": version, "previous": old.get("active")}))


def test_default_and_explicit_opt_out(settings, monkeypatch):
    monkeypatch.setenv("SOOTA_CONFIG_DIR", str(settings.config_dir))
    monkeypatch.setenv("SOOTA_DATA_DIR", str(settings.data_dir))
    monkeypatch.delenv("SOOTA_AUTO_UPDATE")
    assert load_settings().updates.enabled
    monkeypatch.setenv("SOOTA_AUTO_UPDATE", "false")
    assert not load_settings().updates.enabled


def test_online_boundaries(settings):
    settings.updates.enabled = True
    assert should_update(settings, inspect, ("url",), {})
    assert not should_update(settings, inspect, ("url",), {"fetch": False})
    assert not should_update(settings, ingest, ("url",), {"dry_run": True})
    assert not should_update(settings, render, (), {})


def test_updates_all_packages_but_does_not_enable_mcp(settings, monkeypatch):
    settings.updates.enabled = True
    monkeypatch.setattr("soota.auto_updates._latest", lambda url: "3.0.0")
    monkeypatch.setattr("soota.auto_updates._upgrade", install_fake)
    monkeypatch.setattr("soota.providers.mcp_client.StdioMCPClient.call", fail)
    report = automatic_update(settings)
    assert [r["status"] for r in report["providers"]] == ["updated"] * 3
    assert not settings.opencli_mcp.enabled
    assert Path(settings.opencli_mcp.runtime_binary).name == npm_entry(Path(), "opencli-mcp").name
    assert Path(settings.tikhub.runtime_python).name == venv_python(Path()).name
    assert all(r["status"] == "current" for r in automatic_update(settings)["providers"])


def test_failed_update_keeps_current_and_other_providers_continue(settings, monkeypatch):
    install_fake(MCPRuntime(settings), "1.0.0")
    monkeypatch.setattr("soota.auto_updates._latest", lambda url: "3.0.0")

    def upgrade(runtime, version):
        if runtime.command == "opencli-mcp":
            raise ContractError("offline probe failed")
        install_fake(runtime, version)

    monkeypatch.setattr("soota.auto_updates._upgrade", upgrade)
    report = automatic_update(settings, force=True)
    assert MCPRuntime(settings).state()["active"] == "1.0.0"
    assert report["providers"][0]["status"] == "failed"
    assert report["providers"][2]["status"] == "updated"
    assert len(report["warnings"]) == 1


def test_interval_opt_out_and_explicit_external_binary(settings, monkeypatch):
    monkeypatch.setattr("soota.auto_updates._latest", fail)
    assert automatic_update(settings)["providers"] == []
    monkeypatch.setattr("soota.auto_updates._latest", lambda url: "3.0.0")
    monkeypatch.setattr("soota.auto_updates._upgrade", install_fake)
    settings.opencli.binary = "/external/opencli"
    settings.updates.enabled = True
    settings.updates.interval_seconds = 3600
    assert automatic_update(settings)["providers"][1]["status"] == "external"
    monkeypatch.setattr("soota.auto_updates._latest", fail)
    assert automatic_update(settings)["cached"]


def test_updater_lock_preserves_execution(settings, monkeypatch):
    lock = OpenCLIRuntime(settings)
    lock.root = settings.data_dir / "runtimes" / "updates"
    monkeypatch.setattr("soota.auto_updates._latest", fail)
    with lock.lock(exclusive=True):
        result = automatic_update(settings, force=True)
    assert result["warnings"] and result["providers"] == []


def test_cli_updates_then_uses_same_settings_and_emits_single_json(settings, monkeypatch):
    settings.updates.enabled = True
    monkeypatch.setattr("soota.config.load_settings", lambda: settings)
    monkeypatch.setattr("soota.auto_updates._latest", lambda url: "3.0.0")
    monkeypatch.setattr("soota.auto_updates._upgrade", install_fake)
    events = []

    def collect(config, url, **kwargs):
        from soota.features import success

        events.append(config.tikhub.runtime_python)
        return success("done", {})

    collect.__name__ = "inspect"
    monkeypatch.setattr("soota.cli.inspect_cmd", collect)
    result = CliRunner().invoke(app, ["inspect", "https://example.com", "--json"])
    assert result.exit_code == 0, result.output
    assert Path(events[0]) == venv_python(TikHubRuntime(settings).root / "versions" / "3.0.0")
    assert len(json.loads(result.stdout)["data"]["runtime_updates"]["providers"]) == 3


def test_sdk_worker_probe_is_offline_and_process_input_is_bounded():
    result = run_process([sys.executable, "-I", str(WORKER), "--probe"], timeout=20, max_bytes=65536)
    assert result.returncode == 0 and result.stdout.strip() == "2.1.2"
    payload = b"x" * 200_000
    result = run_process(
        [sys.executable, "-c", "import sys; print(len(sys.stdin.buffer.read()))"],
        input_bytes=payload,
        timeout=10,
        max_bytes=65536,
    )
    assert result.stdout.strip() == "200000"


def test_managed_sdk_key_only_on_stdin_and_does_not_retry(settings, monkeypatch):
    from soota.providers.tikhub.managed_client import ManagedClient

    calls = []

    def process(argv, **kwargs):
        assert "synthetic-key" not in " ".join(argv)
        assert json.loads(kwargs["input_bytes"])["api_key"] == "synthetic-key"
        calls.append(argv)
        return ProcessResult(0, '{"ok":false,"error":"TikHubRateLimitError","status_code":429}', "secret stderr")

    monkeypatch.setattr("soota.providers.tikhub.managed_client.run_process", process)
    client = ManagedClient("python", api_key="synthetic-key", base_url="https://example.com", timeout=1)
    with pytest.raises(SourceError, match="限流"):
        client._request("GET", "/test", params={}, json=None)
    assert len(calls) == 1


def test_managed_sdk_obeys_payment_gate_before_dispatch(settings, monkeypatch):
    settings.payment = PaymentConfig(mode="ask")
    monkeypatch.setattr("soota.billing.confirm_payment", lambda data: False)
    monkeypatch.setattr("soota.providers.tikhub.managed_client.run_process", fail)
    provider = TikHubProvider(
        cache=CacheStore(settings.cache_dir),
        api_key="synthetic-key",
        runtime_python="python",
        payment_gate=PaymentGate(settings),
    )
    with pytest.raises(PaymentRequired):
        provider._call("/api/v1/bilibili/web/fetch_one_video_v3", {}, billed=True)
    assert provider.records == []


def test_sdk_failed_probe_does_not_activate_or_change_existing_env(settings, monkeypatch):
    runtime = TikHubRuntime(settings)
    install_fake(runtime, "2.0.0")
    monkeypatch.setattr("soota.providers.managed.shutil.which", lambda binary: "/bin/uv")
    monkeypatch.setattr("soota.providers.managed.run_process", lambda *a, **kw: ProcessResult(0, "", ""))
    monkeypatch.setattr(runtime, "probe", lambda binary: "9.0.0")
    with pytest.raises(ContractError):
        runtime.update("3.0.0")
    assert runtime.state()["active"] == "2.0.0"
    bind_runtimes(settings)
    assert Path(settings.tikhub.runtime_python) == runtime.managed_binary("2.0.0")


def test_estimates_remain_offline(settings):
    from soota.pipeline import estimate

    settings.updates.enabled = True
    assert not should_update(settings, estimate, ("url",), {"mode": "catalog"})
    assert not should_update(settings, estimate, ("url",), {"provider": "opencli"})


@pytest.mark.parametrize("new_daemon", [False, True])
def test_daemon_switch_failure_preserves_matching_runtime(settings, monkeypatch, new_daemon):
    from soota.auto_updates import _upgrade

    runtime = OpenCLIRuntime(settings)
    install_fake(runtime, "1.0.0")
    statuses = iter(
        [
            {"daemonVersion": "1.0.0", "pending": 0},
            {"daemonVersion": "2.0.0" if new_daemon else "1.0.0", "pending": 0},
        ]
    )
    monkeypatch.setattr("soota.providers.opencli_context.daemon_status", lambda: next(statuses))
    monkeypatch.setattr(runtime, "update", lambda version: install_fake(runtime, version))
    monkeypatch.setattr(runtime, "resolve", lambda: ("opencli", "managed"))

    def broken(*args, **kwargs):
        raise SourceError("Connection failed")

    monkeypatch.setattr("soota.providers.opencli_context.connect_runtime", broken)
    with pytest.raises(SourceError):
        _upgrade(runtime, "2.0.0")
    assert runtime.state()["active"] == ("2.0.0" if new_daemon else "1.0.0")


def test_busy_daemon_defers_before_install(settings, monkeypatch):
    from soota.auto_updates import _upgrade

    runtime = OpenCLIRuntime(settings)
    install_fake(runtime, "1.0.0")
    monkeypatch.setattr(
        "soota.providers.opencli_context.daemon_status", lambda: {"daemonVersion": "1.0.0", "pending": 1}
    )
    monkeypatch.setattr(runtime, "update", fail)
    with pytest.raises(SourceError, match="任务"):
        _upgrade(runtime, "2.0.0")
    assert runtime.state()["active"] == "1.0.0"
