import json
from datetime import datetime
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from soota.cli import app
from soota.counterpart import author_evidence, same_episode, youtube_counterpart
from soota.errors import InputError, SourceError
from soota.features import list_features
from soota.models import (
    CaptionResult,
    MediaItem,
    TranscriptDocument,
    TranscriptSegment,
)
from soota.pipeline import ingest
from soota.providers.mcp_client import StdioMCPClient
from soota.providers.opencli import OpenCLIProvider
from soota.providers.opencli_patches import NAVIGATION, REPLACEMENT, apply_managed_fixes
from soota.routing import recognize
from soota.transcripts.promotions import filter_promotions, parse_ranges, sponsor_ranges

BILI = "https://www.bilibili.com/video/BV1M1421t7hT"
YOUTUBE = "https://www.youtube.com/watch?v=abcdefghijk"
CHANNEL = "UC" + "a" * 22


def media(platform, *, linked=True):
    return MediaItem(
        source=recognize(BILI if platform == "bilibili" else YOUTUBE),
        title="How distributed storage protects your data",
        creator="Example Studio (mid: 123)" if platform == "bilibili" else "Example Studio",
        published_at=datetime(2026, 1, 1),
        duration_seconds=300,
        extras={"channelId": CHANNEL, "description": f"https://www.youtube.com/channel/{CHANNEL}" if linked else ""},
        provenance={"provider": "opencli"},
    )


def document():
    return TranscriptDocument(
        status="ready",
        source="unknown",
        segments=[
            TranscriptSegment(
                segment_id=f"seg-{i:06}", start_ms=i * 1000, end_ms=(i + 1) * 1000, text=f"text {i}", source="unknown"
            )
            for i in range(1, 5)
        ],
    )


class Client:
    def __init__(self, *, linked=True, missing=False, ambiguous=False):
        self.linked, self.missing, self.ambiguous = linked, missing, ambiguous
        self.calls = []

    def _run(self, site, command, args):
        self.calls.append((command, args))
        if command == "channel":
            return [{"field": k, "value": v} for k, v in {"name": "Example Studio", "channelId": CHANNEL}.items()]
        if "--type" in args and args[args.index("--type") + 1] == "channel":
            rows = [{"title": "Example Studio", "url": f"https://www.youtube.com/channel/{CHANNEL}"}]
            if self.ambiguous:
                rows += [{"title": "Example Studio", "url": "https://www.youtube.com/@another"}]
            return rows
        return [{"title": media("youtube").title, "channel": "Example Studio", "url": YOUTUBE}]

    def inspect(self, source):
        return media("youtube")

    def get_captions(self, item):
        return (
            CaptionResult(status="missing")
            if self.missing
            else CaptionResult(status="ready", segments=document().segments)
        )


@pytest.mark.parametrize(
    "linked,missing,ambiguous,selected",
    [(True, False, False, True), (False, False, False, True), (True, True, False, False), (True, False, True, False)],
)
def test_counterpart_requires_identity_episode_and_captions(linked, missing, ambiguous, selected):
    original = media("bilibili", linked=linked)
    item, captions, warnings = youtube_counterpart(Client(missing=missing, ambiguous=ambiguous), original)
    assert (item.source.platform == "youtube") == selected
    assert bool(captions) == selected
    assert warnings
    if selected:
        assert item.provenance["selection"]["requested_source"]["id"] == original.source.id
    else:
        assert original.provenance["youtube_check"]["status"] != "selected"


def test_identity_rejects_other_channel_and_lookalike_link():
    original, video = media("bilibili"), media("youtube")
    assert not author_evidence(original, {"channelId": "other"}, video)
    original.extras["description"] = f"https://www.youtube.com.evil.example/channel/{CHANNEL}"
    assert not author_evidence(original, {"channelId": CHANNEL}, video)
    video.duration_seconds = 800
    assert not same_episode(original, video)


def test_filter_preserves_raw_document_ids_and_times():
    raw = document()
    filtered, report, _ = filter_promotions(media("youtube"), raw, automatic=False, ranges=[(2.2, 3.2)])
    assert [s.segment_id for s in filtered.segments] == ["seg-000001", "seg-000004"]
    assert filtered.segments[-1].start_ms == 4000
    assert len(raw.segments) == 4
    assert report["removed_segment_ids"] == ["seg-000002", "seg-000003"]


@pytest.mark.parametrize("value", ["nan:5", "1:inf", "-1:2", "3:2", "a:b", "1:2:3"])
def test_bad_exclusions_rejected(value):
    with pytest.raises(InputError):
        parse_ranges([value])


def test_sponsor_hash_result_is_scoped_and_duration_checked(monkeypatch):
    item = media("youtube")
    data = [
        {"videoID": "other", "segments": [{"category": "sponsor", "segment": [0, 99]}]},
        {"videoID": item.source.id, "segments": [{"category": "sponsor", "segment": [2, 4], "videoDuration": 300}]},
    ]

    def request(url, **kwargs):
        assert item.source.id not in url and item.source.id not in str(kwargs)
        return httpx.Response(200, json=data, request=httpx.Request("GET", url))

    monkeypatch.setattr("soota.transcripts.promotions.httpx.get", request)
    assert sponsor_ranges(item) == ([(2.0, 4.0)], "annotated")
    data[1]["segments"][0]["videoDuration"] = 400
    assert sponsor_ranges(item) == ([], "duration_mismatch")


def test_sponsor_missing_or_unavailable_is_not_ad_free(monkeypatch):
    monkeypatch.setattr(
        "soota.transcripts.promotions.httpx.get",
        lambda url, **kw: httpx.Response(404, request=httpx.Request("GET", url)),
    )
    assert sponsor_ranges(media("youtube")) == ([], "no_annotations")
    _, report, warnings = filter_promotions(media("youtube"), document(), automatic=True, ranges=[])
    assert report["review_required"] and warnings


def test_mcp_disabled_never_starts_a_process(settings, monkeypatch):
    monkeypatch.setattr(
        "soota.providers.mcp_client.subprocess.Popen", lambda *a, **k: pytest.fail("MCP process started")
    )
    with pytest.raises(InputError, match="停用"):
        StdioMCPClient(settings.opencli_mcp).call("sites_search", {})
    assert list_features(settings, provider="opencli-mcp").data["features"] == []
    with pytest.raises(InputError, match="停用"):
        ingest(settings, BILI, provider="opencli-mcp")


def test_filter_roundtrip_and_disabled_search(settings, monkeypatch, tmp_path):
    monkeypatch.setattr("soota.pipeline.youtube_counterpart", lambda *a: pytest.fail("flag is off"))
    monkeypatch.setattr(OpenCLIProvider, "inspect", lambda self, source: media("youtube"))
    monkeypatch.setattr(
        OpenCLIProvider, "get_captions", lambda self, item: CaptionResult(status="ready", segments=document().segments)
    )
    result = ingest(settings, YOUTUBE, provider="opencli", max_cost=0, exclude_ranges=["2:4"])
    folder = Path(result.data["output"])
    assert "text 2" not in (folder / "transcript.md").read_text()
    assert "text 2" in (folder / "transcript.original.md").read_text()
    analysis = tmp_path / "analysis.json"
    analysis.write_text('{"summary":"Example","key_points":[]}')
    manifest_path = folder / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    (folder / "content.md").write_text("Existing user analysis")
    manifest["analysis"] = {"status": "ready", "path": "content.md"}
    manifest["files"]["content.md"] = "content.md"
    manifest_path.write_text(json.dumps(manifest))
    assert "transcript.original.md" in json.loads((folder / "manifest.json").read_text())["files"]
    with pytest.raises(InputError, match="过滤结果变化"):
        ingest(settings, YOUTUBE, provider="opencli", max_cost=0)
    ingest(settings, YOUTUBE, provider="opencli", max_cost=0, force=True)
    assert "text 2" in (folder / "transcript.md").read_text()


def test_ingest_selects_youtube_bundle_and_keeps_request_evidence(settings, monkeypatch):
    original = media("bilibili")
    chosen = media("youtube")
    chosen.provenance["selection"] = {"requested_source": original.source.model_dump()}
    monkeypatch.setattr(OpenCLIProvider, "inspect", lambda self, source: original)
    monkeypatch.setattr(
        "soota.pipeline.youtube_counterpart",
        lambda *a: (chosen, CaptionResult(status="ready", segments=document().segments), []),
    )
    monkeypatch.setattr(OpenCLIProvider, "get_captions", lambda *a: pytest.fail("captions fetched twice"))
    result = ingest(settings, BILI, prefer_youtube=True, exclude_promotions=False, max_cost=0)
    assert "/youtube/abcdefghijk" in result.data["output"]
    manifest = json.loads(Path(result.data["manifest"]).read_text())
    assert manifest["metadata"]["provenance"]["selection"]["requested_source"]["platform"] == "bilibili"


def test_latest_update_resolves_version_at_invocation(monkeypatch):
    calls = []
    monkeypatch.setattr("soota.runtime_cli.package_metadata", lambda: {"version": "9.1.2"})
    monkeypatch.setattr("soota.runtime_cli.OpenCLIRuntime.update", lambda self, v: calls.append(v) or {"version": v})
    result = CliRunner().invoke(app, ["runtime", "update", "--json"])
    assert result.exit_code == 0 and calls == ["9.1.2"]


def test_managed_patch_is_idempotent_and_ignores_changed_upstream(tmp_path):
    target = tmp_path / "node_modules/@jackwener/opencli/dist/src/browser/page.js"
    target.parent.mkdir(parents=True)
    target.write_text(
        "    async goto(url, options) {\n        let result;\n        try {\n" + NAVIGATION + "\nother code"
    )
    apply_managed_fixes(tmp_path)
    first = target.read_text()
    assert REPLACEMENT in first
    apply_managed_fixes(tmp_path)
    assert target.read_text() == first
    target.write_text("new upstream implementation")
    apply_managed_fixes(tmp_path)
    assert target.read_text() == "new upstream implementation"


def test_same_filter_can_be_repeated_without_force(settings, monkeypatch):
    monkeypatch.setattr(OpenCLIProvider, "inspect", lambda *a: media("youtube"))
    monkeypatch.setattr(
        OpenCLIProvider, "get_captions", lambda *a: CaptionResult(status="ready", segments=document().segments)
    )
    for _ in range(2):
        assert ingest(settings, YOUTUBE, max_cost=0, exclude_ranges=["2:3"]).ok


def test_counterpart_source_failure_preserves_bilibili():
    class Broken(Client):
        def _run(self, *args):
            raise SourceError("failed")

    original = media("bilibili")
    item, captions, warnings = youtube_counterpart(Broken(), original)
    assert item is original and captions is None and warnings
    assert item.provenance["youtube_check"]["status"] == "failed"


def test_name_based_selection_does_not_claim_verified_author_from_reverse_link():
    class Reposter(Client):
        def inspect(self, source):
            item = media("youtube")
            item.extras["description"] = "https://space.bilibili.com/123"
            return item

    item, captions, _ = youtube_counterpart(Reposter(), media("bilibili", linked=False))
    assert item.source.platform == "youtube" and captions is not None
    assert not item.provenance["selection"]["author_crosslink_confirmed"]


@pytest.mark.parametrize("first,second", [("01", "02"), ("一", "二")])
def test_adjacent_episodes_never_match(first, second):
    a, b = media("bilibili"), media("youtube")
    a.title = f"分布式存储基础教程第{first}集：数据一致性与复制"
    b.title = f"分布式存储基础教程第{second}集：数据一致性与复制"
    assert not same_episode(a, b)


def test_connect_never_restarts_busy_foreign_daemon(monkeypatch):
    from soota.providers.opencli_context import connect_runtime
    from soota.providers.process import ProcessResult

    calls = []
    monkeypatch.setattr(
        "soota.providers.process.run_process", lambda argv, **kw: calls.append(argv) or ProcessResult(0, "1.8.8", "")
    )
    monkeypatch.setattr(
        "soota.providers.opencli_context.daemon_status", lambda: {"daemonVersion": "1.8.3", "pending": 1}
    )
    with pytest.raises(SourceError):
        connect_runtime("opencli", replace_idle=True)
    assert calls == [["opencli", "--version"]]


def test_connect_starts_missing_daemon_and_waits_for_extension(monkeypatch):
    from soota.providers.opencli_context import connect_runtime
    from soota.providers.opencli_runtime import RuntimeUnavailable
    from soota.providers.process import ProcessResult

    calls, states = [], [None, None, {"daemonVersion": "1.8.8", "extensionConnected": True}]

    def status():
        value = states.pop(0)
        if value is None:
            raise RuntimeUnavailable("not started")
        return value

    monkeypatch.setattr("soota.providers.opencli_context.daemon_status", status)
    monkeypatch.setattr(
        "soota.providers.process.run_process", lambda argv, **kw: calls.append(argv) or ProcessResult(0, "1.8.8", "")
    )
    monkeypatch.setattr("time.sleep", lambda _: None)
    assert connect_runtime("opencli")["extensionConnected"]
    assert calls == [["opencli", "--version"], ["opencli", "daemon", "restart"]]


def test_navigation_patch_verifies_actual_page_and_never_replays_navigation(tmp_path):
    import shutil
    import subprocess

    from soota.providers.opencli_patches import NAVIGATION_HELPER

    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required only for the external runtime patch check")
    script = tmp_path / "navigation.mjs"
    script.write_text(
        NAVIGATION_HELPER
        + r"""
import assert from 'node:assert/strict';
const events = [];
const page = {
    active: 'owned-old',
    async evaluate(code) {
        return code === 'location.href' ? 'about:blank' : { href: 'https://www.bilibili.com/', ready: 'complete' };
    },
    getActivePage() { return this.active; },
    async newTab(url) { events.push(['new', url]); return 'owned-new'; },
    setActivePage(target) { this.active = target; },
    async closeTab(target) { events.push(['close', target]); },
};
assert.equal(await sootaNavigate(page, 'https://www.bilibili.com/'), 'owned-new');
assert.deepEqual(events, [['new', 'https://www.bilibili.com/'], ['close', 'owned-old']]);
let now = 0;
Date.now = () => (now += 20000);
await assert.rejects(() => sootaNavigate(page, 'https://www.youtube.com/'), /did not reach/);
assert.equal(events.filter(([action]) => action === 'new').length, 2);
"""
    )
    result = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_channel_brand_number_is_not_episode_number():
    a, b = media("bilibili"), media("youtube")
    a.creator = "Example 101 (mid: 123)"
    b.creator = "Example 101"
    a.title = "83 storage experiments explained [Example 101]"
    b.title = "83 storage experiments explained"
    assert same_episode(a, b)


def test_explicit_tikhub_overrides_saved_free_caption_preference(settings, monkeypatch):
    from soota.models import CommandResult

    settings.content_selection.prefer_youtube = True
    monkeypatch.setattr(
        "soota.pipeline.estimate",
        lambda *a, **kw: CommandResult(ok=True, status="success", code="ok", exit_code=0, message="estimate", data={}),
    )
    assert ingest(settings, BILI, provider="tikhub", dry_run=True).ok
    with pytest.raises(InputError, match="免费|auto/opencli"):
        ingest(settings, BILI, provider="tikhub", dry_run=True, prefer_youtube=True)
