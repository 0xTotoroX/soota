from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, FiniteFloat, model_validator


class TranscriptMode(StrEnum):
    AUTO = "auto"
    CAPTIONS_ONLY = "captions-only"
    ASR = "asr"
    NONE = "none"


class TranscriptSource(StrEnum):
    UNKNOWN = "unknown"
    OFFICIAL = "official"
    AUTO = "auto"
    ASR = "asr"


class TranscriptStatus(StrEnum):
    READY = "ready"
    MISSING = "missing"
    UNKNOWN = "unknown"
    PENDING = "pending"
    NOT_REQUESTED = "not_requested"


class AnalysisStatus(StrEnum):
    NOT_REQUESTED = "not_requested"
    READY = "ready"
    PENDING = "pending"
    INVALID = "invalid"
    STALE = "stale"


class SourceRef(BaseModel):
    platform: str
    kind: str
    id: str
    url: str
    canonical_url: str

    @property
    def cache_identity(self) -> str:
        return f"{self.platform}:{self.kind}:{self.id}"


class CapabilitySet(BaseModel):
    inspect: bool = False
    captions: bool = False
    audio: bool = False
    catalog: bool = False
    pagination: bool = False
    article: bool = False


class MediaItem(BaseModel):
    source: SourceRef
    title: str | None = None
    creator: str | None = None
    published_at: datetime | None = None
    duration_seconds: int | None = None
    language: str | None = None
    text: str | None = None
    provenance: dict[str, Any] = Field(default_factory=dict)
    extras: dict[str, str] = Field(default_factory=dict)


class CaptionTrack(BaseModel):
    language: str
    name: str | None = None
    kind: TranscriptSource = TranscriptSource.OFFICIAL


class TranscriptSegment(BaseModel):
    segment_id: str
    start_ms: int | None
    end_ms: int | None
    text: str
    language: str | None = None
    source: TranscriptSource


class TranscriptDocument(BaseModel):
    status: TranscriptStatus
    source: TranscriptSource | None = None
    language: str | None = None
    segments: list[TranscriptSegment] = Field(default_factory=list)
    cache_hit: bool = False


class CaptionPayload(BaseModel):
    """Acquired body plus its wire schema; no normalized subtitle segments."""

    format: str
    body: Any


class CaptionAcquisition(BaseModel):
    """READY means a body was acquired; conversion still has to validate it."""

    status: TranscriptStatus
    source: TranscriptSource | None = None
    language: str | None = None
    tracks: list[CaptionTrack] = Field(default_factory=list)
    payload: CaptionPayload | None = None
    cache_hit: bool = Field(default=False, exclude=True)
    reason: str | None = None

    @model_validator(mode="before")
    @classmethod
    def accept_legacy_cache(cls, value: Any) -> Any:
        # Old caches already contain normalized data. Keep it as an explicit
        # input schema so the conversion boundary still validates the body.
        if isinstance(value, dict) and "payload" not in value and value.get("segments"):
            return {**value, "payload": {"format": "soota-segments", "body": value["segments"]}}
        return value


class CaptionResult(BaseModel):
    status: TranscriptStatus
    source: TranscriptSource | None = None
    language: str | None = None
    tracks: list[CaptionTrack] = Field(default_factory=list)
    segments: list[TranscriptSegment] = Field(default_factory=list)
    reason: str | None = None


class AudioAsset(BaseModel):
    path: Path
    mime: str | None = None
    duration_seconds: float | None = None
    provider: str | None = None


class RequestRecord(BaseModel):
    endpoint: str
    billed: bool
    cache_hit: bool = False
    estimated_cost_usd: float | None = None
    note: str | None = None
    actual_cost_usd: float | None = None


class TranscriptManifest(BaseModel):
    status: TranscriptStatus
    source: TranscriptSource | None = None
    language: str | None = None
    path: str | None = None
    segments: int = 0


class AnalysisManifest(BaseModel):
    """Legacy derived-artifact metadata; Soota preserves it but never generates analysis."""

    status: AnalysisStatus = AnalysisStatus.NOT_REQUESTED
    path: str | None = None


class ProcessingManifest(BaseModel):
    provider: str = "tikhub"
    cache_hit: bool = False
    estimated_cost_usd: float | None = 0.0
    actual_cost_usd: float | None = 0.0
    request_count: int = 0
    price_basis: str | None = None
    warnings: list[str] = Field(default_factory=list)
    requests: list[RequestRecord] = Field(default_factory=list)


class ContentBundleManifest(BaseModel):
    schema_version: int = 1
    source: SourceRef
    metadata: dict[str, Any] = Field(default_factory=dict)
    transcript: TranscriptManifest
    analysis: AnalysisManifest = Field(default_factory=AnalysisManifest)
    processing: ProcessingManifest = Field(default_factory=ProcessingManifest)
    files: dict[str, str] = Field(default_factory=dict)

    def metadata_from_item(self, item: MediaItem) -> None:
        self.metadata = {
            "title": item.title,
            "creator": item.creator,
            "published_at": item.published_at.isoformat() if item.published_at else None,
            "duration_seconds": item.duration_seconds,
            "language": item.language,
        }


class CommandResult(BaseModel):
    ok: bool
    status: Literal["success", "partial", "error"]
    code: str
    exit_code: int
    message: str
    hint: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


class CatalogPage(BaseModel):
    items: list[MediaItem]
    next_cursor: str | None = None
    title: str | None = None
    total_items: int | None = None
    completeness: Literal["unknown", "paginated"] = "paginated"
    warnings: list[str] = Field(default_factory=list)


class CatalogJob(BaseModel):
    schema_version: Literal[1] = 1
    kind: Literal["catalog"] = "catalog"
    id: str
    created_at: str
    updated_at: str | None = None
    status: Literal["running", "paused", "completed", "failed"] = "running"
    source: SourceRef
    # Defaults preserve persisted v1 TikHub jobs and their cursor semantics.
    provider: Literal["tikhub", "monid", "treg", "opencli", "opencli-mcp"] = "tikhub"
    profile: str | None = None
    completeness: Literal["unknown", "paginated"] = "paginated"
    warnings: list[str] = Field(default_factory=list)
    resolved: bool = False
    output_root: Path
    max_items: int = Field(ge=1)
    max_pages: int = Field(ge=1)
    max_cost: FiniteFloat | None = Field(default=None, ge=0)
    page_size: int = Field(ge=1, le=200)
    incremental: bool = False
    baseline_loaded: bool = False
    known_ids: list[str] = Field(default_factory=list)
    items: list[MediaItem] = Field(default_factory=list)
    title: str | None = None
    cursor: str | None = None
    visited_cursors: list[str | None] = Field(default_factory=list)
    pending_page: CatalogPage | None = None
    exhausted: bool = False
    pages_fetched: int = 0
    requests: list[RequestRecord] = Field(default_factory=list)
    stop_reason: str | None = None
    error: dict[str, Any] | None = None
