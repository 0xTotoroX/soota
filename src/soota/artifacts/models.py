"""[INPUT]: 来源证据、根内产物路径与持久化 manifest。
[OUTPUT]: v2 兼容模型及显式 raw_root/storage 的 v3 模型。
[POS]: 原文包数据契约，不生成语义内容。
[PROTOCOL]: 变更时更新此头部，然后检查 AGENTS.md
"""

from __future__ import annotations

import hashlib
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, Field, SerializeAsAny, field_validator, model_validator

from soota.models import ContentBundleManifest, SourceRef, TranscriptManifest, TranscriptStatus


def relative_path(value: str) -> str:
    path = PurePosixPath(value)
    if not value or "\\" in value or path.is_absolute() or any(part in {"..", "."} for part in value.split("/")):
        raise ValueError("产物路径必须是内容包内的相对路径。")
    if ":" in value or not path.parts or any(not part for part in value.split("/")) or any(ord(c) < 32 for c in value):
        raise ValueError("产物路径无效。")
    return value


class ContentBlock(BaseModel):
    block_id: str = ""
    kind: Literal["paragraph", "heading", "list", "image", "markdown"] = "paragraph"
    text: str = ""
    level: int = Field(default=2, ge=1, le=6)
    target: str | None = None

    @field_validator("block_id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        if value and (not value.isascii() or any(not (c.isalnum() or c in "-_") for c in value)):
            raise ValueError("正文块 ID 只允许 ASCII 字母、数字、横线和下划线。")
        return value

    @property
    def markdown(self) -> str:
        if self.kind == "heading":
            return f"{'#' * self.level} {self.text}"
        if self.kind == "image":
            return f"![{self.text}]({self.target})" if self.target else self.text
        return self.text


class ContentDocument(BaseModel):
    title: str | None = None
    source: SourceRef
    blocks: list[ContentBlock] = Field(default_factory=list)
    scope: Literal["full", "excerpt", "unknown"] = "unknown"
    body_markdown: str | None = None

    @model_validator(mode="after")
    def identify_blocks(self) -> ContentDocument:
        used: set[str] = set()
        for block in self.blocks:
            if not block.block_id:
                signature = f"{block.kind}\0{block.text}\0{block.target or ''}"
                stem = "para-" + hashlib.sha256(signature.encode()).hexdigest()[:12]
                block.block_id = stem
                suffix = 2
                while block.block_id in used:
                    block.block_id = f"{stem}-{suffix}"
                    suffix += 1
            if block.block_id in used:
                raise ValueError("正文块 ID 重复。")
            used.add(block.block_id)
        return self

    @property
    def body_md(self) -> str:
        if self.body_markdown is not None:
            return self.body_markdown
        return "\n\n".join(block.markdown for block in self.blocks)

    @property
    def has_text(self) -> bool:
        return any(block.text.strip() or block.target for block in self.blocks)


class Artifact(BaseModel):
    path: str
    kind: str
    format: str
    bytes: int = Field(ge=0)
    sha256: str
    provider: str | None = None
    verification: Literal["hash", "media", "unverified"] = "hash"
    completeness: Literal["full", "partial", "unknown"] = "unknown"
    attributes: dict[str, Any] = Field(default_factory=dict)

    _relative_path = field_validator("path")(relative_path)

    @field_validator("sha256")
    @classmethod
    def validate_hash(cls, value: str) -> str:
        if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
            raise ValueError("无效的 SHA-256。")
        return value


class ArtifactV3(Artifact):
    storage: Literal["raw", "control"]


def artifact_storage(path: str, kind: str) -> Literal["raw", "control"]:
    if path in {"content.json", "transcript.json", "context.md", "analysis.json", "analysis-request.json"}:
        return "control"
    return (
        "raw"
        if kind in {"body", "transcript", "transcript-original", "subtitle-export", "image", "audio", "video", "data"}
        else "control"
    )


class Bundle(ContentBundleManifest):
    schema_version: Literal[2, 3] = 2
    transcript: TranscriptManifest = Field(
        default_factory=lambda: TranscriptManifest(status=TranscriptStatus.NOT_REQUESTED)
    )
    content: ContentDocument | None = None
    artifacts: list[SerializeAsAny[Artifact]] = Field(default_factory=list)
    context_fingerprint: str | None = None

    @model_validator(mode="after")
    def unique_artifacts(self) -> Bundle:
        paths = [artifact.path for artifact in self.artifacts]
        if len(paths) != len(set(paths)):
            raise ValueError("产物路径重复。")
        if "manifest.json" in paths or ".soota-export.json" in paths:
            raise ValueError("产物不能使用内容包保留路径。")
        for path in self.files.values():
            relative_path(path)
        if self.content and self.content.source.cache_identity != self.source.cache_identity:
            raise ValueError("正文来源与内容包来源不一致。")
        return self


class BundleV2(Bundle):
    schema_version: Literal[2] = 2


class BundleV3(Bundle):
    schema_version: Literal[3] = 3
    raw_root: Path

    @field_validator("raw_root")
    @classmethod
    def explicit_raw_root(cls, value: Path) -> Path:
        if not value.is_absolute() or value.resolve() != value:
            raise ValueError("v3 原件根必须是无符号链接或越界片段的绝对路径。")
        return value

    @field_validator("artifacts", mode="before")
    @classmethod
    def explicit_storage(cls, value: Any) -> list[ArtifactV3]:
        if not isinstance(value, list):
            raise ValueError("v3 artifacts 必须是数组。")
        return [ArtifactV3.model_validate(a.model_dump() if isinstance(a, Artifact) else a) for a in value]

    @model_validator(mode="after")
    def registered_files(self) -> BundleV3:
        tracked = {a.path for a in self.artifacts}
        for artifact in self.artifacts:
            if not isinstance(artifact, ArtifactV3) or artifact.storage != artifact_storage(
                artifact.path, artifact.kind
            ):
                raise ValueError("v3 原始产物与控制产物的存储根不符合契约。")
            if artifact.path == ".task.lock":
                raise ValueError("产物不能使用任务锁路径。")
        if set(self.files.values()) - tracked - {"manifest.json"}:
            raise ValueError("v3 文件必须登记存储根、大小和 SHA-256。")
        if self.transcript.path and self.transcript.path not in tracked:
            raise ValueError("v3 字幕必须是已登记产物。")
        return self
