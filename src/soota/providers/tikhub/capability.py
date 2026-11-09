"""[INPUT]: 来源平台与种类。
[OUTPUT]: 已实现统一能力清单。
[POS]: TikHub 适配器能力边界，包含微信全号分页。
[PROTOCOL]: 变更时更新此头部，然后检查 AGENTS.md
"""

from __future__ import annotations

from soota.models import CapabilitySet, SourceRef

# Catalogs cover YouTube channels, Bilibili creators and WeChat accounts; playlists remain unsupported.
PLATFORM_CAPABILITIES: dict[str, dict[str, CapabilitySet]] = {
    "wechat_mp": {"user": CapabilitySet(catalog=True, pagination=True)},
    "youtube": {
        "video": CapabilitySet(inspect=True, captions=True, audio=True),
        "channel": CapabilitySet(inspect=False, catalog=True, pagination=True),
        "playlist": CapabilitySet(),
    },
    "bilibili": {
        "video": CapabilitySet(inspect=True, captions=True, audio=True),
        "user": CapabilitySet(inspect=False, catalog=True, pagination=True),
        "shortlink": CapabilitySet(inspect=True, captions=True, audio=True),
    },
}


def capabilities_for(source: SourceRef) -> CapabilitySet:
    by_kind = PLATFORM_CAPABILITIES.get(source.platform, {})
    return by_kind.get(source.kind, CapabilitySet())
