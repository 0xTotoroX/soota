"""目录采集与恢复。

[INPUT]: 来源、排序/预算、分页限制与可选的创建回调。
[OUTPUT]: 目录结果、游标和原请求预留；回调在首个请求前提供子任务身份。
[POS]: 独立目录入口，供统一任务复用；不清零恢复费用。
[PROTOCOL]: 变更时更新此头部，然后检查 AGENTS.md
"""

from __future__ import annotations

import math
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from pydantic import ValidationError

from soota.billing import PaymentGate, selected_provider
from soota.config import SootaSettings
from soota.errors import AuthBudgetError, ContractError, InputError, SootaError, SourceError, StorageError
from soota.models import CatalogJob, CommandResult, RequestRecord
from soota.providers.mcp_client import StdioMCPClient
from soota.providers.monid.provider import MonidProvider
from soota.providers.opencli import OpenCLIProvider
from soota.providers.opencli_context import resolve_profile
from soota.providers.opencli_mcp import OpenCLIMCPProvider
from soota.providers.opencli_runtime import RuntimeUnavailable
from soota.providers.quotes import online_prices
from soota.providers.router import catalog_provider, validate_policy
from soota.providers.tikhub.catalog import YOUTUBE_CHANNEL_ID, catalog_endpoint, is_channel_id
from soota.providers.tikhub.client import TikHubProvider, load_price_snapshot
from soota.providers.treg.provider import TregProvider
from soota.render.catalog import write_catalog
from soota.routing import recognize
from soota.secrets import redact_any
from soota.storage.cache import CacheStore
from soota.storage.jobs import JobStore
from soota.storage.ledger import CostLedger

DEFAULT_MAX_PAGES = 10


def _validate_limits(max_items: int, max_pages: int, max_cost: float | None) -> None:
    if max_items < 1 or max_pages < 1:
        raise InputError("--max-items 和 --max-pages 必须大于 0。")
    if max_cost is not None and (not math.isfinite(max_cost) or max_cost < 0):
        raise InputError("--max-cost 必须是有限的非负数。")


def _reserved_cost(job: CatalogJob) -> Decimal:
    return sum((Decimal(str(record.estimated_cost_usd or 0)) for record in job.requests), Decimal(0))


def _save(job: CatalogJob, store: JobStore) -> None:
    record = redact_any(job.model_dump(mode="json"))
    # Opaque pagination tokens must survive redaction byte-for-byte in the private checkpoint.
    record["cursor"] = job.cursor
    record["visited_cursors"] = job.visited_cursors
    if job.pending_page is not None:
        record["pending_page"]["next_cursor"] = job.pending_page.next_cursor
    store.save(record, redact=False)
    job.updated_at = record["updated_at"]


def estimate_catalog(
    settings: SootaSettings,
    url: str,
    *,
    max_items: int = 100,
    max_pages: int = DEFAULT_MAX_PAGES,
    max_cost: float | None = None,
    incremental: bool = False,
    provider: str | None = None,
    fallback: str | None = None,
) -> CommandResult:
    _validate_limits(max_items, max_pages, max_cost)
    source = recognize(url)
    provider = selected_provider(settings, provider)
    validate_policy(provider, fallback, max_cost)
    selected = catalog_provider(
        source,
        provider,
        mcp_available=max_items <= 100 and StdioMCPClient.available(settings.opencli_mcp),
        settings=settings,
        max_cost=max_cost,
    )
    selected_provider(settings, selected)
    if fallback:
        raise InputError("当前目录来源没有已验证的替代分页契约；不能启用目录 fallback。")
    if selected in {"opencli", "opencli-mcp"}:
        if max_items > (100 if selected == "opencli-mcp" else 200):
            raise InputError("免费来源最近快照超过当前条数上限；更多历史需要独立搜索审计。")
        return CommandResult(
            ok=True,
            status="success",
            code="ok",
            exit_code=0,
            message="最近帖子快照预估完成；未联网、未创建任务。",
            data={
                "mode": "catalog",
                "source": source.model_dump(),
                "provider": selected,
                "max_items": max_items,
                "max_cost": max_cost,
                "incremental": incremental,
                "max_pages": 1,
                "request_limit": 1,
                "estimated_cost_usd": 0,
                "completeness": "unknown",
                "resumable": False,
            },
            warnings=["上限只约束最近快照，不保证获得指定条数或覆盖全部历史。"],
        )
    endpoint = catalog_endpoint(source)
    snapshot = load_price_snapshot()
    counts = {endpoint: max_pages}
    if source.platform == "youtube" and not is_channel_id(source.id):
        counts[YOUTUBE_CHANNEL_ID] = 1
    prices = snapshot.get("endpoints", {})
    if selected in {"monid", "treg"}:
        from soota.providers.priority import quote_rows

        prices = {
            path: quote_rows(settings, source, "api", [selected], endpoint=path)[0]["estimated_cost_usd"]
            for path in counts
        }
    values = [prices.get(path) for path in counts]
    known = all(value is not None and math.isfinite(value) and value >= 0 for value in values)
    cost = sum((Decimal(str(prices[path])) * count for path, count in counts.items()), Decimal(0)) if known else None
    dates = snapshot.get("endpoint_as_of", {})
    return CommandResult(
        ok=True,
        status="success",
        code="ok",
        exit_code=0,
        message="目录预估完成；未联网、未创建任务。",
        data={
            "mode": "catalog",
            "provider": selected,
            "source": source.model_dump(),
            "max_items": max_items,
            "max_pages": max_pages,
            "max_cost": max_cost,
            "incremental": incremental,
            "request_limit": sum(counts.values()),
            "endpoints": sorted(counts),
            "estimated_cost_usd": float(cost) if cost is not None else None,
            "price_basis": {path: dates.get(path, snapshot.get("as_of")) for path in sorted(counts)},
        },
        warnings=["估价按最多页面计算；每次请求前检查剩余预算。频道别名命中缓存时无需再次解析。"],
    )


@online_prices
def catalog(
    settings: SootaSettings,
    url: str,
    *,
    max_items: int = 100,
    max_pages: int = DEFAULT_MAX_PAGES,
    max_cost: float | None = None,
    output: Path | None = None,
    dry_run: bool = False,
    incremental: bool = False,
    provider: str | None = None,
    fallback: str | None = None,
    requester: Callable[..., Any] | None = None,
    _on_created: Callable[[CatalogJob], None] | None = None,
) -> CommandResult:
    _validate_limits(max_items, max_pages, max_cost)
    source = recognize(url)
    provider = selected_provider(settings, provider)
    validate_policy(provider, fallback, max_cost)
    try:
        selected = catalog_provider(
            source,
            provider,
            mcp_available=max_items <= 100 and StdioMCPClient.available(settings.opencli_mcp),
            settings=settings,
            max_cost=max_cost,
        )
    except AuthBudgetError as exc:
        return CommandResult(
            ok=False,
            status="error",
            code=exc.code,
            exit_code=exc.exit_code,
            message=exc.message,
            data={"request_count": 0, "requests_this_run": 0, "stop_reason": "budget_or_auth", "error": exc.to_dict()},
        )
    if fallback:
        raise InputError("当前目录来源没有已验证的替代分页契约；不能启用目录 fallback。")
    if selected in {"opencli", "opencli-mcp"} and max_items > (100 if selected == "opencli-mcp" else 200):
        raise InputError("免费来源最近快照超过当前条数上限。")
    if dry_run:
        return estimate_catalog(
            settings,
            url,
            max_items=max_items,
            max_pages=max_pages,
            max_cost=max_cost,
            incremental=incremental,
            provider=provider,
            fallback=fallback,
        )
    # Keep only the canonical public URL, not incidental query parameters or credentials.
    source.url = source.canonical_url
    store = JobStore(settings.jobs_dir)
    record = store.create(
        {
            "kind": "catalog",
            "provider": selected,
            "profile": resolve_profile(settings.opencli.profile) if selected == "opencli" else None,
            "completeness": "unknown" if selected in {"opencli", "opencli-mcp"} else "paginated",
            "status": "running",
            "source": source.model_dump(mode="json"),
            "output_root": str((output or settings.output_dir).expanduser().resolve()),
            "max_items": max_items,
            "max_pages": max_pages,
            "max_cost": max_cost,
            "page_size": min(200 if selected == "opencli" else 100, max_items),
            "incremental": incremental,
        }
    )
    with store.lock(record["id"]):
        if _on_created is not None:
            _on_created(CatalogJob.model_validate(record))
        return _execute(
            settings, CatalogJob.model_validate(record), store, requester, allow_free_fallback=provider == "auto"
        )


@online_prices
def resume_catalog(
    settings: SootaSettings,
    job_id: str,
    *,
    max_items: int | None = None,
    max_pages: int | None = None,
    max_cost: float | None = None,
    requester: Callable[..., Any] | None = None,
) -> CommandResult:
    store = JobStore(settings.jobs_dir)
    with store.lock(job_id):
        record = store.get(job_id)
        if record.get("kind") != "catalog":
            raise InputError("当前 resume 只支持目录任务。", hint="旧单条视频任务仍可查看；请使用原 URL 再次 ingest。")
        try:
            job = CatalogJob.model_validate(record)
        except ValidationError as exc:
            raise StorageError("目录任务记录不符合当前格式。") from exc
        if job.provider in {"opencli", "opencli-mcp"}:
            raise InputError(
                "最近帖子快照没有分页游标，不能续跑。", hint="重新 catalog --incremental 创建独立快照并去重。"
            )
        items_limit = job.max_items if max_items is None else max_items
        pages_limit = job.max_pages if max_pages is None else max_pages
        cost_limit = job.max_cost if max_cost is None else max_cost
        _validate_limits(items_limit, pages_limit, cost_limit)
        if (
            items_limit < len(job.items)
            or pages_limit < job.pages_fetched
            or (cost_limit is not None and Decimal(str(cost_limit)) < _reserved_cost(job))
        ):
            raise InputError("续跑上限不能低于已保存的条目、页面或累计预算预留。")
        job.max_items, job.max_pages, job.max_cost = items_limit, pages_limit, cost_limit
        return _execute(settings, job, store, requester)


def _known_ids(job: CatalogJob, store: JobStore) -> list[str]:
    known: set[str] = set()
    for record in store.list_jobs(limit=None):
        if record.get("kind") != "catalog" or record.get("id") == job.id:
            continue
        try:
            previous = CatalogJob.model_validate(record)
        except ValidationError:
            continue
        if (
            previous.resolved
            and previous.source.cache_identity == job.source.cache_identity
            and previous.provider == job.provider
            and previous.profile == job.profile
        ):
            known.update(item.source.cache_identity for item in previous.items)
    return sorted(known)


def _provider(
    settings: SootaSettings, job: CatalogJob, store: JobStore, requester: Callable[..., Any] | None
) -> TikHubProvider | OpenCLIProvider | OpenCLIMCPProvider:
    selected_provider(settings, job.provider)
    if job.provider == "opencli":
        bound = settings.model_copy(update={"opencli": settings.opencli.model_copy(update={"profile": job.profile})})
        return OpenCLIProvider(bound)
    if job.provider == "opencli-mcp":
        return OpenCLIMCPProvider(settings)

    def reserve(record: RequestRecord) -> None:
        if not record.billed or record.cache_hit:
            return
        price = record.estimated_cost_usd
        if job.max_cost == 0 or price is None or not math.isfinite(price) or price < 0:
            raise AuthBudgetError("目录接口价格未知，已在请求前停止。", hint="更新受控价格快照后再续跑。")
        if job.max_cost is not None and _reserved_cost(job) + Decimal(str(price)) > Decimal(str(job.max_cost)):
            raise AuthBudgetError(
                "下一次请求将超过该目录任务的累计费用上限。",
                hint=f"用 soota jobs resume {job.id} --max-cost 新的总上限 续跑；已有预留不会清零。",
            )
        date = provider.snapshot.get("endpoint_as_of", {}).get(record.endpoint, provider.snapshot.get("as_of"))
        record.note = f"reserved-before-request;snapshot:{date}"
        job.requests.append(record)
        # Persist the reservation before dispatch, so a killed process cannot reset the cap.
        _save(job, store)
        CostLedger(settings.ledger_path).append(job.source.id, [record], {"job_id": job.id, "reservation": True})

    provider = (
        (TregProvider if job.provider == "treg" else MonidProvider)(
            settings,
            float(Decimal(str(job.max_cost)) - _reserved_cost(job)) if job.max_cost is not None else None,
            purpose="catalog",
            before_request=reserve,
            run_scope="catalog:" + job.id,
        )
        if job.provider in {"monid", "treg"}
        else TikHubProvider(
            runtime_python=settings.tikhub.runtime_python,
            cache=CacheStore(settings.cache_dir),
            base_url=settings.tikhub.base_url,
            timeout=settings.tikhub.timeout_seconds,
            preferred_language=settings.default_language,
            requester=requester,
            before_request=reserve,
            max_retries=0,
            payment_gate=PaymentGate(
                settings, float(Decimal(str(job.max_cost)) - _reserved_cost(job)) if job.max_cost is not None else None
            ),
        )
    )
    return provider


def _execute(
    settings: SootaSettings,
    job: CatalogJob,
    store: JobStore,
    requester: Callable[..., Any] | None,
    *,
    allow_free_fallback: bool = False,
) -> CommandResult:
    previous_requests, previous_items = len(job.requests), len(job.items)
    job.status, job.error = "running", None
    try:
        provider = _provider(settings, job, store, requester)
        if not job.resolved:
            job.source = provider.resolve_catalog_source(job.source)
            job.resolved = True
            _save(job, store)
        if job.incremental and not job.baseline_loaded:
            job.known_ids = _known_ids(job, store)
            job.baseline_loaded = True
            _save(job, store)
        try:
            _collect(job, store, provider)
        finally:
            if isinstance(provider, (OpenCLIProvider, OpenCLIMCPProvider)):
                job.requests.extend(provider.records)
    except KeyboardInterrupt:
        job.status, job.stop_reason = "paused", "interrupted"
    except RuntimeUnavailable as exc:
        if allow_free_fallback and job.provider == "opencli-mcp" and job.pages_fetched == 0 and not job.items:
            job.provider = "opencli"
            job.profile = resolve_profile(settings.opencli.profile)
            job.resolved = False
            job.baseline_loaded = False
            job.known_ids = []
            job.page_size = min(200, job.max_items)
            job.warnings.append(f"opencli-mcp 不可用：{exc.message}；已选择 OpenCLI 快照。")
            _save(job, store)
            return _execute(settings, job, store, requester)
        job.status, job.stop_reason = "failed", "error"
        job.error = exc.to_dict()
    except SootaError as exc:
        job.status = "paused" if isinstance(exc, AuthBudgetError) else "failed"
        job.stop_reason = "budget_or_auth" if isinstance(exc, AuthBudgetError) else "error"
        job.error = exc.to_dict()
    except OSError:
        job.status, job.stop_reason = "failed", "storage"
        job.error = StorageError("目录任务无法读写本地文件，已保留可保存的进度。").to_dict()
    except Exception:
        job.status, job.stop_reason = "failed", "error"
        job.error = SourceError(
            "目录请求或响应处理失败。", hint="已有进度已保留；检查接口后使用 jobs resume。"
        ).to_dict()
    _save(job, store)
    folder: Path | None = None
    try:
        folder = write_catalog(job, float(_reserved_cost(job)))
    except OSError:
        job.status, job.stop_reason = "failed", "storage"
        job.error = StorageError(
            "目录输出无法写入，进度仍保存在任务记录中。", hint="修复输出目录后再 jobs resume。"
        ).to_dict()
        _save(job, store)
    return _result(job, folder, previous_requests, previous_items)


def _collect(job: CatalogJob, store: JobStore, provider: TikHubProvider | OpenCLIProvider | OpenCLIMCPProvider) -> None:
    known = set(job.known_ids)
    collected = {item.source.cache_identity for item in job.items}
    while not job.exhausted:
        if len(job.items) >= job.max_items:
            job.status, job.stop_reason = "paused", "max_items"
            return
        if job.pending_page is None:
            if job.pages_fetched >= job.max_pages:
                job.status, job.stop_reason = "paused", "max_pages"
                return
            if job.cursor in job.visited_cursors:
                raise ContractError(
                    "目录分页游标重复，已停止循环请求。", hint="检查接口后重新 catalog，创建新目录任务。"
                )
            page = provider.list_items(job.source, job.cursor, page_size=job.page_size)
            job.completeness = page.completeness
            job.warnings = list(dict.fromkeys([*job.warnings, *page.warnings]))
            job.pending_page = page
            job.pages_fetched += 1
            job.visited_cursors.append(job.cursor)
            job.title = page.title or job.title
            # Save the entire page before applying item limits. Resume consumes its remainder for free.
            _save(job, store)
            if (
                job.completeness != "unknown"
                and job.incremental
                and page.items
                and all(item.source.cache_identity in known for item in page.items)
            ):
                job.pending_page = None
                job.exhausted = True
                job.stop_reason = "caught_up"
                break
        page = job.pending_page
        while page.items and len(job.items) < job.max_items:
            item = page.items.pop(0)
            identity = item.source.cache_identity
            if identity in known or identity in collected:
                continue
            if item.creator is None and job.source.platform == "youtube":
                item.creator = job.title
            job.items.append(item)
            collected.add(identity)
        if not page.items:
            job.cursor = page.next_cursor
            job.pending_page = None
            job.exhausted = job.cursor is None
        _save(job, store)
        if job.pending_page is None and job.cursor is not None and job.cursor in job.visited_cursors:
            raise ContractError(
                "目录接口返回了重复的下一页游标；本页条目已保存。",
                hint="检查接口后重新 catalog，创建新目录任务。",
            )
    job.status = "completed"
    if job.stop_reason != "caught_up":
        job.stop_reason = "bounded_snapshot" if job.completeness == "unknown" else "end"


def _result(job: CatalogJob, folder: Path | None, previous_requests: int, previous_items: int) -> CommandResult:
    completed = job.status == "completed"
    partial = not completed and bool(job.items) and folder is not None
    error = job.error or {}
    status: Literal["success", "partial", "error"] = (
        "success" if completed else ("partial" if partial or not error else "error")
    )
    exit_code = 0 if completed else (2 if partial or not error else int(error.get("exit_code", 5)))
    data: dict[str, Any] = {
        "job_id": job.id,
        "provider": job.provider,
        "profile": job.profile,
        "completeness": job.completeness,
        "resumable": job.provider not in {"opencli", "opencli-mcp"},
        "source": job.source.model_dump(),
        "job_status": job.status,
        "stop_reason": job.stop_reason,
        "item_count": len(job.items),
        "items_added_this_run": len(job.items) - previous_items,
        "pages_fetched": job.pages_fetched,
        "pending_items": len(job.pending_page.items) if job.pending_page else 0,
        "request_count": len(job.requests),
        "requests_this_run": len(job.requests) - previous_requests,
        "reserved_cost_usd": float(_reserved_cost(job)),
        "actual_cost_usd": None,
        "max_cost": job.max_cost,
        "incremental": job.incremental,
    }
    if folder is not None:
        data.update(output=str(folder), catalog=str(folder / "catalog.json"), content=str(folder / "catalog.md"))
    if error:
        data["error"] = error
    return CommandResult(
        ok=completed,
        status=status,
        code="ok" if completed else str(error.get("code", "catalog_paused")),
        exit_code=exit_code,
        message=f"已保存 {len(job.items)} 条最近快照；完整性未知。"
        if completed and job.completeness == "unknown"
        else f"目录采集完成，共 {len(job.items)} 条。"
        if completed
        else f"目录任务已停止，保留 {len(job.items)} 条。",
        hint=None
        if completed
        else (
            "重新 catalog --incremental 创建独立快照。"
            if job.provider in {"opencli", "opencli-mcp"}
            else str(error.get("hint") or f"soota jobs resume {job.id}；可提高条数、页数或总费用上限。")
        ),
        data=data,
        warnings=job.warnings,
    )
