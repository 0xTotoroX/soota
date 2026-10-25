from __future__ import annotations

import hashlib
import json
from pathlib import Path

from soota.storage.atomic import atomic_write

# Upstream match fragments: Apache-2.0, copyright 2025 jackwener/OpenCLI.
# Soota-authored patch code/additions: Copyright 2026 Soota contributors; GPL-3.0-only.
# Only managed installations are modified; retain their upstream license and notices.
# https://github.com/jackwener/OpenCLI/issues/2487
NAVIGATION = """            result = await sendCommandFull('navigate', {
                url,
                ...this._cmdOpts(),
            });"""
REPLACEMENT = r"""            // Copyright 2026 Soota contributors; GPL-3.0-only.
            // Modified by Soota: supported media adapters use a verified owned tab.
            if (this.surface === 'adapter' && /^https:\/\/(www\.)?(bilibili\.com|youtube\.com)(\/|$)/.test(url)) {
                result = { page: await sootaNavigate(this, url) };
            } else {
                result = await sendCommandFull('navigate', {
                    url,
                    ...this._cmdOpts(),
                });
            }"""
NAVIGATION_HELPER = """// Copyright 2026 Soota contributors; GPL-3.0-only.
// Modified by Soota: avoid Browser Bridge detach/update race for supported media sites.
async function sootaNavigate(page, url) {
    const expected = new URL(url);
    const actual = await page.evaluate('location.href');
    if (actual !== url) {
        const previous = page.getActivePage();
        const target = await page.newTab(url);
        if (!target) throw new Error('Navigation returned no page');
        page.setActivePage(target);
        // Only release the page owned by this adapter session, never user tabs.
        if (previous && previous !== target) await page.closeTab(previous);
    }
    const deadline = Date.now() + 15000;
    while (Date.now() < deadline) {
        const state = await page.evaluate('({ href: location.href, ready: document.readyState })');
        const arrived = new URL(state.href);
        const correctPath = expected.pathname === '/' || arrived.pathname === expected.pathname;
        if (arrived.origin === expected.origin && correctPath && state.ready !== 'loading') {
            return page.getActivePage();
        }
        await new Promise(resolve => setTimeout(resolve, 250));
    }
    throw new Error('Navigation did not reach the requested page');
}
"""


YOUTUBE_TIMEDTEXT_FILTERS = (
    (
        "        if (!url.includes('fmt=json3') || !url.includes('pot='))\n            return false;",
        "        // Copyright 2026 Soota contributors; GPL-3.0-only.\n"
        "        // Modified by Soota: valid JSON3 captions may omit pot.\n"
        "        if (!url.includes('fmt=json3'))\n            return false;",
    ),
    (
        "                        && url.includes('fmt=json3')\n"
        "                        && url.includes('pot=')\n"
        "                        && timedtextUrlMatchesVideo(url));",
        "                        && url.includes('fmt=json3')\n"
        "                        // Copyright 2026 Soota contributors; GPL-3.0-only.\n"
        "                        // Modified by Soota: pot is not required for every caption track.\n"
        "                        && timedtextUrlMatchesVideo(url));",
    ),
    (
        "          if (!url.includes('pot=')) return false;\n"
        "          if (!timedtextUrlMatchesVideo(url)) return false;",
        "          // Copyright 2026 Soota contributors; GPL-3.0-only.\n"
        "          // Modified by Soota: accept captured JSON3 without pot; keep video identity checks.\n"
        "          if (!timedtextUrlMatchesVideo(url)) return false;",
    ),
)

YOUTUBE_LIVE_TRACK_ANCHOR = """        try {
          if (boundOriginalFetch) {"""
YOUTUBE_LIVE_TRACK = """        try {
          // Copyright 2026 Soota contributors; GPL-3.0-only.
          // Modified by Soota: use the loaded player's signed URL before waiting
          // for caption traffic. A separately fetched watch HTML URL may be empty.
          try {
            const response = player.getPlayerResponse?.();
            const tracks = response?.captions?.playerCaptionsTracklistRenderer?.captionTracks;
            const liveTrack = response?.videoDetails?.videoId === targetVideoId
              && Array.isArray(tracks) && tracks.find(candidate => track.vss_id
                ? candidate.vssId === track.vss_id
                : candidate.languageCode === track.languageCode
                  && (candidate.kind || '') === (track.kind || ''));
            if (liveTrack?.baseUrl && boundOriginalFetch) {
              const url = new URL(liveTrack.baseUrl, location.origin);
              if (url.origin === location.origin && url.pathname === '/api/timedtext'
                  && timedtextUrlMatchesVideo(url.href)) {
                url.searchParams.set('fmt', 'json3');
                const resp = await boundOriginalFetch(url.href, {
                  credentials: 'include', signal: AbortSignal.timeout(5000),
                });
                if (resp.ok) {
                  const parsed = parseJson3(await resp.text());
                  if (parsed.rows?.length) return parsed.rows;
                }
              }
            }
          } catch {
            // Token-required tracks and transient failures retain upstream capture.
          }
          if (boundOriginalFetch) {"""

# Adapted from OpenCLI PRs #2446 and #2563 (Apache-2.0).
# Keep filter application/settling intact; only disambiguate the same logical chip.
XIAOHONGSHU_FILTER_SOURCES = [
    "https://github.com/jackwener/OpenCLI/issues/2445",
    "https://github.com/jackwener/OpenCLI/pull/2446",
    "https://github.com/jackwener/OpenCLI/pull/2563",
]
XIAOHONGSHU_FILTERS = (
    (
        """        const visible = (element) => {
          if (!element) return false;
          const rect = element.getBoundingClientRect();
          const style = getComputedStyle(element);
          return rect.width > 0 && rect.height > 0 &&
            style.display !== 'none' && style.visibility !== 'hidden';
        };""",
        """        // Copyright 2026 Soota contributors; GPL-3.0-only.
        // Modified by Soota; adapted from OpenCLI #2563 (Apache-2.0).
        const visible = (element) => {
          if (!element) return false;
          const rect = element.getBoundingClientRect();
          const style = getComputedStyle(element);
          if (element.closest('[data-hp-kind], [aria-hidden="true"]')) return false;
          return rect.width > 0 && rect.height > 0 &&
            style.display !== 'none' && style.visibility !== 'hidden' &&
            Number(style.opacity) > 0.01 && style.pointerEvents !== 'none';
        };""",
    ),
    (
        """          if (options.length !== 1) {
            return { status: 'layout', detail: options.length ? 'ambiguous_option' : 'option_not_found' };
          }
          return { status: 'ok', option: options[0] };""",
        """          // Copyright 2026 Soota contributors; GPL-3.0-only.
          // Modified by Soota; adapted from OpenCLI #2446 (Apache-2.0).
          if (options.length !== 1) {
            const first = options[0];
            const rect = first?.getBoundingClientRect();
            const keys = ['left', 'top', 'width', 'height'];
            const sameOption = rect && keys.every(key => Number.isFinite(rect[key])) &&
              options.every(option => {
                const other = option.getBoundingClientRect();
                return keys.every(key => other[key] === rect[key]) &&
                  option.classList.contains('active') === first.classList.contains('active');
              });
            if (!sameOption) {
              return { status: 'layout', detail: options.length ? 'ambiguous_option' : 'option_not_found' };
            }
          }
          return { status: 'ok', option: options[0] };""",
    ),
)


def _patch_xiaohongshu_filters(install: Path) -> str | None:
    relative = "node_modules/@jackwener/opencli/clis/xiaohongshu/search.js"
    path = install / relative
    if not path.is_file():
        return None
    original = path.read_text(encoding="utf-8")
    if original.count("function buildApplySearchFiltersJs(requestedFilters) {") != 1:
        return None
    if all(original.count(new) == 1 and old not in original for old, new in XIAOHONGSHU_FILTERS):
        return relative
    if not all(original.count(old) == 1 and new not in original for old, new in XIAOHONGSHU_FILTERS):
        return None
    for old, new in XIAOHONGSHU_FILTERS:
        original = original.replace(old, new, 1)
    atomic_write(path, original)
    return relative


def _patch_youtube_live_track(install: Path) -> str | None:
    relative = "node_modules/@jackwener/opencli/clis/youtube/transcript.js"
    path = install / relative
    if not path.is_file():
        return None
    original = path.read_text(encoding="utf-8")
    if original.count(YOUTUBE_LIVE_TRACK) == 1:
        return relative
    if original.count(YOUTUBE_LIVE_TRACK_ANCHOR) != 1:
        return None
    atomic_write(path, original.replace(YOUTUBE_LIVE_TRACK_ANCHOR, YOUTUBE_LIVE_TRACK, 1))
    return relative


def _patch_youtube(install: Path) -> str | None:
    relative = "node_modules/@jackwener/opencli/clis/youtube/transcript.js"
    path = install / relative
    if not path.is_file():
        return None
    original = path.read_text(encoding="utf-8")
    # Patch the three known capture paths together. Do not guess when upstream drifts.
    if all(original.count(new) == 1 for _, new in YOUTUBE_TIMEDTEXT_FILTERS):
        return relative
    if not all(original.count(old) == 1 for old, _ in YOUTUBE_TIMEDTEXT_FILTERS):
        return None
    for old, new in YOUTUBE_TIMEDTEXT_FILTERS:
        original = original.replace(old, new, 1)
    atomic_write(path, original)
    return relative


def _patch_navigation(install: Path) -> str | None:
    relative = "node_modules/@jackwener/opencli/dist/src/browser/page.js"
    path = install / relative
    if not path.is_file():
        return None
    original = path.read_text(encoding="utf-8")
    if REPLACEMENT not in original:
        # Two matching calls exist: first navigation and stale-target recovery.
        # Replace only the first, scoped to the known Page.goto implementation.
        if "    async goto(url, options) {\n        let result;\n        try {\n" + NAVIGATION not in original:
            return None
        original = original.replace(NAVIGATION, REPLACEMENT, 1)
    if NAVIGATION_HELPER not in original:
        original += "\n" + NAVIGATION_HELPER
    atomic_write(path, original)
    return relative


def apply_managed_fixes(install: Path) -> None:
    applied = {
        name: relative
        for name, relative in (
            ("media-owned-tab-navigation", _patch_navigation(install)),
            ("youtube-timedtext-optional-pot", _patch_youtube(install)),
            ("youtube-live-player-caption-url", _patch_youtube_live_track(install)),
            ("xiaohongshu-search-filter-options", _patch_xiaohongshu_filters(install)),
        )
        if relative is not None
    }
    if not applied:
        return
    atomic_write(
        install / "soota-compatibility.json",
        json.dumps(
            {
                "patch": next(iter(applied)),
                "patches": list(applied),
                "modified_by": "Soota",
                "soota_modification_license": "GPL-3.0-only",
                "upstream_sources": {"xiaohongshu-search-filter-options": XIAOHONGSHU_FILTER_SOURCES}
                if "xiaohongshu-search-filter-options" in applied
                else {},
                "files": [
                    {"file": relative, "sha256": hashlib.sha256((install / relative).read_bytes()).hexdigest()}
                    for relative in dict.fromkeys(applied.values())
                ],
            },
            indent=2,
        )
        + "\n",
    )
