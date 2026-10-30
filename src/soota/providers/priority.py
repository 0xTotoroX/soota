"""
[INPUT]: Enabled capabilities, shared TTL quotes, offline snapshots and saved rules
[OUTPUT]: quote_rows、rank_quotes 和 ordered_names，规划/执行共用排序
[POS]: Shared platform and capability routing
[PROTOCOL]: 变更时更新此头部，然后检查 AGENTS.md
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from soota.billing import PaymentGate, provider_enabled
from soota.config import PriorityRule, SootaSettings
from soota.errors import InputError, SootaError
from soota.models import SourceRef
from soota.providers.monid.client import APIFY_CAPTIONS, MonidClient, load_key, quote_cost
from soota.providers.quotes import is_online, namespace, peek_quote, tikhub_requester
from soota.providers.tikhub.catalog import catalog_endpoint
from soota.providers.tikhub.client import (
    BILI_ONE_V3,
    BILI_PLAYURL,
    BILI_SUBTITLE,
    YOUTUBE_CAPTIONS,
    YOUTUBE_INFO,
    YOUTUBE_STREAMS,
    load_price_snapshot,
)
from soota.providers.treg.catalog import ENDPOINTS as TREG_ENDPOINTS
from soota.providers.treg.catalog import caption_endpoint
from soota.providers.treg.catalog import resolve as treg_endpoint
from soota.providers.treg.client import TregClient
from soota.providers.treg.client import load_key as treg_key
from soota.secrets import load_tikhub_api_key

NAMES = ("opencli-mcp", "opencli", "yt-dlp", "tikhub", "monid", "treg")
CAPABILITIES = ("inspect", "captions", "catalog", "api", "audio")


def rule_for(settings: SootaSettings, platform: str, capability: str) -> PriorityRule:
    rules = settings.routing.priorities
    return rules.get(platform, {}).get(capability, rules.get("*", {}).get(capability, PriorityRule()))


def endpoint_chain(source: SourceRef, capability: str, name: str, endpoint: str | None = None) -> list[str]:
    if endpoint:
        return [endpoint]
    if capability == "catalog":
        return [catalog_endpoint(source)] if source.platform in {"youtube", "bilibili"} else []
    yt = source.platform == "youtube"
    if capability == "captions" and name == "monid" and yt:
        return [APIFY_CAPTIONS]
    if source.platform not in {"youtube", "bilibili"}:
        return []
    if capability == "inspect":
        return [YOUTUBE_INFO if yt else BILI_ONE_V3]
    if capability == "captions":
        return [YOUTUBE_INFO, YOUTUBE_CAPTIONS, YOUTUBE_CAPTIONS] if yt else [BILI_ONE_V3, BILI_SUBTITLE]
    if capability == "audio":
        return [YOUTUBE_STREAMS] if yt else [BILI_ONE_V3, BILI_PLAYURL]
    return []


def quote_rows(
    settings: SootaSettings,
    source: SourceRef,
    capability: str,
    names: list[str],
    *,
    endpoint: str | None = None,
    refresh: bool = False,
) -> list[dict[str, Any]]:
    if capability not in CAPABILITIES:
        raise InputError("未知路由能力。")
    bundled = json.loads(Path(__file__).with_name("monid").joinpath("prices.json").read_text(encoding="utf-8"))
    local_path = settings.config_dir / "provider-prices.json"
    try:
        local = json.loads(local_path.read_text(encoding="utf-8")) if local_path.is_file() else {}
        if not isinstance(local, dict) or any(not isinstance(v, dict) for k, v in local.items() if k != "as_of"):
            raise ValueError("invalid price cache")
    except ValueError as exc:
        raise InputError("provider-prices.json 无效；修复该报价缓存后重试。") from exc
    rows: list[dict[str, Any]] = []
    for name in names:
        enabled = provider_enabled(settings, name)
        paths = endpoint_chain(source, capability, name, endpoint) if name in {"tikhub", "monid", "treg"} else []
        if name == "treg":
            try:
                paths = (
                    [caption_endpoint(settings)]
                    if source.platform == "youtube" and capability == "captions" and endpoint is None
                    else [treg_endpoint(path) for path in paths]
                )
            except InputError:
                paths = []
        supported = bool(paths)
        if name not in {"tikhub", "monid", "treg"}:
            from soota.providers.opencli import capabilities_for as opencli_caps
            from soota.providers.opencli_mcp import capabilities_for as mcp_caps
            from soota.providers.ytdlp import capabilities_for as ytdlp_caps

            caps = {"opencli": opencli_caps, "opencli-mcp": mcp_caps, "yt-dlp": ytdlp_caps}[name](source)
            supported = (name == "yt-dlp") if capability == "audio" else bool(getattr(caps, capability, False))
        values: list[float] = []
        reason = None
        cached_prices = bool(paths)
        for path in paths:
            upstream = "apify" if path == APIFY_CAPTIONS else "tikhub"
            prices = (
                bundled[upstream] if name == "monid" else {**load_price_snapshot()["endpoints"], **bundled["tikhub"]}
            )
            # The public Monid catalog is also a corroborated base-price snapshot of these TikHub endpoints.
            value = prices.get(path, bundled["tikhub"].get(path) if name == "tikhub" else None)
            if name == "treg":
                value = TREG_ENDPOINTS[path][2]
            value = local.get(name, {}).get(path, value)
            if name == "treg":
                scope = namespace(settings.treg.base_url, treg_key(settings))
            elif name == "monid":
                scope = namespace(settings.monid.base_url, load_key(settings))
            else:
                scope = namespace(settings.tikhub.base_url, load_tikhub_api_key() or "")
            cached = peek_quote(settings, name, scope, path)
            if cached is not None:
                value = cached["estimated_cost_usd"]
            else:
                cached_prices = False
            if (refresh or is_online()) and enabled and (cached is None or refresh):
                try:
                    if name == "treg":
                        treg = TregClient(settings, 0)
                        try:
                            value = treg.quote(path, refresh=refresh)["estimated_cost_usd"]
                        finally:
                            treg.http.close()
                    elif name == "monid":
                        monid = MonidClient(settings, 0)
                        try:
                            value = quote_cost(monid.inspect(upstream, path, refresh=refresh))
                        finally:
                            monid.http.close()
                    else:
                        from soota.providers.tikhub.client import TikHubProvider
                        from soota.providers.tikhub.registry import load_registry
                        from soota.storage.cache import CacheStore

                        tikhub = TikHubProvider(
                            cache=CacheStore(settings.cache_dir),
                            runtime_python=settings.tikhub.runtime_python,
                            base_url=settings.tikhub.base_url,
                            payment_gate=PaymentGate(settings, 0),
                            requester=tikhub_requester(),
                        )
                        try:
                            value = tikhub.endpoint_price(load_registry().resolve(path), refresh=refresh)
                        finally:
                            close = getattr(tikhub._client, "close", None)
                            if close is not None:
                                close()
                except SootaError as exc:
                    value = None
                    reason = exc.message
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                supported = False
                reason = reason or "缺少已核实的接口覆盖或报价。"
                break
            values.append(float(value))
        rows.append(
            {
                "provider": name,
                "enabled": enabled,
                "supported": supported,
                "estimated_cost_usd": round(sum(values), 9) if supported else None,
                "endpoints": paths,
                "reason": reason,
                "price_basis": (
                    "free-local"
                    if not paths
                    else "ttl-quote"
                    if ((refresh or is_online()) and enabled) or cached_prices
                    else "saved-quote"
                    if any(path in local.get(name, {}) for path in paths)
                    else "advertised-snapshot:" + ("2026-10-28" if name == "treg" else bundled["as_of"])
                ),
            }
        )
    return rows


def ordered_names(
    settings: SootaSettings,
    source: SourceRef,
    capability: str,
    names: list[str],
    *,
    endpoint: str | None = None,
    max_cost: float | None = None,
) -> list[str]:
    if max_cost == 0:
        names = [name for name in names if name not in {"tikhub", "monid", "treg"}]
    rows = quote_rows(settings, source, capability, names, endpoint=endpoint)
    return rank_quotes(settings, source, capability, rows, max_cost=max_cost)


def rank_quotes(
    settings: SootaSettings,
    source: SourceRef,
    capability: str,
    rows: list[dict[str, Any]],
    *,
    max_cost: float | None = None,
) -> list[str]:
    """对已经取得的报价使用同一排序规则，供规划预览复用。"""
    rule = rule_for(settings, source.platform, capability)
    rows = [
        r
        for r in rows
        if r["enabled"]
        and r["supported"]
        and (max_cost is None or r["estimated_cost_usd"] <= max_cost)
        and not (max_cost == 0 and r["provider"] in {"tikhub", "monid", "treg"})
    ]
    order = list(rule.order) + [n for n in NAMES if n not in rule.order]
    rows.sort(key=lambda r: ((r["estimated_cost_usd"] if rule.mode == "cost" else 0), order.index(r["provider"])))
    return [r["provider"] for r in rows]
