# Unified task workflow

Read this for combinations, batches, local inputs, existing bundles, explicit native reads, or acquisition recovery. The maintained user-facing contract is `docs/tasks.md` in the Soota checkout.

## Translate the request into targets

Use only targets requested or necessary for the task:

| Request | CLI target |
| --- | --- |
| Save the actual video or audio | `--want video` or `--want audio` |
| Save captions without ASR | `--want transcript --transcript captions-only` |
| Save source text for an Agent | `--want context` |
| Save an existing webpage with its images | `--want context,images` |
| Return native query data | `--want data` |


For downstream knowledge delivery, save pasted text as a `.txt` file and ingest with `--provider local --want context`. Hand the returned v2 bundle to the LoreLab Skill: content handles analysis, notes, articles, outlines and export; knowledge/study handle knowledge and learning delivery. Ordinary chat summaries need no ingest or archive. Do not pass prose or a Cubox response directly as share text or an input list; collection still stops at the requested source artifacts.

`markdown` keeps the same unprocessed-text boundary as context. Do not map “download this article” to a semantic rewrite. Put optional outputs in `--optional`; do not downgrade a user-required target merely to obtain a successful exit code.

Examples use placeholders:

```bash
soota ingest "URL" --want video,transcript --max-cost 0 --json
soota ingest "/path/to/audio.m4a" --want transcript,context --json
soota ingest "/path/to/captions.vtt" --want transcript --subtitle-format srt,json --json
soota ingest --inputs-file "/path/to/links.txt" --want context --workers 2 --max-items 20 --max-cost 0 --json
soota ingest "/path/to/bundle/manifest.json" --want context --json
```

Input lists accept one link/path per line or a JSON `inputs`/`items` array. Relative paths resolve beside the input list. Existing v1/v2 bundles can supply their saved source and artifacts. Ordinary directories are not recursively imported. Local media processing must preserve original files and neighboring files.

`--workers N` bounds independent items to 1–8, default 1. Enumeration is sequential and deduplication keeps the first occurrence; `items` remain in input order even if completion differs. Inputs resolving to the same bundle directory run sequentially; semantic processing and exports belong to LoreLab content. One item's failure does not stop other items. Worker checkpoints merge only that item's snapshot under a lock, and each worker receives the caller's context variables including the shared task budget. Paid allocation follows reservation order, not input priority.

Respect actual language and media quality requirements. `--language` requests verified subtitle language, `--min-height` sets minimum video height, and `--max-bytes` limits one media file. Untimed source text can be exported as TXT/JSON; do not synthesize SRT/VTT timestamps.

On Windows, external yt-dlp media downloads (including HLS/DASH) and FFmpeg extraction/muxing stop before dispatch because the hard file-size limit is unavailable. Keep the failed required target visible; do not replace the limit with polling, remove it, or silently downgrade the target. Bounded direct HTTP files, existing media verification, captions and UTF-8 task recovery remain usable. Downie is Mac-only; a failure does not authorize paid fallback or a new download.

## Discover only when needed

Known standard tasks can go directly to `ingest`. For an unfamiliar operation, use `capabilities --search ... --json`; inspect the current input contract and state before choosing a command. This discovery does not query websites or authenticate accounts.

```bash
soota capabilities --provider opencli --search search --limit 20 --json
soota ingest --operation opencli.youtube.search --param query=example --want data --max-cost 0 --json
soota ingest --operation opencli.youtube.search --param query=example \
  --expand --want context --max-items 5 --max-cost 0 --json
```

`native` means the raw operation is callable under its contract; `standard` means a reviewed result conversion exists. Neither proves live site success. `--expand` needs a bound list result; do not recursively extract arbitrary URLs from provider JSON. If only native data is supported, deliver that data and report the missing standard contract.

TikHub operations use `tikhub.<endpoint>` and parameters from `api show`. Optional Monid accepts `monid.<endpoint>` for the covered TikHub read contracts; auto routing of `tikhub.<endpoint>` also follows the platform api priority. Use `providers priority set PLATFORM CAPABILITY --mode cost` or `--mode manual --order ...`, and `providers priority show ... --refresh` for free quote refresh. Explicit `--provider` overrides the rule. They remain subject to payment policy and the shared task budget. Publicly saved data redacts credentials and signed links. Media acquisition must use the controlled internal descriptor, not URLs copied from a redacted JSON file.

## Continue a saved task

Read the single JSON result, including `items[].steps`, errors and `missing_outputs`. A job may complete within its requested item limit while enumeration completeness remains unknown. Media whose source duration is unknown must not be called full length.

```bash
soota jobs resume JOB_ID --json
soota jobs resume JOB_ID --workers 2 --json
soota jobs resume JOB_ID --max-items 40 --max-pages 6 --max-cost 0.10 --json
```

Resume limits are new totals. Budget reservations survive errors, process interruption and later resumes. `--max-cost 0` blocks all billed requests; it does not prohibit local ASR or free downloads. Do not retry authentication, payment, rate-limit or format-contract failures as if they were known absent content. Continue independent authorized outputs when available and report the actual blocker.

OpenCLI exposes safe typed diagnostics: `details.upstream_code`, `exit_code`, `category`, `reason`, `retryable`, and attempts for that dispatch, plus a fixed recovery `hint`. Local timeouts have no upstream exit code; unknown codes become `UNKNOWN`. Raw stderr, headers, trace paths and private URLs are omitted. There is no automatic replay. For a confirmed transient, authorized free read, use `jobs resume JOB_ID --max-cost 0 --json` after checking every unfinished item. Resume retains successful verified artifacts but does not filter failures by `retryable`; resolve mixed blockers first or create a bounded input list of reviewed retryable reads. Do not replay writes, paid requests, login/rate limits or uncertain outcomes automatically.

`xiaohongshu/note` rejects a missing signed URL before dispatch (`missing_signed_note_url`). The Agent may use normal search/author pages to recover the same note's actual `xsec_token` URL when acquisition is authorized; do not fabricate tokens or claim a failed search means no source. The managed search patch preserves default and explicit filter checks. Create a new task for a changed URL, leaving the old task and its success artifacts intact. `twitter/thread` rejects an empty result or missing target ID (`target_not_returned`), including historical cached results. Check visibility, increase `limit` (for example 50), and check engagement filtering in a new task. Never silently change the user's limit. Generic searches may legitimately return `[]`; containing the focal post still does not prove a complete author thread.

Resume preserves saved `workers`; an explicit override is validated and persisted without resetting the budget or repeating finished enumeration. Old unified requests without `workers` use 1; legacy catalog jobs reject the override. Ctrl-C stops new scheduling and drains started workers before marking the job `paused`; repeated Ctrl-C during draining does not release the job lock early. Inspect the saved record after interruption and resume that job. Provider timeouts still govern how long draining takes.

Existing files are reused only when their verification and acquisition conditions meet the request. A cache hit cannot turn ASR into original captions, an unknown language into a requested language, or low-resolution video into a requested higher resolution. If the original local input changes during a task, create a new task for the new input.

New tasks persist the random scope used for an unbound browser context, so repeated resume keeps eligible artifacts without sharing that unverified identity across unrelated tasks. Observable profile/acquisition changes still invalidate incompatible caches. Historical requests lacking that scope retain first-resume revalidation. Valid caches/artifacts avoid repeat acquisition and fees; failed or uncertain requests keep their reservations, and a new attempt must reserve again within the remaining total. The CLI does not promise exactly-once execution by an upstream service.

## Hand semantic or export requests to LoreLab

New Soota tasks accept only `video/audio/transcript/context/markdown/images/data`. Use LoreLab content and its Skill for `note/article/analysis/outline`, analysis submissions, rendering or an explicit `--destination` / `--vault` / `--subdir`. `--output` remains the acquisition bundle location. Source collection and raw bundles work independently without LoreLab.

Locate the LoreLab Skill with `lorelab skills list`; its `references/content.md` owns writing, schema guidance, classification review and export. New commands are `lorelab content ingest`, `lorelab content resume JOB --analysis FILE`, `lorelab content render --bundle PATH --analysis FILE`, and `lorelab content export --bundle PATH --destination PATH` (or `--vault PATH --subdir DIR`). Knowledge/study jobs retain their own submission interfaces and frozen state.

Historical semantic/export ingest calls, semantic/delivery job resumes and `soota render` only forward through hidden compatibility bridges, dynamically calling LoreLab in the same Python environment instead of choosing another executable from PATH. Missing LoreLab must produce an installation-required error before changing the task, budget or artifacts. Preserve original task/item IDs, source evidence, paths and cumulative reservations; do not edit a saved request to bypass this boundary. See [migration](../../../docs/knowledge.md).

Report actual source-bundle paths and acquisition completion. A preserved historical analysis field, handoff file or capability listing is not proof of completed semantic writing or knowledge delivery.


## Weir free-credit tasks

Use `ingest URL --provider weir --want context --weir-max-credits N` for X status/Article carrier links and Reddit posts. Full-body acquisition can include `markdown,data`; data-only/native operations, search, comments and threads are not bound. `providers limits weir --monthly-credits N --task-credits N` configures local monthly/default task caps. Resume `--weir-max-credits N` sets a new cumulative total, never a fresh allowance. Free-only mode refuses paid tiers or purchased balances; no concurrent top-ups. `--max-cost 0` also blocks new credit calls, while valid caches remain usable. Usage includes uncertain reservations; never delete the ledger to retry. See [Weir](../../../docs/weir.md) for fee-estimate and cross-client boundaries.
