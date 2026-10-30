"""任务路由偏好。

[INPUT]: 当前配置与 TaskSpec 的已选路由快照。
[OUTPUT]: routing_snapshot / task_settings / task_context，供规划、执行和恢复共用。
[POS]: 不写全局配置、不启用来源、不改变费用确认策略。
[PROTOCOL]: 变更时更新此头部，然后检查 AGENTS.md
"""

from __future__ import annotations

import hashlib
import json

from soota.billing import provider_enabled
from soota.config import RoutingConfig, SootaSettings
from soota.tasks.models import TaskRouting, TaskSpec

PROVIDERS = ("opencli-mcp", "opencli", "yt-dlp", "tikhub", "monid", "treg", "weir", "downie", "http", "local")


def routing_snapshot(settings: SootaSettings) -> TaskRouting:
    return TaskRouting.model_validate(
        {
            "allowed_providers": [name for name in PROVIDERS if provider_enabled(settings, name)],
            "priorities": settings.routing.priorities,
            "treg_youtube_captions": settings.treg.youtube_captions,
        }
    )


def task_settings(settings: SootaSettings, spec: TaskSpec) -> SootaSettings:
    result = settings.model_copy(deep=True)
    result.default_language = spec.language
    if spec.routing is not None:
        disabled = set(result.routing.disabled_providers) | {
            name for name in PROVIDERS if name != "local" and name not in spec.routing.allowed_providers
        }
        result.routing = RoutingConfig.model_validate({**result.routing.model_dump(), "disabled_providers": disabled})
        result.routing.priorities = spec.routing.model_copy(deep=True).priorities
        result.treg.youtube_captions = spec.routing.treg_youtube_captions
    if spec.weir_max_credits is not None:
        current = settings.weir.task_credits
        result.weir.task_credits = (
            min(spec.weir_max_credits, current)
            if spec.routing is not None and current is not None
            else spec.weir_max_credits
        )
    return result


def task_context(settings: SootaSettings, spec: TaskSpec) -> str:
    from soota.tasks.entry import execution_context

    base = execution_context(settings, spec.provider, spec.inputs, scope=spec.execution_scope)
    if spec.routing is None:
        return base
    effective = task_settings(settings, spec)
    values = [
        "planned-v1",
        base,
        spec.routing.model_dump(mode="json"),
        spec.audio_provider,
        sorted(effective.routing.disabled_providers),
    ]
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()
