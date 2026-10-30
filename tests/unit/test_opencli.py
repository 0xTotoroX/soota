import json
import sys
from pathlib import Path

import pytest

from soota.catalog import catalog, resume_catalog
from soota.errors import AuthBudgetError, ContractError, InputError, SourceError
from soota.models import CatalogJob, RequestRecord, TranscriptMode
from soota.pipeline import ingest, inspect
from soota.providers.opencli import OpenCLIProvider, normalize_video
from soota.providers.opencli_runtime import OpenCLIRuntime, RuntimeUnavailable
from soota.providers.process import run_process
from soota.providers.router import RequestBudget, inspect_item
from soota.routing import recognize
from soota.storage.jobs import JobStore
from tests.conftest import load_fixture

BILI = "https://www.bilibili.com/video/BV1M1421t7hT"
X = "https://x.com/example"
REAL_RUN = OpenCLIProvider._run


def video_rows():
    return [
        {"field": k, "value": v}
        for k, v in {
            "bvid": "BV1M1421t7hT",
            "title": "Example",
            "author": "Creator (mid: 123)",
            "duration": "12m34s (754s)",
            "publish_time": "2026-01-01 12:00",
        }.items()
    ]


def post(ident="123", text="A full post", **extra):
    return {"id": ident, "text": text, "author": "example", "created_at": "2026-01-01T12:00:00Z", **extra}


def fail(*args, **kwargs):
    raise AssertionError("No paid request or SDK initialization allowed")


def test_free_metadata_without_key_or_sdk_and_cache_profile_isolation(settings, monkeypatch):
    monkeypatch.setattr("soota.providers.router.TikHubProvider", fail)
    calls = []

    def read(self, *args):
        calls.append(args)
        self.version = "1.8.3"
        return video_rows()

    monkeypatch.setattr(OpenCLIProvider, "_run", read)
    first = inspect(settings, BILI, provider="opencli", max_cost=0)
    second = inspect(settings, BILI, provider="opencli", max_cost=0)
    assert first.data["provider"] == "opencli"
    assert first.data["metadata"]["duration_seconds"] == 754
    assert second.data["requests"][0]["cache_hit"]
    assert len(calls) == 1
    settings.opencli.profile = "separate-account"
    inspect(settings, BILI, provider="opencli")
    assert len(calls) == 2


def test_cache_expiration_and_identity_contract(settings, monkeypatch):
    calls = []
    monkeypatch.setattr(OpenCLIProvider, "_run", lambda *args: calls.append(1) or video_rows())
    settings.opencli.metadata_cache_seconds = 0
    inspect(settings, BILI, provider="opencli")
    inspect(settings, BILI, provider="opencli")
    assert len(calls) == 2
    rows = video_rows()
    rows[0]["value"] = "BV1111111111"
    with pytest.raises(ContractError):
        normalize_video(recognize(BILI), rows)


def test_metadata_bundle_does_not_read_tikhub_or_write_missing_transcript_cache(settings, monkeypatch):
    monkeypatch.setattr("soota.providers.router.TikHubProvider", fail)
    monkeypatch.setattr(OpenCLIProvider, "_run", lambda *args: video_rows())
    result = ingest(settings, BILI, transcript=TranscriptMode.NONE, provider="opencli", max_cost=0)
    manifest = json.loads(Path(result.data["manifest"]).read_text())
    assert result.ok and manifest["processing"]["provider"] == "opencli"
    assert manifest["metadata"]["provenance"]["profile"] == "test-context"
    assert not list(settings.cache_dir.rglob("transcript.*.json"))


@pytest.mark.usefixtures("cached_tikhub_prices")
def test_authorized_fallback_only_for_unavailable_runtime(settings, monkeypatch):
    def unavailable(*args):
        raise RuntimeUnavailable("Bridge unavailable")

    monkeypatch.setattr(OpenCLIProvider, "_run", unavailable)
    calls = []

    def requester(method, path, params):
        calls.append(path)
        return load_fixture("bilibili_info.json")

    with pytest.raises(RuntimeUnavailable):
        inspect_item(settings, recognize(BILI), provider="opencli", requester=fail)
    item, records, warnings = inspect_item(
        settings, recognize(BILI), provider="opencli", fallback="tikhub", max_cost=0.01, requester=requester
    )
    assert item.provenance["provider"] == "tikhub" and warnings
    assert calls == ["/api/v1/bilibili/web/fetch_one_video_v3"]
    assert any(r.billed for r in records)


@pytest.mark.parametrize("error", [AuthBudgetError("login"), SourceError("timeout"), ContractError("bad JSON")])
def test_restricted_or_broken_source_never_falls_back(settings, monkeypatch, error):
    def broken(*args):
        raise error

    monkeypatch.setattr(OpenCLIProvider, "_run", broken)
    monkeypatch.setattr("soota.providers.router.TikHubProvider", fail)
    with pytest.raises(type(error)):
        inspect_item(settings, recognize(BILI), provider="opencli", fallback="tikhub", max_cost=0.01)


def test_zero_budget_never_dispatches_paid_even_with_zero_price(settings, monkeypatch):
    for price in (0, 0.001, None):
        with pytest.raises(AuthBudgetError):
            RequestBudget(0).reserve(RequestRecord(endpoint="paid", billed=True, estimated_cost_usd=price))
    with pytest.raises(AuthBudgetError):
        inspect_item(settings, recognize(BILI), provider="tikhub", max_cost=0, requester=fail)
    with pytest.raises(InputError):
        inspect_item(settings, recognize(BILI), fallback="tikhub", max_cost=0, requester=fail)


def test_snapshot_preserves_posts_deduplicates_and_never_claims_caught_up(settings, monkeypatch):
    monkeypatch.setattr("soota.catalog.TikHubProvider", fail)
    monkeypatch.setattr(OpenCLIProvider, "_run", lambda *args: [post(), post(), post("456", "Second"), {"id": "bad"}])
    first = catalog(settings, X, max_items=5, max_cost=0, provider="opencli")
    assert first.ok and first.data["completeness"] == "unknown" and not first.data["resumable"]
    assert first.data["item_count"] == 2 and first.data["stop_reason"] == "bounded_snapshot"
    payload = json.loads(Path(first.data["catalog"]).read_text())
    assert payload["items"][0]["text"] == "A full post"
    assert any("不符合" in w for w in first.warnings)
    assert payload["processing"]["provider"] == "opencli"
    second = catalog(settings, X, max_items=5, max_cost=0, incremental=True, provider="opencli")
    assert second.data["item_count"] == 0 and second.data["stop_reason"] == "bounded_snapshot"
    settings.opencli.profile = "other"
    third = catalog(settings, X, max_items=5, max_cost=0, incremental=True, provider="opencli")
    assert third.data["item_count"] == 2
    with pytest.raises(InputError, match="游标"):
        resume_catalog(settings, first.data["job_id"])


def test_snapshot_dry_run_is_offline_and_validates_limit(settings):
    result = catalog(settings, X, dry_run=True, max_cost=0)
    assert result.data["estimated_cost_usd"] == 0
    assert not settings.data_dir.exists() and not settings.cache_dir.exists()
    with pytest.raises(InputError):
        catalog(settings, X, max_items=201, max_cost=0)
    with pytest.raises(InputError):
        catalog(settings, X, fallback="tikhub", max_cost=0.01)


def test_old_catalog_record_keeps_tikhub_provider(settings):
    record = JobStore(settings.jobs_dir).create(
        {
            "kind": "catalog",
            "source": recognize(X).model_dump(),
            "output_root": str(settings.output_dir),
            "max_items": 10,
            "max_pages": 1,
            "max_cost": 0.01,
            "page_size": 10,
        }
    )
    job = CatalogJob.model_validate(record)
    assert job.provider == "tikhub" and job.completeness == "paginated"


@pytest.mark.parametrize("url", ["https://x.com/home", "https://x.com/example/likes", "https://x.com/--help"])
def test_x_source_rejects_reserved_and_option_inputs(url):
    with pytest.raises(SourceError):
        recognize(url)


def test_process_argv_is_literal_and_output_is_bounded():
    result = run_process(
        [sys.executable, "-c", "import sys;print(sys.argv[1])", "$(echo unsafe);x"], timeout=2, max_bytes=1024
    )
    assert result.stdout.strip() == "$(echo unsafe);x"
    with pytest.raises(SourceError, match="输出"):
        run_process([sys.executable, "-c", "print('x'*10000)"], timeout=2, max_bytes=1024)
    with pytest.raises(SourceError, match="超时"):
        run_process([sys.executable, "-c", "import time;time.sleep(2)"], timeout=0.05, max_bytes=1024)


def test_runtime_lock_blocks_update_during_collection(settings):
    runtime = OpenCLIRuntime(settings)
    with runtime.lock(), pytest.raises(SourceError, match="正在执行"):
        with OpenCLIRuntime(settings).lock(exclusive=True):
            pass


def test_failed_upgrade_keeps_state_and_never_installs_global(settings, monkeypatch):
    runtime = OpenCLIRuntime(settings)
    runtime.root.mkdir(parents=True)
    state = runtime.root / "state.json"
    state.write_text('{"active": "1.8.3", "previous": null}')
    before = state.read_bytes()
    monkeypatch.setattr("soota.providers.opencli_runtime.shutil.which", lambda value: "/usr/bin/npm")

    def fail_download(*args):
        raise ContractError("bad integrity")

    monkeypatch.setattr("soota.providers.opencli_runtime.package_metadata", fail_download)
    with pytest.raises(ContractError):
        runtime.update("1.8.7")
    assert state.read_bytes() == before
    with pytest.raises(InputError):
        runtime.update("../../global")


def test_managed_update_and_rollback_preserve_external_files(settings, monkeypatch, tmp_path):
    from soota.providers.process import ProcessResult

    runtime = OpenCLIRuntime(settings)
    external = tmp_path / "global-opencli"
    external.write_text("custom user patch")
    monkeypatch.setattr("soota.providers.opencli_runtime.shutil.which", lambda value: str(external))
    monkeypatch.setattr("soota.providers.opencli_runtime.package_metadata", lambda version: {"dist": {}})
    monkeypatch.setattr(
        "soota.providers.opencli_runtime.download_verified", lambda metadata, path: path.write_bytes(b"pkg")
    )
    commands = []

    def install(argv, **kwargs):
        commands.append(argv)
        prefix = Path(argv[argv.index("--prefix") + 1])
        binary = prefix / "node_modules/.bin/opencli"
        binary.parent.mkdir(parents=True)
        binary.write_text("fake installed runtime")
        return ProcessResult(0, "", "")

    monkeypatch.setattr("soota.providers.opencli_runtime.run_process", install)
    monkeypatch.setattr(OpenCLIRuntime, "probe", lambda self, binary: "1.8.7")
    monkeypatch.setattr(OpenCLIRuntime, "status", lambda self: self.state())
    assert runtime.update("1.8.7")["active"] == "1.8.7"
    assert runtime.managed_binary("1.8.7").is_file()
    assert all("-g" not in argv and "--ignore-scripts" in argv for argv in commands)
    assert runtime.rollback()["active"] is None
    assert external.read_text() == "custom user patch"


def test_profile_defaults_and_aliases_bind_actual_context(monkeypatch):
    from soota.providers.opencli_context import resolve_profile

    monkeypatch.delenv("OPENCLI_PROFILE")
    config = {"aliases": {"work": "context-a"}, "defaultContextId": "work"}
    monkeypatch.setattr("soota.providers.opencli_context.profile_config", lambda: config)
    assert resolve_profile(None) == "context-a"
    config["aliases"]["work"] = "context-b"
    assert resolve_profile("work") == "context-b"


def test_daemon_version_mismatch_and_profile_disconnection_never_start_cli(monkeypatch):
    from soota.providers.opencli_context import preflight

    status = {"daemonVersion": "1.8.3", "extensionConnected": True, "contextId": "one"}
    monkeypatch.setattr("soota.providers.opencli_context.daemon_status", lambda *args: status)
    with pytest.raises(SourceError, match="版本不同"):
        preflight("1.8.7", "one")
    status["profileDisconnected"] = True
    with pytest.raises(InputError):
        preflight("1.8.3", "one")


@pytest.mark.parametrize(
    "code,message,expected",
    [
        ("ADAPTER_LOAD", "module failed", SourceError),
        ("BROWSER_CONNECT", "Multiple Browser Bridge profiles are connected", SourceError),
        ("BROWSER_CONNECT", "Browser profile is not connected", SourceError),
        ("BROWSER_CONNECT", "Browser Bridge extension not connected", RuntimeUnavailable),
        ("UNKNOWN", "429 rate limit", AuthBudgetError),
    ],
)
def test_exit_69_is_not_sufficient_to_trigger_paid_fallback(settings, monkeypatch, code, message, expected):
    from soota.providers.process import ProcessResult

    monkeypatch.setattr(OpenCLIProvider, "_run", REAL_RUN)
    monkeypatch.setattr(OpenCLIRuntime, "resolve", lambda self: ("opencli", "external"))
    monkeypatch.setattr("soota.providers.opencli.preflight", lambda *args: None)

    def process(argv, **kwargs):
        return (
            ProcessResult(0, "1.8.3", "")
            if "--version" in argv
            else ProcessResult(69, "", f"ok: false\nerror:\n  code: {code}\n  message: {message}\n")
        )

    monkeypatch.setattr("soota.providers.opencli.run_process", process)
    with pytest.raises(expected) as error:
        OpenCLIProvider(settings).inspect(recognize(BILI))
    assert type(error.value) is expected
