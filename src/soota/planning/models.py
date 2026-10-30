"""规划持久化契约。

[INPUT]: 已验证 TaskSpec、用户选择与可复用采集偏好。
[OUTPUT]: Preferences、Preset、Option、Plan；草稿与采集任务分开存储。
[POS]: 只保存规则到预设，单次输入/输出路径仅留在私有 Plan 中。
[PROTOCOL]: 变更时更新此头部，然后检查 AGENTS.md
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, field_validator

from soota.models import TranscriptMode
from soota.tasks.models import TaskRouting, TaskSpec


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Preferences(Record):
    provider: Literal[
        "auto", "opencli-mcp", "opencli", "yt-dlp", "tikhub", "monid", "treg", "weir", "downie", "http", "local"
    ] = "auto"
    fallback: Literal["tikhub"] | None = None
    audio_provider: Literal["auto", "yt-dlp", "downie", "tikhub", "monid", "treg"] | None = None
    language: str = "zh"
    strict_language: bool = False
    transcript_mode: TranscriptMode = TranscriptMode.AUTO
    subtitle_formats: list[Literal["srt", "vtt", "txt", "json"]] = ["srt"]
    input_format: str | None = None
    min_height: int | None = Field(default=None, gt=0)
    allow_silent: bool = False
    max_items: int = Field(default=100, ge=1)
    max_pages: int = Field(default=10, ge=1)
    workers: int = Field(default=1, ge=1, le=8)
    max_bytes: int = Field(default=2 * 1024**3, ge=1)
    max_cost: FiniteFloat | None = Field(default=None, ge=0)
    weir_max_credits: int | None = Field(default=None, ge=0)
    prefer_youtube: bool | None = None
    exclude_promotions: bool | None = None
    routing: TaskRouting

    @classmethod
    def from_spec(cls, spec: TaskSpec) -> Preferences:
        return cls.model_validate(spec.model_dump(include=set(cls.model_fields)))


class Preset(Record):
    name: str = Field(min_length=1, max_length=128)
    revision: int = Field(default=1, ge=1)
    scope: str
    preferences: Preferences
    updated_at: str

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        value = value.strip()
        if not value or any(ord(char) < 32 for char in value):
            raise ValueError("预设名称不能为空或包含控制字符")
        return value


class PresetFile(Record):
    version: Literal[1] = 1
    presets: list[Preset] = Field(default_factory=list)


class Option(Record):
    id: str
    label: str
    spec: TaskSpec
    report: dict[str, Any]
    review_key: str
    preset_name: str | None = None
    preset_revision: int | None = None
    changes: dict[str, Any] = Field(default_factory=dict)


class Plan(Record):
    version: Literal[1] = 1
    id: str
    goal: str
    created_at: str
    scope: str
    options: list[Option]
    selected: str | None = None
    saved_preset: str | None = None
    job_id: str | None = None
