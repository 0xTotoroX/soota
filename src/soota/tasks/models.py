"""Unified task contracts.

[INPUT]: Validated inputs/targets, acquisition settings and execution limits.
[OUTPUT]: TaskSpec (persistent workers/scope), TaskItem and TaskJob checkpoints.
[POS]: tasks data contracts; TaskJob owns private checkpoint/export locks.
[PROTOCOL]: 变更时更新此头部，然后检查 AGENTS.md
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from threading import RLock
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, PrivateAttr, model_validator

from soota.models import TranscriptMode

# Old semantic/output fields are retained only for persisted request compatibility.
# New source tasks are restricted by inputs.parse_outputs and workflow_compat.
OutputKind = Literal[
    "video", "audio", "transcript", "context", "markdown", "images", "data", "note", "article", "analysis", "outline"
]
ItemStatus = Literal["pending", "running", "completed", "partial", "blocked", "waiting_analysis", "failed"]


class InputSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["auto", "url", "file", "bundle", "collection", "operation"] = "auto"
    value: str = ""
    operation: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    expand: bool = False

    @model_validator(mode="after")
    def validate_input(self) -> InputSpec:
        if self.kind == "operation":
            if not self.operation:
                raise ValueError("operation input requires an operation")
        elif not self.value.strip():
            raise ValueError("input value is empty")
        return self


class OutputSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: OutputKind
    required: bool = True


class TaskSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    inputs: list[InputSpec] = Field(min_length=1)
    outputs: list[OutputSpec] = Field(min_length=1)
    output: Path
    provider: str = "auto"
    fallback: str | None = None
    audio_provider: str | None = None
    language: str = "zh"
    strict_language: bool = False
    execution_context: str = ""
    execution_scope: str = Field(default_factory=lambda: uuid4().hex)
    transcript_mode: TranscriptMode = TranscriptMode.AUTO
    subtitle_formats: list[Literal["srt", "vtt", "txt", "json"]] = ["srt"]
    input_format: str | None = None
    min_height: int | None = Field(default=None, gt=0)
    allow_silent: bool = False
    max_items: int = Field(default=100, ge=1)
    max_pages: int = Field(default=10, ge=1)
    workers: int = Field(default=1, ge=1, le=8, strict=True)
    max_bytes: int = Field(default=2 * 1024**3, ge=1)
    max_cost: FiniteFloat | None = Field(default=None, ge=0)
    weir_max_credits: int | None = Field(default=None, ge=0, strict=True)
    force: bool = False
    prefer_youtube: bool | None = None
    exclude_promotions: bool | None = None
    exclude_ranges: list[str] = Field(default_factory=list)
    destination: Path | None = None
    vault: Path | None = None
    subdir: str = "Soota"

    @property
    def wants(self) -> set[str]:
        return {output.kind for output in self.outputs}

    @property
    def required(self) -> set[str]:
        return {output.kind for output in self.outputs if output.required}

    @property
    def text_signature(self) -> str:
        choices = [
            self.language,
            self.strict_language,
            self.transcript_mode.value,
            self.input_format,
            self.prefer_youtube,
            self.exclude_promotions,
            self.exclude_ranges,
        ]
        return hashlib.sha256(json.dumps(choices, sort_keys=True).encode()).hexdigest()

    @model_validator(mode="after")
    def validate_task(self) -> TaskSpec:
        if len(self.outputs) != len(self.wants):
            raise ValueError("duplicate output kinds")
        if self.provider not in {
            "auto",
            "opencli",
            "opencli-mcp",
            "yt-dlp",
            "tikhub",
            "monid",
            "weir",
            "local",
            "http",
            "downie",
        }:
            raise ValueError("unsupported provider")
        if self.destination is not None and self.vault is not None:
            raise ValueError("choose destination or vault")
        if self.fallback not in {None, "tikhub"}:
            raise ValueError("unsupported fallback")
        return self


class StepState(BaseModel):
    status: ItemStatus = "pending"
    provider: str | None = None
    artifacts: list[str] = Field(default_factory=list)
    error: dict[str, Any] | None = None


class TaskItem(BaseModel):
    item_id: str
    input_index: int
    identity: str
    input_fingerprint: str | None = None
    status: ItemStatus = "pending"
    bundle: str | None = None
    steps: dict[str, StepState] = Field(default_factory=dict)
    errors: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    data: dict[str, Any] = Field(default_factory=dict)


class TaskJob(BaseModel):
    # Worker rows are private copies; only checkpoint merging holds this lock.
    _save_lock: RLock = PrivateAttr(default_factory=RLock)
    _export_lock: RLock = PrivateAttr(default_factory=RLock)
    schema_version: Literal[2] = 2
    kind: Literal["content-task"] = "content-task"
    id: str
    created_at: str
    updated_at: str | None = None
    status: str = "running"
    spec_path: str
    items: list[TaskItem] = Field(default_factory=list)
    expanded: bool = False
    enumeration: dict[str, Any] = Field(default_factory=dict)
    total_inputs: int = 0
    warnings: list[str] = Field(default_factory=list)
