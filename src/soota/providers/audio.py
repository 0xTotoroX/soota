from __future__ import annotations

from collections.abc import Callable

from soota.billing import selected_provider
from soota.config import SootaSettings
from soota.errors import InputError
from soota.models import AudioAsset, MediaItem, RequestRecord, SourceRef
from soota.providers.base import AudioProvider
from soota.providers.downie import DownieProvider
from soota.providers.monid.provider import MonidProvider
from soota.providers.priority import ordered_names, quote_rows
from soota.providers.treg.provider import TregProvider
from soota.providers.ytdlp import YtDlpProvider


def conditional_audio_estimate(
    source: SourceRef, settings: SootaSettings | None = None, name: str = "tikhub"
) -> dict[str, object]:
    from soota.providers.tikhub.client import BILI_ONE_V3, BILI_PLAYURL, YOUTUBE_STREAMS, load_price_snapshot

    if settings is not None and name != "tikhub":
        choices = (
            ordered_names(settings, source, "audio", ["yt-dlp", "tikhub", "monid", "treg"])
            if name == "auto"
            else [name]
        )
        if not choices:
            return {"provider": name, "condition": "confirmed_no_captions", "estimated_cost_usd": None}
        row = quote_rows(settings, source, "audio", [choices[0]])[0]
        return {**row, "condition": "confirmed_no_captions"}
    endpoints = [YOUTUBE_STREAMS] if source.platform == "youtube" else [BILI_ONE_V3, BILI_PLAYURL]
    prices = load_price_snapshot()["endpoints"]
    known = all(isinstance(prices.get(path), (int, float)) for path in endpoints)
    return {
        "provider": "tikhub",
        "condition": "confirmed_no_captions",
        "endpoints": endpoints,
        "estimated_cost_usd": round(sum(prices[path] for path in endpoints), 6) if known else None,
    }


def selected_audio_provider(settings: SootaSettings, explicit: str | None) -> str:
    name = explicit or settings.audio.provider
    if name not in {"auto", "yt-dlp", "downie", "tikhub", "monid", "treg"}:
        raise InputError("--audio-provider 支持 auto/yt-dlp/downie/tikhub/monid/treg。")
    return name


class ExistingAudioProvider:
    def __init__(self, asset: AudioAsset) -> None:
        self.asset = asset
        self.name = asset.provider or "local"
        self.records: list[RequestRecord] = []

    def get_audio(self, item: MediaItem) -> AudioAsset:
        self.records.append(
            RequestRecord(endpoint="local:shared-audio", billed=False, cache_hit=True, estimated_cost_usd=0)
        )
        return self.asset


def audio_provider(
    settings: SootaSettings,
    name: str,
    paid_factory: Callable[[], AudioProvider],
    *,
    asset: AudioAsset | None = None,
    max_cost: float | None = None,
    remaining_cost: Callable[[], float | None] | None = None,
) -> AudioProvider:
    if asset is not None:
        return ExistingAudioProvider(asset)
    selected_provider(settings, name)
    if remaining_cost is not None and name in {"auto", "monid", "treg"}:
        return DeferredAudioProvider(settings, name, paid_factory, remaining_cost)
    if name == "auto":
        return RankedAudioProvider(settings, paid_factory, max_cost)
    if name in {"monid", "treg"}:
        return (TregProvider if name == "treg" else MonidProvider)(settings, max_cost, purpose="audio")
    if name == "yt-dlp":
        return YtDlpProvider(settings)
    if name == "downie":
        return DownieProvider(settings)
    if name == "tikhub":
        return paid_factory()
    raise InputError("无效的音频来源。")


class RankedAudioProvider:
    name = "auto"

    def __init__(
        self, settings: SootaSettings, paid_factory: Callable[[], AudioProvider], max_cost: float | None
    ) -> None:
        self.settings = settings
        self.paid_factory = paid_factory
        self.max_cost = max_cost
        self.records: list[RequestRecord] = []

    def get_audio(self, item: MediaItem) -> AudioAsset:
        choices = ordered_names(
            self.settings, item.source, "audio", ["yt-dlp", "tikhub", "monid", "treg"], max_cost=self.max_cost
        )
        if not choices:
            raise InputError("没有可用音频来源。")
        provider = audio_provider(self.settings, choices[0], self.paid_factory, max_cost=self.max_cost)
        try:
            return provider.get_audio(item)
        finally:
            self.records.extend(provider.records)


class DeferredAudioProvider:
    """Resolve the paid audio budget after caption requests have finished."""

    def __init__(
        self,
        settings: SootaSettings,
        name: str,
        paid_factory: Callable[[], AudioProvider],
        remaining: Callable[[], float | None],
    ) -> None:
        self.settings, self.name = settings, name
        self.paid_factory, self.remaining = paid_factory, remaining
        self.records: list[RequestRecord] = []

    def get_audio(self, item: MediaItem) -> AudioAsset:
        provider = audio_provider(self.settings, self.name, self.paid_factory, max_cost=self.remaining())
        try:
            return provider.get_audio(item)
        finally:
            self.records.extend(provider.records)
