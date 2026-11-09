"""[INPUT]: 已登记目录来源和上游响应。
[OUTPUT]: 接口参数与标准 CatalogPage。
[POS]: YouTube/Bilibili/微信全号目录归一，不负责支付或个人状态。
[PROTOCOL]: 变更时更新此头部，然后检查 AGENTS.md
"""

from __future__ import annotations

import re
from typing import Any

from soota.errors import ContractError, SourceError
from soota.models import CatalogPage, MediaItem, SourceRef
from soota.providers.tikhub.normalize import bilibili_media, unwrap
from soota.providers.tikhub.registry import load_registry

YOUTUBE_CHANNEL_ID = load_registry().resolve("youtube.web.get_channel_id").path
YOUTUBE_CHANNEL_VIDEOS = load_registry().resolve("youtube.web.get_channel_videos").path
BILIBILI_USER_VIDEOS = load_registry().resolve("bilibili.web.fetch_user_post_videos").path
WECHAT_ACCOUNT_ARTICLES = load_registry().resolve("wechat_mp.fetch_account_articles").path


def is_channel_id(value: str) -> bool:
    return re.fullmatch(r"UC[A-Za-z0-9_-]{22}", value) is not None


def catalog_endpoint(source: SourceRef) -> str:
    if source.platform == "wechat_mp" and source.kind == "user":
        return WECHAT_ACCOUNT_ARTICLES
    if source.platform == "youtube" and source.kind == "channel":
        return YOUTUBE_CHANNEL_VIDEOS
    if source.platform == "bilibili" and source.kind == "user":
        return BILIBILI_USER_VIDEOS
    raise SourceError("该来源不支持目录采集。", hint="请使用 YouTube 频道或 Bilibili UP 主主页；播放列表暂不支持。")


def catalog_params(source: SourceRef, cursor: str | None, page_size: int, language: str) -> dict[str, Any]:
    catalog_endpoint(source)
    if source.platform == "wechat_mp":
        if cursor is not None and not re.fullmatch(r"[A-Za-z0-9+/=_-]{1,8192}", cursor):
            raise ContractError("微信全号目录 offset 无效。")
        return {"username": source.id, "page_size": min(page_size, 100), "offset": cursor or "", "raw": False}
    if source.platform == "youtube":
        params: dict[str, Any] = {"channel_id": source.id, "need_format": True, "language_code": language}
        if cursor is not None:
            params["continuation_token"] = cursor
        return params
    if cursor is not None and (not cursor.isdigit() or int(cursor) < 1):
        raise ContractError("Bilibili 目录页码无效。")
    return {"uid": source.id, "pn": int(cursor or "1"), "ps": page_size}


def parse_catalog_page(source: SourceRef, payload: Any, cursor: str | None, page_size: int) -> CatalogPage:
    data = unwrap(payload)
    if source.platform == "wechat_mp":
        return _wechat_page(source, data)
    if source.platform == "youtube":
        return _youtube_page(source, data)
    return _bilibili_page(data, int(cursor or "1"), page_size)


def _video_source(platform: str, value: Any) -> SourceRef:
    pattern = r"[A-Za-z0-9_-]{11}" if platform == "youtube" else r"BV[A-Za-z0-9]{10}"
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise ContractError("目录条目缺少有效视频 ID。")
    url = (
        f"https://www.youtube.com/watch?v={value}"
        if platform == "youtube"
        else f"https://www.bilibili.com/video/{value}"
    )
    return SourceRef(platform=platform, kind="video", id=value, url=url, canonical_url=url)


def _duration(value: Any) -> int | None:
    if not isinstance(value, str) or not re.fullmatch(r"\d+(?::[0-5]\d){1,2}", value):
        return None
    seconds = 0
    for part in value.split(":"):
        seconds = seconds * 60 + int(part)
    return seconds


def _youtube_page(source: SourceRef, data: dict[str, Any]) -> CatalogPage:
    rows = data.get("videos")
    if not isinstance(rows, list):
        raise ContractError("YouTube 目录响应缺少 videos 列表。")
    cursor = data.get("continuation_token")
    if cursor is not None and not isinstance(cursor, str):
        raise ContractError("YouTube 目录 continuation_token 不是字符串。")
    channel = data.get("channel") or {}
    if not isinstance(channel, dict):
        raise ContractError("YouTube 目录 channel 不是对象。")
    if channel.get("id") and channel["id"] != source.id:
        raise ContractError("YouTube 返回了不同频道的目录。")
    title = str(channel["name"]) if channel.get("name") else None
    items: list[MediaItem] = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("title"), str):
            raise ContractError("YouTube 目录条目缺少 title。")
        extras = {"channel_id": source.id}
        if isinstance(row.get("published_time"), str):
            extras["published_label"] = row["published_time"]
        items.append(
            MediaItem(
                source=_video_source("youtube", row.get("video_id")),
                title=row["title"],
                creator=title,
                duration_seconds=_duration(row.get("duration")),
                extras=extras,
            )
        )
    return CatalogPage(items=items, next_cursor=cursor or None, title=title)


def _bilibili_page(data: dict[str, Any], page_number: int, page_size: int) -> CatalogPage:
    rows = data.get("archives")
    page = data.get("page")
    if not isinstance(rows, list) or not isinstance(page, dict):
        raise ContractError("Bilibili 目录响应缺少 archives/page。")
    total = page.get("total")
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        raise ContractError("Bilibili 目录 page.total 无效。")
    if not rows and (page_number - 1) * page_size < total:
        raise ContractError("Bilibili 在到达目录末尾前返回了空页。")
    items: list[MediaItem] = []
    for row in rows:
        if not isinstance(row, dict):
            raise ContractError("Bilibili 目录条目不是对象。")
        items.append(bilibili_media(_video_source("bilibili", row.get("bvid")), row))
    next_cursor = str(page_number + 1) if rows and page_number * page_size < total else None
    return CatalogPage(items=items, next_cursor=next_cursor, total_items=total)


def _wechat_page(source: SourceRef, data: dict[str, Any]) -> CatalogPage:
    from soota.wechat import catalog_item

    if data.get("biz_username") != source.id or not isinstance(data.get("articles"), list):
        raise ContractError("微信全号目录缺少 articles 或返回了其他账号。")
    end, cursor = data.get("is_end"), data.get("next_offset")
    if type(end) not in (int, bool) or end not in (0, 1):
        raise ContractError("微信全号目录缺少明确 is_end。")
    if not end and (not isinstance(cursor, str) or not re.fullmatch(r"[A-Za-z0-9+/=_-]{1,8192}", cursor)):
        raise ContractError("微信全号目录缺少下一页 offset。")
    rows = data["articles"]
    if not end and not rows:
        raise ContractError("微信全号目录尚未结束却返回空页。")
    if type(data.get("count")) is not int or data["count"] != len(rows):
        raise ContractError("微信全号目录 count 与 articles 不一致。")
    items = [catalog_item(row, username=source.id) for row in rows]
    return CatalogPage(items=items, next_cursor=None if end else cursor)
