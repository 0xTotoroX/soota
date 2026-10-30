"""报价时效及任务内共享。

[INPUT]: Provider 的只读报价函数、配置与凭据命名空间。
[OUTPUT]: quote_scope、online_prices、cached_quote、peek_quote。
[POS]: 获取前报价缓存；不承担费用授权、预算预留或采集结果缓存。
[PROTOCOL]: 变更时更新此头部，然后检查 AGENTS.md
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass, field
from functools import wraps
from threading import RLock
from typing import Any, TypeGuard

from soota.config import SootaSettings
from soota.errors import ContractError, InputError, SootaError
from soota.storage.atomic import atomic_write
from soota.storage.locking import lock_file


@dataclass
class QuoteSession:
    online: bool
    tikhub_requester: Callable[..., Any] | None = None
    lock: RLock = field(default_factory=RLock)
    values: dict[str, dict[str, Any]] = field(default_factory=dict)
    failures: dict[str, SootaError] = field(default_factory=dict)
    refreshed: set[str] = field(default_factory=set)


_ACTIVE: ContextVar[QuoteSession | None] = ContextVar("soota_quotes", default=None)


@contextmanager
def quote_scope(*, online: bool = True, tikhub_requester: Callable[..., Any] | None = None) -> Iterator[None]:
    current = _ACTIVE.get()
    if current is not None and current.online == online:
        if tikhub_requester is not None and current.tikhub_requester is None:
            current.tikhub_requester = tikhub_requester
        yield
        return
    token = _ACTIVE.set(QuoteSession(online, tikhub_requester))
    try:
        yield
    finally:
        _ACTIVE.reset(token)


def online_prices[**P, R](fn: Callable[P, R]) -> Callable[P, R]:
    @wraps(fn)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        requester = kwargs.get("requester")
        with quote_scope(
            online=not kwargs.get("dry_run", False)
            and kwargs.get("fetch") is not False
            and kwargs.get("max_cost") != 0,
            tikhub_requester=requester if callable(requester) else None,
        ):
            return fn(*args, **kwargs)

    return wrapped


def is_online() -> bool:
    current = _ACTIVE.get()
    return current is not None and current.online


def tikhub_requester() -> Callable[..., Any] | None:
    current = _ACTIVE.get()
    return current.tikhub_requester if current else None


def namespace(base_url: str, credential: str) -> str:
    return hashlib.sha256(json.dumps([base_url.rstrip("/"), credential]).encode()).hexdigest()


def _key(provider: str, scope: str, endpoint: str) -> str:
    return hashlib.sha256(json.dumps([provider, scope, endpoint]).encode()).hexdigest()


def _read(settings: SootaSettings) -> dict[str, Any]:
    path = settings.config_dir / "provider-quotes.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("entries"), dict):
            raise ValueError("invalid quote cache")
        return dict(data["entries"])
    except ValueError as exc:
        raise InputError("provider-quotes.json 无效，未使用该报价缓存。") from exc


def _fresh(row: Any, ttl: int) -> TypeGuard[dict[str, Any]]:
    if not isinstance(row, dict) or not isinstance(row.get("quote"), dict):
        return False
    stamp = row.get("fetched_at")
    price = row["quote"].get("estimated_cost_usd")
    return (
        isinstance(stamp, (float, int))
        and not isinstance(stamp, bool)
        and math.isfinite(stamp)
        and 0 <= time.time() - stamp < ttl
        and type(price) in {float, int}
        and math.isfinite(price)
        and price >= 0
    )


def peek_quote(settings: SootaSettings, provider: str, scope: str, endpoint: str) -> dict[str, Any] | None:
    key = _key(provider, scope, endpoint)
    session = _ACTIVE.get()
    memory_key = str(settings.config_dir.resolve()) + ":" + key
    if session is not None:
        with session.lock:
            if memory_key in session.failures:
                return None
            row = session.values.get(memory_key)
            if _fresh(row, settings.routing.price_ttl_seconds):
                return dict(deepcopy(row["quote"]))
    row = _read(settings).get(key)
    return deepcopy(row["quote"]) if _fresh(row, settings.routing.price_ttl_seconds) else None


def cached_quote(
    settings: SootaSettings,
    provider: str,
    scope: str,
    endpoint: str,
    fetch: Callable[[], dict[str, Any]],
    *,
    refresh: bool = False,
) -> dict[str, Any]:
    session = _ACTIVE.get() or QuoteSession(True)
    key = _key(provider, scope, endpoint)
    memory_key = str(settings.config_dir.resolve()) + ":" + key
    ttl = settings.routing.price_ttl_seconds
    with session.lock:
        forced = refresh and memory_key not in session.refreshed
        if not forced:
            if memory_key in session.failures:
                raise session.failures[memory_key]
            row = session.values.get(memory_key)
            if _fresh(row, ttl):
                return deepcopy(row["quote"])
        settings.config_dir.mkdir(parents=True, exist_ok=True)
        # 跨进程读改写也互斥；一个过期接口仅由持锁者刷新。
        with (settings.config_dir / "provider-quotes.lock").open("a") as handle:
            lock_file(handle)
            entries = _read(settings)
            row = entries.get(key)
            if forced or not _fresh(row, ttl):
                try:
                    value = fetch()
                    price = value.get("estimated_cost_usd")
                    if (
                        isinstance(price, bool)
                        or not isinstance(price, (int, float))
                        or not math.isfinite(price)
                        or price < 0
                    ):
                        raise ContractError("报价必须是有限、非负的美元金额。")
                except SootaError as exc:
                    session.failures[memory_key] = exc
                    session.values.pop(memory_key, None)
                    # 刷新失败使旧报价失效，不能在下一次排序中偷偷恢复。
                    entries.pop(key, None)
                    atomic_write(
                        settings.config_dir / "provider-quotes.json", json.dumps({"version": 1, "entries": entries})
                    )
                    raise
                row = {"fetched_at": time.time(), "quote": value}
                entries[key] = row
                atomic_write(
                    settings.config_dir / "provider-quotes.json", json.dumps({"version": 1, "entries": entries})
                )
                session.refreshed.add(memory_key)
                session.failures.pop(memory_key, None)
            assert row is not None
            session.values[memory_key] = row
            return deepcopy(row["quote"])
