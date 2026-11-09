"""[INPUT]: 公开来源 URL 或公众号 username。
[OUTPUT]: 规范 SourceRef。
[POS]: 来源身份路由，微信细节委派 wechat.py。
[PROTOCOL]: 变更时更新此头部，然后检查 AGENTS.md
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse, urlunparse

from soota.errors import InputError, SourceError
from soota.models import SourceRef

_YT_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_BV = re.compile(r"BV[0-9A-Za-z]{10}")
_AV = re.compile(r"(?:av|AV)?(\d{1,12})$")
_B23_HOSTS = {"b23.tv", "b23.live", "bili2233.cn"}
_YT_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
    "www.youtu.be",
    "youtube-nocookie.com",
    "www.youtube-nocookie.com",
}
_BL_HOSTS = {
    "bilibili.com",
    "www.bilibili.com",
    "m.bilibili.com",
    "space.bilibili.com",
    *_B23_HOSTS,
}


def _host(netloc: str) -> str:
    host = netloc.split("@")[-1].split(":")[0].lower()
    if host.startswith("www."):
        return host
    return host


def _clean_url(url: str) -> str:
    raw = url.strip()
    if not raw:
        raise InputError("URL 为空。", hint="请提供 YouTube 或 Bilibili 链接。")
    if "://" not in raw:
        raw = "https://" + raw
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"}:
        raise InputError(f"不支持的 URL scheme: {parsed.scheme}", hint="只接受 http/https 链接。")
    if not parsed.netloc:
        raise InputError("无法解析 URL 主机。", hint="检查链接是否完整。")
    return urlunparse(parsed)


def recognize(url: str) -> SourceRef:
    if url.strip().startswith("gh_") or urlparse(url).hostname == "mp.weixin.qq.com":
        from soota.wechat import recognize as recognize_wechat

        return recognize_wechat(url.strip())
    cleaned = _clean_url(url)
    parsed = urlparse(cleaned)
    host = _host(parsed.netloc)
    path = parsed.path or "/"

    if host == "mp.weixin.qq.com":
        from soota.wechat import recognize as recognize_wechat

        return recognize_wechat(cleaned)

    if host in _YT_HOSTS or host.endswith(".youtube.com"):
        return _youtube(cleaned, parsed, host, path)
    if host in _BL_HOSTS or host.endswith(".bilibili.com"):
        return _bilibili(cleaned, parsed, host, path)
    if host in {"x.com", "www.x.com", "twitter.com", "www.twitter.com", "mobile.twitter.com"}:
        parts = [part for part in path.split("/") if part]
        reserved = {"home", "explore", "search", "i", "settings", "messages", "notifications", "intent", "login"}
        if parts and parts[0].lower() not in reserved and re.fullmatch(r"[A-Za-z0-9_]{1,15}", parts[0]):
            handle = parts[0].lower()
            if len(parts) == 1:
                canonical = f"https://x.com/{handle}"
                return SourceRef(platform="twitter", kind="user", id=handle, url=cleaned, canonical_url=canonical)
            if len(parts) == 3 and parts[1] == "status" and re.fullmatch(r"[0-9]+", parts[2]):
                canonical = f"https://x.com/{handle}/status/{parts[2]}"
                return SourceRef(platform="twitter", kind="post", id=parts[2], url=cleaned, canonical_url=canonical)
        raise SourceError("请提供 X 用户主页或帖子链接。")
    raise SourceError(
        f"暂不支持该平台: {host}",
        hint="当前识别 YouTube、Bilibili 和 X；其他平台可查看 api list。",
        details={"host": host},
    )


def _youtube(url: str, parsed: object, host: str, path: str) -> SourceRef:
    query = parse_qs(getattr(parsed, "query", "") or "")
    parts = [p for p in path.split("/") if p]

    if host.endswith("youtu.be") and parts:
        video_id = parts[0]
        if _YT_ID.match(video_id):
            return _yt_video(video_id, url)

    if parts[:1] == ["playlist"] or "list" in query and not query.get("v"):
        playlist = (query.get("list") or [None])[0]
        if playlist:
            return SourceRef(
                platform="youtube",
                kind="playlist",
                id=playlist,
                url=url,
                canonical_url=f"https://www.youtube.com/playlist?list={playlist}",
            )

    if parts and parts[0] == "channel" and len(parts) >= 2:
        channel_id = parts[1]
        return SourceRef(
            platform="youtube",
            kind="channel",
            id=channel_id,
            url=url,
            canonical_url=f"https://www.youtube.com/channel/{channel_id}",
        )
    if parts and parts[0].startswith("@") and len(parts[0]) > 1:
        handle = parts[0]
        return SourceRef(
            platform="youtube",
            kind="channel",
            id=handle,
            url=url,
            canonical_url=f"https://www.youtube.com/{handle}",
        )
    if parts and parts[0] in {"c", "user"} and len(parts) >= 2:
        ident = f"{parts[0]}/{parts[1]}"
        return SourceRef(
            platform="youtube",
            kind="channel",
            id=ident,
            url=url,
            canonical_url=f"https://www.youtube.com/{ident}",
        )

    video_id = (query.get("v") or [None])[0]
    if parts[:1] in (["shorts"], ["embed"], ["live"], ["v"]) and len(parts) >= 2:
        video_id = parts[1]
    if video_id and _YT_ID.match(video_id):
        return _yt_video(video_id, url)

    raise SourceError(
        "无法从 YouTube URL 提取稳定 ID。",
        hint="请使用 watch、shorts、youtu.be 或 channel 链接。",
        details={"url": url},
    )


def _yt_video(video_id: str, url: str) -> SourceRef:
    return SourceRef(
        platform="youtube",
        kind="video",
        id=video_id,
        url=url,
        canonical_url=f"https://www.youtube.com/watch?v={video_id}",
    )


def _bilibili(url: str, parsed: object, host: str, path: str) -> SourceRef:
    query = parse_qs(getattr(parsed, "query", "") or "")
    parts = [p for p in path.split("/") if p]

    if host in _B23_HOSTS:
        code = parts[0] if parts else host
        return SourceRef(
            platform="bilibili",
            kind="shortlink",
            id=code,
            url=url,
            canonical_url=url,
        )

    if host.startswith("space.bilibili.com") or (parts and parts[0] == "space"):
        uid = (parts[0] if parts else None) if host.startswith("space.") else (parts[1] if len(parts) > 1 else None)
        if uid and uid.isdigit():
            return SourceRef(
                platform="bilibili",
                kind="user",
                id=uid,
                url=url,
                canonical_url=f"https://space.bilibili.com/{uid}",
            )

    if parts and parts[0] == "video" and len(parts) >= 2:
        token = parts[1].split("?")[0]
        bv = _BV.search(token)
        if bv:
            bvid = bv.group(0)
            return SourceRef(
                platform="bilibili",
                kind="video",
                id=bvid,
                url=url,
                canonical_url=f"https://www.bilibili.com/video/{bvid}",
            )
        av = _AV.match(token)
        if av:
            aid = av.group(1)
            return SourceRef(
                platform="bilibili",
                kind="video",
                id=f"av{aid}",
                url=url,
                canonical_url=f"https://www.bilibili.com/video/av{aid}",
            )

    bvid_q = (query.get("bvid") or [None])[0]
    if bvid_q and _BV.search(bvid_q):
        bvid = _BV.search(bvid_q).group(0)  # type: ignore[union-attr]
        return SourceRef(
            platform="bilibili",
            kind="video",
            id=bvid,
            url=url,
            canonical_url=f"https://www.bilibili.com/video/{bvid}",
        )

    raise SourceError(
        "无法从 Bilibili URL 提取稳定 ID。",
        hint="请使用 /video/BVxxxx、/video/avxxxx 或 space 主页链接。",
        details={"url": url},
    )
