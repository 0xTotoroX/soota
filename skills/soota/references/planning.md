# Goal planning and presets

Use this for conversational acquisition goals, choosing sources/costs, or reusing a saved setup. The full contract is [docs/planning.md](../../../docs/planning.md).

1. Translate the user's goal into Soota targets and constraints. Use only requested targets; semantic writing/export belongs to LoreLab. Ask only for missing input or a material constraint. Do not guess a paid budget. `--goal` is a short label, not a prompt executed by the CLI. `--want` is required.
2. Call `soota plan create INPUT --goal "GOAL" --want TARGETS --json` with actual provided constraints. Pass only explicit overrides: supplying default language/transcript/provider values would override the stored preset. Input lists and operations use the same flags as ingest. `--offline` provides a quick cached estimate when requested; normal create checks related quotes on demand.
3. Present a concise comparison: route/order, required-step estimate, budget cap, conditional/unknown costs and relevant limitations. Do not present `quoted_steps_usd` as the total bill or `selectable` as live verification. Disabled/unsupported sources are not working alternatives. Weir credits are separate from USD; catalog entries have not been enumerated. The ASR audio provider is separate from an independent audio-download target.
4. If `matches` is nonempty or status is `awaiting_preset_confirmation`, show the matching preset name, relevant preferences, current estimate and `changes`; ask whether to reuse it or choose another option. Structural matches do not establish semantic equivalence. Without a match, ask the user to choose from the feasible options. Existing explicit authorization for an exact displayed option can be used; a generic goal is not selection confirmation.
5. After the actual choice, call `plan select PLAN_ID --option OPTION_ID --confirm --save-as NAME --json`. The name is optional for a matching preset; new plans default to their goal label. A same-name conflict needs a new name or a freshly matched version, never overwrite the JSON manually. Do not save credentials, personal input paths or item-specific filter intervals into reusable rules.
6. If `plan_changed` is returned, show the changed quote/order/availability or policy before requesting a fresh choice. `confirmation_required` means no selection happened. Only after successful selection call `plan run PLAN_ID --json`; this still uses the current payment gate. Do not enable auto-payment, a new source or a higher budget to bypass it.
7. Inspect actual job/artifact results. Repeated run returns the existing job; use `jobs resume` to continue without resetting reservations. New inputs get a new plan/job. The saved preset remains a preference, never an old task's receipt or spending authorization.

`plan show`, `presets list/show` are offline reads. Quotes refresh only on demand when missing/expired, with task sharing; do not create a polling automation. No content request or balance check is needed to make the plan. Keep defaults/private state in the user's configured directories and follow their Git/sync rules. The ordinary `ingest --dry-run` remains a structural offline preview, distinct from this selection workflow.

Example placeholders:

```bash
soota plan create "URL" --goal "保存原字幕" --want transcript,context --transcript captions-only --max-cost 0.05 --json
# After the user chooses:
soota plan select PLAN_ID --option configured --confirm --save-as "视频原字幕" --json
soota plan run PLAN_ID --json
# Later: create for the new input; ask before selecting the returned preset candidate.
soota plan create "NEW_URL" --goal "保存字幕" --want transcript,context --json
soota presets list --json
```
