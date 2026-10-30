"""
[INPUT]: Provider policies, request estimates and settings.
[OUTPUT]: Provider enablement gates and USD payment authorization.
[POS]: Shared authorization; Weir credits use their own provider guard.
[PROTOCOL]: 变更时更新此头部，然后检查 AGENTS.md
"""

from __future__ import annotations

import math
import sys
import time
from collections.abc import Callable
from decimal import Decimal
from typing import Any

from soota.config import PaymentConfig, SootaSettings
from soota.errors import AuthBudgetError, InputError, SootaError
from soota.models import RequestRecord
from soota.storage.locking import lock_file


class PaymentRequired(AuthBudgetError):
    code = "payment_required"


class BalancePaused(AuthBudgetError):
    code = "balance_paused"


def confirm_payment(details: dict[str, Any]) -> bool:
    if not sys.stdin.isatty():
        raise PaymentRequired(
            "本次请求需要付费确认，尚未发送。",
            hint="请在交互终端重跑并确认，或明确配置 soota policy set --mode auto。",
            details=details,
        )
    print(
        f"{details.get('provider', 'tikhub')} {details['endpoint']}：预计 "
        f"{details['estimated_cost_usd']:g} USD。允许本次请求？[y/N] ",
        file=sys.stderr,
        end="",
        flush=True,
    )
    return sys.stdin.readline().strip().lower() in {"y", "yes"}


class PaymentGate:
    """One authorization per dispatched billed request; cache reads never enter this gate."""

    def __init__(self, settings: SootaSettings, max_cost: float | None = None, *, provider: str = "tikhub") -> None:
        self.settings = settings
        self.provider = provider
        self.policy: PaymentConfig = settings.payment
        self.root = settings.data_dir / "billing"
        self.cap = Decimal(str(max_cost)) if max_cost is not None else None
        self.reserved = Decimal(0)

    def authorize(self, record: RequestRecord, wallet: Callable[[], dict[str, float] | None]) -> None:
        if not record.billed or record.cache_hit:
            return
        price = record.estimated_cost_usd
        if self.cap == 0:
            raise AuthBudgetError("--max-cost 0 禁止发送计费接口请求。")
        if price is None or not math.isfinite(price) or price < 0:
            raise AuthBudgetError("接口价格未知或无效，未发送请求。")
        cost = Decimal(str(price))
        if self.cap is not None and self.reserved + cost > self.cap:
            raise AuthBudgetError("下一次请求将超过本次费用上限，未发送请求。")
        if cost > 0:
            details: dict[str, Any] = {
                "provider": self.provider,
                "endpoint": record.endpoint,
                "estimated_cost_usd": price,
                "mode": self.policy.mode,
            }
            floor = self.policy.min_balance_usd
            if floor is not None:
                balance = self._balance(wallet)
                details.update(balance_usd=balance, min_balance_usd=floor)
                if Decimal(str(balance)) - cost <= Decimal(str(floor)):
                    raise BalancePaused("付费余额已达到或本次预估扣费将触及暂停阈值。", details=details)
            if self.policy.mode == "ask" and not confirm_payment(details):
                raise PaymentRequired("已拒绝本次付费请求，未发送。", details=details)
        self.reserved += cost

    def _balance(self, wallet: Callable[[], dict[str, float] | None]) -> float:
        try:
            value = wallet()
        except SootaError as exc:
            raise BalancePaused("无法读取当前付费余额，已在请求前暂停。", details={"cause": exc.code}) from exc
        balance = value.get("balance") if isinstance(value, dict) else None
        if isinstance(balance, bool) or not isinstance(balance, (float, int)) or not math.isfinite(balance):
            raise BalancePaused("无法确认当前付费余额，已暂停；不会使用未知余额继续扣费。")
        return float(balance)

    def pace_metadata(self, path: str, request: Callable[[], Any]) -> Any:
        # Free user-info and endpoint-price APIs each allow one request per second.
        # Pace every app entry point, including estimate, across local processes.
        self.root.mkdir(parents=True, exist_ok=True)
        name = path.rsplit("/", 1)[-1]
        with (self.root / f"{name}-rate.lock").open("a+") as handle:
            lock_file(handle)
            handle.seek(0)
            try:
                previous = float(handle.read() or "0")
            except ValueError:
                previous = 0
            delay = min(1.05, max(0, 1.05 - (time.time() - previous)))
            if delay:
                time.sleep(delay)
            try:
                return request()
            finally:
                handle.seek(0)
                handle.truncate()
                handle.write(str(time.time()))
                handle.flush()


def selected_provider(settings: SootaSettings, explicit: str | None) -> str:
    chosen = explicit if explicit is not None else settings.routing.default_provider
    if chosen == "soota":
        chosen = "yt-dlp"
    if chosen in settings.routing.disabled_providers:
        raise InputError(f"{chosen} 已在来源策略中停用。")
    if chosen == "opencli-mcp" and not settings.opencli_mcp.enabled:
        raise InputError("MCP 接入已停用；请使用 auto、opencli、yt-dlp 或 tikhub。")
    if chosen == "monid" and not settings.monid.enabled:
        raise InputError("Monid 尚未启用；使用 soota providers enable monid。")
    if chosen == "treg" and not settings.treg.enabled:
        raise InputError("treg 尚未启用；使用 soota providers enable treg。")
    if chosen == "weir" and not settings.weir.enabled:
        raise InputError("Weir 尚未启用；使用 soota providers enable weir。")
    return chosen


def provider_enabled(settings: SootaSettings, name: str) -> bool:
    return (
        name not in settings.routing.disabled_providers
        and (name != "opencli-mcp" or settings.opencli_mcp.enabled)
        and (name != "monid" or settings.monid.enabled)
        and (name != "treg" or settings.treg.enabled)
        and (name != "weir" or settings.weir.enabled)
    )


def actual_cost(records: list[RequestRecord]) -> float | None:
    """Current dispatch costs only; an old receipt's cost is not charged again."""
    billed = [r for r in records if r.billed and not r.cache_hit]
    if any(r.actual_cost_usd is None for r in billed):
        return None
    return round(sum(r.actual_cost_usd or 0 for r in billed), 9)
