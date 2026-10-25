from __future__ import annotations

import hashlib
import json
import math
import shutil
import sys
from pathlib import Path
from typing import Any

from soota.errors import InputError, SourceError
from soota.media.models import MediaAsset, MediaKind, MediaStream, MediaVerification, positive_number
from soota.platform_support import WINDOWS
from soota.providers.process import ProcessResult, run_process

IMAGE_CODECS = {"png": "png", "mjpeg": "jpg", "jpeg2000": "jp2", "webp": "webp", "gif": "gif", "bmp": "bmp"}
IMAGE_DEMUXERS = {"image2", "image2pipe", "png_pipe", "jpeg_pipe", "jpegls_pipe", "webp_pipe", "gif", "bmp_pipe"}


def command(
    argv: list[str], *, timeout: float, input_bytes: bytes | None = None, limit_file_bytes: int | None = None
) -> ProcessResult:
    """Do not expose argv, stderr, or raw exception messages: they may contain signed URLs."""
    if limit_file_bytes is not None:
        if type(limit_file_bytes) is not int or limit_file_bytes <= 0:
            raise InputError("媒体字节上限必须是正整数。")
        if WINDOWS:
            raise SourceError(
                "Windows 暂不能为外部媒体工具提供硬文件字节上限，未启动下载或转换。",
                hint="可读取已有媒体或直接 HTTP 文件；需要外部下载/转换时请使用 macOS/Linux。",
                details={"reason": "file_limit_unavailable", "dispatched": False},
            )
        # The inherited OS limit also bounds downloads with unknown Content-Length
        # and FFmpeg outputs, not just yt-dlp's advisory --max-filesize check.
        guard = (
            "import os,resource,sys; size=int(sys.argv[1]); "
            "resource.setrlimit(resource.RLIMIT_FSIZE,(size,size)); os.execv(sys.argv[2],sys.argv[2:])"
        )
        argv = [sys.executable, "-c", guard, str(limit_file_bytes), *argv]
    try:
        result = run_process(argv, timeout=timeout, max_bytes=8_388_608, input_bytes=input_bytes)
    except Exception:
        raise SourceError("媒体工具执行失败或超时。", details={"stage": "media_process"}) from None
    if result.returncode:
        raise SourceError("媒体工具未成功完成。", details={"exit_code": result.returncode})
    return result


def binary(name: str) -> str:
    found = shutil.which(name)
    if found is None:
        raise SourceError(f"媒体处理需要 {name}，当前未找到程序。")
    return found


def file_hash(path: Path, max_bytes: int) -> tuple[str, int]:
    digest = hashlib.sha256()
    count = 0
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                count += len(chunk)
                if count > max_bytes:
                    raise SourceError("媒体文件超过字节上限。")
                digest.update(chunk)
    except OSError:
        raise SourceError("媒体文件无法读取。") from None
    if count == 0:
        raise SourceError("媒体文件为空。")
    return digest.hexdigest(), count


def inspect_media(path: Path, *, timeout: float) -> tuple[str, list[MediaStream], float | None]:
    result = command(
        [
            binary("ffprobe"),
            "-v",
            "error",
            "-protocol_whitelist",
            "file,pipe",
            "-show_entries",
            "format=format_name,duration:stream=index,codec_type,codec_name,duration,width,height:stream_disposition=attached_pic",
            "-of",
            "json",
            str(path),
        ],
        timeout=timeout,
    )
    try:
        raw = json.loads(result.stdout)
        streams = [_stream(value) for value in raw["streams"]]
        fmt = str(raw["format"]["format_name"])
        duration = positive_number(raw["format"].get("duration"))
        if not fmt or not streams:
            raise ValueError("empty probe")
        duration = duration or max((s.duration_seconds or 0 for s in streams), default=0) or None
        return fmt, streams, duration
    except (ValueError, KeyError, TypeError, AttributeError):
        raise SourceError("文件未被识别为有效媒体。") from None


def _stream(value: dict[str, Any]) -> MediaStream:
    return MediaStream(
        index=int(value["index"]),
        type=value["codec_type"],
        codec=value.get("codec_name"),
        duration_seconds=positive_number(value.get("duration")),
        width=value.get("width"),
        height=value.get("height"),
        attached_picture=bool((value.get("disposition") or {}).get("attached_pic")),
    )


def infer_kind(fmt: str, streams: list[MediaStream]) -> MediaKind:
    if set(fmt.split(",")) & IMAGE_DEMUXERS:
        return "image"
    if any(s.type == "video" and not s.attached_picture for s in streams):
        return "video"
    if any(s.type == "audio" for s in streams):
        return "audio"
    raise SourceError("媒体没有可用的音频、视频或图片内容。")


def extension(asset: MediaAsset) -> str:
    if asset.kind == "image":
        return IMAGE_CODECS.get(asset.streams[0].codec or "", "image")
    formats = set(asset.format.split(","))
    if "mp4" in formats or "mov" in formats:
        return "m4a" if asset.kind == "audio" else "mp4"
    if "matroska" in formats or "webm" in formats:
        return "mka" if asset.kind == "audio" else "mkv"
    for name in ("mp3", "wav", "flac", "ogg", "aac", "avi", "mpegts", "mpeg"):
        if name in formats:
            return "ts" if name == "mpegts" else name
    return "media"


def validate_limits(max_bytes: int, max_duration_seconds: float | None, timeout: float) -> None:
    if type(max_bytes) is not int or max_bytes <= 0:
        raise InputError("媒体字节上限必须是正整数。")
    if max_duration_seconds is not None and positive_number(max_duration_seconds) is None:
        raise InputError("媒体时长上限必须是有限正数。")
    if not math.isfinite(timeout) or timeout <= 0:
        raise InputError("媒体超时必须是有限正数。")


def verify_media(
    path: Path,
    *,
    kind: MediaKind | None,
    max_bytes: int,
    max_duration_seconds: float | None,
    timeout: float,
    expected_duration: float | None,
    min_height: int | None,
    allow_silent: bool,
    source_provider: str | None,
) -> MediaAsset:
    path = path.resolve()
    if not path.is_file():
        raise SourceError("媒体文件不存在。")
    before = path.stat()
    if before.st_size > max_bytes:
        raise SourceError("媒体文件超过字节上限。")
    fmt, streams, duration = inspect_media(path, timeout=timeout)
    actual_kind = infer_kind(fmt, streams)
    kind = kind or actual_kind
    if kind != actual_kind:
        raise SourceError("实际媒体类型与请求不符。", details={"expected": kind, "actual": actual_kind})
    video = [s for s in streams if s.type == "video" and not s.attached_picture]
    audio = [s for s in streams if s.type == "audio"]
    if kind == "video" and not allow_silent and not audio:
        raise SourceError("视频缺少音轨；未声明允许无声视频。")
    if kind == "image" and (audio or not video or any(s.codec not in IMAGE_CODECS for s in video)):
        raise SourceError("文件不是支持的图片格式。")
    if kind in {"video", "image"} and any(not s.width or not s.height for s in video):
        raise SourceError("媒体画面尺寸无效。")
    if min_height is not None:
        if min_height <= 0 or kind != "video":
            raise InputError("最低画面高度只适用于视频，且必须大于零。")
        if not video or max(s.height or 0 for s in video) < min_height:
            raise SourceError("视频未达到要求的最低画面高度。")
    if max_duration_seconds is not None and duration is not None and duration > max_duration_seconds:
        raise SourceError("媒体文件超过时长上限。")
    decoded_durations = []
    for stream in streams:
        if stream.type not in {"video", "audio"} or stream.attached_picture:
            continue
        decoded = _decode_stream(path, stream.index, timeout)
        if kind != "image":
            if decoded is None:
                raise SourceError("媒体轨没有可解码的有效时长。")
            decoded_durations.append(decoded)
            if stream.duration_seconds is None:
                stream.duration_seconds = decoded
    duration = duration or max(decoded_durations, default=0) or None
    if max_duration_seconds is not None and any(value > max_duration_seconds for value in decoded_durations):
        raise SourceError("媒体文件超过时长上限。")
    verification = _duration_check(streams, duration, expected_duration, kind)
    if expected_duration is not None:
        for decoded_duration in decoded_durations:
            _duration_check([], decoded_duration, expected_duration, kind)
    digest, size = file_hash(path, max_bytes)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
        raise SourceError("验证期间媒体文件发生变化，未接受该结果。")
    return MediaAsset(
        path=path,
        kind=kind,
        format=fmt,
        size=size,
        sha256=digest,
        duration_seconds=duration,
        streams=streams,
        verification=verification,
        source_provider=source_provider,
    )


def _duration_check(
    streams: list[MediaStream], duration: float | None, expected: float | None, kind: MediaKind
) -> MediaVerification:
    if expected is None:
        return MediaVerification()
    if kind == "image" or positive_number(expected) is None:
        raise InputError("期望时长必须是音视频的有限正数。")
    # A small absolute allowance covers container rounding and codec padding;
    # the capped relative allowance does not forgive minutes missing from long media.
    tolerance = min(3.0, max(0.5, expected * 0.01))
    durations = [s.duration_seconds for s in streams if s.type in {"audio", "video"} and not s.attached_picture]
    for measured in [duration, *[d for d in durations if d is not None]]:
        if measured is None or abs(measured - expected) > tolerance:
            raise SourceError(
                "媒体时长与来源不符，可能不完整。",
                details={"expected_seconds": expected, "actual_seconds": measured, "tolerance_seconds": tolerance},
            )
    return MediaVerification(
        expected_duration_seconds=expected,
        duration_checked=True,
        duration_tolerance_seconds=tolerance,
        completeness="duration_matched",
    )


def _decode_stream(path: Path, index: int, timeout: float) -> float | None:
    result = command(
        [
            binary("ffmpeg"),
            "-nostdin",
            "-v",
            "error",
            "-xerror",
            "-abort_on",
            "empty_output_stream",
            "-err_detect",
            "explode",
            "-protocol_whitelist",
            "file,pipe",
            "-i",
            str(path),
            "-map",
            f"0:{index}",
            "-progress",
            "pipe:1",
            "-nostats",
            "-f",
            "null",
            "-",
        ],
        timeout=timeout,
    )
    values = [line.split("=", 1)[1] for line in result.stdout.splitlines() if line.startswith("out_time_us=")]
    value = positive_number(values[-1]) if values else None
    return value / 1_000_000 if value is not None else None
