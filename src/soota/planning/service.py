"""目标到方案、确认与执行。

[INPUT]: Agent 归一的任务条件、用户的方案选择和当前运行配置。
[OUTPUT]: create_plan / select_plan / run_plan / show_plan / manage_presets。
[POS]: 规划仅查按需报价；确认后保存偏好，执行复用 tasks.runner 与原预算账本。
[PROTOCOL]: 变更时更新此头部，然后检查 AGENTS.md
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from soota.billing import provider_enabled
from soota.config import SootaSettings
from soota.errors import InputError
from soota.models import CommandResult
from soota.planning.models import Option, Plan, Preferences, Preset
from soota.planning.routes import PAID, preview, scope_for
from soota.planning.store import delete_preset, load_plan, plan_lock, presets, save_plan, save_preset
from soota.providers.quotes import quote_scope
from soota.secrets import redact_any
from soota.storage.jobs import JobStore
from soota.tasks.entry import prepare_task
from soota.tasks.inputs import source_for
from soota.tasks.models import TaskSpec
from soota.tasks.policy import PROVIDERS, routing_snapshot, task_settings
from soota.tasks.runner import execute
from soota.tasks.state import create_job


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _replace(spec: TaskSpec, **updates: Any) -> TaskSpec:
    return TaskSpec.model_validate({**spec.model_dump(), **updates})


def _key(settings: SootaSettings, spec: TaskSpec, report: dict[str, Any]) -> str:
    # 时效刷新但价格相同时不要求再次选择；费用、可用性、顺序改变时必须重审。
    routes = []
    for row in report["routes"]:
        routes.append(
            {
                **{key: value for key, value in row.items() if key != "candidates"},
                "candidates": [
                    {key: value for key, value in candidate.items() if key not in {"price_basis", "reason"}}
                    for candidate in row.get("candidates", [])
                ],
            }
        )
    effective = task_settings(settings, spec)
    context = {
        "routes": routes,
        "payment": effective.payment.model_dump(),
        "providers": [name for name in PROVIDERS if provider_enabled(effective, name)],
        "tikhub": effective.tikhub.base_url,
        "monid": effective.monid.base_url,
        "treg": effective.treg.base_url,
        "weir": effective.weir.model_dump(),
        "preferences": Preferences.from_spec(spec).model_dump(mode="json"),
    }
    return hashlib.sha256(json.dumps(context, sort_keys=True).encode()).hexdigest()


def _option(settings: SootaSettings, ident: str, label: str, spec: TaskSpec, *, offline: bool) -> Option:
    report = preview(settings, spec, offline=offline)
    return Option(id=ident, label=label, spec=spec, report=report, review_key=_key(settings, spec, report))


def _merge(base: TaskSpec, preset: Preset, explicit: set[str]) -> TaskSpec:
    saved = preset.preferences.model_dump()
    current = base.model_dump()
    saved.update({key: current[key] for key in explicit if key in Preferences.model_fields})
    # 本次提出的费用上限只会收紧旧预设，不能被旧的更高额度覆盖。
    for key in ("max_cost", "weir_max_credits"):
        limits = [value for value in (getattr(preset.preferences, key), current[key]) if value is not None]
        saved[key] = min(limits) if limits else None
    return _replace(base, **saved)


def _public(plan: Plan) -> dict[str, Any]:
    matches = [option.id for option in plan.options if option.preset_name]
    return dict(
        redact_any(
            {
                "id": plan.id,
                "goal": plan.goal,
                "created_at": plan.created_at,
                "status": "started"
                if plan.job_id
                else "selected"
                if plan.selected
                else "awaiting_preset_confirmation"
                if matches
                else "awaiting_choice",
                "question": None
                if plan.selected
                else "是否沿用匹配的预设？也可以选择其他方案。"
                if matches
                else "请选择本次方案。",
                "matches": matches,
                "selected": plan.selected,
                "saved_preset": plan.saved_preset,
                "job_id": plan.job_id,
                "options": [
                    {
                        "id": option.id,
                        "label": option.label,
                        "preset_name": option.preset_name,
                        "preset_revision": option.preset_revision,
                        "changes": option.changes,
                        "preferences": Preferences.from_spec(option.spec).model_dump(mode="json"),
                        "report": option.report,
                    }
                    for option in plan.options
                ],
            }
        )
    )


def _result(plan: Plan, message: str, *, code: str = "ok") -> CommandResult:
    return CommandResult(
        ok=True, status="success", code=code, exit_code=0, message=message, data={"plan": _public(plan)}
    )


def create_plan(
    settings: SootaSettings,
    goal: str,
    url: str | None = None,
    *,
    offline: bool = False,
    **options: Any,
) -> CommandResult:
    goal = goal.strip()
    if not goal or len(goal) > 500:
        raise InputError("请给出 1–500 字的目标说明，目标产物通过 --want 指定。")
    if not options.get("want"):
        raise InputError("规划需要 --want；由 Agent 将自然语言目标转换成采集目标，不在 CLI 内猜测。")
    base = prepare_task(settings, url, **options)
    if base.wants - {"video", "audio", "transcript", "context", "markdown", "images", "data"}:
        raise InputError("语义目标请交给 LoreLab；此处只规划采集任务。")
    base = _replace(
        base, routing=routing_snapshot(settings), audio_provider=base.audio_provider or settings.audio.provider
    )
    scope = scope_for(base)
    explicit = {key for key, value in options.items() if value is not None}
    if "transcript" in explicit:
        explicit.add("transcript_mode")
    if "language" in explicit:
        explicit.add("strict_language")
    choices: list[Option] = []
    with quote_scope(online=not offline and base.max_cost != 0):
        for preset in presets(settings):
            if preset.scope != scope:
                continue
            spec = _merge(base, preset, explicit)
            option = _option(
                settings, "preset:" + preset.name, "沿用预设「" + preset.name + "」", spec, offline=offline
            )
            option.preset_name, option.preset_revision = preset.name, preset.revision
            before, after = (
                preset.preferences.model_dump(mode="json"),
                Preferences.from_spec(spec).model_dump(mode="json"),
            )
            option.changes = {
                key: {"saved": before[key], "this_run": after[key]} for key in before if before[key] != after[key]
            }
            choices.append(option)
        if base.provider not in PAID | {"weir"}:
            free = _replace(base, max_cost=0, weir_max_credits=0, fallback=None)
            choices.append(_option(settings, "free", "只用免费来源", free, offline=offline))
        if base.max_cost != 0 or not choices:
            choices.append(_option(settings, "configured", "按当前排序和预算", base, offline=offline))
        video_inputs = all(
            source_for(seed).platform in {"youtube", "bilibili"} and source_for(seed).kind in {"video", "shortlink"}
            for seed in base.inputs
        )
        if base.provider == "auto" and video_inputs:
            for name in ("yt-dlp", "tikhub", "monid", "treg"):
                if not provider_enabled(settings, name) or base.max_cost == 0 and name in PAID:
                    continue
                fixed = _replace(base, provider=name, fallback=None)
                option = _option(settings, "provider:" + name, "指定来源 " + name, fixed, offline=offline)
                if option.report["selectable"]:
                    choices.append(option)
    plan = Plan(id=uuid4().hex, goal=goal, created_at=_now(), scope=scope, options=choices)
    save_plan(settings, plan)
    return _result(plan, "已生成可选方案；匹配预设仍需确认。尚未采集或发送计费请求。")


def show_plan(settings: SootaSettings, plan_id: str) -> CommandResult:
    return _result(load_plan(settings, plan_id), "已读取规划；本次未联网刷新。")


def _recheck(settings: SootaSettings, plan: Plan, option: Option) -> bool:
    report = preview(settings, option.spec)
    key = _key(settings, option.spec, report)
    changed = key != option.review_key
    option.report, option.review_key = report, key
    if changed:
        plan.selected = None
        plan.saved_preset = None
    save_plan(settings, plan)
    return changed


def select_plan(
    settings: SootaSettings,
    plan_id: str,
    option_id: str,
    *,
    confirm: bool = False,
    save_as: str | None = None,
) -> CommandResult:
    with plan_lock(settings, plan_id):
        plan = load_plan(settings, plan_id)
        if plan.job_id:
            raise InputError("此规划已创建任务；请用 jobs resume 续跑或为新输入重新规划。")
        option = next((row for row in plan.options if row.id == option_id), None)
        if option is None:
            raise InputError("方案不存在；请从 plan show 返回的 options 中选择。")
        if not confirm:
            return _result(plan, "请先确认方案；尚未保存预设或执行。", code="confirmation_required")
        if option.preset_name:
            current = next((row for row in presets(settings) if row.name == option.preset_name), None)
            if current is None or current.revision != option.preset_revision:
                raise InputError("匹配的预设已修改或删除；请重新规划后确认。")
        if _recheck(settings, plan, option):
            return _result(plan, "报价、来源或费用策略已变化；请查看新方案后再次选择。", code="plan_changed")
        if not option.report["selectable"]:
            raise InputError("该方案没有满足目标和预算的可用来源；请调整条件后重新规划。")
        if plan.selected == option.id and (save_as is None or save_as == plan.saved_preset):
            return _result(plan, "方案已选定并保存；可执行 plan run。")
        try:
            preset = Preset(
                name=save_as or option.preset_name or plan.goal[:128],
                scope=plan.scope,
                preferences=Preferences.from_spec(option.spec),
                updated_at=_now(),
            )
        except ValidationError as exc:
            raise InputError("预设名称无效；请使用 1–128 字的单行名称。") from exc
        revision = option.preset_revision if preset.name == option.preset_name else None
        saved = save_preset(settings, preset, expected_revision=revision)
        plan.selected, plan.saved_preset = option.id, saved.name
        option.preset_name, option.preset_revision = saved.name, saved.revision
        save_plan(settings, plan)
        return _result(plan, "已选定方案并保存预设；执行仍遵守现有费用确认和预算。")


def run_plan(settings: SootaSettings, plan_id: str) -> CommandResult:
    with plan_lock(settings, plan_id):
        plan = load_plan(settings, plan_id)
        if plan.job_id:
            record = JobStore(settings.jobs_dir).get(plan.job_id)
            result = _result(plan, "此规划已有任务；使用 jobs resume 继续，不会重复创建任务。", code="task_exists")
            result.data.update(job_id=plan.job_id, job_status=record.get("status"))
            return result
        if plan.selected is None:
            return _result(plan, "尚未确认方案；请先选择。", code="confirmation_required")
        option = next(row for row in plan.options if row.id == plan.selected)
        with quote_scope(online=option.spec.max_cost != 0):
            if _recheck(settings, plan, option):
                return _result(plan, "方案条件已变化；尚未执行，请查看后重新选择。", code="plan_changed")
            if not option.report["selectable"]:
                raise InputError("当前方案不可执行；请重新规划。")
            # 先持久化任务绑定再派发，重复 run 不会创建新账本或重复消费。
            job = create_job(settings, option.spec)
            plan.job_id = job.id
            save_plan(settings, plan)
            result = execute(settings, option.spec, job=job)
            result.data.update(plan_id=plan.id, preset=plan.saved_preset)
            return result


def manage_presets(settings: SootaSettings, *, name: str | None = None, delete: bool = False) -> CommandResult:
    if delete:
        if name is None:
            raise InputError("删除预设需要名称。")
        delete_preset(settings, name)
    rows = presets(settings)
    if name and not delete:
        rows = [row for row in rows if row.name == name]
        if not rows:
            raise InputError("预设不存在。")
    return CommandResult(
        ok=True,
        status="success",
        code="ok",
        exit_code=0,
        message="已删除预设。" if delete else "已读取预设；未查询价格或执行任务。",
        data={"plan": {"presets": [row.model_dump(mode="json") for row in rows]}},
    )
