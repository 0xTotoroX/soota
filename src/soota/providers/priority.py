"""
[INPUT]: Enabled capabilities, price snapshots and saved rules
[OUTPUT]: quote_rows and ordered_names
[POS]: Shared platform and capability routing
[PROTOCOL]: 变更时更新此头部，然后检查 AGENTS.md
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from soota.billing import PaymentGate, provider_enabled
from soota.config import PriorityRule, SootaSettings
from soota.errors import InputError, SootaError
from soota.models import SourceRef
from soota.providers.monid.client import APIFY_CAPTIONS, MonidClient, quote_cost
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

NAMES = ("opencli-mcp", "opencli", "yt-dlp", "tikhub", "monid")
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
        paths = endpoint_chain(source, capability, name, endpoint) if name in {"tikhub", "monid"} else []
        supported = bool(paths)
        if name not in {"tikhub", "monid"}:
            from soota.providers.opencli import capabilities_for as opencli_caps
            from soota.providers.opencli_mcp import capabilities_for as mcp_caps
            from soota.providers.ytdlp import capabilities_for as ytdlp_caps

            caps = {"opencli": opencli_caps, "opencli-mcp": mcp_caps, "yt-dlp": ytdlp_caps}[name](source)
            supported = (name == "yt-dlp") if capability == "audio" else bool(getattr(caps, capability, False))
        values: list[float] = []
        reason = None
        for path in paths:
            upstream = "apify" if path == APIFY_CAPTIONS else "tikhub"
            prices = (
                bundled[upstream] if name == "monid" else {**load_price_snapshot()["endpoints"], **bundled["tikhub"]}
            )
            # The public Monid catalog is also a corroborated base-price snapshot of these TikHub endpoints.
            value = prices.get(path, bundled["tikhub"].get(path) if name == "tikhub" else None)
            value = local.get(name, {}).get(path, value)
            if refresh and enabled:
                try:
                    if name == "monid":
                        value = quote_cost(MonidClient(settings, 0).inspect(upstream, path))
                    else:
                        from soota.providers.tikhub.client import TikHubProvider
                        from soota.providers.tikhub.registry import load_registry
                        from soota.storage.cache import CacheStore

                        value = TikHubProvider(
                            cache=CacheStore(settings.cache_dir),
                            runtime_python=settings.tikhub.runtime_python,
                            base_url=settings.tikhub.base_url,
                            payment_gate=PaymentGate(settings, 0),
                        ).endpoint_price(load_registry().resolve(path))
                    local.setdefault(name, {})[path] = value
                except SootaError as exc:
                    value = None
                    reason = exc.message
                    local.setdefault(name, {})[path] = None
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
                    else "live"
                    if refresh and enabled
                    else "saved-quote"
                    if any(path in local.get(name, {}) for path in paths)
                    else "advertised-snapshot:" + bundled["as_of"]
                ),
            }
        )
    if refresh:
        from soota.storage.atomic import atomic_write

        local["as_of"] = datetime.now(UTC).isoformat()
        atomic_write(local_path, json.dumps(local, indent=2))
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
    rule = rule_for(settings, source.platform, capability)
    rows = quote_rows(settings, source, capability, names, endpoint=endpoint)
    rows = [
        r for r in rows if r["enabled"] and r["supported"] and (max_cost is None or r["estimated_cost_usd"] <= max_cost)
    ]
    order = list(rule.order) + [n for n in NAMES if n not in rule.order]
    rows.sort(key=lambda r: ((r["estimated_cost_usd"] if rule.mode == "cost" else 0), order.index(r["provider"])))
    return [r["provider"] for r in rows]
