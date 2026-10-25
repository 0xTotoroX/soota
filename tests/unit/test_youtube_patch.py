import json
import shutil
import subprocess

import pytest

from soota.providers.opencli_patches import (
    YOUTUBE_LIVE_TRACK,
    YOUTUBE_LIVE_TRACK_ANCHOR,
    YOUTUBE_TIMEDTEXT_FILTERS,
    apply_managed_fixes,
)

RELATIVE = "node_modules/@jackwener/opencli/clis/youtube/transcript.js"


def fixture_source():
    first, second, third = [old for old, _ in YOUTUBE_TIMEDTEXT_FILTERS]
    return (
        """
const videoId = 'Example0001';
function timedtextUrlMatchesVideo(url) {
    return new URL(url).searchParams.get('v') === videoId;
}
function capture(url) {
    if (!url.includes('/api/timedtext')) return false;
"""
        + first
        + """
    if (!timedtextUrlMatchesVideo(url)) return false;
    return true;
}
function resources(urls) {
    return urls.filter(url => url.includes('/api/timedtext')
"""
        + second
        + """
}
function hook(url) {
    if (!url || !url.includes('/api/timedtext')) return false;
    if (!url.includes('fmt=json3')) return false;
"""
        + third
        + """
    return true;
}
"""
    )


def write_fixture(tmp_path):
    path = tmp_path / RELATIVE
    path.parent.mkdir(parents=True)
    path.write_text(fixture_source())
    return path


def test_patch_accepts_caption_urls_with_or_without_pot_and_keeps_video_scope(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required for the managed JavaScript runtime regression")
    path = write_fixture(tmp_path)
    script = tmp_path / "check.mjs"
    checks = """
import assert from 'node:assert/strict';
const valid = 'https://www.youtube.com/api/timedtext?v=Example0001&fmt=json3&lang=zh';
for (const filter of [capture, url => resources([url]).length === 1, hook]) {
    assert.equal(filter(valid), EXPECT_NO_POT);
    assert.equal(filter(valid + '&pot=token'), true);
    assert.equal(filter(valid.replace('Example0001', 'Example0002') + '&pot=token'), false);
    assert.equal(filter(valid.replace('Example0001', 'Example0001suffix')), false);
    assert.equal(filter(valid.replace('json3', 'srv3')), false);
    assert.equal(filter(valid.replace('/api/timedtext', '/watch')), false);
}
"""
    for patched in (False, True):
        if patched:
            apply_managed_fixes(tmp_path)
        script.write_text(path.read_text() + checks.replace("EXPECT_NO_POT", str(patched).lower()))
        result = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stderr
    manifest = json.loads((tmp_path / "soota-compatibility.json").read_text())
    assert manifest["soota_modification_license"] == "GPL-3.0-only"
    assert "Copyright 2026 Soota contributors; GPL-3.0-only" in path.read_text()
    assert manifest["patches"] == ["youtube-timedtext-optional-pot"]
    assert manifest["files"][0]["file"] == RELATIVE


def test_patch_is_idempotent(tmp_path):
    path = write_fixture(tmp_path)
    apply_managed_fixes(tmp_path)
    original = path.read_bytes()
    manifest = (tmp_path / "soota-compatibility.json").read_bytes()
    apply_managed_fixes(tmp_path)
    assert path.read_bytes() == original
    assert (tmp_path / "soota-compatibility.json").read_bytes() == manifest


@pytest.mark.parametrize("change", ["drift", "duplicate"])
def test_patch_skips_entire_transcript_when_any_capture_path_does_not_match(tmp_path, change):
    path = write_fixture(tmp_path)
    first = YOUTUBE_TIMEDTEXT_FILTERS[0][0]
    altered = path.read_text().replace(first, "// changed upstream") if change == "drift" else path.read_text() + first
    path.write_text(altered)
    apply_managed_fixes(tmp_path)
    assert path.read_text() == altered
    assert not (tmp_path / "soota-compatibility.json").exists()


def test_live_player_url_before_capture_with_bounded_failure_and_identity_checks(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required for the managed JavaScript runtime regression")
    source = (
        """
import assert from 'node:assert/strict';
let state;
const location = { origin: 'https://www.youtube.com' };
async function extract() {
    const targetVideoId = 'Example0001';
    const track = state.selected || {languageCode: 'zh', vss_id: '.zh'};
    const player = {getPlayerResponse: () => state.response};
    function timedtextUrlMatchesVideo(url) {
        return new URL(url).searchParams.get('v') === targetVideoId;
    }
    function parseJson3(text) {
        try { return {rows: JSON.parse(text).events}; } catch { return {error: 'invalid JSON3'}; }
    }
    const boundOriginalFetch = async (url, options) => {
        state.requests.push(url);
        assert.equal(new URL(url).searchParams.get('fmt'), 'json3');
        assert.equal(options.credentials, 'include');
        assert.ok(options.signal instanceof AbortSignal);
        if (state.failure) throw new Error(state.failure);
        return {ok: state.ok !== false, text: async () => state.body};
    };
"""
        + YOUTUBE_LIVE_TRACK_ANCHOR
        + """
            state.captureCalls++;
          }
          return null;
        } finally {
          state.cleanupCalls++;
        }
}
function reset() {
    state = {
        response: {videoDetails: {videoId: 'Example0001'}, captions: {
            playerCaptionsTracklistRenderer: {captionTracks: [{
                baseUrl: 'https://www.youtube.com/api/timedtext?v=Example0001&lang=zh',
                languageCode: 'zh', vssId: '.zh',
            }]},
        }},
        body: JSON.stringify({events: [{start: 0, end: 1.5, text: 'Synthetic caption'}]}),
        requests: [], captureCalls: 0, cleanupCalls: 0,
    };
}
reset();
assert.deepEqual(await extract(), EXPECT_SUCCESS ? JSON.parse(state.body).events : null);
assert.equal(state.requests.length, EXPECT_SUCCESS ? 1 : 0);
assert.equal(state.captureCalls, EXPECT_SUCCESS ? 0 : 1);
assert.equal(state.cleanupCalls, 1);
if (EXPECT_SUCCESS) {
    for (const change of [
        () => {state.response.videoDetails.videoId = 'Example0002';},
        () => {state.response.captions.playerCaptionsTracklistRenderer.captionTracks[0].vssId = 'a.zh';},
        () => {state.selected = {languageCode: 'en'};},
        () => {state.response.captions = undefined;},
        ...[
            'https://www.youtube.com/api/timedtext?v=Example0001suffix',
            'https://www.youtube.com/watch?v=Example0001',
            'https://example.org/api/timedtext?v=Example0001',
        ].map(url => () => {state.response.captions.playerCaptionsTracklistRenderer.captionTracks[0].baseUrl = url;}),
    ]) {
        reset(); change();
        assert.equal(await extract(), null);
        assert.equal(state.requests.length, 0);
        assert.equal(state.captureCalls, 1);
        assert.equal(state.cleanupCalls, 1);
    }
    for (const change of [
        () => {state.body = '';},
        () => {state.body = 'malformed';},
        () => {state.body = '{"events":[]}';},
        () => {state.ok = false;},
        () => {state.failure = 'TimeoutError';},
        () => {state.failure = 'NetworkError';},
    ]) {
        reset(); change();
        assert.equal(await extract(), null);
        assert.equal(state.requests.length, 1);
        assert.equal(state.captureCalls, 1);
        assert.equal(state.cleanupCalls, 1);
    }
}
"""
    )
    path = tmp_path / RELATIVE
    path.parent.mkdir(parents=True)
    path.write_text(source)
    script = tmp_path / "live.mjs"
    for patched in (False, True):
        if patched:
            apply_managed_fixes(tmp_path)
        script.write_text(path.read_text().replace("EXPECT_SUCCESS", str(patched).lower()))
        result = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stderr
    original = path.read_bytes()
    apply_managed_fixes(tmp_path)
    assert path.read_bytes() == original


@pytest.mark.parametrize("count", [0, 2])
def test_live_track_patch_skips_changed_or_ambiguous_upstream(tmp_path, count):
    path = tmp_path / RELATIVE
    path.parent.mkdir(parents=True)
    source = YOUTUBE_LIVE_TRACK_ANCHOR * count
    path.write_text(source)
    apply_managed_fixes(tmp_path)
    assert path.read_text() == source
    assert not (tmp_path / "soota-compatibility.json").exists()


def test_live_track_upgrade_keeps_existing_patch_and_one_file_hash(tmp_path):
    path = write_fixture(tmp_path)
    apply_managed_fixes(tmp_path)
    path.write_text(path.read_text() + YOUTUBE_LIVE_TRACK_ANCHOR)
    apply_managed_fixes(tmp_path)
    assert YOUTUBE_LIVE_TRACK in path.read_text()
    manifest = json.loads((tmp_path / "soota-compatibility.json").read_text())
    assert manifest["patches"] == ["youtube-timedtext-optional-pot", "youtube-live-player-caption-url"]
    assert len(manifest["files"]) == 1
