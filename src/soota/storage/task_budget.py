"""Durable task-wide request reservations.

[INPUT]: RequestRecord prices, optional payment guard, task folder and total cap.
[OUTPUT]: TaskBudget and context-scoped reserve_active_task before billed sends.
[POS]: storage budget ledger; thread/file locks serialize reserve and persistence.
[PROTOCOL]: 变更时更新此头部，然后检查 AGENTS.md
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from decimal import Decimal
from pathlib import Path
from threading import RLock
from typing import Any

from soota.errors import AuthBudgetError
from soota.models import RequestRecord
from soota.storage.atomic import atomic_write
from soota.storage.locking import lock_file, unlock_file


class TaskBudget:
    """A task's durable reservations, shared by every provider and resume."""

    def __init__(self, folder: Path, cap: float | None) -> None:
        self.folder = folder
        self.folder.mkdir(parents=True, exist_ok=True)
        self.path = folder / "budget.json"
        self.cap = Decimal(str(cap)) if cap is not None else None
        self._lock = RLock()

    def snapshot(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {"reserved_cost_usd": "0", "requests": []}
        return dict(json.loads(self.path.read_text(encoding="utf-8")))

    def reserve(self, record: RequestRecord, before: Callable[[RequestRecord], None] | None = None) -> None:
        if not record.billed or record.cache_hit:
            if before is not None:
                before(record)
            return
        price = record.estimated_cost_usd
        if price is None or not math.isfinite(price) or price < 0 or self.cap == 0:
            raise AuthBudgetError("任务禁止该计费请求：预算为零或价格未知。")
        with self._lock, (self.folder / "budget.lock").open("a") as handle:
            lock_file(handle)
            try:
                saved = self.snapshot()
                total = Decimal(saved["reserved_cost_usd"]) + Decimal(str(price))
                if self.cap is not None and total > self.cap:
                    raise AuthBudgetError("下一次请求将超过任务累计预算，已在发送前停止。")
                if before is not None:
                    before(record)
                saved["reserved_cost_usd"] = str(total)
                saved["requests"].append(record.model_dump(mode="json"))
                atomic_write(self.path, json.dumps(saved, ensure_ascii=False, indent=2) + "\n")
            finally:
                unlock_file(handle)


_ACTIVE: ContextVar[TaskBudget | None] = ContextVar("soota_task_budget", default=None)


def reserve_active_task(record: RequestRecord, before: Callable[[RequestRecord], None] | None = None) -> None:
    budget = _ACTIVE.get()
    if budget is not None:
        budget.reserve(record, before)
    elif before is not None:
        before(record)


@contextmanager
def task_budget_scope(budget: TaskBudget) -> Iterator[None]:
    token = _ACTIVE.set(budget)
    try:
        yield
    finally:
        _ACTIVE.reset(token)


def active_task_identity() -> str | None:
    budget = _ACTIVE.get()
    return str(budget.folder.resolve()) if budget is not None else None
