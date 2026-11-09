"""[INPUT]: 明确的 YouTube/Bilibili 公开账号地址与现有免费 Provider。
[OUTPUT]: 有界账号资料、稳定身份及只读请求记录。
[POS]: 采集层的账号证据适配，不推断跨平台同一人、不拉取全量目录。
[PROTOCOL]: 变更时更新此头部，然后检查 AGENTS.md
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from urllib.parse import urlsplit

from soota.config import SootaSettings
from soota.counterpart import links
from soota.errors import ContractError, InputError
from soota.providers.opencli import OpenCLIProvider
from soota.providers.ytdlp import YtDlpProvider
from soota.routing import recognize
from soota.secrets import redact_any


def inspect_account(settings: SootaSettings, url: str, *, provider: str = "yt-dlp") -> dict:
    parsed = urlsplit(url)
    if parsed.username or parsed.password:
        raise InputError("公开账号地址不能包含登录信息。")
    source = recognize(url)
    if source.platform not in {"youtube", "bilibili"} or source.kind not in {"channel", "user"}:
        raise InputError("需要 YouTube 频道或 Bilibili UP 主主页。")
    if provider == "opencli":
        if source.platform != "youtube":
            raise InputError("OpenCLI 尚未接入 Bilibili 账号资料；可用已选样本的视频作者证据。")
        client = OpenCLIProvider(settings)
        rows = client._run("youtube", "channel", [source.id, "--limit", "1"])
        fields = {r["field"]: r.get("value") for r in rows if isinstance(r.get("field"), str)}
        info = {"channel_id": fields.get("channelId"), "channel": fields.get("name"),
                "uploader_id": fields.get("handle"), "description": fields.get("description")}
    elif provider == "yt-dlp":
        client = YtDlpProvider(settings)
        raw = client._run(source, ["--yes-playlist", "--flat-playlist", "--playlist-end", "1",
                                   "--dump-single-json", "--simulate"], "account")
        try:
            info = json.loads(raw)
        except ValueError as exc:
            raise ContractError("账号资料不是有效 JSON。") from exc
    else:
        raise InputError("账号证据只使用明确选择的免费 yt-dlp/opencli，不自动切换来源。")
    if not isinstance(info, dict):
        raise ContractError("账号资料格式无效。")
    aliases = {source.canonical_url}
    if source.platform == "youtube":
        ident = str(info.get("channel_id") or "")
        if not re.fullmatch(r"UC[A-Za-z0-9_-]{22}", ident):
            raise ContractError("频道资料缺少稳定 channel ID。")
        handle = str(info.get("uploader_id") or "")
        if source.id.startswith("UC") and source.id != ident:
            raise ContractError("返回的频道 ID 与请求不一致。")
        if source.id.startswith("@") and source.id.casefold() != handle.casefold():
            raise ContractError("频道 handle 与请求不一致，不能绑定到另一账号。")
        resolved = recognize(f"https://www.youtube.com/channel/{ident}")
        if handle.startswith("@"):
            aliases.add(recognize(f"https://www.youtube.com/{handle}").canonical_url)
    else:
        if str(info.get("id")) != source.id:
            raise ContractError("返回的 UP 主 ID 与请求不一致。")
        resolved = source
    aliases.add(resolved.canonical_url)
    description = str(redact_any(str(info.get("description") or "")))
    outgoing = []
    for link in links(description):
        parsed = urlsplit(link)
        if parsed.scheme in {"http", "https"} and not parsed.username and not parsed.password:
            outgoing.append(link)
    return {"source": source.model_dump(mode="json"), "resolved": resolved.model_dump(mode="json"),
            "aliases": sorted(aliases), "name": info.get("channel") or info.get("uploader") or info.get("title"),
            "description": description, "links": sorted(set(outgoing)), "provider": provider,
            "observed_at": datetime.now(UTC).isoformat(),
            "requests": [r.model_dump(mode="json") for r in client.records], "scope": "profile_with_at_most_one_entry"}
