"""
[INPUT]: MonidClient, shared TikHub contracts and raw caption models
[OUTPUT]: MonidProvider
[POS]: Acquisition adapter; shared conversion parses its raw payload
[PROTOCOL]: 变更时更新此头部，然后检查 AGENTS.md
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from typing import Any

from soota.config import SootaSettings
from soota.errors import ContractError
from soota.models import CaptionAcquisition, CaptionPayload, MediaItem, RequestRecord, SourceRef, TranscriptSource
from soota.models import TranscriptStatus as Status
from soota.providers.monid.client import APIFY_CAPTIONS, MonidClient, quote_cost
from soota.providers.tikhub.client import TikHubProvider
from soota.providers.tikhub.registry import ApiEndpoint
from soota.storage.cache import CacheStore


class MonidProvider(TikHubProvider):
    name = "monid"

    def __init__(
        self,
        settings: SootaSettings,
        max_cost: float | None = None,
        *,
        purpose: str = "captions",
        client: MonidClient | None = None,
        before_request: Callable[[RequestRecord], None] | None = None,
        run_scope: str | None = None,
    ) -> None:
        self.monid = client or MonidClient(settings, max_cost, before_request=before_request, run_scope=run_scope)
        self.purpose = purpose
        self.settings = settings
        namespace = hashlib.sha256((settings.monid.base_url + self.monid.key).encode()).hexdigest()[:12]
        super().__init__(
            cache=CacheStore(settings.cache_dir / "monid" / namespace / settings.default_language),
            api_key="",
            preferred_language=settings.default_language,
        )
        self.records = self.monid.records
        self.raw: dict[str, Any] | None = None

    def _call(
        self, path: str, params: dict[str, Any], *, billed: bool, method: str = "GET", json_body: Any = None
    ) -> Any:
        inputs: dict[str, Any] = {"queryParams": params}
        if json_body is not None:
            inputs["body"] = json_body
        response = self.monid.run("tikhub", path, inputs)
        return response.get("output")

    def endpoint_price(self, endpoint: ApiEndpoint, *, refresh: bool = False) -> float:
        cost = quote_cost(self.monid.inspect("tikhub", endpoint.path, refresh=refresh))
        self.snapshot.setdefault("endpoints", {})[endpoint.path] = cost
        return cost

    def get_wallet(self) -> dict[str, float]:
        return self.monid.wallet()

    def inspect(self, source: SourceRef) -> MediaItem:
        if source.platform != "youtube" or self.purpose != "captions":
            item = super().inspect(source)
            item.provenance.update(provider="monid", upstream_provider="tikhub")
            return item
        cached = self.cache.get_json(source, "inspect")
        if cached:
            self.records.append(RequestRecord(endpoint="cache:inspect", billed=False, cache_hit=True))
            return MediaItem.model_validate(cached["item"])
        row, run_id = self._youtube_row(source)
        item = MediaItem(
            source=source,
            title=row.get("title"),
            creator=row.get("channel_name"),
            duration_seconds=row.get("duration_seconds"),
        )
        item.provenance.update(provider="monid", upstream_provider="apify", endpoint=APIFY_CAPTIONS, run_id=run_id)
        self.cache.put_json(source, "inspect", {"item": item.model_dump(mode="json")})
        return item

    def _youtube_row(self, source: SourceRef) -> tuple[dict[str, Any], str | None]:
        if self.raw is None:
            body: dict[str, Any] = {"youtube_url": source.canonical_url, "include_transcript_text": True}
            language = self.preferred_language.split("-")[0].lower()
            if re.fullmatch("[a-z]{2}", language):
                body["language"] = language
            self.raw = self.monid.run("apify", APIFY_CAPTIONS, {"body": body}, reuse=True)
        rows = self.raw.get("output")
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
            raise ContractError("Monid 单视频字幕必须返回一条对象记录。")
        row = rows[0]
        if row.get("video_id") != source.id:
            raise ContractError("Monid 返回字幕的视频 ID 与输入不一致。")
        return row, self.raw.get("runId")

    def _youtube_captions(self, item: MediaItem) -> CaptionAcquisition:
        row, run_id = self._youtube_row(item.source)
        item.provenance.update(provider="monid", upstream_provider="apify", endpoint=APIFY_CAPTIONS, run_id=run_id)
        if row.get("error_category") == "transcripts_disabled" and row.get("transcript") == []:
            return CaptionAcquisition(status=Status.MISSING, reason="transcripts_disabled")
        if row.get("status") == "error" or not row.get("transcript"):
            return CaptionAcquisition(status=Status.UNKNOWN, reason=str(row.get("error_category") or "empty_body"))
        auto = row.get("is_auto_generated")
        kind = (
            TranscriptSource.AUTO
            if auto is True
            else TranscriptSource.OFFICIAL
            if auto is False
            else TranscriptSource.UNKNOWN
        )
        language = row.get("language") or None
        return CaptionAcquisition(
            status=Status.READY,
            source=kind,
            language=language,
            payload=CaptionPayload(format="youtube-transcript", body=row["transcript"]),
        )
