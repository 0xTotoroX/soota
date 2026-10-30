"""treg 获取适配。

[INPUT]: TregClient、TikHub 获取契约及原始字幕模型。
[OUTPUT]: TregProvider。
[POS]: 获取原始正文，统一转换层解析及验证后再缓存。
[PROTOCOL]: 变更时更新此头部，然后检查 AGENTS.md
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from soota.config import SootaSettings
from soota.errors import ContractError
from soota.models import (
    CapabilitySet,
    CaptionAcquisition,
    CaptionPayload,
    MediaItem,
    RequestRecord,
    SourceRef,
    TranscriptSource,
)
from soota.models import TranscriptStatus as Status
from soota.providers.tikhub.client import TikHubProvider
from soota.providers.tikhub.registry import ApiEndpoint
from soota.providers.treg.catalog import CRAWL4AI, caption_endpoint, resolve
from soota.providers.treg.client import TregClient
from soota.storage.cache import CacheStore


class TregProvider(TikHubProvider):
    name = "treg"

    def __init__(
        self,
        settings: SootaSettings,
        max_cost: float | None = None,
        *,
        purpose: str = "captions",
        client: TregClient | None = None,
        before_request: Callable[[RequestRecord], None] | None = None,
        run_scope: str | None = None,
    ) -> None:
        self.treg = client or TregClient(settings, max_cost, before_request=before_request, run_scope=run_scope)
        self.settings, self.purpose = settings, purpose
        self.caption_endpoint = caption_endpoint(settings)
        super().__init__(
            cache=CacheStore(
                settings.cache_dir / "treg" / self.treg.namespace / self.caption_endpoint / settings.default_language
            ),
            api_key="",
            preferred_language=settings.default_language,
        )
        self.records = self.treg.records
        self.raw: dict[str, Any] | None = None
        self.raw_source_id: str | None = None

    def _call(
        self, path: str, params: dict[str, Any], *, billed: bool, method: str = "GET", json_body: Any = None
    ) -> Any:
        return self.treg.run(resolve(path), params=params, body=json_body)

    def endpoint_price(self, endpoint: ApiEndpoint, *, refresh: bool = False) -> float:
        cost = float(self.treg.quote(resolve(endpoint.path), refresh=refresh)["estimated_cost_usd"])
        self.snapshot.setdefault("endpoints", {})[endpoint.path] = cost
        return cost

    def get_wallet(self) -> dict[str, float]:
        return self.treg.wallet()

    def capabilities(self, source: SourceRef) -> CapabilitySet:
        caps = super().capabilities(source)
        return caps if source.platform == "youtube" else caps.model_copy(update={"catalog": False, "pagination": False})

    def inspect(self, source: SourceRef) -> MediaItem:
        if source.platform != "youtube" or self.purpose != "captions":
            item = super().inspect(source)
            item.provenance.update(provider="treg", upstream_provider="tikhub")
            return item
        cached = self.cache.get_json(source, "inspect")
        if cached:
            self.records.append(RequestRecord(endpoint="cache:inspect", billed=False, cache_hit=True))
            return MediaItem.model_validate(cached["item"])
        # Cache metadata only. Caption caching still requires successful conversion.
        row = self._youtube_row(source)
        item = MediaItem(
            source=source,
            title=row.get("title"),
            provenance={
                "provider": "treg",
                "upstream_provider": self.settings.treg.youtube_captions,
                "endpoint": self.caption_endpoint,
            },
        )
        self.cache.put_json(source, "inspect", {"item": item.model_dump(mode="json")})
        return item

    def _youtube_row(self, source: SourceRef) -> dict[str, Any]:
        if self.raw_source_id != source.id:
            self.raw = None
        if self.raw is None:
            if self.caption_endpoint == CRAWL4AI:
                self.raw = self.treg.run(
                    self.caption_endpoint,
                    body={
                        "video": source.canonical_url,
                        "language": self.preferred_language,
                    },
                )
            else:
                self.raw = self.treg.run(
                    self.caption_endpoint,
                    params={
                        "url": source.canonical_url,
                        "language": self.preferred_language.split("-")[0],
                    },
                )
            self.raw_source_id = source.id
        if self.caption_endpoint == CRAWL4AI:
            meta = self.raw.get("meta", {})
            if not isinstance(meta, dict) or meta.get("partial") is True or meta.get("blocked"):
                raise ContractError("treg Crawl4AI 返回受阻或不完整字幕。")
            rows = self.raw.get("rows")
            if not isinstance(rows, list):
                raise ContractError("treg Crawl4AI 字幕缺少 rows。")
            if not rows:
                return {}  # 无目标语言、拒绝访问或无字幕均可能为空，不能确认无轨道。
            if len(rows) != 1 or not isinstance(rows[0], dict) or rows[0].get("video_id") != source.id:
                raise ContractError("treg 字幕视频身份与输入不一致。")
            return dict(rows[0])
        if self.raw.get("videoId") != source.id:
            raise ContractError("treg 字幕视频身份与输入不一致。")
        if self.raw.get("success") is not True:
            raise ContractError("treg ScrapeCreators 未确认字幕请求成功。")
        return self.raw

    def _youtube_captions(self, item: MediaItem) -> CaptionAcquisition:
        row = self._youtube_row(item.source)
        if self.caption_endpoint == CRAWL4AI:
            body = row.get("segments")
            language = row.get("language")
            auto = row.get("is_auto_generated")
            format_name = "crawl4ai-youtube"
        else:
            body = row.get("transcript")
            language, auto = None, None
            format_name = "scrapecreators-youtube"
        if not body:
            return CaptionAcquisition(status=Status.UNKNOWN, reason="empty_or_unconfirmed_captions")
        if language is not None and not isinstance(language, str):
            raise ContractError("treg 字幕语言字段无效。")
        kind = (
            TranscriptSource.AUTO
            if auto is True
            else TranscriptSource.OFFICIAL
            if auto is False
            else TranscriptSource.UNKNOWN
        )
        return CaptionAcquisition(
            status=Status.READY, language=language, source=kind, payload=CaptionPayload(format=format_name, body=body)
        )
