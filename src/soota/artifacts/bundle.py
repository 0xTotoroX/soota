"""[INPUT]: 原文模型、显式原件根、登记产物及旧包。
[OUTPUT]: load/write/verify、单根 safe_path 与跨根 artifact_path。
[POS]: v1/v2 兼容和 opt-in v3 包持久化；路径与哈希检查的共同实现。
[PROTOCOL]: 变更时更新此头部，然后检查 AGENTS.md
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from soota.artifacts.models import (
    Artifact,
    ArtifactV3,
    Bundle,
    BundleV2,
    BundleV3,
    ContentBlock,
    ContentDocument,
    artifact_storage,
    relative_path,
)
from soota.errors import ContractError, InputError, SourcePreserved, StorageError
from soota.models import (
    AnalysisStatus,
    MediaItem,
    ProcessingManifest,
    SourceRef,
    TranscriptDocument,
    TranscriptManifest,
)
from soota.models import TranscriptStatus as Status
from soota.render.bundle import STALE_WARNING
from soota.render.markdown import render_context, render_transcript
from soota.storage.atomic import atomic_write
from soota.transcripts.captions import parse_transcript_markdown


def bundle_folder(path: Path) -> Path:
    return path.parent if path.name == "manifest.json" else path


def safe_path(folder: Path, name: str) -> Path:
    try:
        relative_path(name)
    except ValueError as exc:
        raise InputError(str(exc)) from exc
    result = folder / name
    if not result.resolve().is_relative_to(folder.resolve()):
        raise InputError("产物路径或符号链接指向内容包之外。")
    return result


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _check_roots(folder: Path, bundle: Bundle) -> None:
    if isinstance(bundle, BundleV3):
        raw, control = bundle.raw_root, folder.resolve()
        if raw.resolve() != raw or raw.is_relative_to(control) or control.is_relative_to(raw):
            raise InputError("原件根与控制包根必须独立，且原件根不能经符号链接重定向。")


def artifact_path(folder: Path, bundle: Bundle, name: str) -> Path:
    """解析登记产物；safe_path 仍只负责单个根内的路径检查。"""
    _check_roots(folder, bundle)
    if isinstance(bundle, BundleV3) and name != "manifest.json":
        known = next((a for a in bundle.artifacts if a.path == name), None)
        if not isinstance(known, ArtifactV3):
            raise InputError(f"v3 产物未登记存储根：{name}")
        root = bundle.raw_root if known.storage == "raw" else folder
        return safe_path(root, name)
    return safe_path(folder, name)


def _write_root(folder: Path, bundle: Bundle, path: str, kind: str | None) -> Path:
    _check_roots(folder, bundle)
    if not isinstance(bundle, BundleV3):
        return folder
    known = next((a for a in bundle.artifacts if a.path == path), None)
    if isinstance(known, ArtifactV3):
        return bundle.raw_root if known.storage == "raw" else folder
    return bundle.raw_root if artifact_storage(path, kind or "") == "raw" else folder


def load_bundle(path: Path) -> Bundle:
    folder = bundle_folder(path)
    try:
        payload = json.loads(safe_path(folder, "manifest.json").read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("schema_version", 1) not in {1, 2, 3}:
            raise ValueError("不支持的内容包版本。")
        legacy = payload.get("schema_version", 1) == 1
        bundle: Bundle = (
            BundleV3.model_validate(payload)
            if payload.get("schema_version") == 3
            else BundleV2.model_validate({**payload, "schema_version": 2})
        )
        _check_roots(folder, bundle)
        if legacy:
            for name in bundle.files.values():
                target = safe_path(folder, name)
                if name == "manifest.json" or not target.is_file():
                    continue
                kind = {"context.md": "context", "transcript.md": "transcript", "content.md": "article"}.get(
                    name, "data"
                )
                bundle.artifacts.append(
                    _artifact(target, name, kind, target.suffix.lstrip("."), bundle.processing.provider)
                )
            if (folder / "context.md").is_file():
                bundle.context_fingerprint = file_sha256(safe_path(folder, "context.md"))
            elif (folder / "transcript.md").is_file():
                bundle.context_fingerprint = file_sha256(safe_path(folder, "transcript.md"))
        for artifact in bundle.artifacts:
            artifact_path(folder, bundle, artifact.path)
        return bundle
    except (ValueError, ValidationError) as exc:
        raise ContractError("内容包 manifest 不符合契约。", hint=str(exc)) from exc
    except OSError as exc:
        raise StorageError("无法读取内容包 manifest。") from exc


def save_bundle(folder: Path, bundle: Bundle) -> None:
    _check_roots(folder, bundle)
    safe_path(folder, "manifest.json")
    bundle.files["manifest.json"] = "manifest.json"
    atomic_write(folder / "manifest.json", bundle.model_dump_json(indent=2) + "\n")


def _artifact(path: Path, relative: str, kind: str, format: str, provider: str | None) -> Artifact:
    return Artifact(
        path=relative, kind=kind, format=format, bytes=path.stat().st_size, sha256=file_sha256(path), provider=provider
    )


def _check_writable(folder: Path, bundle: Bundle, path: str, *, kind: str | None = None, force: bool = False) -> Path:
    target = safe_path(_write_root(folder, bundle, path, kind), path)
    if target.exists() and not target.is_file():
        raise InputError(f"产物路径不是文件：{path}")
    if target.is_file() and not force:
        known = next((artifact for artifact in bundle.artifacts if artifact.path == path), None)
        if known is None or known.sha256 != file_sha256(target):
            raise InputError(f"已有文件未登记或已被修改，未覆盖：{path}", hint="使用新目录，或明确授权 --force。")
    return target


def write_artifact(
    folder: Path,
    bundle: Bundle,
    *,
    path: str,
    kind: str,
    format: str,
    content: str | bytes | Path,
    provider: str | None = None,
    verification: str = "hash",
    completeness: str = "unknown",
    attributes: dict[str, Any] | None = None,
    force: bool = False,
) -> Artifact:
    if path in {"manifest.json", ".soota-export.json"}:
        raise InputError("该路径由内容包管理，不能作为普通产物写入。")
    if isinstance(bundle, BundleV3):
        if path == ".task.lock":
            raise InputError("产物不能使用任务锁路径。")
        known = next((a for a in bundle.artifacts if a.path == path), None)
        if isinstance(known, ArtifactV3) and known.storage != artifact_storage(path, kind):
            raise InputError("已有产物的存储根不允许通过更换类型重定位。")
    target = _check_writable(folder, bundle, path, kind=kind, force=force)
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=".soota-", dir=target.parent)
    try:
        with os.fdopen(handle, "wb") as output:
            if isinstance(content, Path):
                with content.open("rb") as source:
                    shutil.copyfileobj(source, output)
            else:
                output.write(content.encode("utf-8") if isinstance(content, str) else content)
            output.flush()
            os.fsync(output.fileno())
        artifact = _artifact(Path(temporary), path, kind, format, provider)
        artifact = Artifact.model_validate(
            {
                **artifact.model_dump(),
                "verification": verification,
                "completeness": completeness,
                "attributes": attributes or {},
            }
        )
        if isinstance(bundle, BundleV3):
            artifact = ArtifactV3.model_validate(
                {**artifact.model_dump(), "storage": "raw" if target.is_relative_to(bundle.raw_root) else "control"}
            )
        Path(temporary).replace(target)
    finally:
        Path(temporary).unlink(missing_ok=True)
    bundle.artifacts = [current for current in bundle.artifacts if current.path != path] + [artifact]
    bundle.files[path] = path
    save_bundle(folder, bundle)
    return artifact


def verify_artifacts(folder: Path, bundle: Bundle) -> list[str]:
    _check_roots(folder, bundle)
    issues: list[str] = []
    tracked = {artifact.path for artifact in bundle.artifacts}
    for name in set(bundle.files.values()) - tracked - {"manifest.json"}:
        if not artifact_path(folder, bundle, name).is_file():
            issues.append(f"missing:{name}")
    for artifact in bundle.artifacts:
        path = artifact_path(folder, bundle, artifact.path)
        if not path.is_file():
            issues.append(f"missing:{artifact.path}")
        elif path.stat().st_size != artifact.bytes or file_sha256(path) != artifact.sha256:
            issues.append(f"changed:{artifact.path}")
    return issues


def load_transcript(folder: Path, bundle: Bundle | None = None) -> TranscriptDocument | None:
    folder = bundle_folder(folder)
    bundle = bundle or load_bundle(folder)
    if not bundle.transcript.path:
        return None
    path = artifact_path(folder, bundle, bundle.transcript.path)
    if not path.is_file():
        raise StorageError("已登记的字幕文件不存在。")
    known = next((artifact for artifact in bundle.artifacts if artifact.path == bundle.transcript.path), None)
    if known and file_sha256(path) != known.sha256:
        raise InputError("已保存的字幕被修改，未使用与记录不一致的正文。")
    return parse_transcript_markdown(path.read_text(encoding="utf-8"), bundle.transcript)


def content_context(content: ContentDocument) -> str:
    lines = [
        f"# {content.title or content.source.id}",
        "",
        f"source: {content.source.canonical_url}",
        f"scope: {content.scope}",
        "",
    ]
    for block in content.blocks:
        lines.extend([f'<a id="{block.block_id}"></a>', f"[{block.block_id}] {block.markdown}", ""])
    return "\n".join(lines)


def content_from_markdown(
    source: SourceRef, text: str, title: str | None = None, scope: str = "unknown"
) -> ContentDocument:
    blocks = [ContentBlock(kind="markdown", text=part) for part in re.split(r"\n[ \t]*\n+", text) if part.strip()]
    return ContentDocument.model_validate(
        {"source": source, "title": title, "scope": scope, "blocks": blocks, "body_markdown": text}
    )


def write_bundle(
    folder: Path,
    item: MediaItem,
    *,
    document: TranscriptDocument | None = None,
    content: ContentDocument | None = None,
    processing: ProcessingManifest | None = None,
    raw_root: Path | None = None,
    force: bool = False,
) -> Bundle:
    previous = load_bundle(folder) if (folder / "manifest.json").is_file() else None
    if previous and raw_root is not None:
        if not isinstance(previous, BundleV3) or previous.raw_root != raw_root.expanduser().resolve():
            raise InputError("已有内容包的存储契约或原件根不同，未迁移或覆盖。")
    if previous and previous.source.cache_identity != item.source.cache_identity:
        raise InputError("已有内容包属于不同来源，未覆盖文件。")
    if content and content.source.cache_identity != item.source.cache_identity:
        raise InputError("正文来源与内容包来源不一致。")
    has_transcript = document is not None and document.status == Status.READY and bool(document.segments)
    has_content = content is not None and content.has_text
    attempting_source = document is not None or content is not None
    if previous and not force:
        if document is not None and not has_transcript and previous.transcript.status == Status.READY:
            raise SourcePreserved("本次未取得有效正文，已有原文已保留。")
        if content is not None and not has_content and previous.content and previous.content.has_text:
            raise SourcePreserved("本次未取得有效正文，已有原文已保留。")
    bundle = (
        previous.model_copy(deep=True)
        if previous
        else BundleV3(source=item.source, raw_root=raw_root.expanduser().resolve())
        if raw_root is not None
        else BundleV2(source=item.source)
    )
    _check_roots(folder, bundle)
    if not previous:
        bundle.processing = ProcessingManifest(provider=str(item.provenance.get("provider", "unknown")))
    bundle.metadata_from_item(item)
    bundle.metadata["provenance"] = item.provenance
    if processing is not None:
        bundle.processing = processing
    source_files: dict[str, tuple[str, str, str]] = {}
    if document is not None:
        bundle.transcript = TranscriptManifest(
            status=document.status,
            source=document.source,
            language=document.language,
            path="transcript.md",
            segments=len(document.segments),
        )
        source_files["transcript.md"] = ("transcript", "md", render_transcript(item, document))
        source_files["transcript.json"] = ("transcript", "json", document.model_dump_json(indent=2) + "\n")
    if content is not None:
        bundle.content = content
        source_files["body.md"] = ("body", "md", content.body_md)
        source_files["content.json"] = ("body", "json", content.model_dump_json(indent=2) + "\n")
    if attempting_source:
        effective_document = document
        if effective_document is None and previous:
            effective_document = load_transcript(folder, previous)
        parts = [content_context(bundle.content)] if bundle.content is not None else []
        if effective_document is not None:
            transcript_context = render_context(item, effective_document)
            if isinstance(bundle, BundleV3):
                transcript_context = re.sub(
                    r"(?m)^\[(seg-[A-Za-z0-9_-]+)\]", r'<a id="\1"></a>\n[\1]', transcript_context
                )
            parts.append(transcript_context)
        source_files["context.md"] = ("context", "md", "\n".join(parts))
    # Check all source destinations before replacing any of them.
    for name, (kind, _, _) in source_files.items():
        _check_writable(folder, bundle, name, kind=kind, force=force)
    if "context.md" in source_files:
        fingerprint = hashlib.sha256(source_files["context.md"][2].encode()).hexdigest()
        if previous and previous.context_fingerprint != fingerprint and bundle.analysis.status == AnalysisStatus.READY:
            bundle.analysis.status = AnalysisStatus.STALE
            if STALE_WARNING not in bundle.processing.warnings:
                bundle.processing.warnings.append(STALE_WARNING)
        bundle.context_fingerprint = fingerprint
    folder.mkdir(parents=True, exist_ok=True)
    for name, (kind, format, body) in source_files.items():
        write_artifact(
            folder,
            bundle,
            path=name,
            kind=kind,
            format=format,
            content=body,
            provider=bundle.processing.provider,
            force=force,
        )
    save_bundle(folder, bundle)
    return bundle
