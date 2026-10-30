from __future__ import annotations

import hashlib
import json
import math
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import httpx

from soota import TIKHUB_SDK_VERSION
from soota.billing import PaymentGate
from soota.errors import AuthBudgetError, ContractError, SootaError, SourceError, TranscriptError
from soota.models import (
    AudioAsset,
    CapabilitySet,
    CaptionAcquisition,
    CaptionPayload,
    CaptionTrack,
    CatalogPage,
    MediaItem,
    RequestRecord,
    SourceRef,
    TranscriptSource,
    TranscriptStatus,
)
from soota.providers.quotes import cached_quote, namespace
from soota.providers.tikhub.capability import capabilities_for
from soota.providers.tikhub.catalog import (
    YOUTUBE_CHANNEL_ID,
    catalog_endpoint,
    catalog_params,
    is_channel_id,
    parse_catalog_page,
)
from soota.providers.tikhub.normalize import (
    bilibili_caption_tracks,
    bilibili_empty_tracks_confirmed,
    bilibili_media,
    pick_audio_url,
    prefer_language,
    track_kind_from_bili,
    unwrap,
    youtube_caption_content,
    youtube_caption_tracks,
    youtube_media,
)
from soota.providers.tikhub.registry import ApiEndpoint, load_registry
from soota.secrets import load_tikhub_api_key, redact_url
from soota.storage.cache import CacheStore
from soota.storage.task_budget import reserve_active_task

YOUTUBE_INFO = load_registry().resolve("youtube.web.get_video_info").path
YOUTUBE_CAPTIONS = load_registry().resolve("youtube.web.get_video_captions").path
YOUTUBE_CAPTIONS_RESULT = load_registry().resolve("youtube.web.get_video_captions_result").path
YOUTUBE_STREAMS = load_registry().resolve("youtube.web.get_video_streams").path
BILI_ONE_V3 = load_registry().resolve("bilibili.web.fetch_one_video").path
BILI_SUBTITLE = load_registry().resolve("bilibili.web.fetch_video_subtitle").path
BILI_PLAYURL = load_registry().resolve("bilibili.web.fetch_video_playurl").path
ENDPOINT_INFO = "/api/v1/tikhub/user/get_endpoint_info"

SNAPSHOT_PATH = Path(__file__).with_name("price_snapshot.json")


def load_price_snapshot() -> dict[str, Any]:
    payload = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ContractError("价格快照不是对象。")
    return cast(dict[str, Any], payload)


class TikHubProvider:
    name = "tikhub"

    def __init__(
        self,
        *,
        cache: CacheStore,
        api_key: str | None = None,
        base_url: str = "https://api.tikhub.io",
        timeout: float = 60.0,
        requester: Callable[..., Any] | None = None,
        http_get: Callable[[str], httpx.Response] | None = None,
        preferred_language: str = "zh",
        sleep: Callable[[float], None] = time.sleep,
        before_request: Callable[[RequestRecord], None] | None = None,
        max_retries: int = 2,
        payment_gate: PaymentGate | None = None,
        runtime_python: str | None = None,
    ) -> None:
        self.runtime_python = runtime_python
        self.cache = cache
        self.api_key = api_key if api_key is not None else load_tikhub_api_key()
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.preferred_language = preferred_language
        self._requester = requester
        self._http_get = http_get
        self._client: Any = None
        self._sleep = sleep
        self.before_request = before_request
        self.payment_gate = payment_gate
        self.max_retries = 0 if payment_gate is not None else max_retries
        self.records: list[RequestRecord] = []
        self.snapshot = load_price_snapshot()

    def recognize(self, url: str) -> SourceRef | None:
        from soota.routing import recognize

        try:
            source = recognize(url)
        except (SourceError, Exception):
            return None
        if source.platform in {"youtube", "bilibili"}:
            return source
        return None

    def capabilities(self, source: SourceRef) -> CapabilitySet:
        return capabilities_for(source)

    def resolve_catalog_source(self, source: SourceRef) -> SourceRef:
        catalog_endpoint(source)
        if source.platform != "youtube" or is_channel_id(source.id):
            return source
        # Handles and legacy /c/name aliases can contain slashes; keep them out of cache paths.
        ref = source.model_copy(update={"id": "ref-" + hashlib.sha256(source.id.encode()).hexdigest()[:20]})
        cached = self.cache.get_json(ref, "catalog_source")
        if cached:
            self.records.append(RequestRecord(endpoint="cache:catalog_source", billed=False, cache_hit=True))
            return SourceRef.model_validate(cached)
        payload = self._call(YOUTUBE_CHANNEL_ID, {"channel_url": source.canonical_url}, billed=True)
        data = unwrap(payload)
        channel_id = data.get("channel_id")
        if not isinstance(channel_id, str) or not is_channel_id(channel_id):
            raise ContractError("YouTube 频道解析结果缺少有效 channel_id。")
        url = f"https://www.youtube.com/channel/{channel_id}"
        resolved = source.model_copy(update={"id": channel_id, "url": url, "canonical_url": url})
        self.cache.put_json(ref, "catalog_source", resolved.model_dump(mode="json"))
        return resolved

    def list_items(self, source: SourceRef, cursor: str | None = None, *, page_size: int = 100) -> CatalogPage:
        path = catalog_endpoint(source)
        params = catalog_params(source, cursor, page_size, self.preferred_language)
        payload = self._call(path, params, billed=True)
        return parse_catalog_page(source, payload, cursor, page_size)

    def endpoint_price(self, endpoint: ApiEndpoint, *, refresh: bool = False) -> float:
        def fetch() -> dict[str, Any]:
            payload = self._call(ENDPOINT_INFO, {"endpoint": endpoint.path}, billed=False)
            data = unwrap(payload)
            if data.get("endpoint_uri") != endpoint.path:
                raise ContractError("TikHub 返回了不同接口的价格信息。")
            price = data.get("endpoint_cost")
            if isinstance(price, bool) or not isinstance(price, (int, float)) or not math.isfinite(price) or price < 0:
                raise AuthBudgetError("该接口价格未知。")
            return {"estimated_cost_usd": float(price)}

        detail = (
            cached_quote(
                self.payment_gate.settings,
                "tikhub",
                namespace(self.base_url, self.api_key or ""),
                endpoint.path,
                fetch,
                refresh=refresh,
            )
            if self.payment_gate is not None
            else fetch()
        )
        price = float(detail["estimated_cost_usd"])
        self.snapshot.setdefault("endpoints", {})[endpoint.path] = price
        return price

    def query(
        self, endpoint: ApiEndpoint, params: dict[str, Any], body: Any = None, *, allow_auxiliary: bool = False
    ) -> Any:
        if not endpoint.callable and not allow_auxiliary:
            raise SourceError("该接口不属于 Soota 的内容查询能力。")
        payload = self._call(endpoint.path, params, billed=True, method=endpoint.method, json_body=body)
        if isinstance(payload, dict) and payload.get("code", 200) not in {200, 0, "200", None}:
            if payload.get("code") in {401, 402, 403, "401", "402", "403"}:
                raise AuthBudgetError("TikHub 返回了认证或余额错误。")
            raise ContractError("TikHub 返回了业务错误。", details={"code": str(payload.get("code"))})
        return payload

    def estimate_requests(self, source: SourceRef, *, need_captions: bool, need_audio: bool) -> list[str]:
        caps = self.capabilities(source)
        endpoints: list[str] = []
        if source.platform == "youtube" and source.kind == "video":
            endpoints.append(YOUTUBE_INFO)
            if need_captions and caps.captions:
                endpoints.extend([YOUTUBE_CAPTIONS, YOUTUBE_CAPTIONS])
            if need_audio and caps.audio:
                endpoints.append(YOUTUBE_STREAMS)
        elif source.platform == "bilibili" and source.kind in {"video", "shortlink"}:
            endpoints.append(BILI_ONE_V3)
            if need_captions and caps.captions:
                endpoints.append(BILI_SUBTITLE)
            if need_audio and caps.audio:
                endpoints.append(BILI_PLAYURL)
        return endpoints

    def estimate_cost(self, endpoints: list[str]) -> tuple[float | None, str]:
        prices = self.snapshot.get("endpoints", {})
        total = 0.0
        known = True
        for path in endpoints:
            price = prices.get(path)
            if price is None:
                known = False
            else:
                total += float(price)
        basis = f"snapshot:{self.snapshot.get('as_of')}"
        if not endpoints:
            return (0.0, basis)
        return (total if known else None, basis)

    def get_wallet(self) -> dict[str, float] | None:
        if not self.api_key and self._requester is None:
            return None
        payload = self._call("/api/v1/tikhub/user/get_user_info", {}, billed=False)
        user = payload.get("user_data") if isinstance(payload, dict) else None
        if not isinstance(user, dict):
            raise ContractError("TikHub 用户信息缺少 user_data。")
        wallet: dict[str, float] = {}
        for name in ("balance", "free_credit"):
            value = user.get(name)
            if isinstance(value, bool) or not isinstance(value, (int, float, str)):
                raise ContractError(f"TikHub 用户信息缺少有效 {name}。")
            try:
                number = float(value)
            except ValueError as exc:
                raise ContractError(f"TikHub {name} 无效。") from exc
            if not math.isfinite(number) or number < 0:
                raise ContractError(f"TikHub {name} 无效。")
            wallet[name] = number
        return wallet

    def assess_wallet(self, endpoints: list[str], wallet: dict[str, float]) -> dict[str, Any]:
        prices = self.snapshot.get("endpoints", {})
        free_map = self.snapshot.get("allow_free_credit", {})
        paid_required = 0.0
        freeable_required = 0.0
        unknown = False
        for path in endpoints:
            price = prices.get(path) if isinstance(prices, dict) else None
            if price is None:
                unknown = True
                continue
            amount = float(price)
            allows_free = bool(free_map.get(path, False)) if isinstance(free_map, dict) else False
            if allows_free:
                freeable_required += amount
            else:
                paid_required += amount
        balance = float(wallet.get("balance") or 0)
        free_credit = float(wallet.get("free_credit") or 0)
        shortfall_paid = max(0.0, round(paid_required - balance, 6))
        remaining_paid = max(0.0, balance - paid_required)
        shortfall_freeable = max(0.0, round(freeable_required - remaining_paid - free_credit, 6))
        shortfall = round(shortfall_paid + shortfall_freeable, 6)
        sufficient: bool | None
        if unknown:
            sufficient = None
        else:
            sufficient = shortfall == 0.0
        return {
            "balance": balance,
            "free_credit": free_credit,
            "paid_required_usd": round(paid_required, 6),
            "freeable_required_usd": round(freeable_required, 6),
            "shortfall_usd": None if unknown else shortfall,
            "sufficient": sufficient,
        }

    def inspect(self, source: SourceRef) -> MediaItem:
        cached = self.cache.get_json(source, "inspect")
        if cached:
            self.records.append(RequestRecord(endpoint="cache:inspect", billed=False, cache_hit=True))
            return MediaItem.model_validate(cached["item"])
        payload = self._inspect_payload(source)
        item = youtube_media(source, payload) if source.platform == "youtube" else bilibili_media(source, payload)
        self.cache.put_json(source, "inspect", {"item": item.model_dump(mode="json"), "raw": payload})
        if item.source.id != source.id:
            self.cache.put_json(item.source, "inspect", {"item": item.model_dump(mode="json"), "raw": payload})
        return item

    def get_captions(self, item: MediaItem) -> CaptionAcquisition:
        cached = self.cache.get_json(item.source, "captions")
        if (
            cached
            and cached.get("status") == TranscriptStatus.READY
            and (cached.get("payload") or cached.get("segments"))
        ):
            self.records.append(RequestRecord(endpoint="cache:captions", billed=False, cache_hit=True))
            return CaptionAcquisition.model_validate(cached).model_copy(update={"cache_hit": True})
        if item.source.platform == "youtube":
            result = self._youtube_captions(item)
        else:
            result = self._bilibili_captions(item)
        return result

    def cache_captions(self, item: MediaItem, result: CaptionAcquisition) -> None:
        self.cache.put_json(item.source, "captions", result.model_dump(mode="json"), redact=False)

    def get_audio(self, item: MediaItem) -> AudioAsset:
        dest = self.cache.audio_path(item.source)
        if dest.is_file() and dest.stat().st_size > 0:
            self.records.append(RequestRecord(endpoint="cache:audio", billed=False, cache_hit=True))
            origin = self.cache.get_json(item.source, "audio_origin") or {}
            return AudioAsset(path=dest, provider=origin.get("provider", "unknown"))
        if item.source.platform == "bilibili" and not item.extras.get("cid"):
            item = self.inspect(item.source)
        # Signed transport data stays in memory. A failed explicit TikHub
        # acquisition must not silently switch to a different source.
        payload = self._audio_payload(item)
        url = pick_audio_url(payload)
        self._download(url, dest)
        if not dest.is_file() or dest.stat().st_size == 0:
            raise TranscriptError("TikHub 音频下载结果为空，未自动换源。")
        self.cache.put_json(item.source, "audio_origin", {"provider": self.name})
        return AudioAsset(path=dest, provider=self.name)

    def _inspect_payload(self, source: SourceRef) -> Any:
        if source.platform == "youtube":
            return self._call(YOUTUBE_INFO, {"video_id": source.id, "need_format": True}, billed=True)
        return self._call(BILI_ONE_V3, {"url": source.canonical_url or source.url}, billed=True)

    def _youtube_captions(self, item: MediaItem) -> CaptionAcquisition:
        listed = self._call(
            YOUTUBE_CAPTIONS,
            {"video_id": item.source.id, "format": "srt"},
            billed=True,
        )
        listed = self._await_captions(listed, item.source.id)
        tracks, data = youtube_caption_tracks(listed)
        content = youtube_caption_content(listed, language=None, source_kind=TranscriptSource.OFFICIAL)
        if content.status == TranscriptStatus.READY:
            return content
        if content.status in {TranscriptStatus.MISSING, TranscriptStatus.UNKNOWN} and not tracks:
            return content
        track = prefer_language(tracks or content.tracks, self.preferred_language)
        if track is None:
            return CaptionAcquisition(status=content.status, tracks=tracks, reason=content.reason)
        fetched = self._call(
            YOUTUBE_CAPTIONS,
            {
                "video_id": item.source.id,
                "language_code": track.language,
                "format": "srt",
            },
            billed=True,
        )
        fetched = self._await_captions(fetched, item.source.id)
        return youtube_caption_content(fetched, language=track.language, source_kind=track.kind)

    def _await_captions(self, payload: Any, video_id: str) -> Any:
        data = unwrap(payload) if isinstance(payload, dict) else {}
        job_id = str(data.get("job_id") or "")
        status = str(data.get("status") or "")
        if not job_id or status not in {"processing", "queued", "active"}:
            return payload
        for _ in range(20):
            self._sleep(3)
            result = self._call(
                YOUTUBE_CAPTIONS_RESULT,
                {"job_id": job_id, "format": "srt"},
                billed=False,
            )
            inner = unwrap(result)
            inner_status = str(inner.get("status") or "")
            if inner_status == "completed" or inner.get("content"):
                return result
            if inner_status in {"failed", "error"}:
                raise TranscriptError("YouTube 字幕异步任务失败。", hint="稍后重试同一视频，不要换 endpoint 死磕。")
        raise TranscriptError("YouTube 字幕异步任务超时。", hint="可稍后再 ingest；result 接口不计费。")

    def _bilibili_captions(self, item: MediaItem) -> CaptionAcquisition:
        inspect_cached = self.cache.get_json(item.source, "inspect") or {}
        inspect_raw = inspect_cached.get("raw") if isinstance(inspect_cached, dict) else None
        tracks_raw = bilibili_caption_tracks(inspect_raw) if inspect_raw is not None else []
        payload = None
        if not tracks_raw:
            aid = item.extras.get("aid")
            cid = item.extras.get("cid")
            if aid and cid:
                payload = self._call(BILI_SUBTITLE, {"a_id": aid, "c_id": cid}, billed=True)
                if unwrap(payload).get("need_login_subtitle") is True:
                    raise AuthBudgetError("TikHub 返回 B 站字幕需要登录，未判为无字幕。")
                tracks_raw = bilibili_caption_tracks(payload)
            elif inspect_raw is None:
                payload = self._inspect_payload(item.source)
                tracks_raw = bilibili_caption_tracks(payload)
        if not tracks_raw:
            absent = payload is not None and bilibili_empty_tracks_confirmed(payload)
            return CaptionAcquisition(
                status=TranscriptStatus.MISSING if absent else TranscriptStatus.UNKNOWN,
                reason="empty_track_list" if absent else "track_visibility_unknown",
            )
        official = [t for t in tracks_raw if track_kind_from_bili(t) == TranscriptSource.OFFICIAL]
        chosen = (official or tracks_raw)[0]
        kind = track_kind_from_bili(chosen)
        language = str(chosen.get("lan") or self.preferred_language)
        url = str(chosen.get("subtitle_url") or chosen.get("url"))
        if url.startswith("//"):
            url = "https:" + url
        body = self._get_json(url)
        self.records.append(RequestRecord(endpoint="cdn:bilibili-subtitle", billed=False, note=redact_url(url)))
        return CaptionAcquisition(
            status=TranscriptStatus.READY,
            source=kind,
            language=language,
            tracks=[CaptionTrack(language=language, kind=kind)],
            payload=CaptionPayload(format="bilibili-json", body=body),
        )

    def _audio_payload(self, item: MediaItem) -> Any:
        if item.source.platform == "youtube":
            return self._call(YOUTUBE_STREAMS, {"video_id": item.source.id}, billed=True)
        cid = item.extras.get("cid")
        if not cid:
            raise TranscriptError("Bilibili 音频缺少 cid。", hint="先 inspect 成功后再拉音频。")
        return self._call(BILI_PLAYURL, {"bv_id": item.source.id, "cid": cid}, billed=True)

    def _call(
        self, path: str, params: dict[str, Any], *, billed: bool, method: str = "GET", json_body: Any = None
    ) -> Any:
        if billed and self.payment_gate is not None and self.payment_gate.cap == 0:
            raise AuthBudgetError("--max-cost 0 禁止发送计费接口请求。")
        prices = self.snapshot.get("endpoints", {})
        cost = prices.get(path)
        if billed and self.payment_gate is not None:
            cost = self.endpoint_price(load_registry().resolve(path))
        record = RequestRecord(
            endpoint=path,
            billed=billed,
            cache_hit=False,
            estimated_cost_usd=float(cost) if cost is not None else None,
        )
        if self.payment_gate is not None:
            self.payment_gate.authorize(record, self.get_wallet)
        reserve_active_task(record, self.before_request)
        self.records.append(record)

        def dispatch() -> Any:
            if self._requester is not None:
                if method == "GET":
                    return self._requester(method, path, params)
                return self._requester(method, path, params, json=json_body)
            try:
                client = self._sdk()
                return client._request(method, path, params=params, json=json_body)
            except SootaError:
                raise
            except Exception as exc:
                raise _map_tikhub_error(exc) from exc

        if self.payment_gate is not None and path in {ENDPOINT_INFO, "/api/v1/tikhub/user/get_user_info"}:
            return self.payment_gate.pace_metadata(path, dispatch)
        return dispatch()

    def _sdk(self) -> Any:
        if self._client is not None:
            return self._client
        if not self.api_key:
            raise AuthBudgetError(
                "未找到 TikHub API Key。",
                hint="export TIKHUB_API_KEY 或 Keychain service=soota.tikhub account=api_key。不要把 Key 发给聊天。",
            )
        if self.runtime_python:
            from soota.providers.tikhub.managed_client import ManagedClient

            self._client = ManagedClient(
                self.runtime_python, api_key=self.api_key, base_url=self.base_url, timeout=self.timeout
            )
            return self._client
        from tikhub import TikHub

        self._client = TikHub(
            api_key=self.api_key, base_url=self.base_url, timeout=self.timeout, max_retries=self.max_retries
        )
        if getattr(self._client, "__version__", None) not in {None, TIKHUB_SDK_VERSION}:
            pass
        return self._client

    def _get_json(self, url: str) -> Any:
        if self._http_get is not None:
            response = self._http_get(url)
            return response.json()
        with httpx.Client(timeout=self.timeout, follow_redirects=True) as client:
            response = client.get(url)
            response.raise_for_status()
            return response.json()

    def _download(self, url: str, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".part")
        if self._http_get is not None:
            response = self._http_get(url)
            tmp.write_bytes(response.content)
            tmp.replace(dest)
            self.records.append(RequestRecord(endpoint="cdn:audio", billed=False, note=redact_url(url)))
            return
        timeout = httpx.Timeout(connect=30.0, read=300.0, write=30.0, pool=30.0)
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            with client.stream("GET", url) as response:
                response.raise_for_status()
                with tmp.open("wb") as handle:
                    for chunk in response.iter_bytes():
                        handle.write(chunk)
        tmp.replace(dest)
        self.records.append(RequestRecord(endpoint="cdn:audio", billed=False, note=redact_url(url)))


def _map_tikhub_error(exc: Exception) -> Exception:
    name = type(exc).__name__
    message = str(exc)
    status = getattr(exc, "status_code", None)
    if status == 402 or " HTTP 402" in message or message.startswith("402 "):
        return AuthBudgetError(
            "TikHub 余额不足（HTTP 402）。",
            hint="到 user.tikhub.io 充值后再 ingest 一次。不要重复重试失败的计费接口。",
        )
    if name in {"TikHubAuthError", "TikHubConfigError"}:
        return AuthBudgetError("TikHub 鉴权失败。", hint="检查 TIKHUB_API_KEY 是否有效，且不要把 Key 写入配置文件。")
    if name in {"TikHubPermissionError"}:
        return AuthBudgetError("TikHub 拒绝该请求。", hint="可能是余额不足或权限不足，先在控制台查看额度。")
    if name in {"TikHubRateLimitError"}:
        return SourceError("TikHub 限流。", hint="降低并发，稍后重试同一来源。")
    if name in {"TikHubNotFoundError"}:
        return SourceError("来源不存在或 endpoint 已移除。", hint=message)
    if name in {"TikHubValidationError"}:
        return ContractError("TikHub 响应无法解析。", hint=message)
    if name.startswith("TikHub"):
        return SourceError(f"TikHub 调用失败: {message}", hint="查看 ledger 中的 endpoint，不要无上限重试计费接口。")
    return SourceError(message)
