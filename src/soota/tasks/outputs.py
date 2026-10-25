"""[INPUT]: 采集正文、媒体和字幕目标。
[OUTPUT]: 来源图片与字幕格式导出；不生成语义交接或分析产物。
[POS]: Soota 原文产物层，后续内容整理归 LoreLab。
[PROTOCOL]: 变更时更新此头部，然后检查 AGENTS.md
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from soota.artifacts.bundle import (
    content_from_markdown,
    load_transcript,
    save_bundle,
    write_artifact,
    write_bundle,
)
from soota.artifacts.models import BundleV2
from soota.billing import selected_provider
from soota.config import SootaSettings
from soota.errors import InputError, SootaError, SourceError
from soota.media import MediaDescriptor, MediaService
from soota.models import MediaItem, TranscriptMode, TranscriptSource
from soota.tasks.acquire import public_text
from soota.tasks.models import InputSpec, TaskItem, TaskSpec


def _export_subtitles(folder: Path, bundle: BundleV2, spec: TaskSpec) -> None:
    document = load_transcript(folder, bundle)
    if document is None or not document.segments:
        raise SourceError("没有可导出的字幕。")
    if spec.transcript_mode == TranscriptMode.CAPTIONS_ONLY and document.source == TranscriptSource.ASR:
        raise SourceError("本次要求原字幕；已有 ASR 结果不能冒充原字幕。")
    if (
        spec.strict_language
        and document.language
        and document.language.split("-")[0].lower() != spec.language.split("-")[0].lower()
    ):
        raise SourceError("已有字幕语言不满足本次指定语言。")
    from soota.transcripts.captions import ms_to_clock

    for format in spec.subtitle_formats:
        target = f"captions.{format}"
        if format in {"srt", "vtt"}:
            if any(s.start_ms is None or s.end_ms is None for s in document.segments):
                raise SourceError("字幕时间轴不完整，不能生成 SRT/VTT；可选择 txt/json。")
            parts = ["WEBVTT\n"] if format == "vtt" else []
            for index, segment in enumerate(document.segments, 1):
                assert segment.start_ms is not None and segment.end_ms is not None
                separator = "," if format == "srt" else "."
                start = f"{ms_to_clock(segment.start_ms)}{separator}{segment.start_ms % 1000:03d}"
                end = f"{ms_to_clock(segment.end_ms)}{separator}{segment.end_ms % 1000:03d}"
                parts.extend([str(index), f"{start} --> {end}", segment.text, ""])
            body = "\n".join(parts)
        elif format == "txt":
            body = "\n".join(s.text for s in document.segments) + "\n"
        else:
            body = document.model_dump_json(indent=2) + "\n"
        write_artifact(
            folder,
            bundle,
            path=target,
            kind="subtitle-export",
            format=format,
            content=body,
            provider=bundle.processing.provider,
            force=spec.force,
        )


def _images(
    settings: SootaSettings, spec: TaskSpec, seed: InputSpec, work: Path, folder: Path, bundle: BundleV2, row: TaskItem
) -> None:
    if bundle.content is None:
        raise SourceError("没有可供获取图片的正文。")
    references = work / "references.json"
    private = json.loads(references.read_text(encoding="utf-8")) if references.is_file() else {}
    raw_body = private.get("body", bundle.content.body_md)
    base = private.get("base", str(Path(seed.value).parent) if seed.kind == "file" else seed.value)
    matches = list(re.finditer(r"!\[[^\]]*\]\(<?([^\s)>]+)>?(?:\s+[^)]*)?\)", raw_body))
    urls = list(dict.fromkeys(match[1] for match in matches))
    if not urls:
        raise SourceError("正文没有提供图片引用；未把空图集标为已交付。")
    service = MediaService(settings, work / "images", max_bytes=spec.max_bytes)
    failures = []
    body = raw_body
    for index, url in enumerate(urls, 1):
        try:
            if urlsplit(url).scheme in {"http", "https"} or urlsplit(base).scheme in {"http", "https"}:
                if spec.provider == "local":
                    raise InputError("local 模式不能获取远程图片。")
                selected_provider(settings, "http")
                absolute = urljoin(base, url)
                asset = service.from_descriptor(MediaDescriptor(url=absolute, source_provider="http"), kind="image")
            else:
                if urlsplit(url).scheme:
                    raise InputError("不支持正文中的本地文件 URL 协议。")
                original = (Path(base) / url).resolve()
                if seed.kind != "file" and not original.is_relative_to(
                    (folder if seed.kind == "bundle" else work).resolve()
                ):
                    raise InputError("工具正文引用了任务目录外的本地附件。")
                asset = service.from_file(original, kind="image")
            path = f"attachments/image-{index:03d}{asset.path.suffix}"
            write_artifact(
                folder,
                bundle,
                path=path,
                kind="image",
                format=asset.format,
                content=asset.path,
                provider=asset.source_provider,
                verification="media",
                attributes={
                    **asset.model_dump(mode="json", exclude={"path"}),
                    "acquisition_context": spec.execution_context,
                    "input_fingerprint": row.input_fingerprint,
                },
                force=spec.force,
            )
            body = body.replace(url, path)
        except (SootaError, OSError):
            failures.append(index)
    content = content_from_markdown(bundle.source, public_text(body), bundle.content.title, bundle.content.scope)
    item = MediaItem(
        source=bundle.source,
        title=bundle.metadata.get("title"),
        creator=bundle.metadata.get("creator"),
        provenance=bundle.metadata.get("provenance", {}),
    )
    updated = write_bundle(folder, item, content=content, force=spec.force)
    for artifact in updated.artifacts:
        if artifact.kind in {"body", "context", "transcript"}:
            artifact.attributes.update(
                acquisition_context=spec.execution_context,
                input_fingerprint=row.input_fingerprint,
                text_signature=spec.text_signature,
            )
    save_bundle(folder, updated)
    if failures:
        raise SourceError("部分图片未能保存，已保留其余产物。", details={"failed_image_indexes": failures})
