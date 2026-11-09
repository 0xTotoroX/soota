"""[INPUT]: 微信公开链接及采集到的文章标识。
[OUTPUT]: 稳定 SourceRef、文章身份和目录条目。
[POS]: 微信采集共享的无网络身份解析；不管理个人账号或回执。
[PROTOCOL]: 变更时更新此头部，然后检查 AGENTS.md
"""

from __future__ import annotations

import hashlib
import html
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

from soota.errors import ContractError, InputError
from soota.models import MediaItem, SourceRef

HOST = "mp.weixin.qq.com"
ALBUM_ENDPOINT = f"https://{HOST}/mp/appmsgalbum"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def query(url: str) -> dict[str, str]:
    parsed = urlsplit(html.unescape(url))
    if parsed.scheme not in {"http", "https"} or parsed.hostname != HOST or parsed.username or parsed.password:
        raise InputError("需要不含登录凭据的 mp.weixin.qq.com 公开链接。")
    values = parse_qs(parsed.query)
    if any(len(rows) != 1 for rows in values.values()):
        raise InputError("微信链接包含重复参数。")
    return {key: rows[0] for key, rows in values.items()}


def article_identity(url: str, fields: dict[str, Any] | None = None) -> dict[str, str]:
    values = query(url)
    if not re.fullmatch(r"/s(?:/[A-Za-z0-9_-]+/?)?", urlsplit(html.unescape(url)).path):
        raise InputError("文章身份必须来自 /s 链接。")
    fields = fields or {}
    result = {}
    for key, aliases in {
        "__biz": ("__biz", "biz"),
        "mid": ("mid", "msgid", "app_msg_id"),
        "idx": ("idx", "itemidx"),
        "sn": ("sn",),
    }.items():
        candidates = [str(value) for name in aliases if (value := fields.get(name)) not in (None, "")]
        if values.get(key):
            candidates.append(values[key])
        if len(set(candidates)) > 1:
            raise ContractError(f"微信文章 {key} 与链接身份不一致。")
        result[key] = candidates[0] if candidates else ""
    if not re.fullmatch(r"[A-Za-z0-9+/=_-]{1,256}", result["__biz"]):
        raise ContractError("微信文章缺少有效 __biz。")
    if not all(re.fullmatch(r"[1-9][0-9]{0,19}", result[key]) for key in ("mid", "idx")):
        raise ContractError("微信文章缺少有效 mid/idx。")
    if result["sn"] and not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", result["sn"]):
        raise ContractError("微信文章 sn 无效。")
    return result


def article_source(url: str, fields: dict[str, Any] | None = None) -> SourceRef:
    parts = article_identity(url, fields)
    # sn 是访问参数；同一 biz/mid/idx 的不同分享链接仍是同一篇文章。
    ident = hashlib.sha256(parts["__biz"].encode()).hexdigest()[:20] + f"-{parts['mid']}-{parts['idx']}"
    canonical = f"https://{HOST}/s?" + urlencode({key: value for key, value in parts.items() if value})
    return SourceRef(platform="wechat_mp", kind="article", id=ident, url=canonical, canonical_url=canonical)


def recognize(url: str) -> SourceRef:
    if re.fullmatch(r"gh_[A-Za-z0-9_-]{1,60}", url):
        url = f"https://{HOST}/mp/profile_ext?" + urlencode({"username": url})
    values = query(url)
    path = urlsplit(url).path
    if path == "/mp/profile_ext" and re.fullmatch(r"gh_[A-Za-z0-9_-]{1,60}", values.get("username", "")):
        ident = values["username"]
        canonical = f"https://{HOST}/mp/profile_ext?" + urlencode({"username": ident})
        return SourceRef(platform="wechat_mp", kind="user", id=ident, url=canonical, canonical_url=canonical)
    if path == "/mp/appmsgalbum":
        biz, album = values.get("__biz", ""), values.get("album_id", "")
        if not re.fullmatch(r"[A-Za-z0-9+/=_-]{1,256}", biz) or not re.fullmatch(r"[1-9][0-9]{0,29}", album):
            raise InputError("合集链接需要有效 __biz 与 album_id。")
        canonical = ALBUM_ENDPOINT + "?" + urlencode({"__biz": biz, "album_id": album})
        ident = hashlib.sha256(biz.encode()).hexdigest()[:20] + "-" + album
        return SourceRef(platform="wechat_mp", kind="album", id=ident, url=canonical, canonical_url=canonical)
    if path == "/s" or re.fullmatch(r"/s/[A-Za-z0-9_-]+/?", path):
        if all(values.get(key) for key in ("__biz", "mid", "idx")):
            return article_source(url)
        if path != "/s":
            canonical = f"https://{HOST}{path.rstrip('/')}"
            return SourceRef(
                platform="wechat_mp",
                kind="article",
                id="short-" + path.split("/")[2],
                url=canonical,
                canonical_url=canonical,
            )
    raise InputError("需要微信文章、带 __biz 的合集链接，或公众号 username（gh_xxx）。")


def timestamp(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool) or not re.fullmatch(r"[0-9]{1,12}", str(value)):
        raise ContractError("微信 create_time 不是秒级时间戳。")
    try:
        return datetime.fromtimestamp(int(value), UTC)
    except (OverflowError, ValueError, OSError) as exc:
        raise ContractError("微信 create_time 超出有效范围。") from exc


def catalog_item(row: dict[str, Any], *, biz: str | None = None, username: str | None = None) -> MediaItem:
    if not isinstance(row, dict) or not isinstance(row.get("url"), str) or not isinstance(row.get("title"), str):
        raise ContractError("微信目录条目缺少 url/title。")
    fields = {**row, **({"__biz": biz} if biz else {})}
    parts = article_identity(row["url"], fields)
    extras = {**parts, "msgid": parts["mid"], "itemidx": parts["idx"]}
    for name in ("pos_num", "create_time", "album_id"):
        if row.get(name) is not None:
            extras[name] = str(row[name])
    if username:
        extras["username"] = username
    return MediaItem(
        source=article_source(row["url"], parts),
        title=html.unescape(row["title"]),
        creator=row.get("account"),
        published_at=timestamp(row.get("create_time")),
        extras=extras,
    )
