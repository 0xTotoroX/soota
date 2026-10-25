"""
[INPUT]: Local runtimes, reviewed bindings and enabled providers.
[OUTPUT]: Offline capability inventory, including Weir URL body contracts.
[POS]: Discovery only; never claims live account or website verification.
[PROTOCOL]: 变更时更新此头部，然后检查 AGENTS.md
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import json
import re
import shutil
from pathlib import Path
from typing import Any

from soota.billing import provider_enabled
from soota.config import SootaSettings
from soota.content.native_bindings import CONTENT_BINDINGS, MEDIA_BINDINGS, URL_ROW_BINDINGS
from soota.errors import SootaError
from soota.providers import opencli_commands
from soota.providers.opencli_runtime import OpenCLIRuntime
from soota.providers.process import run_process
from soota.providers.tikhub.registry import load_registry
from soota.transcripts.formats import FORMATS

ARGUMENT_TYPES = {"str", "string", "int", "number", "float", "bool", "boolean"}
STANDARD_OPENCLI = {
    "youtube/video": "video-metadata",
    "bilibili/video": "video-metadata",
    "youtube/transcript": "transcript",
    "bilibili/subtitle": "transcript",
    **CONTENT_BINDINGS,
    **dict.fromkeys(MEDIA_BINDINGS, "local-media-candidates"),
    **dict.fromkeys(URL_ROW_BINDINGS, "url-rows"),
}


def _opencli_capability(row: dict[str, Any], version: str | None) -> dict[str, Any]:
    contract = STANDARD_OPENCLI.get(row["command"])
    compatible = all(arg.get("type", "str") in ARGUMENT_TYPES for arg in row["args"])
    state = ("standard" if contract else "native") if compatible else "discovered"
    if row["access"] != "read":
        state = "blocked"
    return {
        "id": "opencli." + row["command"].replace("/", "."),
        "provider": "opencli",
        "operation": row["command"],
        "description": row.get("description", ""),
        "state": state,
        "runtime_version": version,
        "access": row["access"],
        "browser": row["browser"],
        "domain": row.get("domain"),
        "args": row["args"],
        "columns": row.get("columns", []),
        "output_contract": contract or "provider_native",
        "reason": "内容获取不执行远程写操作。" if state == "blocked" else None,
        "verification": None,
    }


def _package_version(package: str) -> str | None:
    try:
        return importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        return None


def _tikhub_version(settings: SootaSettings) -> str | None:
    if not settings.tikhub.runtime_python:
        return _package_version("tikhub")
    result = run_process(
        [
            settings.tikhub.runtime_python,
            "-I",
            "-c",
            "import importlib.metadata; print(importlib.metadata.version('tikhub'))",
        ],
        timeout=15,
        max_bytes=65536,
    )
    version = result.stdout.strip()
    return version if result.returncode == 0 and re.fullmatch(r"[0-9][0-9A-Za-z.+_-]*", version) else None


def _processor_inventory(settings: SootaSettings) -> list[dict[str, Any]]:
    executables = {
        "yt-dlp": settings.local.binary,
        "ffmpeg": "ffmpeg",
        "ffprobe": "ffprobe",
        "whisper-cli": settings.asr.whisper_bin,
        "mlx-whisper": settings.asr.mlx_whisper_bin,
    }
    results: list[dict[str, Any]] = [
        {
            "id": name,
            "available": shutil.which(binary) is not None,
            "state": "discovered" if shutil.which(binary) else "blocked",
            "verification": None,
        }
        for name, binary in executables.items()
    ]
    installed = importlib.util.find_spec("faster_whisper") is not None
    results.append(
        {
            "id": "faster-whisper",
            "available": installed,
            "state": "discovered" if installed else "blocked",
            "verification": None,
        }
    )
    results.append(
        {
            "id": "subtitle-conversion",
            "available": True,
            "state": "standard",
            "formats": sorted(FORMATS),
            "verification": None,
        }
    )
    return results


def discover_capabilities(settings: SootaSettings) -> dict[str, Any]:
    """Inspect local registries only. No updates, website calls, MCP startup or credential probes."""
    providers: list[dict[str, Any]] = []
    capabilities: list[dict[str, Any]] = []
    warnings: list[str] = []
    runtime = OpenCLIRuntime(settings)
    if not provider_enabled(settings, "opencli"):
        providers.append(
            {
                "provider": "opencli",
                "enabled": False,
                "available": False,
                "state": "blocked",
                "reason": "来源策略已停用。",
            }
        )
    else:
        try:
            # Holding the shared lock keeps the version and registry on the same managed install.
            with runtime.lock():
                status = runtime.status()
                rows = opencli_commands.commands(settings) if status["available"] else []
            version = status.get("version")
            providers.append(
                {
                    "provider": "opencli",
                    "available": status["available"],
                    "runtime_version": version,
                    "ownership": status.get("ownership"),
                    "state": "discovered" if status["available"] else "blocked",
                    "browser_checked": False,
                }
            )
            capabilities.extend(_opencli_capability(row, version) for row in rows)
        except (SootaError, OSError) as exc:
            providers.append(
                {
                    "provider": "opencli",
                    "available": False,
                    "state": "blocked",
                    "reason": "本机 OpenCLI 运行时或命令契约不可用。",
                }
            )
            warnings.append(f"OpenCLI 发现失败：{type(exc).__name__}；未执行站点请求。")
    mcp = settings.opencli_mcp
    mcp_enabled = provider_enabled(settings, "opencli-mcp")
    mcp_installed = shutil.which(mcp.binary or mcp.runtime_binary or "opencli-mcp") is not None
    providers.append(
        {
            "provider": "opencli-mcp",
            "enabled": mcp_enabled,
            "available": mcp_enabled and mcp_installed,
            "state": "discovered" if mcp_enabled and mcp_installed else "blocked",
            "reason": "未启动 MCP；站点命令未探测。" if mcp_enabled else "MCP 当前停用，未启动进程。",
            "verification": None,
        }
    )
    registry = load_registry()
    try:
        sdk_version = _tikhub_version(settings) if provider_enabled(settings, "tikhub") else None
    except (SootaError, OSError):
        sdk_version = None
        warnings.append("TikHub 当前运行时版本无法核实；未查询账号或发起 API 请求。")
    providers.append(
        {
            "provider": "tikhub",
            "available": sdk_version is not None,
            "state": "discovered" if sdk_version else "blocked",
            "runtime_version": sdk_version,
            "registry_as_of": registry.as_of,
            "credentials_checked": False,
            "prices_checked": False,
        }
    )
    for endpoint in registry.preferred() if provider_enabled(settings, "tikhub") else []:
        callable_read = sdk_version is not None and endpoint.callable and not endpoint.deprecated
        capabilities.append(
            {
                "id": "tikhub." + endpoint.key,
                "provider": "tikhub",
                "operation": endpoint.key,
                "description": endpoint.summary,
                "state": "native" if callable_read else "blocked",
                "runtime_version": sdk_version,
                "access": "read" if endpoint.callable else "unclassified",
                "input_schema": endpoint.query_schema,
                "tasks": endpoint.tasks,
                "output_contract": "provider_native",
                "requires_payment_policy": True,
                "verification": None,
            }
        )
    monid_enabled = provider_enabled(settings, "monid")
    providers.append(
        {
            "provider": "monid",
            "enabled": monid_enabled,
            "available": monid_enabled,
            "state": "discovered" if monid_enabled else "blocked",
            "credentials_checked": False,
            "prices_checked": False,
            "verification": None,
        }
    )
    if monid_enabled:
        prices = json.loads(
            Path(__file__).parents[1].joinpath("providers/monid/prices.json").read_text(encoding="utf-8")
        )
        for endpoint in registry.preferred():
            if endpoint.path not in prices["tikhub"]:
                continue
            capabilities.append(
                {
                    "id": "monid." + endpoint.key,
                    "provider": "monid",
                    "operation": endpoint.key,
                    "description": endpoint.summary,
                    "state": "native" if endpoint.callable and not endpoint.deprecated else "blocked",
                    "access": "read" if endpoint.callable else "unclassified",
                    "input_schema": endpoint.query_schema,
                    "tasks": endpoint.tasks,
                    "output_contract": "provider_native",
                    "requires_payment_policy": True,
                    "verification": None,
                }
            )
    weir_enabled = provider_enabled(settings, "weir")
    providers.append(
        {
            "provider": "weir",
            "enabled": weir_enabled,
            "state": "standard" if weir_enabled else "blocked",
            "verification": None,
        }
    )
    for operation, description in (
        ("x.post", "X 单帖正文"),
        ("x.article", "分享帖承载的 X Article 全文"),
        ("reddit.post", "Reddit 单帖正文，不含评论"),
    ):
        capabilities.append(
            {
                "id": "weir." + operation,
                "provider": "weir",
                "operation": operation,
                "description": description,
                "state": "standard" if weir_enabled else "blocked",
                "access": "read",
                "output_contract": "single-item-body",
                "verification": None,
                "input": "ingest URL --provider weir",
                "free_only": True,
            }
        )
    return {
        "providers": providers,
        "capabilities": capabilities,
        "processors": _processor_inventory(settings),
        "warnings": warnings,
        "state_definitions": {
            "discovered": "本机发现运行时或元数据，尚无统一执行或产物契约。",
            "native": "可经原生参数与数据通道调用，未承诺标准正文或媒体产物。",
            "standard": "已有共享输出契约和转换入口，仍需具体来源的运行验收。",
            "verified": "需要具体来源、运行时版本、目标和实际产物的验证记录；发现本身不会生成此状态。",
            "blocked": "运行时不可用、契约不支持、停用或不符合只读获取范围。",
        },
    }
