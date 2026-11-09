from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from soota.config import SootaSettings
from soota.errors import AuthBudgetError, ContractError, InputError, SourceError
from soota.models import (
    AudioAsset,
    CapabilitySet,
    CaptionAcquisition,
    CaptionPayload,
    MediaItem,
    RequestRecord,
    SourceRef,
    TranscriptSource,
    TranscriptStatus,
)
from soota.providers.opencli_runtime import RuntimeUnavailable
from soota.providers.process import run_process
from soota.routing import recognize
from soota.secrets import redact_any
from soota.storage.cache import CacheStore

CAPTION_FORMAT_PREFERENCE = ("json3", "vtt", "ttml", "srv3", "srv2", "srv1", "srt", "xml", "txt")


def capabilities_for(source: SourceRef) -> CapabilitySet:
    supported = source.platform in {"youtube", "bilibili"} and source.kind == "video"
    return CapabilitySet(inspect=supported, captions=supported, audio=supported)


class YtDlpProvider:
    """yt-dlp metadata and captions for YouTube and Bilibili."""

    name = "yt-dlp"

    def __init__(self, settings: SootaSettings) -> None:
        if "yt-dlp" in settings.routing.disabled_providers:
            raise InputError("yt-dlp 已在来源策略中停用。")
        self.settings = settings
        self.config = settings.local
        scope = hashlib.sha256(
            f"{self.config.binary}|{self.config.cookies_browser}|{settings.default_language}".encode()
        ).hexdigest()[:16]
        self.cache = CacheStore(settings.cache_dir / "yt-dlp" / scope)
        self.legacy_cache = CacheStore(settings.cache_dir / "soota" / scope)
        self.records: list[RequestRecord] = []

    def _run(self, source: SourceRef, args: list[str], operation: str) -> str:
        binary = shutil.which(self.config.binary)
        if not binary:
            raise RuntimeUnavailable("未找到 yt-dlp。", hint="安装 yt-dlp，或通过 SOOTA_YTDLP_BINARY 指定程序。")
        argv = [
            binary,
            "--ignore-config",
            "--no-playlist",
            "--no-progress",
            "--socket-timeout",
            "20",
            "--retries",
            "0",
            "--extractor-retries",
            "0",
        ]
        if operation != "audio":
            argv.append("--skip-download")
        if self.config.cookies_browser:
            argv += ["--cookies-from-browser", self.config.cookies_browser]
        self.records.append(
            RequestRecord(endpoint=f"yt-dlp:{source.platform}/{operation}", billed=False, estimated_cost_usd=0)
        )
        try:
            result = run_process(
                argv + args + ["--", source.canonical_url],
                timeout=self.settings.audio.timeout_seconds if operation == "audio" else self.config.timeout_seconds,
                max_bytes=self.config.max_output_bytes,
            )
        except FileNotFoundError as exc:
            raise RuntimeUnavailable("yt-dlp 程序已不可用。") from exc
        if result.returncode:
            message = result.stderr.lower()
            if any(
                text in message for text in ("429", "403", "sign in", "login", "cookies", "captcha", "private video")
            ):
                raise AuthBudgetError("yt-dlp 遇到登录、Cookie 或访问限制；请检查浏览器状态，不自动切换付费来源。")
            raise SourceError("yt-dlp 采集失败，未自动重试或切换付费来源。", details={"exit_code": result.returncode})
        if "subtitles are only available when logged in" in result.stderr.lower():
            raise AuthBudgetError("B 站字幕需要登录；请检查 yt-dlp 使用的浏览器 Cookie。")
        return result.stdout

    def _cached(self, source: SourceRef, artifact: str) -> dict[str, Any] | None:
        cached = self.cache.get_json(source, artifact)
        if not cached and source.platform == "youtube":
            cached = self.legacy_cache.get_json(source, artifact)
        if cached and 0 <= time.time() - cached.get("timestamp", 0) < self.config.cache_seconds:
            self.records.append(
                RequestRecord(endpoint=f"yt-dlp:cache:{artifact}", billed=False, cache_hit=True, estimated_cost_usd=0)
            )
            return dict(cached["result"])
        return None

    def inspect(self, source: SourceRef, *, include_captions: bool = True) -> MediaItem:
        if not capabilities_for(source).inspect:
            raise SourceError("yt-dlp Provider 当前接入 YouTube/Bilibili 单视频元数据和字幕。")
        cached = self._cached(source, "inspect")
        if cached:
            item = MediaItem.model_validate(cached)
            if item.source.cache_identity != source.cache_identity:
                raise ContractError("yt-dlp 缓存来源不匹配。")
            item.provenance["provider"] = self.name
            old_tracks = item.provenance.get("caption_tracks", {})
            if any(old_tracks.get(kind) for kind in ("official", "auto")):
                item.provenance.setdefault("caption_availability", "present")
            legacy_filtered = (
                include_captions
                and item.provenance.get("caption_availability") == "present"
                and not any(old_tracks.get(k) for k in ("official", "auto"))
                and "caption_formats" not in item.provenance
            )
            if not legacy_filtered and (
                not include_captions
                or item.provenance.get("caption_probe_complete")
                or (item.provenance.get("caption_availability") == "present")
            ):
                return item
        args = ["--dump-single-json", "--simulate"]
        args += (
            ["--write-subs", "--write-auto-subs"] if include_captions else ["--no-write-subs", "--no-write-auto-subs"]
        )
        raw = self._run(source, args, "video")
        try:
            info = json.loads(raw)
            if not isinstance(info, dict):
                raise ValueError("invalid metadata")
            actual_id = str(info.get("id") or "")
            if source.platform == "bilibili":
                actual_id = actual_id.removesuffix("_p1")
                if source.id.startswith("av") and info.get("original_url") == source.canonical_url:
                    resolved = recognize(str(info.get("webpage_url") or ""))
                    if resolved.platform == "bilibili" and resolved.kind == "video" and resolved.id == actual_id:
                        source = resolved.model_copy(update={"url": source.url})
            if actual_id != source.id:
                raise ValueError("source mismatch")
            # Store only selected metadata and track names, never signed URLs or headers.
            tracks: dict[str, list[str]] = {"official": [], "auto": []}
            available: dict[str, dict[str, list[str]]] = {"official": {}, "auto": {}}
            actual_tracks = 0
            complete = include_captions and isinstance(info.get("subtitles"), dict)
            for kind, key in (("official", "subtitles"), ("auto", "automatic_captions")):
                if source.platform == "youtube" and not isinstance(info.get(key), dict):
                    complete = False
                for lang, formats in (info.get(key) or {}).items():
                    if lang in {"live_chat", "danmaku"}:
                        continue
                    actual_tracks += 1
                    supported = [f for f in CAPTION_FORMAT_PREFERENCE if any(v.get("ext") == f for v in formats)]
                    if not supported:
                        continue
                    actual_kind = "auto" if source.platform == "bilibili" and lang.startswith("ai-") else kind
                    tracks[actual_kind].append(lang)
                    available[actual_kind][lang] = supported
            item = MediaItem(
                source=source,
                title=info.get("title"),
                creator=info.get("channel") or info.get("uploader"),
                duration_seconds=int(info["duration"]) if info.get("duration") is not None else None,
                language=info.get("language"),
                published_at=(datetime.strptime(info["upload_date"], "%Y%m%d").replace(tzinfo=UTC)
                              if info.get("upload_date") else None),
                extras={key: str(redact_any(str(value))) for key, value in {
                    "description": info.get("description"), "channelId": info.get("channel_id"),
                    "channel_url": info.get("channel_url"), "uploader_id": info.get("uploader_id"),
                    "uploader_url": info.get("uploader_url"),
                }.items() if value is not None},
                provenance={
                    "provider": self.name,
                    "engine": "yt-dlp",
                    "caption_tracks": tracks,
                    "caption_formats": available,
                    "caption_availability": "present" if actual_tracks else "absent" if complete else "unknown",
                    "caption_probe_complete": complete,
                },
            )
        except (ValueError, TypeError, AttributeError, KeyError, OverflowError) as exc:
            raise ContractError("yt-dlp 元数据格式无效或来源不匹配。") from exc
        self.cache.put_json(source, "inspect", {"timestamp": time.time(), "result": item.model_dump(mode="json")})
        return item

    def get_captions(self, item: MediaItem) -> CaptionAcquisition:
        if not capabilities_for(item.source).captions:
            raise SourceError("yt-dlp Provider 当前接入 YouTube/Bilibili 字幕。")
        cached = self._cached(item.source, "captions")
        if (
            cached
            and cached.get("status") == TranscriptStatus.READY
            and (cached.get("payload") or cached.get("segments"))
        ):
            return CaptionAcquisition.model_validate(cached).model_copy(update={"cache_hit": True})
        tracks = item.provenance.get("caption_tracks", {})
        selected = select_track(tracks, self.settings.default_language)
        if selected is None:
            absent = item.provenance.get("caption_availability") == "absent"
            return CaptionAcquisition(
                status=TranscriptStatus.MISSING if absent else TranscriptStatus.UNKNOWN,
                reason="empty_track_list" if absent else "no_supported_track",
            )
        language, kind = selected
        bili = item.source.platform == "bilibili"
        formats = item.provenance.get("caption_formats", {}).get(kind.value, {}).get(language)
        extension = next((fmt for fmt in CAPTION_FORMAT_PREFERENCE if fmt in formats), None) if formats else None
        extension = extension or ("srt" if bili else "json3")
        # Bilibili AI tracks are exposed by yt-dlp as ordinary subtitles.
        ordinary = bili or kind == TranscriptSource.OFFICIAL
        with tempfile.TemporaryDirectory(prefix="soota-captions-") as directory:
            self._run(
                item.source,
                [
                    "--write-subs" if ordinary else "--no-write-subs",
                    "--no-write-auto-subs" if ordinary else "--write-auto-subs",
                    "--sub-langs",
                    re.escape(language),
                    "--sub-format",
                    extension,
                    "-o",
                    str(Path(directory) / "captions.%(ext)s"),
                ],
                "transcript",
            )
            files = list(Path(directory).glob(f"captions.*.{extension}"))
            if len(files) != 1:
                raise SourceError("yt-dlp 未生成所选字幕文件；未把失败缓存为无字幕。")
            if files[0].stat().st_size > self.config.max_output_bytes:
                raise SourceError("yt-dlp 字幕文件超过大小上限。")
            try:
                text = files[0].read_text(encoding="utf-8-sig")
            except UnicodeError as exc:
                raise ContractError("yt-dlp 字幕不是有效 UTF-8 文本。") from exc
            result = CaptionAcquisition(
                status=TranscriptStatus.READY,
                language=language,
                source=kind,
                payload=CaptionPayload(format=extension, body=text),
            )
        return result

    def cache_captions(self, item: MediaItem, result: CaptionAcquisition) -> None:
        self.cache.put_json(
            item.source, "captions", {"timestamp": time.time(), "result": result.model_dump(mode="json")}, redact=False
        )

    def get_audio(self, item: MediaItem) -> AudioAsset:
        if not capabilities_for(item.source).audio:
            raise SourceError("yt-dlp 音频获取仅接入 YouTube/Bilibili 单视频。")
        dest = self.cache.audio_path(item.source)
        if dest.is_file() and dest.stat().st_size:
            self.records.append(RequestRecord(endpoint="yt-dlp:cache:audio", billed=False, cache_hit=True))
            return AudioAsset(path=dest, provider=self.name)
        if shutil.which("ffmpeg") is None:
            raise RuntimeUnavailable("yt-dlp 音频提取需要 ffmpeg。")
        with tempfile.TemporaryDirectory(prefix="audio-", dir=dest.parent) as directory:
            self._run(
                item.source,
                [
                    "-f",
                    "bestaudio/best",
                    "--extract-audio",
                    "--audio-format",
                    "m4a",
                    "--no-write-subs",
                    "--no-write-auto-subs",
                    "-o",
                    str(Path(directory) / "audio.%(ext)s"),
                ],
                "audio",
            )
            produced = Path(directory) / "audio.m4a"
            if not produced.is_file() or not produced.stat().st_size:
                raise SourceError("yt-dlp 未生成有效音频文件。")
            produced.replace(dest)
        return AudioAsset(path=dest, provider=self.name)


def select_track(tracks: dict[str, Any], preferred: str) -> tuple[str, TranscriptSource] | None:
    candidates = [(lang, TranscriptSource(kind)) for kind in ("official", "auto") for lang in tracks.get(kind, [])]
    if not candidates:
        return None

    def rank(track: tuple[str, TranscriptSource]) -> tuple[int, bool, str]:
        lang, kind = track
        lang = lang.removeprefix("ai-")
        match = 0 if lang == preferred else 1 if lang.split("-")[0] == preferred.split("-")[0] else 2
        return match, kind != TranscriptSource.OFFICIAL, lang

    return min(candidates, key=rank)
