from __future__ import annotations

import json
from pathlib import Path

from soota.errors import InputError, SootaError, SourcePreserved
from soota.models import (
    AnalysisManifest,
    AnalysisStatus,
    ContentBundleManifest,
    MediaItem,
    TranscriptDocument,
    TranscriptStatus,
)
from soota.render.markdown import render_context, render_transcript
from soota.storage.atomic import atomic_write
from soota.transcripts.captions import parse_transcript_markdown

STALE_WARNING = "字幕原文已变化，保留的 content.md 可能过时；未自动改写。"


def bundle_dir(output_root: Path, item: MediaItem) -> Path:
    return output_root / item.source.platform / item.source.id


def write_bundle(
    output_root: Path,
    item: MediaItem,
    document: TranscriptDocument,
    manifest: ContentBundleManifest,
    analysis: object | None = None,
    *,
    force: bool = False,
) -> Path:
    folder = bundle_dir(output_root, item)
    return write_bundle_to(folder, item, document, manifest, analysis, force=force)


def write_bundle_to(
    folder: Path,
    item: MediaItem,
    document: TranscriptDocument,
    manifest: ContentBundleManifest,
    analysis: object | None = None,
    *,
    force: bool = False,
) -> Path:
    if analysis is not None:
        from soota.workflow_compat import invoke

        return invoke(
            "write_legacy",
            folder=folder,
            item=item,
            document=document,
            manifest=manifest,
            analysis=analysis,
            force=force,
        )
    folder.mkdir(parents=True, exist_ok=True)
    context_path = folder / "context.md"
    transcript_path = folder / "transcript.md"
    manifest_path = folder / "manifest.json"
    previous = (
        ContentBundleManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
        if manifest_path.is_file()
        else None
    )
    if previous and previous.source.cache_identity != item.source.cache_identity:
        raise InputError("已有内容包属于不同来源，未覆盖文件。")
    if (
        analysis is None
        and previous
        and previous.transcript.status == TranscriptStatus.READY
        and not force
        and (document.status != TranscriptStatus.READY or not document.segments)
    ):
        raise SourcePreserved(
            f"本次字幕状态为 {document.status.value}，已有有效原文已保留，未覆盖为空内容。",
            hint="仅查询信息可用 inspect；确需替换源文件时使用 --force 或新的输出目录。",
            details={"attempt_status": document.status.value, "existing_manifest": str(manifest_path)},
        )
    if previous:
        manifest.files = {**previous.files, **manifest.files}

    transcript = render_transcript(item, document)
    changed = _source_changed(transcript_path, previous, document)
    _preserve_analysis(folder, manifest, previous, changed)
    atomic_write(context_path, render_context(item, document))
    atomic_write(transcript_path, transcript)
    manifest.transcript.path = "transcript.md"
    manifest.transcript.segments = len(document.segments)
    manifest.transcript.status = document.status
    manifest.transcript.source = document.source
    manifest.transcript.language = document.language
    for name in ("context.md", "transcript.md", "content.md", "manifest.json"):
        if name == "manifest.json" or (folder / name).is_file():
            manifest.files[name] = name
        else:
            manifest.files.pop(name, None)
    atomic_write(manifest_path, json.dumps(manifest.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n")
    return folder


def _source_changed(path: Path, previous: ContentBundleManifest | None, current: TranscriptDocument) -> bool:
    if previous is None or not path.is_file():
        return True
    try:
        old = parse_transcript_markdown(path.read_text(encoding="utf-8"), previous.transcript)
    except SootaError:
        return True

    def signature(document: TranscriptDocument) -> tuple[object, ...]:
        return (
            document.status,
            document.source,
            document.language,
            [(segment.segment_id, segment.start_ms, segment.end_ms, segment.text) for segment in document.segments],
        )

    return signature(old) != signature(current)


def _preserve_analysis(
    folder: Path,
    manifest: ContentBundleManifest,
    previous: ContentBundleManifest | None,
    source_changed: bool,
) -> None:
    if not (folder / "content.md").is_file():
        manifest.analysis = AnalysisManifest()
        return
    manifest.analysis = previous.analysis.model_copy() if previous else AnalysisManifest()
    manifest.analysis.path = "content.md"
    if source_changed and manifest.analysis.status == AnalysisStatus.READY:
        manifest.analysis.status = AnalysisStatus.STALE
    if manifest.analysis.status == AnalysisStatus.STALE:
        manifest.processing.warnings.append(STALE_WARNING)
