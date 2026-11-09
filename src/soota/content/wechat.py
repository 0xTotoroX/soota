"""[INPUT]: 微信匿名 HTTP 页面、DOM 和脚本中的公开元数据。
[OUTPUT]: 原顺序 Markdown、账号/发布时间/身份/合集与明确的不可读状态。
[POS]: 单篇格式解析；不做语义写作，不调用浏览器或账号接口。
[PROTOCOL]: 变更时更新此头部，然后检查 AGENTS.md
"""

from __future__ import annotations

import hashlib
import html
import re
from datetime import datetime, timedelta, timezone

from bs4 import BeautifulSoup, Tag
from markdownify import MarkdownConverter

from soota.content.native_types import NativeContent
from soota.errors import ContractError, InputError, RemoteDeleted, VerificationRequired
from soota.wechat import article_identity, article_source, query, timestamp


def _script_value(scripts: str, names: tuple[str, ...]) -> str:
    for name in names:
        match = re.search(r"(?<![\w$'\"])" + re.escape(name) + r"\s*[:=]\s*(['\"])([^\r\n]*?)\1", scripts)
        if match:
            return html.unescape(match[2].replace(r"\x26", "&").replace(r"\/", "/"))
        match = re.search(r"(?<![\w$'\"])" + re.escape(name) + r"\s*[:=]\s*([0-9]+)\b", scripts)
        if match:
            return match[1]
    return ""


def check_page(soup: BeautifulSoup, url: str, body: str) -> None:
    # 只读可见状态页；正文里讨论删除/验证码或脚本含模板文案不算状态证据。
    content = soup.select_one("#js_content")
    if content and (content.get_text(strip=True) or content.find(["img", "video"])):
        return
    if soup.select_one(".share_content_page, #js_article, #img_swiper_content"):
        return
    text = soup.get_text(" ", strip=True)
    reason = None
    if re.search(r"环境异常|去验证|完成验证|访问过于频繁", text):
        raise VerificationRequired(
            "微信要求环境验证，未自动重试或打开浏览器。", details={"retryable": False, "source_url": url}
        )
    if re.search(r"该内容已被发布者删除|内容已被发布者删除|此内容已被发布者删除", text):
        reason = "publisher_deleted"
    elif re.search(r"(?:此|该)内容因违规无法查看|(?:此|该)内容被投诉且经审核涉嫌违规|涉嫌违反相关法律法规和政策", text):
        reason = "policy_removed"
    if reason:
        raise RemoteDeleted(
            "微信确认该内容已删除或因违规无法查看。",
            details={
                "retryable": False,
                "reason": reason,
                "source_url": url,
                "response_sha256": hashlib.sha256(body.encode()).hexdigest(),
                "evidence_text": text[:1000],
            },
        )


class _Converter(MarkdownConverter):
    def convert_pre(self, el: Tag, text: str, parent_tags: set[str]) -> str:
        # 保留行首空格，并让正文中的 ``` 不会提前终止代码围栏。
        code = el.get_text().replace("\xa0", " ").strip("\n")
        fence = "`" * max(3, max((len(m[0]) + 1 for m in re.finditer(r"`+", code)), default=3))
        language = str(el.get("data-language") or el.get("data-lang") or "")
        if not re.fullmatch(r"[\w+-]*", language):
            language = ""
        return f"\n\n{fence}{language}\n{code}\n{fence}\n\n"


def parse_article(body: str, url: str) -> NativeContent:
    soup = BeautifulSoup(body, "html.parser")
    check_page(soup, url, body)
    content = soup.select_one("#js_content")
    scripts = "\n".join(node.get_text() for node in soup.find_all("script"))
    fields = {
        "__biz": _script_value(scripts, ("biz", "__biz")),
        "mid": _script_value(scripts, ("mid", "appmsgid")),
        "idx": _script_value(scripts, ("idx", "itemidx")),
        "sn": _script_value(scripts, ("sn",)),
    }
    parts = article_identity(url, fields)
    source = article_source(url, parts)
    title_node, author_node = soup.select_one("#activity-name"), soup.select_one("#js_name")
    title = title_node.get_text(" ", strip=True) if title_node else _script_value(scripts, ("msg_title",))
    if not title:
        meta_title = soup.find("meta", property="og:title")
        if meta_title and meta_title.get("content"):
            title = meta_title["content"].strip()
    author = author_node.get_text(" ", strip=True) if author_node else _script_value(scripts, ("nickname",))
    if not author:
        meta_author = soup.find("meta", property="og:article:author") or soup.find("meta", property="author")
        if meta_author and meta_author.get("content"):
            author = meta_author["content"].strip()
    raw_time = _script_value(scripts, ("create_time", "ct"))
    published = None
    if raw_time:
        if re.fullmatch(r"[0-9]{1,12}", raw_time):
            published = timestamp(raw_time)
        elif re.fullmatch(r"\d{4}-\d{2}-\d{2}(?: \d{2}:\d{2}(?::\d{2})?)?", raw_time):
            published = datetime.fromisoformat(raw_time).replace(tzinfo=timezone(timedelta(hours=8)))
        else:
            published = timestamp(raw_time)
    if published is None:
        node = soup.select_one("#publish_time")
        label = node.get_text(" ", strip=True) if node else ""
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}(?: \d{2}:\d{2}(?::\d{2})?)?", label):
            published = datetime.fromisoformat(label).replace(tzinfo=timezone(timedelta(hours=8)))
    albums = set(re.findall(r"[\"']?album_id[\"']?\s*[:=]\s*[\"']?([0-9]+)", scripts))
    for node in soup.select("[data-album-id], a[href*='appmsgalbum']"):
        if node.get("data-album-id"):
            albums.add(str(node["data-album-id"]))
        if node.get("href"):
            try:
                album = query(str(node["href"])).get("album_id")
                if album:
                    albums.add(album)
            except InputError:
                continue
    albums = {value for value in albums if re.fullmatch(r"[1-9][0-9]*", value)}

    if content is None:
        if soup.select_one(".share_content_page, #js_article, #img_swiper_content"):
            desc_meta = soup.find("meta", property="og:description")
            desc_text = desc_meta["content"].strip() if desc_meta and desc_meta.get("content") else ""
            desc_text = desc_text.replace(r"\x0a", "\n").replace("\r\n", "\n")
            img_urls: list[str] = []
            for m in re.finditer(r'cdn_url:\s*[\'"](https?://[^\'"]+)[\'"]', scripts):
                u = m.group(1).replace(r"\x26", "&").replace(r"\/", "/")
                if ("from=appmsg" in u or "0?wx_fmt=" in u) and u not in img_urls:
                    img_urls.append(u)
            parts_md: list[str] = []
            if desc_text:
                parts_md.append(desc_text)
            for u in img_urls:
                parts_md.append(f"![]({u})")
            markdown = "\n\n".join(parts_md).strip() + "\n"
            if not markdown.strip():
                raise ContractError("微信正文为空。")
        else:
            raise ContractError("微信页面没有 #js_content，不能将提示页当作正文。")
    else:
        for node in content.select(
            "script, style, #js_pay_area, #js_reward_area, .reward_area, .qr_code_pc, "
            ".rich_media_extra, #js_pc_qr_code, .wx_profile_card"
        ):
            node.decompose()
        for img in content.find_all("img"):
            src = str(img.get("data-src") or img.get("src") or "")
            if src.startswith("//"):
                src = "https:" + src
            if src.startswith(("https://", "http://")):
                img["src"] = src
            else:
                img.decompose()
        for block in content.select(".code-snippet__fix"):
            block.name = "pre"
        for pre in content.find_all("pre"):
            for br in pre.find_all("br"):
                br.replace_with("\n")
            lines = pre.find_all("code", recursive=False)
            if len(lines) > 1:
                code = "\n".join(line.get_text() for line in lines)
                pre.clear()
                pre.append(code)
        markdown = _Converter(heading_style="ATX", strip_pre=None).convert(str(content)).strip() + "\n"
        if not markdown.strip():
            desc_meta = soup.find("meta", property="og:description")
            desc_text = desc_meta["content"].strip() if desc_meta and desc_meta.get("content") else ""
            cover_meta = soup.find("meta", property="og:image")
            cover_url = cover_meta["content"].strip() if cover_meta and cover_meta.get("content") else ""
            fallback_parts: list[str] = []
            if title:
                fallback_parts.append(title)
            if desc_text:
                fallback_parts.append(desc_text)
            if cover_url:
                fallback_parts.append(f"![]({cover_url})")
            markdown = "\n\n".join(fallback_parts).strip() + "\n"
            if not markdown.strip():
                raise ContractError("微信正文为空。")
    metadata = {
        **parts,
        "title": title,
        "author": author,
        "publish_time": published.isoformat() if published else None,
        "create_time": str(int(published.timestamp())) if published else None,
        "album_ids": sorted(albums),
        "source": source.model_dump(mode="json"),
        "html": body,
    }
    return NativeContent(
        raw=metadata,
        body=markdown,
        title=title,
        source_url=source.canonical_url,
        provider="http",
        scope="item",
        operation="wechat_mp.article",
    )
