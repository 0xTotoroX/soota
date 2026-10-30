"""受控 treg 接口映射。

[INPUT]: 官方公开 catalog 的接口身份、路径与基准报价。
[OUTPUT]: ENDPOINTS、caption_endpoint、resolve。
[POS]: 仅登记已适配读取契约；执行前校验在线目录，不动态执行搜索结果。
[PROTOCOL]: 变更时更新此头部，然后检查 AGENTS.md
"""

from __future__ import annotations

from soota.config import SootaSettings
from soota.errors import InputError

CRAWL4AI = "crawl4ai.youtube.video.captions"
SCRAPECREATORS = "scrapecreators.x.v1-youtube-video-transcript"
# id: (upstream path, HTTP method, offline USD estimate), checked 2026-10-28.
ENDPOINTS = {
    CRAWL4AI: ("/recipes/youtube-captions", "POST", 0.00165),
    SCRAPECREATORS: ("/v1/youtube/video/transcript", "GET", 0.00188),
    "tikhub.x.youtube-web-v2-get-video-info-v2": ("/api/v1/youtube/web_v2/get_video_info_v2", "GET", 0.001),
    "tikhub.x.youtube-web-v2-get-video-streams-v2": ("/api/v1/youtube/web_v2/get_video_streams_v2", "GET", 0.003),
    "tikhub.x.youtube-web-v2-get-channel-id": ("/api/v1/youtube/web_v2/get_channel_id", "GET", 0.001),
    "tikhub.youtube.channel.videos": ("/api/v1/youtube/web_v2/get_channel_videos", "GET", 0.001),
    "tikhub.x.bilibili-web-fetch-one-video-v3": ("/api/v1/bilibili/web/fetch_one_video_v3", "GET", 0.001),
    "tikhub.x.bilibili-web-fetch-video-subtitle": ("/api/v1/bilibili/web/fetch_video_subtitle", "GET", 0.001),
    "tikhub.x.bilibili-web-fetch-video-playurl": ("/api/v1/bilibili/web/fetch_video_playurl", "GET", 0.001),
}


def caption_endpoint(settings: SootaSettings) -> str:
    return CRAWL4AI if settings.treg.youtube_captions == "crawl4ai" else SCRAPECREATORS


def resolve(path: str) -> str:
    if path in ENDPOINTS:
        return path
    for key, (upstream, _, _) in ENDPOINTS.items():
        if path == upstream:
            return key
    raise InputError("treg 尚未适配该接口；不会自动调用目录搜索结果。")
