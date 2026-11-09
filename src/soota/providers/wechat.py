"""[INPUT]: 微信文章/合集公开 URL、匿名 HTTP 和分页游标。
[OUTPUT]: 单篇 NativeContent 或标准 CatalogPage；不调用账号登录和浏览器。
[POS]: 微信免费获取边界；复用受限 HTTP 读取及独立 catalog 的恢复机制。
[PROTOCOL]: 变更时更新此头部，然后检查 AGENTS.md
"""

from __future__ import annotations

import json
import time
from typing import Any
from urllib.parse import urlencode

from bs4 import BeautifulSoup

from soota.content.native_types import NativeContent
from soota.content.wechat import check_page, parse_article
from soota.errors import ContractError, InputError, SourceError, VerificationRequired
from soota.media.download import http_response, response_chunks
from soota.models import CatalogPage, RequestRecord, SourceRef
from soota.wechat import ALBUM_ENDPOINT, USER_AGENT, catalog_item, query, recognize


def fetch(url: str) -> str:
    # 新客户端只使用固定公开请求头；重定向也不携带 Cookie。
    deadline = time.monotonic() + 30
    with http_response(
        url, headers={"User-Agent": USER_AGENT}, timeout=30, anonymous=True, accepted_statuses=(200, 401, 403, 404, 429)
    ) as (response, _):
        body = b"".join(response_chunks(response, max_bytes=16 * 1024 * 1024, deadline=deadline)).decode("utf-8")
        if response.status_code != 200:
            check_page(BeautifulSoup(body, "html.parser"), url, body)
            raise SourceError(
                "微信 HTTP 请求未成功；状态码不能作为远端删除证据。", details={"status_code": response.status_code}
            )
        return body


def capture(url: str) -> NativeContent:
    source = recognize(url)
    if source.kind != "article":
        raise InputError("正文获取需要微信单篇链接。")
    try:
        return parse_article(fetch(source.canonical_url), source.canonical_url)
    except (VerificationRequired, ContractError):
        if "chksm=" in url and url != source.canonical_url:
            return parse_article(fetch(url), source.canonical_url)
        raise


def album_page(source: SourceRef, payload: Any) -> CatalogPage:
    if not isinstance(payload, dict) or not isinstance(payload.get("base_resp"), dict):
        raise ContractError("微信合集响应缺少 base_resp。")
    if payload["base_resp"].get("ret") not in (0, "0"):
        raise SourceError(
            "微信合集返回业务错误，未将其当作空目录。", details={"upstream_code": str(payload["base_resp"].get("ret"))}
        )
    data = payload.get("getalbum_resp")
    if not isinstance(data, dict) or not isinstance(data.get("article_list"), list):
        raise ContractError("微信合集缺少 article_list。")
    flag = data.get("continue_flag")
    if type(flag) not in (int, str) or flag not in (0, 1, "0", "1"):
        raise ContractError("微信合集缺少明确 continue_flag。")
    values = query(source.canonical_url)
    items = []
    for row in data["article_list"]:
        if not isinstance(row, dict) or not str(row.get("pos_num", "")).isdigit() or int(row["pos_num"]) < 1:
            raise ContractError("微信合集条目缺少有效 pos_num。")
        items.append(catalog_item({**row, "album_id": values["album_id"]}, biz=values["__biz"]))
    more = str(flag) == "1"
    if more and not items:
        raise ContractError("微信合集仍有下一页却返回空页。")
    cursor = f"{items[-1].extras['msgid']}:{items[-1].extras['itemidx']}" if more else None
    total = data.get("total_count")
    if total is not None:
        if not str(total).isdigit() or isinstance(total, bool):
            raise ContractError("微信合集 total_count 无效。")
        total = int(total)
    return CatalogPage(items=items, next_cursor=cursor, title=data.get("album_name"), total_items=total)


class WechatAlbumProvider:
    def __init__(self) -> None:
        self.records: list[RequestRecord] = []

    def resolve_catalog_source(self, source: SourceRef) -> SourceRef:
        if source.platform != "wechat_mp" or source.kind != "album":
            raise InputError("匿名 HTTP 目录只支持微信公开合集。")
        return source

    def list_items(self, source: SourceRef, cursor: str | None = None, *, page_size: int = 20) -> CatalogPage:
        self.resolve_catalog_source(source)
        params: dict[str, str | int] = {
            **query(source.canonical_url),
            "action": "getalbum",
            "f": "json",
            "count": min(page_size, 100),
            "is_reverse": 0,
        }
        if cursor is not None:
            parts = cursor.split(":")
            if len(parts) != 2 or not all(part.isdigit() and int(part) > 0 for part in parts):
                raise ContractError("微信合集游标无效。")
            params.update(begin_msgid=parts[0], begin_itemidx=parts[1])
        self.records.append(RequestRecord(endpoint=ALBUM_ENDPOINT, billed=False, estimated_cost_usd=0))
        body = fetch(ALBUM_ENDPOINT + "?" + urlencode(params))
        try:
            payload = json.loads(body)
        except ValueError as exc:
            check_page(BeautifulSoup(body, "html.parser"), source.canonical_url, body)
            raise ContractError("微信合集没有返回 JSON。") from exc
        return album_page(source, payload)
