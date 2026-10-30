from __future__ import annotations

import json
import math
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import quote, quote_plus

from jsonschema import Draft202012Validator

from soota.billing import PaymentGate
from soota.config import SootaSettings
from soota.errors import AuthBudgetError, InputError, SootaError, SourceError
from soota.models import CommandResult, RequestRecord
from soota.providers.quotes import online_prices
from soota.providers.tikhub.capability import PLATFORM_CAPABILITIES
from soota.providers.tikhub.client import TikHubProvider
from soota.providers.tikhub.registry import PLATFORM_NAMES, TASK_NAMES, ApiEndpoint, load_registry
from soota.providers.treg.provider import TregProvider
from soota.secrets import CREDENTIAL_FIELDS, REDACTED, redact_any
from soota.storage.atomic import atomic_write
from soota.storage.cache import CacheStore
from soota.storage.ledger import CostLedger


def _success(message: str, data: dict[str, Any], warnings: list[str] | None = None) -> CommandResult:
    return CommandResult(
        ok=True, status="success", code="ok", exit_code=0, message=message, data=data, warnings=warnings or []
    )


def platforms() -> CommandResult:
    registry = load_registry()
    preferred = registry.preferred()
    rows = []
    for platform, name in PLATFORM_NAMES.items():
        endpoints = [endpoint for endpoint in preferred if endpoint.platform == platform and endpoint.callable]
        normalized = PLATFORM_CAPABILITIES.get(platform, {})
        rows.append(
            {
                "platform": platform,
                "name": name,
                "api_operations": len(endpoints),
                "resources": sorted({endpoint.resource or "api" for endpoint in endpoints}),
                "api_tasks": sorted({task for endpoint in endpoints for task in endpoint.tasks}),
                "session_operations": sum(endpoint.session_required for endpoint in endpoints),
                "normalized": {kind: caps.model_dump() for kind, caps in normalized.items()},
                "api_integration": "generic_schema_validated_client",
                "live_verified": False,
            }
        )
    return _success(
        f"{len(rows)} 个平台/产品入口。",
        {
            "as_of": registry.as_of,
            "spec_version": registry.spec_version,
            "documented_endpoints": len(registry.endpoints),
            "platforms": rows,
            "task_names": TASK_NAMES,
        },
        ["API 查询返回平台 JSON；标准化字幕、ASR 与可续跑目录目前支持 YouTube、Bilibili。微信按三项产品列出。"],
    )


def list_endpoints(
    *,
    platform: str | None = None,
    task: str | None = None,
    search: str | None = None,
    all_versions: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> CommandResult:
    if limit < 1 or offset < 0:
        raise InputError("--limit 必须大于 0，--offset 不能小于 0。")
    if task is not None and task not in TASK_NAMES:
        raise InputError("未知任务类型。", hint="可选：" + ", ".join(TASK_NAMES))
    registry = load_registry()
    if platform is not None and platform not in {endpoint.platform for endpoint in registry.endpoints}:
        raise InputError("未知平台。", hint="用 soota platforms 查看平台标识。")
    preferred = {endpoint.path for endpoint in registry.preferred()}
    candidates = registry.endpoints if all_versions else registry.preferred()
    selected = [
        endpoint
        for endpoint in candidates
        if (platform is None or endpoint.platform == platform)
        and (task is None or task in endpoint.tasks)
        and (search is None or search.casefold() in f"{endpoint.key} {endpoint.summary}".casefold())
    ]
    rows = [
        {
            "key": endpoint.key,
            "method": endpoint.method,
            "path": endpoint.path,
            "summary": endpoint.summary,
            "tasks": endpoint.tasks,
            "version": endpoint.version,
            "preferred": endpoint.path in preferred,
            "callable": endpoint.callable,
            "session_required": endpoint.session_required,
        }
        for endpoint in selected[offset : offset + limit]
    ]
    return _success(
        f"找到 {len(selected)} 个接口，本页 {len(rows)} 个。",
        {
            "as_of": registry.as_of,
            "endpoints": rows,
            "total": len(selected),
            "offset": offset,
            "next_offset": offset + len(rows) if offset + len(rows) < len(selected) else None,
        },
    )


def show_endpoint(selector: str) -> CommandResult:
    registry = load_registry()
    endpoint = registry.resolve(selector)
    return _success(
        endpoint.summary,
        {
            "requested": selector,
            "as_of": registry.as_of,
            "endpoint": endpoint.model_dump(mode="json", by_alias=True),
            "query_schema": endpoint.query_schema,
        },
        ["按新版参数契约调用；历史接口的参数名不会被静默猜测或转换。"],
    )


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.expanduser().read_text(encoding="utf-8"))
    except ValueError as exc:
        raise InputError("参数文件不是有效 JSON。") from exc


def _credential_values(value: Any, sensitive: bool = False) -> list[str]:
    if isinstance(value, str):
        return [value] if sensitive and value else []
    if isinstance(value, dict):
        return [
            secret
            for key, item in value.items()
            for secret in _credential_values(item, sensitive or str(key).lower() in CREDENTIAL_FIELDS)
        ]
    if isinstance(value, list):
        return [secret for item in value for secret in _credential_values(item, sensitive)]
    return []


def _protect(value: Any, secrets: list[str]) -> Any:
    value = redact_any(value)
    if isinstance(value, str):
        for secret in secrets:
            for variant in {secret, quote(secret, safe=""), quote_plus(secret)}:
                value = value.replace(variant, REDACTED)
        return value
    if isinstance(value, dict):
        return {key: _protect(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [_protect(item, secrets) for item in value]
    return value


def _validate(value: Any, schema: dict[str, Any], label: str) -> None:
    try:
        json.dumps(value, allow_nan=False)
    except (ValueError, TypeError) as exc:
        raise InputError(f"{label} 必须是有效 JSON，数值须有限。") from exc
    error = next(Draft202012Validator(schema).iter_errors(value), None)
    if error is not None:
        # Validator messages can quote secrets. Report paths and constraints, not supplied values.
        location = ".".join(str(part) for part in error.absolute_path) or "根对象"
        raise InputError(
            f"{label} 的 {location} 不符合接口参数要求（{error.validator}）。",
            hint="用 soota api show 接口标识 --json 查看当前必填字段、类型和限制。",
        )


def prepare_request(
    endpoint: ApiEndpoint, params: list[str], params_file: Path | None, body_file: Path | None
) -> tuple[dict[str, Any], Any]:
    query = _read_json(params_file) if params_file is not None else {}
    if not isinstance(query, dict):
        raise InputError("--params-file 必须包含 JSON 对象。")
    fields = {parameter.name: parameter.schema_ for parameter in endpoint.parameters}
    for pair in params:
        if "=" not in pair:
            raise InputError("--param 格式应为 name=value。")
        name, value = pair.split("=", 1)
        if name in query:
            raise InputError(f"参数 {name} 被重复提供。")
        schema = fields.get(name, {})
        if Draft202012Validator(schema).is_valid(value):
            query[name] = value  # Keep string IDs, including 64-bit IDs and leading zeroes, unchanged.
        else:
            try:
                query[name] = json.loads(value)
            except ValueError as exc:
                raise InputError(f"参数 {name} 类型不正确。", hint="数组与复杂参数可通过 --params-file 提供。") from exc
    _validate(query, endpoint.query_schema, "查询参数")
    body = _read_json(body_file) if body_file is not None else None
    if body_file is not None and endpoint.body_schema is None:
        raise InputError("当前接口不接受 JSON 请求体；请使用 --param 或 --params-file。")
    if body_file is None and endpoint.body_required:
        raise InputError("当前接口需要 JSON 请求体。", hint="使用 --body 请求体.json；先 api show 查看格式。")
    if body_file is not None and endpoint.body_schema is not None:
        _validate(body, endpoint.body_schema, "请求体")
    return query, body


@online_prices
def call_endpoint(
    settings: SootaSettings,
    selector: str,
    *,
    params: list[str] | None = None,
    params_file: Path | None = None,
    body_file: Path | None = None,
    dry_run: bool = False,
    max_cost: float | None = None,
    output: Path | None = None,
    requester: Callable[..., Any] | None = None,
    allow_auxiliary: bool = False,
    provider_name: str = "tikhub",
) -> CommandResult:
    from soota.billing import actual_cost, provider_enabled, selected_provider
    from soota.models import SourceRef
    from soota.providers.monid.provider import MonidProvider
    from soota.providers.priority import ordered_names, quote_rows

    endpoint = load_registry().resolve(selector)
    if (not endpoint.callable and not allow_auxiliary) or endpoint.deprecated:
        raise SourceError("该接口不属于当前可用的内容查询接口。", hint="用 api list 查看 callable 字段。")
    query, body = prepare_request(endpoint, params or [], params_file, body_file)
    credentials = _credential_values(query) + _credential_values(body, endpoint.body_sensitive)
    if max_cost is not None and (not math.isfinite(max_cost) or max_cost < 0):
        raise InputError("--max-cost 必须是有限的非负数。")
    if output is not None:
        output = output.expanduser().resolve()
        if output.exists() and not dry_run:
            raise InputError("输出文件已存在。", hint="请使用新的 --output 路径，避免覆盖已有结果。")
    platform = endpoint.path.split("/")[3]
    source = SourceRef(platform=platform, kind="video", id="api", url="", canonical_url="")
    if provider_name == "auto":
        names = ordered_names(settings, source, "api", ["tikhub", "monid", "treg"], endpoint=endpoint.path)
        if not names and provider_enabled(settings, "tikhub"):
            names = ["tikhub"]  # Direct API obtains a live quote before dispatch, including uncached paths.
        if not names:
            raise SourceError("没有覆盖此接口的已启用来源。")
        provider_name = names[0]
    if provider_name not in {"tikhub", "monid", "treg"}:
        raise InputError("api call --provider 支持 auto/tikhub/monid/treg。")
    selected_provider(settings, provider_name)
    request = {
        "provider": provider_name,
        "key": endpoint.key,
        "method": endpoint.method,
        "path": endpoint.path,
        "params": query,
        "body": body,
    }
    if dry_run:
        price = quote_rows(settings, source, "api", [provider_name], endpoint=endpoint.path)[0]["estimated_cost_usd"]
        return _success(
            "请求已校验；未联网、未创建任务或文件。",
            {
                "request": _protect(request, credentials),
                "as_of": load_registry().as_of,
                "estimated_cost_usd": price,
                "max_cost": max_cost,
            },
            ["未缓存的接口单价显示为 null；执行前会查询免费的定价元数据。"],
        )
    reserved: list[RequestRecord] = []

    def reserve(record: RequestRecord) -> None:
        if (
            not record.billed
            or record.cache_hit
            or (
                not record.endpoint.startswith("treg:")
                and record.endpoint not in {endpoint.path, "monid:tikhub:" + endpoint.path}
            )
        ):
            return
        price = record.estimated_cost_usd
        if price is None or not math.isfinite(price) or price < 0:
            raise AuthBudgetError("该接口价格未知或无效，未发送查询请求。")
        if max_cost is not None and Decimal(str(price)) > Decimal(str(max_cost)):
            raise AuthBudgetError(f"接口费用 {price:g} USD 超过本次 --max-cost {max_cost}。")
        record.note = f"reserved-before-request;live-price:{datetime.now(UTC).isoformat()}"
        CostLedger(settings.ledger_path).append(endpoint.key, [record], {"kind": "api", "reservation": True})
        reserved.append(record)

    provider = (
        (TregProvider if provider_name == "treg" else MonidProvider)(
            settings, max_cost, purpose="api", before_request=reserve
        )
        if provider_name in {"monid", "treg"}
        else TikHubProvider(
            runtime_python=settings.tikhub.runtime_python,
            cache=CacheStore(settings.cache_dir),
            base_url=settings.tikhub.base_url,
            timeout=settings.tikhub.timeout_seconds,
            requester=requester,
            before_request=reserve,
            max_retries=0,
            payment_gate=PaymentGate(settings, max_cost),
        )
    )
    if provider.api_key:
        credentials.append(provider.api_key)
    if isinstance(provider, MonidProvider):
        credentials.append(provider.monid.key)
    if isinstance(provider, TregProvider):
        credentials.append(provider.treg.key)
    try:
        price = provider.endpoint_price(endpoint)
        response = provider.query(endpoint, query, body, allow_auxiliary=allow_auxiliary)
    except SootaError as exc:
        return CommandResult(
            ok=False,
            status="error",
            code=exc.code,
            exit_code=exc.exit_code,
            message=_protect(exc.message, credentials),
            hint=_protect(exc.hint, credentials),
            data={
                "endpoint": endpoint.path,
                "details": _protect(exc.details, credentials),
                "reserved_cost_usd": sum(record.estimated_cost_usd or 0 for record in reserved),
                "requests": [record.model_dump() for record in provider.records],
            },
        )
    data: dict[str, Any] = {
        "request": _protect(request, credentials),
        "response": _protect(response, credentials),
        "reserved_cost_usd": price,
        "actual_cost_usd": actual_cost(provider.records),
        "requests": [record.model_dump() for record in provider.records],
    }
    if output is not None:
        atomic_write(
            output.expanduser(), json.dumps(_protect(response, credentials), ensure_ascii=False, indent=2) + "\n"
        )
        data["output"] = str(output.expanduser().resolve())
        del data["response"]
    return _success("已收到接口响应。", data)
