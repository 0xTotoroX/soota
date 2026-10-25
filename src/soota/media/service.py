from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from soota.config import SootaSettings
from soota.errors import InputError, SourceError
from soota.media.download import checked_url, download_http, download_page, inspect_manifest
from soota.media.models import MediaAsset, MediaDescriptor, MediaKind, positive_number
from soota.media.probe import binary, command, extension, file_hash, validate_limits, verify_media
from soota.storage.atomic import atomic_write

DEFAULT_MAX_BYTES = 2 * 1024**3


class MediaService:
    """Acquire and verify media in a private content-addressed workspace, without routing providers."""

    def __init__(
        self,
        settings: SootaSettings,
        work_dir: Path | None = None,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        max_duration_seconds: float | None = None,
        timeout_seconds: float | None = None,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.settings = settings
        self.root = (work_dir or settings.cache_dir / "media").resolve()
        self.max_bytes = max_bytes
        self.max_duration_seconds = max_duration_seconds
        self.timeout = timeout_seconds if timeout_seconds is not None else settings.audio.timeout_seconds
        self.http_client = http_client
        validate_limits(max_bytes, max_duration_seconds, self.timeout)
        (self.root / "objects").mkdir(parents=True, exist_ok=True)
        (self.root / "operations").mkdir(parents=True, exist_ok=True)
        (self.root / ".staging").mkdir(parents=True, exist_ok=True)

    def verify(
        self,
        path: Path,
        *,
        kind: MediaKind | None = None,
        expected_duration: float | None = None,
        min_height: int | None = None,
        allow_silent: bool = False,
        source_provider: str | None = None,
    ) -> MediaAsset:
        return verify_media(
            path,
            kind=kind,
            max_bytes=self.max_bytes,
            max_duration_seconds=self.max_duration_seconds,
            timeout=self.timeout,
            expected_duration=expected_duration,
            min_height=min_height,
            allow_silent=allow_silent,
            source_provider=source_provider,
        )

    def from_file(
        self,
        path: Path,
        *,
        kind: MediaKind | None = None,
        expected_duration: float | None = None,
        min_height: int | None = None,
        allow_silent: bool = False,
        source_provider: str = "local",
    ) -> MediaAsset:
        path = path.expanduser().resolve()
        if not path.is_file():
            raise InputError("本地媒体文件不存在。")
        digest, _ = file_hash(path, self.max_bytes)
        options = _options(kind, expected_duration, min_height, allow_silent, source_provider)
        key = self._key("file", digest, options)
        if cached := self._cached(key, options):
            return cached
        with self._temporary() as folder:
            copy = Path(folder) / "input.media"
            self._copy(path, copy)
            verified = self.verify(copy, allow_silent=True, source_provider=source_provider)
            if verified.sha256 != digest:
                raise SourceError("复制期间本地媒体发生变化，未接受该结果。")
            if kind == "audio" and verified.kind == "video":
                converted = self.extract_audio(verified, expected_duration=expected_duration)
                return self._publish(converted, key)
            verified = self.verify(copy, **options)
            return self._publish(verified, key)

    def from_page(
        self,
        url: str,
        *,
        kind: MediaKind = "video",
        source_provider: str = "yt-dlp",
        expected_duration: float | None = None,
        min_height: int | None = None,
        allow_silent: bool = False,
    ) -> MediaAsset:
        checked_url(url)
        options = _options(kind, expected_duration, min_height, allow_silent, source_provider)
        scope = [url, self.settings.local.binary, self.settings.local.cookies_browser]
        key = self._key("page", scope, options)
        if cached := self._cached(key, options):
            return cached
        with self._temporary() as folder:
            produced, duration = download_page(
                self.settings,
                url,
                Path(folder),
                kind=kind,
                max_bytes=self.max_bytes,
                timeout=self.timeout,
                min_height=min_height,
                allow_silent=allow_silent,
            )
            options["expected_duration"] = expected_duration if expected_duration is not None else duration
            return self._publish(self.verify(produced, **options), key)

    def from_descriptor(
        self,
        descriptor: MediaDescriptor,
        *,
        kind: MediaKind = "video",
        expected_duration: float | None = None,
        min_height: int | None = None,
        allow_silent: bool = False,
        source_provider: str | None = None,
    ) -> MediaAsset:
        provider = source_provider or descriptor.source_provider
        binary("ffmpeg")
        binary("ffprobe")
        options = _options(kind, expected_duration, min_height, allow_silent, provider)
        if descriptor.path is not None:
            return self.from_file(descriptor.path, **options)
        if descriptor.url is None:
            return self._from_tracks(descriptor, options)
        identity = descriptor.identity or [descriptor.url, descriptor.headers, descriptor.protocol]
        key = self._key("remote", identity, options)
        if cached := self._cached(key, options):
            return cached
        with self._temporary() as folder:
            path = Path(folder) / "download.media"
            if _manifest(descriptor):
                manifest_url, manifest_headers = inspect_manifest(
                    descriptor.url,
                    headers=descriptor.headers,
                    timeout=self.timeout,
                    client=self.http_client,
                )
                info = {"url": manifest_url, "http_headers": manifest_headers}
                path, duration = download_page(
                    self.settings,
                    descriptor.url,
                    Path(folder),
                    kind=kind,
                    max_bytes=self.max_bytes,
                    timeout=self.timeout,
                    allow_silent=allow_silent,
                    min_height=min_height,
                    descriptor=info,
                )
                options["expected_duration"] = expected_duration if expected_duration is not None else duration
            else:
                download_http(
                    descriptor.url,
                    path,
                    headers=descriptor.headers,
                    max_bytes=self.max_bytes,
                    timeout=self.timeout,
                    client=self.http_client,
                )
            # A combined video is a valid source for an audio-only request.
            if kind == "audio":
                detected = self.verify(path, allow_silent=True, source_provider=provider)
                if detected.kind == "video":
                    return self._publish(self.extract_audio(detected, expected_duration=expected_duration), key)
            return self._publish(self.verify(path, **options), key)

    def _from_tracks(self, descriptor: MediaDescriptor, options: dict[str, Any]) -> MediaAsset:
        kind = options["kind"]
        if kind == "image":
            raise InputError("分轨描述不能作为图片输入。")
        if kind == "video" and descriptor.video_url is None:
            raise InputError("媒体描述缺少视频轨。")
        video = audio = None
        if descriptor.video_url and kind != "audio":
            video_descriptor = MediaDescriptor(
                url=descriptor.video_url,
                headers={**descriptor.headers, **descriptor.video_headers},
                source_provider=options["source_provider"],
                protocol=descriptor.protocol,
                identity=f"{descriptor.identity}:video" if descriptor.identity else None,
            )
            video = self.from_descriptor(video_descriptor, **{**options, "kind": "video", "allow_silent": True})
        if descriptor.audio_url:
            audio_descriptor = MediaDescriptor(
                url=descriptor.audio_url,
                headers={**descriptor.headers, **descriptor.audio_headers},
                source_provider=options["source_provider"],
                protocol=descriptor.protocol,
                identity=f"{descriptor.identity}:audio" if descriptor.identity else None,
            )
            audio = self.from_descriptor(audio_descriptor, **{**options, "kind": "audio", "min_height": None})
        if kind == "audio":
            if audio is not None:
                return audio
            if descriptor.video_url:
                combined = descriptor.model_copy(update={"url": descriptor.video_url, "video_url": None})
                combined.headers = {**descriptor.headers, **descriptor.video_headers}
                return self.from_descriptor(combined, **options)
        if video is not None and audio is not None:
            return self.mux(
                video,
                audio,
                expected_duration=options["expected_duration"],
                min_height=options["min_height"],
                source_provider=options["source_provider"],
            )
        if video is not None:
            return self.verify(video.path, **options)
        raise InputError("媒体描述缺少本次请求需要的音视频轨。")

    def extract_audio(
        self,
        asset_or_path: MediaAsset | Path,
        *,
        expected_duration: float | None = None,
        source_provider: str | None = None,
    ) -> MediaAsset:
        original = self._input_asset(asset_or_path)
        if original.kind == "image" or not any(s.type == "audio" for s in original.streams):
            raise SourceError("源文件没有可提取的音轨。")
        expected = expected_duration if expected_duration is not None else original.duration_seconds
        provider = source_provider or original.source_provider
        options = _options("audio", expected, None, False, provider)
        key = self._key("extract-audio", [original.sha256, expected_duration is not None], options)
        if cached := self._cached(key, options):
            return cached
        with self._temporary() as folder:
            output = Path(folder) / "audio.m4a"
            command(
                [
                    binary("ffmpeg"),
                    "-nostdin",
                    "-v",
                    "error",
                    "-xerror",
                    "-protocol_whitelist",
                    "file,pipe",
                    "-i",
                    str(original.path),
                    "-map",
                    "0:a:0",
                    "-vn",
                    "-c:a",
                    "aac",
                    str(output),
                ],
                timeout=self.timeout,
                limit_file_bytes=self.max_bytes,
            )
            verified = self.verify(output, **options)
            if expected_duration is None:
                verified.verification.completeness = "input_duration_matched"
            return self._publish(verified, key)

    def mux(
        self,
        video: MediaAsset | Path,
        audio: MediaAsset | Path,
        *,
        expected_duration: float | None = None,
        min_height: int | None = None,
        source_provider: str | None = None,
    ) -> MediaAsset:
        video_asset, audio_asset = self._input_asset(video), self._input_asset(audio)
        if video_asset.kind != "video" or not any(s.type == "audio" for s in audio_asset.streams):
            raise SourceError("合轨需要有效的视频画面和音轨。")
        expected = expected_duration if expected_duration is not None else video_asset.duration_seconds
        provider = source_provider or video_asset.source_provider
        options = _options("video", expected, min_height, False, provider)
        key = self._key("mux", [video_asset.sha256, audio_asset.sha256, expected_duration is not None], options)
        if cached := self._cached(key, options):
            return cached
        with self._temporary() as folder:
            output = Path(folder) / "video.mkv"
            command(
                [
                    binary("ffmpeg"),
                    "-nostdin",
                    "-v",
                    "error",
                    "-xerror",
                    "-protocol_whitelist",
                    "file,pipe",
                    "-i",
                    str(video_asset.path),
                    "-protocol_whitelist",
                    "file,pipe",
                    "-i",
                    str(audio_asset.path),
                    "-map",
                    f"0:{next(s.index for s in video_asset.streams if s.type == 'video' and not s.attached_picture)}",
                    "-map",
                    f"1:{next(s.index for s in audio_asset.streams if s.type == 'audio')}",
                    "-c",
                    "copy",
                    str(output),
                ],
                timeout=self.timeout,
                limit_file_bytes=self.max_bytes,
            )
            verified = self.verify(output, **options)
            if expected_duration is None:
                verified.verification.completeness = "input_duration_matched"
            return self._publish(verified, key)

    def _input_asset(self, value: MediaAsset | Path) -> MediaAsset:
        path = value.path if isinstance(value, MediaAsset) else value
        provider = value.source_provider if isinstance(value, MediaAsset) else "local"
        verified = self.verify(path, allow_silent=True, source_provider=provider)
        if isinstance(value, MediaAsset) and value.sha256 != verified.sha256:
            raise SourceError("媒体输入已发生变化，不能继续使用旧的验证记录。")
        return verified

    def _temporary(self) -> tempfile.TemporaryDirectory[str]:
        return tempfile.TemporaryDirectory(prefix="media-", dir=self.root / ".staging")

    def _copy(self, source: Path, destination: Path) -> None:
        total = 0
        with source.open("rb") as origin, destination.open("xb") as target:
            while chunk := origin.read(1024 * 1024):
                total += len(chunk)
                if total > self.max_bytes:
                    raise SourceError("媒体文件超过字节上限。")
                target.write(chunk)
            target.flush()
            os.fsync(target.fileno())

    def _key(self, operation: str, identity: object, options: dict[str, Any]) -> str:
        data = [1, operation, identity, options, self.max_bytes, self.max_duration_seconds]
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()

    def _cached(self, key: str, options: dict[str, Any]) -> MediaAsset | None:
        receipt = self.root / "operations" / f"{key}.json"
        if not receipt.is_file() or receipt.stat().st_size > 1_048_576:
            return None
        try:
            saved = MediaAsset.model_validate_json(receipt.read_text(encoding="utf-8"))
            if not saved.path.resolve().is_relative_to((self.root / "objects").resolve()) or saved.path.is_symlink():
                return None
            expected = options["expected_duration"]
            if expected is None:
                expected = saved.verification.expected_duration_seconds
            verified = self.verify(saved.path, **{**options, "expected_duration": expected})
            if verified.sha256 != saved.sha256:
                return None
            verified.verification.completeness = saved.verification.completeness
            return verified.model_copy(update={"cache_hit": True})
        except (ValueError, OSError, SourceError):
            return None

    def _publish(self, asset: MediaAsset, key: str) -> MediaAsset:
        folder = self.root / "objects" / asset.sha256[:2]
        folder.mkdir(parents=True, exist_ok=True)
        dest = folder / f"{asset.sha256}.{extension(asset)}"
        if dest.exists() or dest.is_symlink():
            if dest.is_symlink() or file_hash(dest, self.max_bytes)[0] != asset.sha256:
                raise SourceError("受管产物位置存在不同内容，未覆盖该文件。")
        elif asset.path.resolve().is_relative_to((self.root / ".staging").resolve()):
            # Link is an atomic, no-overwrite publication on the same filesystem.
            with asset.path.open("rb") as handle:
                os.fsync(handle.fileno())
            try:
                os.link(asset.path, dest)
            except FileExistsError:
                if dest.is_symlink() or file_hash(dest, self.max_bytes)[0] != asset.sha256:
                    raise SourceError("产物发布位置出现冲突，未覆盖该文件。") from None
        else:
            with self._temporary() as temporary:
                copy = Path(temporary) / "publish.media"
                self._copy(asset.path, copy)
                if file_hash(copy, self.max_bytes)[0] != asset.sha256:
                    raise SourceError("媒体发布期间文件发生变化。")
                try:
                    os.link(copy, dest)
                except FileExistsError:
                    if dest.is_symlink() or file_hash(dest, self.max_bytes)[0] != asset.sha256:
                        raise SourceError("产物发布位置出现冲突，未覆盖该文件。") from None
        result = asset.model_copy(update={"path": dest})
        atomic_write(self.root / "operations" / f"{key}.json", result.model_dump_json(indent=2))
        return result


def _options(
    kind: MediaKind | None,
    expected_duration: float | None,
    min_height: int | None,
    allow_silent: bool,
    source_provider: str | None,
) -> dict[str, Any]:
    if kind not in {None, "video", "audio", "image"}:
        raise InputError("媒体目标支持 video/audio/image。")
    if expected_duration is not None and (kind == "image" or positive_number(expected_duration) is None):
        raise InputError("期望时长必须是音视频的有限正数。")
    if min_height is not None and (type(min_height) is not int or min_height <= 0 or kind in {"audio", "image"}):
        raise InputError("最低画面高度只适用于视频，且必须是正整数。")
    return {
        "kind": kind,
        "expected_duration": expected_duration,
        "min_height": min_height,
        "allow_silent": allow_silent,
        "source_provider": source_provider,
    }


def _manifest(descriptor: MediaDescriptor) -> bool:
    if descriptor.protocol != "auto":
        return descriptor.protocol in {"hls", "dash"}
    return Path(urlsplit(descriptor.url or "").path).suffix.lower() in {".m3u8", ".mpd"}
