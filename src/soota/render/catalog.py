"""[INPUT]: 目录任务与累计预留。
[OUTPUT]: 标准 catalog.json/catalog.md。
[POS]: 目录输出，保留声明总数和完整性，不生成语义文章。
[PROTOCOL]: 变更时更新此头部，然后检查 AGENTS.md
"""

from __future__ import annotations

import html
import json
import re
from pathlib import Path

from soota.models import CatalogJob, MediaItem
from soota.secrets import redact_any
from soota.storage.atomic import atomic_write


def _text(value: str) -> str:
    return re.sub(r"([\\`*_\[\]|])", r"\\\1", html.escape(" ".join(value.split()), quote=False))


def _row(index: int, item: MediaItem) -> str:
    title = _text(item.title or item.source.id)
    creator = _text(item.creator or "—")
    published = item.published_at.date().isoformat() if item.published_at else item.extras.get("published_label", "—")
    duration = "—"
    if item.duration_seconds is not None:
        minutes, seconds = divmod(item.duration_seconds, 60)
        duration = f"{minutes}:{seconds:02d}"
    return f"| {index} | [{title}]({item.source.canonical_url}) | {creator} | {_text(published)} | {duration} |"


def write_catalog(job: CatalogJob, reserved_cost: float) -> Path:
    folder = job.output_root / "catalogs" / job.id
    payload = {
        "schema_version": 1,
        "job_id": job.id,
        "source": job.source.model_dump(mode="json"),
        "title": job.title,
        "updated_at": job.updated_at,
        "status": job.status,
        "stop_reason": job.stop_reason,
        "incremental": job.incremental,
        "item_count": len(job.items),
        "total_items": job.total_items,
        "pages_fetched": job.pages_fetched,
        "items": [item.model_dump(mode="json") for item in job.items],
        "processing": {
            "provider": job.provider,
            "profile": job.profile,
            "completeness": job.completeness,
            "warnings": job.warnings,
            "reserved_cost_usd": reserved_cost,
            "actual_cost_usd": None,
            "requests": [record.model_dump(mode="json") for record in job.requests],
        },
        "error": job.error,
    }
    title = _text(job.title or job.source.id)
    lines = [
        f"# {title} · 内容目录",
        "",
        f"来源：{job.source.canonical_url}",
        f"任务：`{job.id}` · 状态：`{job.status}` · 条目：{len(job.items)} · 已读取页面：{job.pages_fetched}",
        "",
        "本目录保存链接、已有元数据及帖子正文，不包含视频字幕、转写或摘要。",
    ]
    if job.incremental:
        lines.extend(["", "这是增量目录，只列出此前目录任务中尚未收录的条目。"])
    if job.completeness == "unknown":
        lines.extend(["", "最近帖子快照：完整性未知，不代表全部历史或已追平；没有可续跑游标。"])
    if job.status != "completed" and job.provider not in {"opencli", "opencli-mcp"}:
        lines.extend(
            ["", f"当前目录尚未完成（{job.stop_reason}），进度已保存。", f"续跑：`soota jobs resume {job.id}`"]
        )
    lines.extend(["", "| # | 内容 | 作者 | 发布时间 | 时长 |", "| --- | --- | --- | --- | --- |"])
    lines.extend(_row(index, item) for index, item in enumerate(job.items, 1))
    for item in job.items:
        if item.text:
            lines.extend(["", f"### {item.source.id}", "", _text(item.text), ""])
    cost_note = (
        "本次免费来源快照没有 TikHub API 费用。"
        if job.provider in {"opencli", "opencli-mcp", "http"}
        else f"请求预算已预留 {reserved_cost:g} USD；实际扣费以 TikHub 账单为准。"
    )
    lines.extend(["", cost_note, ""])
    atomic_write(folder / "catalog.json", json.dumps(redact_any(payload), ensure_ascii=False, indent=2) + "\n")
    atomic_write(folder / "catalog.md", "\n".join(lines))
    return folder
