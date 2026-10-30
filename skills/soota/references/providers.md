# Provider configuration

Use the Soota CLI instead of editing credentials into a project or copying provider CLIs into collection workflows.

```bash
soota providers auth monid
soota providers enable monid
soota providers auth treg
soota providers enable treg
soota providers show --json
soota providers priority show youtube captions --json
soota providers priority set youtube captions --mode cost
soota providers priority set bilibili captions --mode manual --order opencli,yt-dlp,tikhub,monid
soota providers priority show xiaohongshu api --endpoint xiaohongshu.web.fetch_note_detail --refresh --json
```

Monid and treg are disabled by default. treg credentials use `TREG_TOKEN` or `credentials/treg.key`; details and supported routes are in [treg](../../../docs/treg.md). Credentials come from `MONID_API_KEY` or the local config directory; the hidden prompt and `--key-file` avoid echoing the secret. `enable` does not change payment policy. Do not enable automatic payment without user authorization.

Rules are scoped by platform and capability: `inspect`, `captions`, `catalog`, `api`, `audio`. `*` is the platform fallback. Cost mode compares the full known request chain; manual mode uses the specified order then appends remaining available providers. Explicit `--provider` overrides saved rules. `--refresh` queries free metadata and saves prices; offline preview uses bundled/saved estimates. Online routing and billed dispatch share TTL quotes (default 86400 seconds in `[routing] price_ttl_seconds`), refreshing missing/expired entries once per task and endpoint. Refresh failures exclude that candidate; legacy snapshots do not authorize billing. Unknown prices are never zero. Direct native API calls can obtain an uncached price before dispatch.

Monid YouTube captions use the reviewed single-video Apify tool. Bilibili captions and covered TikHub native APIs use relay; coverage is not universal. Standard subtitle conversion still belongs to the shared format layer. Do not infer standard article or media contracts from an endpoint listing.

ASR audio defaults to yt-dlp; `--audio-provider auto` uses the audio rule. Paid sources share the remaining budget. Monid saves run IDs and receipts; resume polls the original run. An ambiguous POST must not be blindly submitted again. Completed no-caption rows can still be charged; report returned actual fees separately from estimates, and do not equate HTTP 200 with complete subtitles.

For the reviewed OpenCLI 1.8.8 Xiaohongshu search filter issue, the existing managed-patch command is `soota runtime update --version 1.8.8 --json`. Inspect the managed runtime's `soota-compatibility.json` for `xiaohongshu-search-filter-options`, source links and file hashes. Patches require unique known anchors and skip drift/partial matches. The patch filters noninteractive ghosts and merges only identical logical options; it does not skip default filters. Do not replace a compatible runtime with an unverified major release or modify global OpenCLI. Apply only after active runtime calls finish, and distinguish patch/contract checks from a real search and signed-note read.


## Weir

Weir is an optional REST body provider, separate from caption/API price ranking. `providers auth weir`, `providers enable/disable weir`, `providers limits weir --monthly-credits N --task-credits N` and `providers usage weir` manage credentials, local credit caps and offline accounting. `providers auth weir --key-name NAME` adds/updates a private named backup; `providers keys weir [--enable NAME|--disable NAME]` shows names/status and effective priority without secrets. Primary then backups use durable ordered round-robin for new data calls; confirmed 401 skips to the next key within that call. Insufficient funds, paid balance, limits, upstream errors and uncertain charges stop. `WEIR_API_KEY` exclusively overrides the entire local key pool, with no file fallback. Monthly caps use one UTC calendar month across keys/accounts; usage includes key names and current-month totals. Current use is free-only; `policy auto` does not authorize purchased credits. Auto uses existing OpenCLI body capability first, and considers enabled Weir only before dispatch when that capability is unavailable. See [Weir contract](../../../docs/weir.md).
