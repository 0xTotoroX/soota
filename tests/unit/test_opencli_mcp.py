import json
import sys
import textwrap
from pathlib import Path

import pytest

from soota.catalog import catalog, estimate_catalog
from soota.config import OpenCLIMCPConfig
from soota.errors import AuthBudgetError, ContractError, SourceError
from soota.features import call_feature
from soota.models import TranscriptMode
from soota.pipeline import ingest
from soota.providers.mcp_client import StdioMCPClient
from soota.providers.opencli import OpenCLIProvider
from soota.providers.opencli_mcp import OpenCLIMCPProvider, normalize_video
from soota.providers.opencli_runtime import RuntimeUnavailable
from soota.routing import recognize

BILI = "https://www.bilibili.com/video/BV1M1421t7hT"
X = "https://x.com/example"


@pytest.fixture(autouse=True)
def experimental_mcp(settings):
    settings.opencli_mcp.enabled = True


@pytest.mark.parametrize("dry_run", [False, True])
def test_stdio_client_initializes_and_decodes_one_bounded_tool_result(tmp_path, dry_run, monkeypatch):
    fake = tmp_path / "fake-mcp"
    marker = tmp_path / "session-closed"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        + textwrap.dedent(
            """
            import json
            import sys
            from pathlib import Path
            for line in sys.stdin:
                request = json.loads(line)
                if request.get('method') == 'initialize':
                    response = {'jsonrpc':'2.0','id':request['id'],'result':{
                        'protocolVersion':'2025-06-18',
                        'serverInfo':{'name':'opencli-mcp','version':'0.0.25'}}}
                elif request.get('method') == 'tools/call':
                    if request['params']['name'] == 'sites_search':
                        data = {'ok':True,'results':[{'site':'bilibili','name':'video',
                            'access':'read','domain':'bilibili.com',
                            'args':[{'name':'bvid','type':'string','required':True}]}]}
                    else:
                        assert request['params']['name'] == 'site_run'
                        assert request['params']['arguments']['command'] == 'video'
                        data = {'ok':True,'rows':[{'title':'Example'}]}
                    response = {'jsonrpc':'2.0','id':request['id'],'result':{
                        'content':[{'type':'text','text':json.dumps(data)}],
                        'isError':False}}
                else:
                    continue
                sys.stdout.write(json.dumps(response) + '\\n')
                sys.stdout.flush()
            Path(__file__).with_name('session-closed').write_text('closed on EOF')
            """
        )
    )
    if dry_run:
        fake.write_text(
            fake.read_text().replace("assert request['params']['name'] == 'site_run'", "raise SystemExit(9)")
        )
    monkeypatch.setattr("soota.providers.mcp_client.command_argv", lambda argv: [sys.executable, str(fake)])
    result = StdioMCPClient(
        OpenCLIMCPConfig(enabled=True, binary=sys.executable, timeout_seconds=2)
    ).call_site_readonly("bilibili", "video", {"bvid": "BV1M1421t7hT"}, dry_run=dry_run)
    if dry_run:
        assert result["dispatched"] is False
    else:
        assert result == {"ok": True, "rows": [{"title": "Example"}]}
    assert marker.read_text() == "closed on EOF"


def test_readonly_guard_rejects_write_override_and_changed_contract(tmp_path, monkeypatch):
    client = StdioMCPClient(OpenCLIMCPConfig(enabled=True, binary="opencli-mcp"))
    args = {"bvid": "BV1M1421t7hT"}
    with pytest.raises(ContractError, match="只读契约"):
        client._assert_read_contract(
            {"results": [{"site": "bilibili", "name": "video", "access": "write", "domain": "bilibili.com"}]},
            "bilibili",
            "video",
            args,
            "bilibili.com",
        )
    override = tmp_path / ".opencli-mcp" / "adapters" / "bilibili" / "video.js"
    override.parent.mkdir(parents=True)
    override.write_text("export default {}")
    monkeypatch.setenv("HOME", str(tmp_path))
    with pytest.raises(ContractError, match="覆盖"):
        client.call_site_readonly("bilibili", "video", args, expected_domain="bilibili.com")


def test_bilibili_mcp_precedes_opencli_and_keeps_unknown_track_language(settings, monkeypatch):
    monkeypatch.setattr(StdioMCPClient, "available", lambda config: True)
    monkeypatch.setattr(OpenCLIProvider, "__init__", lambda self, settings: pytest.fail("OpenCLI must not run"))

    def response(self, site, command, args):
        assert site == "bilibili" and args["bvid"] == "BV1M1421t7hT"
        if command == "video":
            return {"ok": True, "rows": [{"bvid": "BV1M1421t7hT", "title": "Example", "duration_s": 12}]}
        return {"ok": True, "rows": [{"index": 1, "from_s": 0.2, "to_s": 1.5, "content": "真实字幕"}]}

    monkeypatch.setattr(OpenCLIMCPProvider, "_run", response)
    result = ingest(settings, BILI, transcript=TranscriptMode.CAPTIONS_ONLY, max_cost=0)
    assert result.ok and result.data["provider"] == "opencli-mcp"
    manifest = json.loads(Path(result.data["manifest"]).read_text())
    assert manifest["transcript"]["language"] is None and manifest["transcript"]["source"] == "unknown"
    assert "真实字幕" in Path(result.data["transcript"]).read_text()


def test_auto_falls_back_only_when_mcp_is_unavailable(settings, monkeypatch):
    monkeypatch.setattr(StdioMCPClient, "available", lambda config: True)
    monkeypatch.setattr(OpenCLIMCPProvider, "_run", lambda *args: (_ for _ in ()).throw(RuntimeUnavailable("no host")))
    from tests.unit.test_opencli import video_rows

    monkeypatch.setattr(
        OpenCLIProvider,
        "_run",
        lambda self, site, command, args: (
            video_rows() if command == "video" else [{"from": "0.2s", "to": "1.5s", "content": "OpenCLI 字幕"}]
        ),
    )
    result = ingest(settings, BILI, max_cost=0)
    assert result.ok and result.data["provider"] == "opencli"
    assert "OpenCLI 字幕" in Path(result.data["transcript"]).read_text()


def test_metadata_survives_both_caption_runtimes_becoming_unavailable(settings, monkeypatch):
    from soota.providers.ytdlp import YtDlpProvider

    def unavailable(*args):
        raise RuntimeUnavailable("yt-dlp absent")

    monkeypatch.setattr(YtDlpProvider, "inspect", unavailable)
    monkeypatch.setattr(StdioMCPClient, "available", lambda config: True)

    def mcp_response(self, site, command, args):
        if command == "video":
            return {"ok": True, "rows": [{"bvid": "BV1M1421t7hT", "title": "Example"}]}
        raise RuntimeUnavailable("MCP browser disconnected")

    monkeypatch.setattr(OpenCLIMCPProvider, "_run", mcp_response)
    monkeypatch.setattr(
        OpenCLIProvider, "_run", lambda *args: (_ for _ in ()).throw(RuntimeUnavailable("Bridge unavailable"))
    )
    result = ingest(settings, BILI, max_cost=0)
    assert result.status == "partial" and result.data["provider"] == "opencli-mcp"
    assert Path(result.data["manifest"]).is_file()


def test_mcp_access_failure_never_replays_in_opencli_or_tikhub(settings, monkeypatch):
    monkeypatch.setattr(StdioMCPClient, "available", lambda config: True)
    monkeypatch.setattr(
        OpenCLIMCPProvider, "_run", lambda *args: (_ for _ in ()).throw(AuthBudgetError("auth_required"))
    )
    monkeypatch.setattr(OpenCLIProvider, "__init__", lambda self, settings: pytest.fail("must stop"))
    with pytest.raises(AuthBudgetError):
        ingest(settings, BILI, max_cost=0)


def test_mcp_unknown_outcome_never_replays_in_opencli(settings, monkeypatch):
    monkeypatch.setattr(StdioMCPClient, "available", lambda config: True)
    monkeypatch.setattr(
        OpenCLIMCPProvider, "_run", lambda *args: (_ for _ in ()).throw(SourceError("command_outcome_unknown"))
    )
    monkeypatch.setattr(OpenCLIProvider, "__init__", lambda self, settings: pytest.fail("must stop"))
    with pytest.raises(SourceError, match="command_outcome_unknown"):
        ingest(settings, BILI, max_cost=0)


def test_mcp_x_catalog_is_bounded_free_snapshot(settings, monkeypatch):
    monkeypatch.setattr(StdioMCPClient, "available", lambda config: True)
    monkeypatch.setattr(
        OpenCLIMCPProvider,
        "_run",
        lambda self, site, command, args: {
            "ok": True,
            "rows": [{"id": "123456", "text": "Post", "author": "example", "created_at": "2026-09-24T10:00:00Z"}],
            "nextCursor": "opaque",
        },
    )
    result = catalog(settings, X, max_items=2, max_cost=0)
    assert result.ok and result.data["provider"] == "opencli-mcp"
    assert result.data["completeness"] == "unknown" and result.data["resumable"] is False
    assert "TikHub" in Path(result.data["content"]).read_text()


def test_x_snapshot_uses_opencli_when_requested_count_exceeds_mcp_limit(settings, monkeypatch):
    monkeypatch.setattr(StdioMCPClient, "available", lambda config: True)
    estimate = estimate_catalog(settings, X, max_items=150, max_cost=0)
    assert estimate.data["provider"] == "opencli"


def test_x_mcp_host_fallback_reloads_opencli_incremental_baseline(settings, monkeypatch):
    monkeypatch.setattr(
        OpenCLIProvider,
        "_run",
        lambda *args: [{"id": "123456", "text": "Post", "author": "example", "created_at": "2026-09-24T10:00:00Z"}],
    )
    first = catalog(settings, X, provider="opencli", max_items=5, max_cost=0)
    assert first.data["item_count"] == 1
    monkeypatch.setattr(StdioMCPClient, "available", lambda config: True)
    monkeypatch.setattr(OpenCLIMCPProvider, "_run", lambda *args: (_ for _ in ()).throw(RuntimeUnavailable("no host")))
    second = catalog(settings, X, max_items=5, max_cost=0, incremental=True)
    assert second.ok and second.data["provider"] == "opencli"
    assert second.data["item_count"] == 0
    assert any("opencli-mcp 不可用" in warning for warning in second.warnings)


def test_mcp_rejects_non_finite_video_duration():
    with pytest.raises(ContractError, match="时长"):
        normalize_video(recognize(BILI), {"bvid": "BV1M1421t7hT", "title": "Example", "duration_s": float("nan")})


def test_mcp_feature_output_stays_off_stdout(settings, monkeypatch, tmp_path):
    monkeypatch.setattr(StdioMCPClient, "available", lambda config: True)
    monkeypatch.setattr(
        OpenCLIMCPProvider,
        "_run",
        lambda self, site, command, args: {"ok": True, "rows": [{"body": "large response"}]},
    )
    output = tmp_path / "response.json"
    result = call_feature(settings, "opencli-mcp.bilibili.video", params=["bvid=BV1M1421t7hT"], output=output)
    assert result.data["output"] == str(output)
    assert "response" not in result.data
    assert "large response" in output.read_text()
    with pytest.raises(SourceError):
        call_feature(
            settings,
            "youtube.video.detail",
            params=["url=https://www.youtube.com/watch?v=dQw4w9WgXcQ"],
            provider="opencli-mcp",
        )
