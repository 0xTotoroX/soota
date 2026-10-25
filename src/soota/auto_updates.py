"""Update published provider packages before online CLI work, without billing or enabling MCP."""

from __future__ import annotations

import inspect
import json
import time
from collections.abc import Callable
from typing import Any

import httpx

from soota.config import SootaSettings
from soota.errors import SootaError, SourceError
from soota.models import CommandResult
from soota.providers.managed import MCPRuntime, TikHubRuntime, bind_runtimes
from soota.providers.opencli_runtime import OpenCLIRuntime, validate_version
from soota.storage.atomic import atomic_write
from soota.updates import PROVIDERS

ONLINE_COMMANDS = {
    "inspect",
    "ingest",
    "catalog",
    "resume_catalog",
    "resume",
    "call_endpoint",
    "call_feature",
    "discover",
    "run_site",
}


def should_update(
    settings: SootaSettings, fn: Callable[..., Any], args: tuple[Any, ...], kwargs: dict[str, Any]
) -> bool:
    if not settings.updates.enabled or fn.__name__ not in ONLINE_COMMANDS:
        return False
    arguments = inspect.signature(fn).bind(settings, *args, **kwargs)
    arguments.apply_defaults()
    values = arguments.arguments
    if values.get("dry_run") or values.get("fetch") is False:
        return False
    if fn.__module__ == "soota.tasks.entry" and fn.__name__ == "ingest":
        from soota.workflow_compat import ingest_needs_workflow

        if ingest_needs_workflow(
            values.get("want"), values.get("optional"), values.get("destination"), values.get("vault")
        ):
            return False  # LoreLab owns update policy for forwarded content tasks.
        if values.get("operation") is None:
            from soota.tasks.inputs import parse_inputs

            inputs = parse_inputs(values.get("url"), values.get("inputs_file"))
            if all(item.kind in {"file", "bundle"} for item in inputs):
                return False
    if fn.__module__ == "soota.tasks.runner" and fn.__name__ == "resume":
        if values.get("analysis") is not None:
            return False
        from soota.workflow_compat import job_needs_workflow

        if job_needs_workflow(settings, values["job_id"]):
            return False
        from soota.storage.jobs import JobStore

        record = JobStore(settings.jobs_dir).get(values["job_id"])
        if record.get("kind") == "content-task":
            from soota.tasks.state import load_spec

            spec = load_spec(settings, values["job_id"])
            if all(item.kind in {"file", "bundle"} for item in spec.inputs):
                return False
    if fn.__name__ in {"discover", "run_site"} and not settings.opencli_mcp.enabled:
        return False
    return True


def _latest(url: str) -> str:
    response = httpx.get(url, timeout=15)
    response.raise_for_status()
    payload = response.json()
    version = (payload.get("info") or payload)["version"]
    validate_version(version)
    return str(version)


def _upgrade(runtime: OpenCLIRuntime, latest: str) -> None:
    from soota.providers.opencli_context import connect_runtime, daemon_status

    daemon = None
    if runtime.command == "opencli":
        try:
            daemon = daemon_status()
        except SourceError:
            pass
        if daemon and daemon.get("daemonVersion") != latest:
            if daemon.get("pending") != 0 or daemon.get("sessionLeases"):
                raise SourceError("OpenCLI 仍有执行中的浏览器任务，稍后再更新。")
    old = runtime.state()
    if old.get("active") != latest or not runtime.managed_binary(latest).is_file():
        runtime.update(latest)
    if daemon and daemon.get("daemonVersion") != latest:
        binary, _ = runtime.resolve()
        try:
            connect_runtime(binary, replace_idle=True)
        except SootaError:
            try:
                observed = daemon_status()
            except SourceError:
                observed = None
            # A newly started daemon with a disconnected extension still belongs to the new version.
            if not observed or observed.get("daemonVersion") != latest:
                with runtime.lock(exclusive=True):
                    if runtime.state().get("active") == latest:
                        atomic_write(runtime.root / "state.json", json.dumps(old))
            raise


def automatic_update(settings: SootaSettings, *, force: bool = False) -> dict[str, Any]:
    report: dict[str, Any] = {"providers": [], "warnings": []}
    if not force and not settings.updates.enabled:
        return report
    lock = OpenCLIRuntime(settings)
    lock.root = settings.data_dir / "runtimes" / "updates"
    state_path = lock.root / "last-check.json"
    try:
        with lock.lock(exclusive=True):
            if not force and settings.updates.interval_seconds and state_path.is_file():
                try:
                    saved = json.loads(state_path.read_text(encoding="utf-8"))
                    if time.time() - saved["checked_at"] < settings.updates.interval_seconds:
                        return {**saved, "cached": True}
                except (ValueError, KeyError, TypeError):
                    pass
            runtimes = {
                "opencli-mcp": MCPRuntime(settings),
                "opencli": OpenCLIRuntime(settings),
                "tikhub": TikHubRuntime(settings),
            }
            for name, _, registry in PROVIDERS:
                if name in settings.routing.disabled_providers:
                    report["providers"].append({"provider": name, "status": "disabled", "active_version": None})
                    continue
                runtime = runtimes[name]
                row: dict[str, Any] = {"provider": name, "status": "failed"}
                try:
                    if runtime.configured_binary:
                        row["status"] = "external"
                        report["warnings"].append(
                            f"{name} 使用显式外部路径，未覆盖；移除 binary 配置可交由 Soota 更新。"
                        )
                    else:
                        latest = _latest(registry)
                        current = runtime.state().get("active")
                        row.update(previous_version=current, latest_version=latest)
                        if current == latest and runtime.managed_binary(latest).is_file():
                            if name == "opencli":
                                _upgrade(runtime, latest)
                            row["status"] = "current"
                        else:
                            _upgrade(runtime, latest)
                            row["status"] = "updated"
                except (SootaError, httpx.HTTPError, OSError, ValueError, KeyError, TypeError) as exc:
                    # Never expose registry responses, subprocess stderr or private paths.
                    reason = exc.message if isinstance(exc, SootaError) else "无法检查或安装最新版"
                    report["warnings"].append(f"{name}：{reason}；详见当前运行时状态。")
                row["active_version"] = runtime.state().get("active")
                report["providers"].append(row)
            report["checked_at"] = time.time()
            atomic_write(state_path, json.dumps(report, ensure_ascii=False, indent=2))
    except (SootaError, OSError):
        report["warnings"].append("Provider 更新检查正在执行或状态目录不可写；本次未完成检查，采集仍受运行时锁约束。")
    bind_runtimes(settings)
    return report


def apply_updates(settings: SootaSettings) -> CommandResult:
    report = automatic_update(settings, force=True)
    return CommandResult(
        ok=not bool(report["warnings"]),
        status="partial" if report["warnings"] else "success",
        code="provider_updates_partial" if report["warnings"] else "ok",
        exit_code=1 if report["warnings"] else 0,
        message="Provider 更新检查已完成。",
        data={"runtime_updates": report},
        warnings=report["warnings"],
    )
