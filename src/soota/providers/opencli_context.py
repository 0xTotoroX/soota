from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import httpx

from soota.errors import ContractError, InputError, SourceError
from soota.providers.opencli_runtime import RuntimeUnavailable


def daemon_status(context: str | None = None) -> dict[str, Any]:
    port = os.environ.get("OPENCLI_DAEMON_PORT", "19825")
    if not port.isdigit() or not 1 <= int(port) <= 65535:
        raise InputError("OPENCLI_DAEMON_PORT 无效。")
    try:
        response = httpx.get(
            f"http://127.0.0.1:{port}/status",
            headers={"X-OpenCLI": "1"},
            params={"contextId": context} if context else {},
            timeout=2,
            trust_env=False,
        )
        response.raise_for_status()
        value = response.json()
        if not isinstance(value, dict):
            raise ValueError
        return value
    except httpx.ConnectError as exc:
        raise RuntimeUnavailable(
            "OpenCLI 守护进程未运行。", hint="运行 soota runtime connect，启动 Soota 选用的版本并连接 Browser Bridge。"
        ) from exc
    except (httpx.HTTPError, ValueError) as exc:
        raise SourceError("无法确认 OpenCLI 守护进程状态；未启动或重启任何进程。") from exc


def profile_config() -> dict[str, Any]:
    root = Path(os.environ.get("OPENCLI_CONFIG_DIR", str(Path.home() / ".opencli")))
    path = root / "browser-profiles.json"
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (ValueError, OSError) as exc:
        raise ContractError("无法读取 OpenCLI 浏览器 profile 配置。") from exc


def resolve_profile(requested: str | None) -> str:
    config = profile_config()
    aliases = config.get("aliases", {})
    if not isinstance(aliases, dict):
        raise ContractError("OpenCLI profile aliases 配置无效。")
    name = requested or os.environ.get("OPENCLI_PROFILE") or config.get("defaultContextId")
    context = aliases.get(name, name) if isinstance(name, str) else None
    if not context:
        status = daemon_status()
        if status.get("profileRequired"):
            raise InputError("连接了多个浏览器 profile，请在 Soota 配置中指定 profile。")
        context = status.get("contextId")
    if not isinstance(context, str) or not context.strip() or context.startswith("-"):
        raise InputError("无法确定实际浏览器上下文；请先配置 OpenCLI profile。")
    # Passing a context ID must not be reinterpreted as a different mutable alias by OpenCLI.
    if aliases.get(context, context) != context:
        raise InputError("OpenCLI context ID 与别名冲突，请先修正 profile 配置。")
    return context


def preflight(version: str, context: str) -> None:
    status = daemon_status(context)
    if status.get("daemonVersion") != version:
        raise SourceError(
            "OpenCLI 与正在运行的守护进程版本不同；已停止调用，避免重启其他采集任务。",
            hint="等待已有任务结束后运行 soota runtime connect，或 soota runtime rollback。",
        )
    if status.get("profileRequired") or status.get("profileDisconnected"):
        raise InputError("指定浏览器 profile 不可用；请检查连接或选择正确账号。")
    if not status.get("extensionConnected"):
        raise RuntimeUnavailable("OpenCLI Browser Bridge 扩展未连接。")
    if status.get("contextId") != context:
        raise ContractError("OpenCLI 守护进程返回了不同的浏览器上下文。")


def connect_runtime(binary: str, *, replace_idle: bool = False) -> dict[str, Any]:
    """Prepare the selected runtime without replaying any site operation."""
    import time

    from soota.providers.process import run_process

    probe = run_process([binary, "--version"], timeout=15, max_bytes=65536)
    if probe.returncode or not probe.stdout.strip():
        raise RuntimeUnavailable("OpenCLI 版本检查失败，未启动守护进程。")
    version = probe.stdout.strip()
    try:
        status = daemon_status()
    except RuntimeUnavailable:
        status = None
    if status is not None and status.get("daemonVersion") != version:
        if not replace_idle or status.get("pending") != 0 or status.get("sessionLeases"):
            raise SourceError("OpenCLI 守护进程版本不同或仍有会话，请等任务结束后运行 soota runtime connect。")
    if status is None or status.get("daemonVersion") != version:
        result = run_process([binary, "daemon", "restart"], timeout=20, max_bytes=65536)
        if result.returncode:
            raise RuntimeUnavailable("OpenCLI 守护进程启动失败。")
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            status = daemon_status()
        except RuntimeUnavailable:
            time.sleep(0.5)
            continue
        if status.get("extensionConnected") and status.get("daemonVersion") == version:
            return status
        time.sleep(0.5)
    raise RuntimeUnavailable("OpenCLI 已启动，但 Browser Bridge 未连接；请打开安装扩展的浏览器。")
