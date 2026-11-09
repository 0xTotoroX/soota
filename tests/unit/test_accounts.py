"""公开账号资料只读、有界、身份校验及元数据保留。"""

import json

import pytest

from soota.accounts import inspect_account
from soota.errors import ContractError, InputError
from soota.providers.process import ProcessResult
from soota.providers.ytdlp import YtDlpProvider
from soota.routing import recognize

CHANNEL = "UCAAAAAAAAAAAAAAAAAAAAAA"


def test_bounded_profile_capture_does_not_save_entries_or_signed_media(settings, monkeypatch):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return ProcessResult(0, json.dumps({"channel_id": CHANNEL, "channel": "作者甲", "uploader_id": "@author",
            "description": "主页 https://space.bilibili.com/123456", "entries": [{"url": "private-signed-media"}]}), "")

    monkeypatch.setattr("soota.providers.ytdlp.shutil.which", lambda _: "/test/yt-dlp")
    monkeypatch.setattr("soota.providers.ytdlp.run_process", run)
    result = inspect_account(settings, "https://www.youtube.com/@author")
    assert result["resolved"]["id"] == CHANNEL
    assert result["links"] == ["https://space.bilibili.com/123456"]
    assert "private-signed-media" not in json.dumps(result)
    assert calls[0][calls[0].index("--playlist-end") + 1] == "1"
    assert "--skip-download" in calls[0] and "--flat-playlist" in calls[0]
    assert not any(r["billed"] for r in result["requests"])


def test_profile_identity_mismatch_does_not_bind_alias(settings, monkeypatch):
    monkeypatch.setattr(YtDlpProvider, "_run",
                        lambda *args: json.dumps({"channel_id": CHANNEL, "uploader_id": "@other"}))
    with pytest.raises(ContractError, match="handle"):
        inspect_account(settings, "https://www.youtube.com/@author")
    monkeypatch.setattr(YtDlpProvider, "_run", lambda *args: json.dumps({"id": "999999"}))
    with pytest.raises(ContractError, match="ID"):
        inspect_account(settings, "https://space.bilibili.com/123456")
    with pytest.raises(InputError):
        inspect_account(settings, "https://space.bilibili.com/123456", provider="tikhub")


def test_video_owner_description_and_publication_date_retained(settings, monkeypatch):
    monkeypatch.setattr(YtDlpProvider, "_run", lambda *args: json.dumps({
        "id": "Example0001", "title": "样本", "channel_id": CHANNEL,
        "channel_url": "https://www.youtube.com/channel/" + CHANNEL,
        "description": "公开资料 https://space.bilibili.com/123456", "upload_date": "20260102", "duration": 10,
    }))
    result = YtDlpProvider(settings).inspect(
        recognize("https://www.youtube.com/watch?v=Example0001"), include_captions=False)
    assert result.extras["channelId"] == CHANNEL and "123456" in result.extras["description"]
    assert result.published_at.isoformat() == "2026-01-02T00:00:00+00:00"
