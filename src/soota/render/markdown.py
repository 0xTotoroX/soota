from __future__ import annotations

from soota.models import MediaItem, TranscriptDocument
from soota.transcripts.captions import ms_to_clock


def render_context(item: MediaItem, document: TranscriptDocument) -> str:
    """Serialize source text in cue order without rewriting or merging it."""
    lines = [
        f"# {item.title or item.source.id}",
        "",
        f"source: {item.provenance.get('transcript_source_url') or item.source.canonical_url}",
        f"transcript_status: {document.status.value}",
        f"transcript_source: {document.source.value if document.source else 'unknown'}",
        f"language: {document.language or 'unknown'}",
        "",
    ]
    lines.extend(f"[{segment.segment_id}] {segment.text}" for segment in document.segments)
    return "\n".join(lines) + "\n"


def _timestamp(ms: int | None) -> str:
    if ms is None:
        return "未知"
    return f"{ms_to_clock(ms)}.{max(0, ms) % 1000:03d}"


def render_transcript(item: MediaItem, document: TranscriptDocument) -> str:
    title = item.title or item.source.id
    lines = [
        f"# {title} — 转写",
        "",
        f"- 来源: {document.source or document.status.value}",
        f"- 语言: {document.language or '-'}",
        f"- 片段: {len(document.segments)}",
        "",
    ]
    if not document.segments:
        lines.append("无可用字幕或转写。")
        lines.append("")
        return "\n".join(lines)
    for segment in document.segments:
        timing = (
            "无时间轴" if segment.start_ms is None else f"{_timestamp(segment.start_ms)}-{_timestamp(segment.end_ms)}"
        )
        lines.extend(
            [
                f"## {segment.segment_id} · {timing}",
                "",
                f'<a id="{segment.segment_id}"></a>',
                "",
                segment.text,
                "",
            ]
        )
    return "\n".join(lines)
