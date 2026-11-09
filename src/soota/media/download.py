"""[INPUT]: HTTP 地址、请求头、时间和字节上限。
[OUTPUT]: 受控流式响应及媒体下载。
[POS]: 共享网络边界；微信可匿名读取错误状态正文，不改变媒体默认校验。
[PROTOCOL]: 变更时更新此头部，然后检查 AGENTS.md
"""

from __future__ import annotations

import json
import shlex
import time
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from soota.config import SootaSettings
from soota.errors import AuthBudgetError, InputError, SourceError
from soota.media.models import MediaKind, positive_number
from soota.media.probe import binary, command


def checked_url(url: str) -> str:
    try:
        if any(ord(char) < 32 or ord(char) == 127 for char in url):
            raise ValueError("URL contains controls")
        parts = urlsplit(url)
        if parts.scheme not in {"https", "http"} or not parts.hostname or parts.username or parts.password:
            raise ValueError("invalid media URL")
    except (ValueError, TypeError):
        raise InputError("媒体地址必须是不含用户凭据的 HTTP/HTTPS 地址。") from None
    return url


def checked_headers(headers: dict[str, str]) -> dict[str, str]:
    if any("\r" in key + value or "\n" in key + value for key, value in headers.items()):
        raise InputError("媒体请求头包含非法换行。")
    return {**headers, "Accept-Encoding": "identity"}


@contextmanager
def http_response(
    url: str,
    *,
    headers: dict[str, str],
    timeout: float,
    client: httpx.Client | None = None,
    anonymous: bool = False,
    accepted_statuses: tuple[int, ...] = (200,),
) -> Iterator[tuple[httpx.Response, dict[str, str]]]:
    checked_url(url)
    request_headers = checked_headers(headers)
    deadline = time.monotonic() + timeout
    connection = (
        nullcontext(client)
        if client is not None
        else httpx.Client(
            timeout=httpx.Timeout(timeout, connect=min(30.0, timeout)),
            follow_redirects=False,
        )
    )
    try:
        with connection as http:
            current_url = url
            for redirect in range(11):
                if anonymous:
                    http.cookies.clear()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise SourceError("媒体下载超过时间上限。")
                with http.stream(
                    "GET", current_url, headers=request_headers, follow_redirects=False, timeout=remaining
                ) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        if redirect == 10 or not response.headers.get("location"):
                            raise SourceError("媒体重定向次数过多或缺少目标。")
                        next_url = str(response.url.join(response.headers["location"]))
                        checked_url(next_url)
                        previous, following = urlsplit(current_url), urlsplit(next_url)
                        if (previous.scheme, previous.netloc) != (following.scheme, following.netloc):
                            request_headers = {
                                key: value
                                for key, value in request_headers.items()
                                if key.lower() in {"user-agent", "accept", "accept-encoding"}
                            }
                        current_url = next_url
                        continue
                    if response.status_code in accepted_statuses:
                        yield response, request_headers
                        return
                    if response.status_code in {401, 403}:
                        raise AuthBudgetError("媒体地址拒绝访问或已经失效；未自动刷新或更换来源。")
                    response.raise_for_status()
                    if response.status_code != 200:
                        raise SourceError("媒体服务器没有返回完整文件。")
                    yield response, request_headers
                    return
    except httpx.HTTPError:
        raise SourceError("媒体传输失败；未自动刷新地址或更换来源。") from None


def response_chunks(response: httpx.Response, *, max_bytes: int, deadline: float) -> Iterator[bytes]:
    declared = _content_length(response.headers.get("content-length"))
    if declared is not None and declared > max_bytes:
        raise SourceError("媒体响应超过字节上限。")
    size = 0
    for chunk in response.iter_bytes():
        if time.monotonic() > deadline:
            raise SourceError("媒体下载超过时间上限。")
        size += len(chunk)
        if size > max_bytes:
            raise SourceError("媒体响应超过字节上限。")
        yield chunk
    if not size:
        raise SourceError("媒体服务器返回空文件。")
    received = response.num_bytes_downloaded if response.headers.get("content-encoding") else size
    if declared is not None and received != declared:
        raise SourceError("媒体响应长度不符，下载可能不完整。")


def download_http(
    url: str,
    dest: Path,
    *,
    headers: dict[str, str],
    max_bytes: int,
    timeout: float,
    client: httpx.Client | None = None,
) -> None:
    deadline = time.monotonic() + timeout
    try:
        with http_response(url, headers=headers, timeout=timeout, client=client) as (response, _):
            with dest.open("xb") as handle:
                for chunk in response_chunks(response, max_bytes=max_bytes, deadline=deadline):
                    handle.write(chunk)
    except OSError:
        raise SourceError("媒体临时文件无法写入。") from None


def inspect_manifest(
    url: str,
    *,
    headers: dict[str, str],
    timeout: float,
    client: httpx.Client | None = None,
) -> tuple[str, dict[str, str]]:
    # Keep potentially signed fragment URLs in memory. A login page must never
    # become an implicit page-extraction request when a manifest was requested.
    deadline = time.monotonic() + timeout
    with http_response(url, headers=headers, timeout=timeout, client=client) as (response, final_headers):
        body = b"".join(response_chunks(response, max_bytes=2_097_152, deadline=deadline))
        valid = body.lstrip().startswith(b"#EXTM3U")
        if not valid:
            try:
                root = ET.fromstring(body)
                valid = root.tag.rsplit("}", 1)[-1] == "MPD"
                if valid and root.get("type") == "dynamic":
                    raise InputError("动态直播清单没有固定完整时长，当前不作为完整媒体下载。")
            except ET.ParseError:
                valid = False
        if not valid:
            raise SourceError("媒体地址返回的不是 HLS 或 DASH 清单；未转入网页提取。")
        return str(response.url), final_headers


def _content_length(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        size = int(value)
        if size < 0:
            raise ValueError("negative length")
        return size
    except ValueError:
        raise SourceError("媒体响应的长度字段无效。") from None


def download_page(
    settings: SootaSettings,
    url: str,
    directory: Path,
    *,
    kind: MediaKind,
    max_bytes: int,
    timeout: float,
    min_height: int | None = None,
    allow_silent: bool = False,
    descriptor: dict[str, Any] | None = None,
) -> tuple[Path, float | None]:
    checked_url(url)
    binary("ffmpeg")
    binary("ffprobe")
    if kind == "image":
        raise InputError("图片直链使用 HTTP 下载；网页中的图片须先由获取层选定。")
    selector = "bestaudio/best" if kind == "audio" else "bv*+ba/b"
    if kind == "video" and allow_silent:
        selector += "/bv"
    if min_height is not None:
        if kind != "video" or min_height <= 0:
            raise InputError("最低画面高度只适用于视频，且必须大于零。")
        selector = f"bv*[height>={min_height}]+ba/b[height>={min_height}]"
        if allow_silent:
            selector += f"/bv[height>={min_height}]"
    argv = [
        binary(settings.local.binary),
        "--ignore-config",
        "--no-cache-dir",
        "--no-playlist",
        "--no-progress",
        "--no-warnings",
        "--output-na-placeholder",
        "null",
        "--socket-timeout",
        "20",
        "--retries",
        "0",
        "--extractor-retries",
        "0",
        "--fragment-retries",
        "0",
        "--abort-on-unavailable-fragments",
        "--abort-on-error",
        "--no-write-info-json",
        "--no-embed-info-json",
        "--no-embed-metadata",
        "--no-write-subs",
        "--no-write-auto-subs",
        "--max-filesize",
        str(max_bytes),
        "-f",
        selector,
        "-o",
        str(directory / "media.%(ext)s"),
        "--print",
        "after_move:%(filepath)j",
        "--print",
        "after_move:%(duration)j",
    ]
    if kind == "audio":
        argv += ["--extract-audio", "--audio-format", "m4a"]
    else:
        argv += ["--merge-output-format", "mkv"]
    if descriptor is None:
        if settings.local.cookies_browser:
            argv += ["--cookies-from-browser", settings.local.cookies_browser]
        argv += ["--", url]
        result = command(argv, timeout=timeout, limit_file_bytes=max_bytes)
    else:
        # Only the verified manifest URL is supplied, never an original webpage
        # or a cached info-json whose download error could trigger re-extraction.
        private_args = ["--force-generic-extractor"]
        for name, value in checked_headers(descriptor.get("http_headers", {})).items():
            private_args += ["--add-headers", f"{name}:{value}"]
        private_args += [checked_url(descriptor["url"])]
        argv += ["--config-locations", "-"]
        result = command(
            argv, timeout=timeout, input_bytes=(shlex.join(private_args) + "\n").encode(), limit_file_bytes=max_bytes
        )
    try:
        lines = result.stdout.strip().splitlines()
        if len(lines) != 2:
            raise ValueError("unexpected tool output")
        produced = Path(json.loads(lines[0])).resolve()
        duration = positive_number(json.loads(lines[1]))
        if produced.parent != directory.resolve() or not produced.is_file() or produced.is_symlink():
            raise ValueError("invalid produced path")
        return produced, duration
    except (ValueError, TypeError, OSError):
        raise SourceError("下载器没有返回任务目录内的最终媒体文件。") from None
