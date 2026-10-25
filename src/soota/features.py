from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

from soota.api import _protect, call_endpoint, prepare_request
from soota.billing import provider_enabled, selected_provider
from soota.config import SootaSettings
from soota.errors import InputError, SourceError
from soota.feature_aliases import ALIASES, FeatureAlias, alias_params
from soota.models import CommandResult, SourceRef
from soota.providers import opencli_commands
from soota.providers.mcp_client import StdioMCPClient, require_enabled
from soota.providers.opencli_mcp import READ_COMMANDS, OpenCLIMCPProvider
from soota.providers.opencli_runtime import RuntimeUnavailable
from soota.providers.priority import NAMES, quote_rows, rule_for
from soota.providers.tikhub.registry import load_registry
from soota.secrets import CREDENTIAL_FIELDS
from soota.storage.atomic import atomic_write

MCP_DESCRIPTIONS = {
    ("bilibili", "video"): "Bilibili 单视频详情",
    ("bilibili", "subtitle"): "Bilibili 单视频字幕",
    ("twitter", "tweets"): "X 最近帖子快照",
}


def success(message: str, data: dict[str, Any], warnings: list[str] | None = None) -> CommandResult:
    return CommandResult(
        ok=True, status="success", code="ok", exit_code=0, message=message, data=data, warnings=warnings or []
    )


def list_features(
    settings: SootaSettings,
    *,
    provider: str | None = None,
    site: str | None = None,
    search: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> CommandResult:
    if provider == "soota":
        provider = "yt-dlp"
    if (
        provider not in {None, "opencli-mcp", "opencli", "yt-dlp", "tikhub", "monid"}
        or not 1 <= limit <= 5000
        or offset < 0
    ):
        raise InputError("provider 支持 opencli-mcp/opencli/yt-dlp/tikhub/monid；limit 为 1–5000，offset 非负。")
    rows: list[dict[str, Any]] = []
    warnings: list[str] = []
    if settings.opencli_mcp.enabled and provider in {None, "opencli-mcp"}:
        installed = StdioMCPClient.available(settings.opencli_mcp)
        if not installed:
            warnings.append("opencli-mcp 未安装；已列出 Soota 已核验的只读能力，安装后可调用。")
        for site_name, command_name in sorted(READ_COMMANDS):
            operation = f"{site_name}/{command_name}"
            rows.append(
                {
                    "id": "opencli-mcp." + operation.replace("/", "."),
                    "site": site_name,
                    "description": MCP_DESCRIPTIONS[(site_name, command_name)],
                    "providers": ["opencli-mcp"],
                    "access": "read",
                    "callable": installed,
                    "browser": True,
                    "plans": [{"provider": "opencli-mcp", "operation": operation}],
                }
            )
    for paid_name in ("tikhub", "monid"):
        if paid_name == "monid" and provider is None and not provider_enabled(settings, "monid"):
            continue
        if provider not in {None, paid_name}:
            continue
        for endpoint in load_registry().preferred():
            if paid_name == "monid":
                source = SourceRef(platform=endpoint.platform, kind="query", id="feature", url="", canonical_url="")
                if not quote_rows(settings, source, "api", [paid_name], endpoint=endpoint.path)[0]["supported"]:
                    continue
                if not endpoint.callable or endpoint.deprecated:
                    continue
            rows.append(
                {
                    "id": paid_name + "." + endpoint.key,
                    "site": endpoint.platform,
                    "description": endpoint.summary,
                    "providers": [paid_name],
                    "access": "read" if endpoint.callable else "unclassified",
                    "callable": not endpoint.deprecated,
                    "requires_allow_write": not endpoint.callable,
                    "plans": [{"provider": paid_name, "operation": endpoint.key}],
                    "reason": None if endpoint.callable else "辅助接口；需显式 --allow-write，价格未知时仍暂停",
                }
            )
    for alias in ALIASES:
        row = alias_view(settings, alias)
        if provider:
            row["plans"] = [plan for plan in row["plans"] if plan["provider"] == provider]
            if not row["plans"]:
                continue
            row["providers"] = [plan["provider"] for plan in row["plans"]]
        rows.append(row)
    selected = [
        row
        for row in rows
        if (site is None or row["site"] == site)
        and (search is None or search.casefold() in f"{row['id']} {row['description']}".casefold())
    ]
    selected.sort(key=lambda row: row["id"])
    page = selected[offset : offset + limit]
    return success(
        f"找到 {len(selected)} 个功能入口，本页 {len(page)} 个。",
        {
            "features": page,
            "total": len(selected),
            "offset": offset,
            "next_offset": offset + len(page) if offset + len(page) < len(selected) else None,
            "inventory": "Soota 业务入口和 TikHub 封装；OpenCLI 原生命令直接使用上游 CLI",
        },
        warnings,
    )


def alias_view(settings: SootaSettings, alias: FeatureAlias, *, preferred: str | None = None) -> dict[str, Any]:
    plans = [{"provider": "opencli", "operation": alias.opencli}, {"provider": "tikhub", "operation": alias.tikhub}]
    if alias.id in {"youtube.video.detail", "bilibili.video.detail"}:
        plans.insert(1, {"provider": "yt-dlp", "operation": alias.opencli})
    if settings.opencli_mcp.enabled and alias.opencli_mcp:
        plans.insert(0, {"provider": "opencli-mcp", "operation": alias.opencli_mcp})
    endpoint = load_registry().resolve(alias.tikhub)
    source = SourceRef(platform=endpoint.platform, kind="query", id="feature", url="", canonical_url="")
    quotes = {
        row["provider"]: row for row in quote_rows(settings, source, "api", ["tikhub", "monid"], endpoint=endpoint.path)
    }
    if quotes["monid"]["supported"]:
        plans.append({"provider": "monid", "operation": alias.tikhub})
    rule = rule_for(settings, endpoint.platform, "inspect" if alias.id.endswith("video.detail") else "api")
    order = list(rule.order) + [name for name in NAMES if name not in rule.order]

    def plan_cost(plan: dict[str, str]) -> float:
        price = quotes.get(plan["provider"], {}).get("estimated_cost_usd", 0)
        return float(price) if price is not None else float("inf")

    plans.sort(key=lambda plan: (plan_cost(plan) if rule.mode == "cost" else 0, order.index(plan["provider"])))
    preferred = preferred or settings.routing.default_provider
    if preferred != "auto":
        plans.sort(key=lambda plan: plan["provider"] != preferred)
    return {
        "id": alias.id,
        "site": alias.id.split(".")[0],
        "description": alias.description,
        "providers": [plan["provider"] for plan in plans],
        "access": "read",
        "callable": True,
        "plans": plans,
        "required": [alias.parameter],
        "mapping": asdict(alias),
        "optional": {"sort": ["top", "latest"]} if alias.id == "twitter.post.search" else {},
        "output_contract": "provider_native",
        "payment": settings.payment.model_dump(),
    }


def resolve_opencli(settings: SootaSettings, selector: str) -> dict[str, Any]:
    for command in opencli_commands.commands(settings):
        if selector == "opencli." + command["command"].replace("/", "."):
            return command
    raise RuntimeUnavailable("当前 OpenCLI 运行时没有该功能。", hint="通过 opencli list -f json 查询上游当前能力。")


def show_feature(settings: SootaSettings, selector: str) -> CommandResult:
    alias = next((item for item in ALIASES if item.id == selector), None)
    if alias:
        return success(alias.description, {"feature": alias_view(settings, alias)})
    if selector.startswith("opencli."):
        raise InputError("OpenCLI 原生命令映射已移除。", hint="使用 opencli list -f json 和上游命令的 --help。")
    if selector.startswith("opencli-mcp."):
        site_name, command_name = resolve_mcp(selector)
        return success(
            "opencli-mcp 已核验只读功能。",
            {"feature": {"id": selector, "site": site_name, "command": command_name, "access": "read"}},
        )
    if selector.startswith(("tikhub.", "monid.")):
        endpoint = load_registry().resolve(selector.split(".", 1)[1])
        return success(
            "供应商原生功能契约。",
            {
                "feature": {
                    "id": selector,
                    **endpoint.model_dump(mode="json", by_alias=True),
                    "query_schema": endpoint.query_schema,
                }
            },
        )
    raise InputError("未知功能标识。", hint="使用 features list 查询完整 id。")


def read_params(params: list[str] | None, params_file: Path | None) -> dict[str, Any]:
    values: dict[str, Any] = {}
    if params_file:
        try:
            values = json.loads(params_file.expanduser().read_text(encoding="utf-8"))
            if not isinstance(values, dict):
                raise ValueError
        except ValueError as exc:
            raise InputError("参数文件需要 JSON 对象。") from exc
    for pair in params or []:
        key, sep, value = pair.partition("=")
        if not sep or not key:
            raise InputError("--param 使用 name=value。")
        if key in values:
            raise InputError(f"重复参数 {key}。")
        values[key] = value
    return values


def _native_opencli(
    settings: SootaSettings, selector: str, params: dict[str, Any], *, dry_run: bool, allow_write: bool
) -> CommandResult:
    command = resolve_opencli(settings, selector)
    arguments = opencli_commands.command_arguments(command, params)
    if dry_run:
        return success(
            "参数已校验；未执行业务命令。",
            {
                "plan": {
                    "provider": "opencli",
                    "operation": command["command"],
                    "access": command["access"],
                    "requires_allow_write": command["access"] == "write",
                    "arguments": arguments,
                    "tikhub_cost_usd": 0,
                    "native_service_cost": "unknown",
                }
            },
        )
    response = opencli_commands.execute(settings, command, params, allow_write=allow_write)
    return success(
        "已收到 OpenCLI 原生结果。",
        {
            "provider": "opencli",
            "operation": command["command"],
            "output_contract": "provider_native",
            "response": response,
            "tikhub_cost_usd": 0,
        },
    )


def resolve_mcp(selector: str) -> tuple[str, str]:
    parts = selector.removeprefix("opencli-mcp.").split(".")
    if len(parts) != 2 or (parts[0], parts[1]) not in READ_COMMANDS:
        raise InputError("未知或未核验的 opencli-mcp 功能。")
    return parts[0], parts[1]


def mcp_arguments(site: str, command: str, params: dict[str, Any]) -> dict[str, Any]:
    if site == "bilibili":
        allowed = {"bvid", "page"} if command == "video" else {"bvid", "lang", "page"}
        if params.keys() - allowed or not isinstance(params.get("bvid"), str):
            raise InputError("Bilibili 功能需要 bvid，可选 lang/page。")
        bvid = params["bvid"].strip()
        if not re.fullmatch(r"BV[A-Za-z0-9]{10}", bvid):
            raise InputError("bvid 必须是裸 BV 视频 ID。")
        arguments: dict[str, Any] = {"bvid": bvid}
        if "lang" in params:
            if (
                command != "subtitle"
                or not isinstance(params["lang"], str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{2,20}", params["lang"])
            ):
                raise InputError("lang 无效。")
            arguments["lang"] = params["lang"]
        if "page" in params:
            try:
                page = int(params["page"])
            except (TypeError, ValueError) as exc:
                raise InputError("page 必须是正整数。") from exc
            if page < 1 or str(page) != str(params["page"]):
                raise InputError("page 必须是正整数。")
            arguments["page"] = page
        return arguments
    if params.keys() - {"username", "limit"} or not isinstance(params.get("username"), str):
        raise InputError("X 最近帖子需要 username，可选 limit。")
    username = params["username"].removeprefix("@")
    if not re.fullmatch(r"[A-Za-z0-9_]{1,15}", username):
        raise InputError("username 无效。")
    arguments = {"username": username}
    if "limit" in params:
        try:
            limit = int(params["limit"])
        except (TypeError, ValueError) as exc:
            raise InputError("limit 必须是 1–100。") from exc
        if not 1 <= limit <= 100 or str(limit) != str(params["limit"]):
            raise InputError("limit 必须是 1–100。")
        arguments["limit"] = limit
    return arguments


def _native_mcp(settings: SootaSettings, selector: str, params: dict[str, Any], *, dry_run: bool) -> CommandResult:
    require_enabled(settings.opencli_mcp)
    site_name, command_name = resolve_mcp(selector)
    arguments = mcp_arguments(site_name, command_name, params)
    operation = f"{site_name}/{command_name}"
    if dry_run:
        return success(
            "参数已校验；未执行业务命令。",
            {
                "plan": {
                    "provider": "opencli-mcp",
                    "operation": operation,
                    "access": "read",
                    "arguments": arguments,
                    "tikhub_cost_usd": 0,
                }
            },
        )
    response = OpenCLIMCPProvider(settings)._run(site_name, command_name, arguments)
    return success(
        "已收到 opencli-mcp 原生结果。",
        {
            "provider": "opencli-mcp",
            "operation": operation,
            "output_contract": "provider_native",
            "response": response,
            "tikhub_cost_usd": 0,
        },
    )


def call_feature(
    settings: SootaSettings,
    selector: str,
    *,
    params: list[str] | None = None,
    params_file: Path | None = None,
    body_file: Path | None = None,
    output: Path | None = None,
    provider: str | None = None,
    dry_run: bool = False,
    max_cost: float | None = None,
    allow_write: bool = False,
    requester: Callable[..., Any] | None = None,
) -> CommandResult:
    if provider == "soota":
        provider = "yt-dlp"
    chosen = selected_provider(settings, provider)
    if chosen not in {"auto", "opencli-mcp", "opencli", "yt-dlp", "tikhub", "monid"}:
        raise InputError("provider 支持 auto/opencli-mcp/opencli/yt-dlp/tikhub/monid。")
    if output is not None and output.expanduser().exists() and not dry_run:
        raise InputError("输出文件已存在。", hint="请使用新的 --output 路径，避免覆盖已有结果。")
    if selector.startswith(("tikhub.", "monid.")):
        native_provider = selector.split(".", 1)[0]
        if provider not in {None, "auto", native_provider}:
            raise InputError("原生功能标识与显式 Provider 不一致。")
        return call_endpoint(
            settings,
            selector.split(".", 1)[1],
            provider_name="monid" if selector.startswith("monid.") else "tikhub",
            params=params,
            params_file=params_file,
            body_file=body_file,
            output=output,
            dry_run=dry_run,
            max_cost=max_cost,
            requester=requester,
            allow_auxiliary=allow_write if selector.startswith("tikhub.") else False,
        )
    values = read_params(params, params_file)
    secrets = [str(value) for key, value in values.items() if key.lower() in CREDENTIAL_FIELDS]
    if body_file:
        raise InputError("--body 仅适用于 tikhub.* 或 monid.* 原生功能。")
    if selector.startswith("opencli."):
        raise InputError("OpenCLI 原生命令映射已移除。", hint="直接调用 OpenCLI，或选择 Soota 的统一业务入口。")
    elif selector.startswith("opencli-mcp."):
        if provider in {"opencli", "yt-dlp", "tikhub", "monid"}:
            raise InputError("原生 opencli-mcp 功能不能指定其他 Provider；请选择统一功能 id。")
        result = _native_mcp(settings, selector, values, dry_run=dry_run)
    else:
        alias = next((item for item in ALIASES if item.id == selector), None)
        if alias is None:
            raise InputError("未知功能标识。", hint="使用 features list 查询完整 id。")
        cheap, paid = alias_params(alias, values)
        secrets.extend(str(v) for k, v in paid.items() if k.lower() in CREDENTIAL_FIELDS)
        endpoint = load_registry().resolve(alias.tikhub)
        paid_pairs = [f"{key}={value}" for key, value in paid.items()]
        # Validate the mapped paid plan offline too, so a bad alias never silently charges.
        prepare_request(endpoint, paid_pairs, None, None)
        result = None
        unavailable: list[str] = []
        plans = alias_view(settings, alias, preferred=chosen)["plans"]
        selected_plans = [
            plan
            for plan in plans
            if (chosen == "auto" or plan["provider"] == chosen) and provider_enabled(settings, plan["provider"])
        ]
        for plan in selected_plans:
            name = plan["provider"]
            try:
                if name == "opencli-mcp":
                    assert alias.opencli_mcp is not None
                    result = _native_mcp(
                        settings,
                        "opencli-mcp." + alias.opencli_mcp.replace("/", "."),
                        {"bvid": cheap[alias.opencli_parameter]},
                        dry_run=dry_run,
                    )
                elif name == "opencli":
                    result = _native_opencli(
                        settings,
                        "opencli." + alias.opencli.replace("/", "."),
                        cheap,
                        dry_run=dry_run,
                        allow_write=False,
                    )
                elif name == "yt-dlp":
                    from soota.pipeline import inspect

                    result = inspect(settings, str(values["url"]), provider=name, fetch=not dry_run, max_cost=0)
                    result.data = {
                        "provider": name,
                        "operation": alias.opencli,
                        "output_contract": "soota_metadata",
                        "response": result.data,
                    }
                else:
                    result = call_endpoint(
                        settings,
                        alias.tikhub,
                        params=paid_pairs,
                        dry_run=dry_run,
                        max_cost=max_cost,
                        requester=requester,
                        output=output,
                        provider_name=name,
                    )
                    result.data.update(provider=name, operation=alias.tikhub, output_contract="provider_native")
                break
            except RuntimeUnavailable as exc:
                if chosen != "auto":
                    raise
                unavailable.append(exc.message)
        if result is None:
            raise SourceError("所选来源尚未接入此功能或没有可用方案。")
        plans = alias_view(settings, alias, preferred=chosen)["plans"]
        if chosen != "auto":
            plans.sort(key=lambda plan: plan["provider"] != chosen)
        result.data["feature"] = {"id": alias.id, "plans": plans}
        if unavailable:
            result.warnings.extend(unavailable)
    assert result is not None
    result.data = _protect(result.data, secrets)
    if output is not None and not dry_run and "response" in result.data:
        target = output.expanduser().resolve()
        atomic_write(target, json.dumps(result.data.pop("response"), ensure_ascii=False, indent=2) + "\n")
        result.data["output"] = str(target)
    return result
