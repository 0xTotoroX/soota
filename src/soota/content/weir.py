"""Reviewed Weir single-item source-text bindings.

[INPUT]: X status / Reddit post URL and raw Weir response.
[OUTPUT]: NativeContent preserving source body and target identity.
[POS]: Deterministic content conversion; HTTP/credits belong to providers.weir.
[PROTOCOL]: 变更时更新此头部，然后检查 AGENTS.md
"""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from soota.billing import selected_provider
from soota.config import SootaSettings
from soota.content.native_types import NativeContent
from soota.errors import AuthBudgetError, ContractError, InputError, SourceError
from soota.models import SourceRef
from soota.providers.weir import WeirClient, load_key
from soota.storage.cache import CacheStore
from soota.storage.task_budget import active_task_identity


def target(url: str) -> tuple[str, str]:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
        raise InputError("Weir 输入需要公开 HTTP(S) 帖子链接。")
    if parsed.hostname in {"x.com", "www.x.com", "twitter.com", "www.twitter.com", "mobile.twitter.com"}:
        match = re.fullmatch(r"/(?:[A-Za-z0-9_]{1,15}/status|i/web/status)/([0-9]{1,19})/?", parsed.path)
        if match:
            return "x", match[1]
        raise InputError("Weir 需要分享 X 帖子或 Article 的 status 链接，不能将 Article ID 当成帖子 ID。")
    if parsed.hostname in {"reddit.com", "www.reddit.com", "old.reddit.com", "new.reddit.com", "m.reddit.com"}:
        match = re.fullmatch(r"/(?:r/[A-Za-z0-9_]+/)?comments/([A-Za-z0-9]{1,13})(?:/[^/]+)?/?", parsed.path)
        if match:
            return "reddit", match[1].lower()
    if parsed.hostname == "redd.it":
        match = re.fullmatch(r"/([A-Za-z0-9]{1,13})/?", parsed.path)
        if match:
            return "reddit", match[1].lower()
    raise InputError("Weir 当前只支持 X status 与 Reddit 帖子正文；不接受评论定位或集合链接。")


def supports(url: str) -> bool:
    try:
        target(url)
        return True
    except InputError:
        return False


def _text(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SourceError("Weir 未返回可用全文，不能将摘要、标题或链接当作正文。")
    return value


def _x_body(data: dict[str, Any], depth: int = 0) -> tuple[str, str]:
    if depth > 5:
        raise ContractError("X 转帖嵌套超出已支持范围。")
    article = data.get("article")
    if article is not None:
        if not isinstance(article, dict):
            raise ContractError("X Article 字段格式无效。")
        return _text(article.get("markdown") or article.get("text")), _text(article.get("title"))
    body = _text(data.get("text"))
    retweet = data.get("retweetedPost")
    if retweet is not None:
        if not isinstance(retweet, dict):
            raise ContractError("X 转帖字段格式无效。")
        original, title = _x_body(retweet, depth + 1)
        return body + "\n\n" + original, title
    if re.fullmatch(r"https?://(?:www\.)?x\.com/i/article/\d+/?", body.strip()):
        raise SourceError("X 仅返回 Article 链接，缺少全文。")
    return body, ""


def convert(data: dict[str, Any], url: str, platform: str, identity: str) -> NativeContent:
    if data.get("id") != identity:
        raise ContractError("Weir 返回的帖子 ID 与请求目标不一致。")
    operation = f"weir.{platform}.post"
    if platform == "reddit":
        if data.get("removedByCategory") or data.get("selftext") in ("[removed]", "[deleted]"):
            raise SourceError("Reddit 正文已移除，不能将占位文字当作全文。")
        body, title = _text(data.get("selftext")), _text(data.get("title"))
    else:
        body, title = _x_body(data)
        if title:
            operation = "weir.x.article"
    return NativeContent(
        raw=data,
        body=body,
        title=title,
        source_url=url,
        provider="weir",
        scope="item",
        operation=operation,
        format="markdown" if platform == "reddit" or title else "text",
    )


def _complete_articles(
    data: dict[str, Any], client: WeirClient, save: Callable[[], None], *, allow_credits: bool, depth: int = 0
) -> None:
    if depth > 5:
        raise ContractError("X 转帖嵌套超出已支持范围。")
    article = data.get("article")
    if isinstance(article, dict) and not (article.get("markdown") or article.get("text")):
        identity = data.get("id")
        if not isinstance(identity, str) or not re.fullmatch(r"[0-9]{1,19}", identity):
            raise ContractError("X Article 缺少有效分享帖身份。")
        if not allow_credits:
            raise AuthBudgetError("禁止新的 credits 请求，当前 Article 检查点尚无全文。")
        full = client.fetch(f"/v1/x/posts/{identity}/article", max_credits=2)
        if full.get("postId") != identity or full.get("id") != article.get("id"):
            raise ContractError("X Article 与分享帖身份不一致，未保存为正文。")
        data["article"] = full
        save()
    retweet = data.get("retweetedPost")
    if isinstance(retweet, dict):
        _complete_articles(retweet, client, save, allow_credits=allow_credits, depth=depth + 1)


def capture(settings: SootaSettings, url: str, *, allow_credits: bool = True) -> NativeContent:
    selected_provider(settings, "weir")
    platform, identity = target(url)
    key = load_key(settings)
    if key is None:
        raise AuthBudgetError("缺少 Weir API Key；设置 WEIR_API_KEY 或使用 soota providers auth weir。")
    source = SourceRef(platform=platform, kind="post", id=identity, url=url, canonical_url=url)
    cache = CacheStore(settings.cache_dir)
    artifact = "weir-body-" + hashlib.sha256(key.encode()).hexdigest()[:24]
    saved = cache.get_json(source, artifact)
    if saved and settings.weir.cache_seconds > 0:
        stamp, data = saved.get("time"), saved.get("data")
        if (
            isinstance(stamp, (int, float))
            and not isinstance(stamp, bool)
            and 0 <= time.time() - stamp < settings.weir.cache_seconds
            and isinstance(data, dict)
        ):
            return convert(data, url, platform, identity)
    # A successful carrier read is a task checkpoint even when Article acquisition fails.
    task = active_task_identity()
    checkpoint = CacheStore(Path(task) / "weir") if task else None
    acquired = checkpoint.get_json(source, artifact) if checkpoint else None
    data = acquired.get("data") if acquired else None
    client = WeirClient(settings)
    if data is None:
        if not allow_credits:
            raise AuthBudgetError("--max-cost 0 禁止新 credits 计费请求；可用 --weir-max-credits 设置免费额度上限。")
        data = client.fetch(f"/v1/{platform}/posts/{identity}", max_credits=4 if platform == "reddit" else 2)
    if not isinstance(data, dict) or data.get("id") != identity:
        raise ContractError("Weir 返回或检查点的帖子 ID 与请求目标不一致。")

    def save_checkpoint() -> None:
        if checkpoint:
            checkpoint.put_json(source, artifact, {"data": data}, redact=False)

    save_checkpoint()
    if platform == "x":
        _complete_articles(data, client, save_checkpoint, allow_credits=allow_credits)
    result = convert(data, url, platform, identity)
    cache.put_json(source, artifact, {"time": time.time(), "data": data}, redact=False)
    return result
