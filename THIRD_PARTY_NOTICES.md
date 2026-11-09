# Third-party notices

Soota's GPL-3.0-only declaration covers the code it is entitled to license. It does
not replace the licenses, copyright notices, or other requirements of third-party
components.
Dependencies are installed separately; their runtimes are not included in Soota's
wheel. Its OpenCLI compatibility module includes upstream source match fragments,
which retain Apache-2.0 licensing as described below. The copies below document the reviewed
versions. They are included in Soota's wheel and source distribution.

Copyright 2026 Soota contributors. The GPL declaration applies from this licensing
change to current and later versions that Soota is entitled to license. Earlier
Apache-2.0 grants are not retroactively withdrawn. The unchanged former top-level
text is retained as [historical Apache-2.0 evidence](LICENSES/soota-historical-Apache-2.0-LICENSE);
it is not an alternative license grant for newly authored GPL-covered changes.

## WeChat HTML conversion dependencies

The WeChat DOM conversion additionally uses separately installed Beautiful Soup
4.15.0 and markdownify 1.2.3. Both use the MIT license; their unmodified installed
license texts are retained as [Beautiful Soup](LICENSES/beautifulsoup4-4.15.0-LICENSE)
and [markdownify](LICENSES/markdownify-1.2.3-LICENSE). No library source is vendored.
Source references: <https://www.crummy.com/software/BeautifulSoup/> and
<https://github.com/matthewwithanm/python-markdownify>.

## TikHub Python SDK 2.1.2

- Integration: pinned Python dependency (`tikhub==2.1.2`); no upstream files modified.
- Release: <https://pypi.org/project/tikhub/2.1.2/>.
- Declared source: <https://github.com/TikHub/TikHub-API-Python-SDK-V2>.
- License text: [the unmodified release LICENSE](LICENSES/tikhub-2.1.2-LICENSE), Apache-2.0.
- **Unresolved upstream conflict:** the same wheel's METADATA and the source
  distribution's pyproject.toml declare MIT, while both distributions contain
  an Apache-2.0 LICENSE. This is not evidence of a dual-license grant. Soota
  preserves the actual license text without claiming that the conflict is resolved.
  MIT and Apache-2.0 are each compatible with GPL version 3, but that compatibility
  does not establish which grant the upstream intended or resolve the discrepancy.
- No upstream NOTICE was present in the reviewed wheel or source distribution.
- Reviewed 2026-09-22 against the artifacts pinned by `uv.lock`:
  wheel SHA-256 `9de5b6828ab2e04a97efed6054af6015ac3546cc42cf8ecaaa1e26d48faee710`;
  sdist SHA-256 `7d6be6728a4a4441e3dc3a441777414e12be072024bd73bd408cc23675f7fbbf`.

## OpenCLI 1.8.3 (adapter reference version)

- Integration: separately installed executable invoked through a read-only command allowlist;
  upstream source match fragments are embedded in Soota's compatibility patch module.
- Source: <https://github.com/jackwener/OpenCLI/tree/v1.8.3>.
- Package: <https://www.npmjs.com/package/@jackwener/opencli/v/1.8.3>.
- Copyright 2025 jackwener. License: Apache-2.0, consistently declared by
  package.json and [the unmodified LICENSE](LICENSES/opencli-1.8.3-LICENSE).
- No root NOTICE was present in the reviewed release. Soota can apply narrowly
  matched compatibility fixes to its managed runtime (see `providers/opencli_patches.py`);
  modified files identify Soota and retain upstream copyright and Apache-2.0
  notices. Soota-authored additions carry GPL-3.0-only modification notices; the
  compatibility manifest records that scope. Original upstream code is not
  relabeled as Soota-owned GPL code. Global installations
  are not modified. User-installed or Soota-managed runtimes retain
  their own license files; this reference copy does not describe every later release.

- The Xiaohongshu search compatibility patch additionally uses source fragments
  from OpenCLI v1.8.8 and adapts [PR #2446](https://github.com/jackwener/OpenCLI/pull/2446)
  and [PR #2563](https://github.com/jackwener/OpenCLI/pull/2563), under Apache-2.0.
  `tests/fixtures/opencli_xiaohongshu_filters.js` retains the upstream filter
  function and attribution; a separate synthetic DOM harness supplies no account data.
  Soota additions retain their modification notices, and the managed compatibility
  manifest records the upstream references and modified-file hashes.

## opencli-mcp 0.0.22 (external runtime, disabled by default)

- Integration: separately installed local MCP process and Chrome extension; no upstream code or runtime is bundled in Soota's Python wheel.
- Source: <https://github.com/jackwener/opencli-mcp/tree/v0.0.22>.
- npm package: <https://www.npmjs.com/package/opencli-mcp/v/0.0.22>.
- Copyright 2026 jackwener. License: Apache-2.0 as declared by its source LICENSE and npm package metadata.
- Soota calls only an audited read-only site-command allowlist through MCP stdio; the external installation retains its own license files.

## SponsorBlock annotation data (optional, fetched at runtime)

- Source and attribution: SponsorBlock contributors, <https://sponsor.ajay.app/>.
- API/database license: CC BY-NC-SA 4.0, per
  <https://github.com/ajayyy/SponsorBlock/wiki/Database-and-API-License>.
- Soota does not bundle the database. Optional promotion filtering queries annotated
  ranges and records attribution in the output manifest. These data retain their
  noncommercial/share-alike requirements; Soota GPL-3.0-only does not replace them.
  Commercial permission for Soota code is not commercial permission for these data;
  commercial users must separately establish data rights or avoid these annotations.
- Missing annotations do not establish that a video is free of promotion.

## yt-dlp (external executable)

- Source: <https://github.com/yt-dlp/yt-dlp>.
- Integration: optional separately installed executable used by the built-in `soota` Provider for YouTube metadata and captions. No upstream source or runtime is bundled or modified.
- Upstream source is primarily Unlicense; distributed binaries and dependencies may carry additional licenses. The external installation retains its own license materials.

## webvtt-py 0.5.1

- Integration: Python dependency used for WebVTT cue parsing; no upstream source is copied or modified.
- Source: <https://github.com/glut23/webvtt-py>.
- License: MIT, copyright 2016 Alejandro Mendez; [unmodified distribution license](LICENSES/webvtt-py-0.5.1-LICENSE).
- Soota checks cue coverage and reads full millisecond timestamps and raw cue text before removing supported markup.

## Other dependencies

The dependencies listed in `pyproject.toml` and their transitive dependencies
retain their own distribution metadata and license files in the Python environment.
This list is not a license inventory for a bundled Python/Node application.
If such an application is distributed later, its actual included dependencies
and applicable notices must be inventoried at that time.
