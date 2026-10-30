"""Persistent credit reservations, independent of dollar estimates.

[INPUT]: Provider usage period, active task identity and integer credit limits.
[OUTPUT]: Shared UTC calendar-month/task caps, key attribution and durable settlements.
[POS]: Cross-task/provider credit ledger; uncertain sends retain reservations.
[PROTOCOL]: 变更时更新此头部，然后检查 AGENTS.md
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from soota.errors import AuthBudgetError, StorageError
from soota.storage.atomic import atomic_write
from soota.storage.locking import lock_file, unlock_file


class CreditEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    period: str
    task: str
    reserved: int = Field(ge=0, strict=True)
    charged: int | None = Field(default=None, ge=0, strict=True)
    month: str | None = Field(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    key_name: str | None = None

    @property
    def counted(self) -> int:
        return self.reserved if self.charged is None else self.charged


class CreditState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = 1
    blocked: bool = False
    requests: list[CreditEntry] = Field(default_factory=list)


class CreditBudget:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.path = root / "credits.json"
        self.state = CreditState()

    @contextmanager
    def locked(self) -> Iterator[CreditBudget]:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (self.root / "credits.lock").open("a") as handle:
            lock_file(handle)
            try:
                try:
                    self.state = (
                        CreditState.model_validate_json(self.path.read_text(encoding="utf-8"))
                        if self.path.exists()
                        else CreditState()
                    )
                    if self.state.version != 1:
                        raise ValueError("unknown ledger version")
                except (ValueError, ValidationError) as exc:
                    raise StorageError("credits 账本无效；未重置额度或发送请求。") from exc
                yield self
            finally:
                unlock_file(handle)

    def save(self) -> None:
        atomic_write(self.path, self.state.model_dump_json(indent=2) + "\n")

    @staticmethod
    def _in_month(entry: CreditEntry, month: str) -> bool:
        if entry.month is not None:
            return entry.month == month
        # Legacy entries have no dispatch month. Count unexpired reset periods
        # conservatively; switching credentials must not release old spending.
        try:
            reset = datetime.fromisoformat(entry.period.replace("Z", "+00:00"))
            if reset.tzinfo is None:
                raise ValueError("missing timezone")
            return reset > datetime.fromisoformat(month + "-01T00:00:00+00:00")
        except ValueError as exc:
            raise StorageError("旧 Weir 账本周期无效；未清零或发送请求。") from exc

    def totals(self, month: str, task: str, key_name: str) -> tuple[int, int]:
        return (
            sum(
                r.counted
                for r in self.state.requests
                if self._in_month(r, month) and (r.key_name or "legacy") == key_name
            ),
            sum(r.counted for r in self.state.requests if r.task == task),
        )

    def reserve(
        self,
        *,
        period: str,
        task: str,
        credits: int,
        monthly_cap: int | None,
        task_cap: int | None,
        key_name: str | None = None,
    ) -> str:
        if self.state.blocked:
            raise AuthBudgetError("上次 credits 扣费超过已知上限；需核对账单与接口契约后才能继续。")
        month = datetime.now(UTC).strftime("%Y-%m")
        monthly, total = self.totals(month, task, key_name or "legacy")
        if monthly_cap is not None and monthly + credits > monthly_cap:
            raise AuthBudgetError("下一次 Weir 请求将超过每月 credits 上限，未发送。")
        if task_cap is not None and total + credits > task_cap:
            raise AuthBudgetError("下一次 Weir 请求将超过任务累计 credits 上限，未发送。")
        entry = CreditEntry(id=uuid4().hex, period=period, task=task, reserved=credits, month=month, key_name=key_name)
        self.state.requests.append(entry)
        self.save()
        return entry.id

    def settle(self, identity: str, charged: int) -> None:
        entry = next(r for r in self.state.requests if r.id == identity)
        entry.charged = charged
        self.state.blocked = self.state.blocked or charged > entry.reserved
        self.save()
        if self.state.blocked:
            raise AuthBudgetError("Weir 实际扣费超过已知上限，已记录并停止后续调用。")

    def summary(self, task: str | None = None) -> dict[str, object]:
        entries = [r for r in self.state.requests if task is None or r.task == task]
        periods = sorted({r.period for r in entries})
        month = datetime.now(UTC).strftime("%Y-%m")
        return {
            "blocked": self.state.blocked,
            "charged_credits": sum(r.charged or 0 for r in entries),
            "pending_credits": sum(r.reserved for r in entries if r.charged is None),
            "counted_credits": sum(r.counted for r in entries),
            "current_month": month,
            "monthly_counted_credits": sum(r.counted for r in entries if self._in_month(r, month)),
            "monthly_key_counted_credits": {
                name: sum(
                    r.counted
                    for r in entries
                    if self._in_month(r, month) and (r.key_name or "legacy") == name
                )
                for name in sorted({r.key_name or "legacy" for r in entries})
            },
            "keys": [
                {
                    "name": name,
                    "charged_credits": sum(r.charged or 0 for r in entries if (r.key_name or "legacy") == name),
                    "pending_credits": sum(
                        r.reserved for r in entries if (r.key_name or "legacy") == name and r.charged is None
                    ),
                }
                for name in sorted({r.key_name or "legacy" for r in entries})
            ],
            "periods": [
                {"resets_at": p, "counted_credits": sum(r.counted for r in entries if r.period == p)} for p in periods
            ],
        }
