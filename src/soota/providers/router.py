"""来源路由。

[INPUT]: 能力、显式来源、排序规则与剩余预算。
[OUTPUT]: catalog_provider、inspect_item 与单次请求预算检查。
[POS]: 复用共享报价/排序，实际收费仍须经 PaymentGate 和持久账本。
[PROTOCOL]: 变更时更新此头部，然后检查 AGENTS.md
"""

from __future__ import annotations

import math
from collections.abc import Callable
from decimal import Decimal
from typing import Any, Literal

from soota.billing import PaymentGate, provider_enabled, selected_provider
from soota.config import SootaSettings
from soota.errors import AuthBudgetError, InputError, SourceError
from soota.models import MediaItem, RequestRecord, SourceRef
from soota.providers.monid.provider import MonidProvider
from soota.providers.opencli import OpenCLIProvider
from soota.providers.opencli import capabilities_for as opencli_capabilities
from soota.providers.opencli_mcp import OpenCLIMCPProvider
from soota.providers.opencli_mcp import capabilities_for as mcp_capabilities
from soota.providers.opencli_runtime import RuntimeUnavailable
from soota.providers.priority import ordered_names
from soota.providers.tikhub.capability import capabilities_for as tikhub_capabilities
from soota.providers.tikhub.client import TikHubProvider
from soota.providers.treg.provider import TregProvider
from soota.providers.ytdlp import YtDlpProvider
from soota.providers.ytdlp import capabilities_for as ytdlp_capabilities
from soota.storage.cache import CacheStore

ProviderName = Literal["auto", "opencli-mcp", "opencli", "yt-dlp", "tikhub", "monid", "treg"]


def validate_policy(provider: str, fallback: str | None, max_cost: float | None) -> None:
    if provider not in {"auto", "opencli-mcp", "opencli", "yt-dlp", "tikhub", "monid", "treg"} or fallback not in {
        None,
        "tikhub",
    }:
        raise InputError(
            "--provider 支持 auto/opencli-mcp/opencli/yt-dlp/tikhub/monid/treg；--fallback 只支持 tikhub。"
        )
    if fallback and provider == "tikhub":
        raise InputError("TikHub 已是指定来源，无需再指定同名 fallback。")
    if max_cost is not None and (not math.isfinite(max_cost) or max_cost < 0):
        raise InputError("--max-cost 必须是有限的非负数。")
    if fallback and max_cost == 0:
        raise InputError("--max-cost 0 禁止 TikHub 付费兜底。")


def catalog_provider(
    source: SourceRef,
    provider: str,
    *,
    mcp_available: bool = False,
    settings: SootaSettings | None = None,
    max_cost: float | None = None,
) -> Literal["opencli-mcp", "opencli", "tikhub", "monid", "treg"]:
    if settings is not None and provider == "auto":
        names = [
            n
            for n, cap in (
                ("opencli-mcp", mcp_available and mcp_capabilities(source).catalog),
                ("opencli", opencli_capabilities(source).catalog),
                ("tikhub", tikhub_capabilities(source).catalog),
                ("monid", tikhub_capabilities(source).catalog),
                ("treg", tikhub_capabilities(source).catalog),
            )
            if cap
        ]
        from soota.storage.task_budget import remaining_active_budget

        ranked = ordered_names(settings, source, "catalog", names, max_cost=remaining_active_budget(max_cost))
        if not ranked:
            if names and remaining_active_budget(max_cost) is not None:
                raise AuthBudgetError("当前预算内没有报价已知的可用目录来源，未发送请求。")
            raise SourceError("没有可用目录来源。")
        provider = ranked[0]
    if provider in {"monid", "treg"}:
        if not tikhub_capabilities(source).catalog or provider == "treg" and source.platform != "youtube":
            raise SourceError(f"{provider} 尚未接入此目录契约。")
        return "treg" if provider == "treg" else "monid"
    if provider in {"yt-dlp", "soota"}:
        raise SourceError("yt-dlp Provider 尚未接入目录能力。")
    if (provider == "opencli-mcp" or provider == "auto" and mcp_available) and mcp_capabilities(source).catalog:
        return "opencli-mcp"
    if provider == "opencli-mcp":
        raise SourceError("opencli-mcp 尚未接入该目录能力。")
    if provider != "tikhub" and opencli_capabilities(source).catalog:
        return "opencli"
    if provider != "opencli" and tikhub_capabilities(source).catalog:
        return "tikhub"
    raise SourceError("指定来源不具备该目录能力。", hint="X 使用 OpenCLI 最近快照；YouTube/Bilibili 目录使用 TikHub。")


class RequestBudget:
    def __init__(self, cap: float) -> None:
        self.cap = Decimal(str(cap))
        self.spent = Decimal(0)

    def reserve(self, record: RequestRecord) -> None:
        if not record.billed or record.cache_hit:
            return
        price = record.estimated_cost_usd
        if self.cap == 0 or price is None or not math.isfinite(price) or price < 0:
            raise AuthBudgetError("零预算禁止付费请求；未知价格也不会执行。")
        cost = Decimal(str(price))
        if self.spent + cost > self.cap:
            raise AuthBudgetError("下一次请求将超过费用上限，已在请求前停止。")
        self.spent += cost


def remaining_budget(cap: float | None, records: list[RequestRecord]) -> float | None:
    if cap is None:
        return None
    remaining = Decimal(str(cap))
    for record in records:
        if record.billed and not record.cache_hit:
            if record.estimated_cost_usd is None:
                raise AuthBudgetError("前序请求费用未知，停止后续付费请求。")
            remaining -= Decimal(str(record.estimated_cost_usd))
    if remaining < 0:
        raise AuthBudgetError("前序请求已超过本次费用上限，停止后续付费请求。")
    return float(remaining)


def inspect_item(
    settings: SootaSettings,
    source: SourceRef,
    *,
    provider: str = "auto",
    fallback: str | None = None,
    max_cost: float | None = None,
    requester: Callable[..., Any] | None = None,
    caption_metadata: bool = True,
) -> tuple[MediaItem, list[RequestRecord], list[str]]:
    provider = selected_provider(settings, provider)
    validate_policy(provider, fallback, max_cost)
    if provider == "auto" and fallback is None and max_cost != 0 and provider_enabled(settings, "tikhub"):
        fallback = "tikhub"
    warnings: list[str] = []
    records: list[RequestRecord] = []
    candidates: dict[str, Any] = {
        "opencli-mcp": (mcp_capabilities(source).inspect, OpenCLIMCPProvider),
        "opencli": (opencli_capabilities(source).inspect, OpenCLIProvider),
        "yt-dlp": (ytdlp_capabilities(source).inspect, YtDlpProvider),
    }
    names = [n for n, (cap, _) in candidates.items() if cap]
    if tikhub_capabilities(source).inspect:
        names += ["tikhub", "monid", "treg"]
    ranking = ordered_names(settings, source, "inspect", names, max_cost=max_cost) if provider == "auto" else [provider]
    for name in ranking:
        if name in {"monid", "treg"}:
            monid = (TregProvider if name == "treg" else MonidProvider)(
                settings, remaining_budget(max_cost, records), purpose="inspect"
            )
            item = monid.inspect(source)
            return item, [*records, *monid.records], warnings
        if name == "tikhub":
            provider = "tikhub"
            break
        capability, factory = candidates.get(name, (False, None))
        if not capability:
            raise SourceError("所选来源不支持该元数据能力。")

        if not provider_enabled(settings, name):
            if provider == name:
                raise InputError(f"{name} 接入已停用。")
            continue
        if provider not in {"auto", name}:
            continue
        if not capability:
            if provider == name:
                raise SourceError(f"{name} 尚不具备该元数据能力。")
            continue
        cheap: Any = None
        try:
            cheap = factory(settings)
            item = (
                cheap.inspect(source, include_captions=False)
                if isinstance(cheap, YtDlpProvider) and not caption_metadata
                else cheap.inspect(source)
            )
            return item, [*records, *cheap.records], warnings
        except RuntimeUnavailable as exc:
            if provider != "auto" and fallback != "tikhub":
                raise
            warnings.append(f"{name} 不可用：{exc.message}")
            if cheap is not None:
                records.extend(cheap.records)
    if provider != "tikhub" and fallback != "tikhub":
        raise SourceError("没有匹配的免费元数据来源。", hint="可显式选择 --provider tikhub 并提供 --max-cost。")
    if not tikhub_capabilities(source).inspect:
        raise SourceError("TikHub 尚未接入该来源的统一元数据解析。")
    selected_provider(settings, "tikhub")
    paid = TikHubProvider(
        runtime_python=settings.tikhub.runtime_python,
        cache=CacheStore(settings.cache_dir),
        base_url=settings.tikhub.base_url,
        timeout=settings.tikhub.timeout_seconds,
        preferred_language=settings.default_language,
        requester=requester,
        before_request=RequestBudget(max_cost).reserve if max_cost is not None else None,
        max_retries=0,
        payment_gate=PaymentGate(settings, max_cost),
    )
    item = paid.inspect(source)
    item.provenance = {"provider": "tikhub", "cache_hit": any(r.cache_hit for r in paid.records)}
    records.extend(paid.records)
    return item, records, warnings
