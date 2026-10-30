"""只读的能力与费用预览。

[INPUT]: TaskSpec、现有来源规则和按需报价缓存。
[OUTPUT]: 分步骤候选顺序、报价小计、条件费用与未验证范围。
[POS]: 不枚举内容、不查余额、不发送计费请求；使用执行器相同的排序函数。
[PROTOCOL]: 变更时更新此头部，然后检查 AGENTS.md
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from soota.billing import provider_enabled
from soota.config import SootaSettings
from soota.content.weir import supports as weir_supports
from soota.errors import InputError, SootaError
from soota.models import SourceRef, TranscriptMode
from soota.providers.priority import NAMES, quote_rows, rank_quotes, rule_for
from soota.providers.quotes import quote_scope
from soota.tasks.dependencies import acquisition_goals, task_stages
from soota.tasks.inputs import source_for
from soota.tasks.models import InputSpec, TaskSpec
from soota.tasks.policy import task_settings

PAID = {"tikhub", "monid", "treg"}
TEXT = {"context", "markdown", "transcript"}


def scope_for(spec: TaskSpec) -> str:
    import json

    shapes = set()
    for seed in spec.inputs:
        source = source_for(seed)
        detail = (
            seed.operation
            if seed.kind == "operation"
            else Path(seed.value).suffix.lower()
            if seed.kind == "file"
            else ""
        )
        shapes.add((source.platform, source.kind, detail or "", seed.expand))
    return json.dumps([sorted(shapes), sorted((o.kind, o.required) for o in spec.outputs)], ensure_ascii=False)


def _quoted(
    settings: SootaSettings,
    spec: TaskSpec,
    source: SourceRef,
    capability: str,
    *,
    provider: str | None = None,
    endpoint: str | None = None,
    condition: str = "always",
) -> dict[str, Any]:
    chosen = provider or spec.provider
    names = ["yt-dlp", "tikhub", "monid", "treg"] if capability == "audio" else list(NAMES)
    if capability == "api":
        names = ["tikhub", "monid", "treg"]
    if chosen != "auto":
        names = [chosen] if chosen in names else []
    if spec.max_cost == 0:
        names = [name for name in names if name not in PAID]
    rows = quote_rows(settings, source, capability, names, endpoint=endpoint)
    order = rank_quotes(settings, source, capability, rows, max_cost=spec.max_cost)
    first = next((row for row in rows if order and row["provider"] == order[0]), None)
    return {
        "platform": source.platform,
        "capability": capability,
        "condition": condition,
        "order": order,
        "candidates": rows,
        "rule": rule_for(settings, source.platform, capability).model_dump(),
        "estimated_cost_usd": first["estimated_cost_usd"] if first else None,
        "selectable": bool(order),
        "coverage": "quoted-contract",
    }


def _local(
    settings: SootaSettings,
    source: SourceRef,
    capability: str,
    names: list[str],
    *,
    condition: str = "always",
    coverage: str = "runtime-unverified",
) -> dict[str, Any]:
    order = [name for name in names if provider_enabled(settings, name)]
    return {
        "platform": source.platform,
        "capability": capability,
        "condition": condition,
        "order": order,
        "estimated_cost_usd": 0 if order else None,
        "selectable": bool(order),
        "coverage": coverage,
    }


def _native(settings: SootaSettings, spec: TaskSpec, seed: InputSpec, source: SourceRef) -> list[dict[str, Any]]:
    operation = seed.operation or ""
    name = operation.split(".", 1)[0]
    from soota.content.native_bindings import CONTENT_BINDINGS, MEDIA_BINDINGS, URL_ROW_BINDINGS

    native = operation.removeprefix("opencli.").replace(".", "/")
    supported = {"data"}
    if native in CONTENT_BINDINGS:
        supported.update({"context", "markdown", "images"})
    if native in MEDIA_BINDINGS:
        supported.update({"video", "audio", "images"})
    if seed.expand:
        if native not in URL_ROW_BINDINGS or name == "opencli-mcp":
            raise InputError("此原生接口没有已核实的列表展开契约。")
        supported = set(spec.wants)
    missing = spec.wants - supported
    if missing & spec.required:
        raise InputError("原生接口没有所需产物的转换契约：" + ", ".join(sorted(missing & spec.required)))
    if name in PAID:
        from soota.api import _validate
        from soota.providers.tikhub.registry import load_registry

        endpoint = load_registry().resolve(operation.split(".", 1)[1])
        if not endpoint.callable or endpoint.deprecated or seed.expand:
            raise InputError("规划仅支持已审查的读取接口；该原生接口不能作为集合展开。")
        _validate(seed.params, endpoint.query_schema, "查询参数")
        chosen = name if name in {"monid", "treg"} else spec.provider
        if name in {"monid", "treg"} and spec.provider not in {"auto", name}:
            raise InputError("原生操作与指定来源不一致。")
        if chosen not in {"auto", *PAID}:
            raise InputError("原生操作与指定来源不一致。")
        source = source.model_copy(update={"platform": endpoint.path.split("/")[3]})
        rows = [_quoted(settings, spec, source, "api", provider=chosen, endpoint=endpoint.path)]
    else:
        actual = "opencli-mcp" if name == "opencli-mcp" else "opencli"
        allowed = spec.provider in {"auto", actual} and (not seed.expand or actual == "opencli")
        rows = [_local(settings, source, "native-read", [actual] if allowed else [])]
    if seed.expand:
        rows[0]["coverage"] = "enumeration-only"
    if missing:
        rows.append(
            {
                "capability": "optional-outputs",
                "required": False,
                "outputs": sorted(missing),
                "condition": "always",
                "order": [],
                "selectable": False,
                "estimated_cost_usd": None,
            }
        )
    return rows


def _steps(settings: SootaSettings, spec: TaskSpec, seed: InputSpec) -> list[dict[str, Any]]:
    source = source_for(seed)
    if seed.kind == "operation":
        return _native(settings, spec, seed, source)
    if seed.kind in {"file", "bundle"}:
        row = _local(settings, source, "local-parse", ["local"], coverage="local-input-unverified")
        if seed.kind == "bundle" or Path(seed.value).suffix.lower() == ".json":
            row.update(estimated_cost_usd=None, coverage="bundle-or-descriptor-dependent")
        return [row]
    if source.kind in {"channel", "user", "playlist"} or seed.kind == "collection":
        if source.kind == "playlist":
            return [
                _local(
                    settings,
                    source,
                    "catalog",
                    ["yt-dlp"] if spec.provider in {"auto", "yt-dlp"} else [],
                    coverage="enumeration-only",
                )
            ]
        row = _quoted(settings, spec, source, "catalog")
        row["coverage"] = "enumeration-only"
        # 页面次数是上限；别名解析可能因缓存而省去，单独列出不混作每页费。
        row["request_limit"] = spec.max_pages if row["order"] and row["order"][0] in PAID else 1
        catalog_rows = [row]
        if row["order"] and row["order"][0] in PAID and source.platform == "youtube":
            from soota.providers.tikhub.catalog import YOUTUBE_CHANNEL_ID, is_channel_id

            if not is_channel_id(source.id):
                catalog_rows.append(
                    _quoted(settings, spec, source, "api", provider=row["order"][0], endpoint=YOUTUBE_CHANNEL_ID)
                )
        return catalog_rows
    from soota.tasks.acquire import _direct_url

    if _direct_url(seed.value):
        name = "http" if spec.provider == "auto" else spec.provider
        return [_local(settings, source, "direct-media", [name] if name in {"http", "yt-dlp"} else [])]
    rows: list[dict[str, Any]] = []
    media_goals, text_goals = acquisition_goals(spec, seed)
    video = source.platform in {"youtube", "bilibili"} and source.kind in {"video", "shortlink"}
    if text_goals:
        if video:
            text_settings = settings.model_copy(deep=True)
            if spec.strict_language and spec.provider == "auto":
                text_settings.routing.disabled_providers.update({"opencli", "opencli-mcp"})
            capability = "inspect" if spec.transcript_mode in {TranscriptMode.ASR, TranscriptMode.NONE} else "captions"
            rows.append(_quoted(text_settings, spec, source, capability))
            if spec.fallback == "tikhub" and spec.provider not in {"auto", "tikhub"}:
                rows.append(
                    _quoted(
                        text_settings, spec, source, capability, provider="tikhub", condition="provider_unavailable"
                    )
                )
            if spec.transcript_mode in {TranscriptMode.ASR, TranscriptMode.AUTO} and not spec.wants & {
                "audio",
                "video",
            }:
                audio = spec.audio_provider or settings.audio.provider
                condition = "confirmed_no_captions" if spec.transcript_mode == TranscriptMode.AUTO else "always"
                rows.append(
                    _local(settings, source, "audio", ["downie"], condition=condition)
                    if audio == "downie"
                    else _quoted(settings, spec, source, "audio", provider=audio, condition=condition)
                )
        else:
            names = ["opencli"] if spec.provider == "auto" else [spec.provider]
            names = [name for name in names if name in {"opencli", "weir"}]
            if spec.provider == "auto" and weir_supports(seed.value):
                names.append("weir")
            if not weir_supports(seed.value) or spec.max_cost == 0 or spec.weir_max_credits == 0:
                names = [name for name in names if name != "weir"]
            rows.append(_local(settings, source, "body", names))
    if "video" in spec.wants:
        names = ["opencli", "yt-dlp"] if spec.provider == "auto" else [spec.provider]
        if source.platform == "youtube":
            names = [name for name in names if name != "opencli"]
        rows.append(
            _local(settings, source, "video", [name for name in names if name in {"opencli", "yt-dlp", "downie"}])
        )
    elif "audio" in spec.wants:
        if video and spec.provider in {"auto", "yt-dlp", *PAID}:
            rows.append(_quoted(settings, spec, source, "audio"))
        else:
            names = ["yt-dlp"] if spec.provider == "auto" else [spec.provider]
            rows.append(
                _local(settings, source, "audio", [name for name in names if name in {"yt-dlp", "opencli", "downie"}])
            )
    if "images" in spec.wants:
        names = ["opencli"] if "images" in media_goals else ["http"]
        rows.append(_local(settings, source, "images", names, coverage="artifact-dependent"))
    if "data" in spec.wants and not text_goals:
        rows.append(_quoted(settings, spec, source, "inspect"))
    for row in rows:
        capability = row["capability"]
        targets = (
            text_goals
            if capability in {"captions", "body"}
            else text_goals or {"data"}
            if capability == "inspect"
            else ({"audio", "video"} & media_goals)
            if capability == "video"
            else {"audio"}
            if capability == "audio" and "audio" in media_goals
            else text_goals
            if capability == "audio"
            else {"images"}
        )
        row["required"] = bool(targets & spec.required)
        row["outputs"] = sorted(targets)
    return rows


def preview(settings: SootaSettings, spec: TaskSpec, *, offline: bool = False) -> dict[str, Any]:
    effective = task_settings(settings, spec)
    rows: list[dict[str, Any]] = []
    warnings = ["报价是各步骤首选来源的请求估计；不含重试、换源、转写耗时和集合内未枚举条目，不能当作总费用承诺。"]
    with quote_scope(online=not offline and spec.max_cost != 0):
        # 相同形状共用报价；不执行内容获取来估算数量。
        seen: set[str] = set()
        for index, seed in enumerate(spec.inputs[: spec.max_items]):
            identity = source_for(seed).cache_identity
            if identity in seen:
                continue
            seen.add(identity)
            seen_steps: set[str] = set()
            for phase, stage in enumerate(task_stages(spec)):
                try:
                    steps = _steps(effective, stage, seed)
                except SootaError as exc:
                    steps = [
                        {
                            "capability": "unavailable",
                            "selectable": False,
                            "condition": "always",
                            "order": [],
                            "estimated_cost_usd": None,
                            "reason": exc.message,
                        }
                    ]
                for row in steps:
                    identity = str((row["capability"], row["condition"], row["order"]))
                    if identity in seen_steps:
                        continue
                    seen_steps.add(identity)
                    rows.append({"input_index": index, **row, "required": phase == 0 and row.get("required", True)})
    required = [row for row in rows if row["condition"] == "always" and row.get("required", True)]
    known = all(row["estimated_cost_usd"] is not None for row in required)
    subtotal = round(sum(row["estimated_cost_usd"] for row in required), 9) if known else None
    fits = subtotal is None or spec.max_cost is None or subtotal <= spec.max_cost
    if not fits:
        warnings.append("必需步骤的报价小计已超过本次总预算，请选择更便宜方案或调整范围。")
    if any(row.get("coverage") == "bundle-or-descriptor-dependent" for row in rows):
        warnings.append("旧包或 JSON 媒体描述可能需要补取，费用未知；实际获取仍受当前来源和预算限制。")
    if any("weir" in row["order"] for row in rows):
        warnings.append("Weir 使用独立 credits 配额；美元小计不代表 credits 免费，仍受任务和月度限额约束。")
    if any(row.get("coverage") == "enumeration-only" for row in rows):
        warnings.append("集合尚未枚举，当前只估计枚举接口；条目采集仍受全批预算控制。")
    return {
        "selectable": bool(required) and all(row["selectable"] for row in required) and fits,
        "routes": rows,
        "quoted_steps_usd": subtotal,
        "fits_budget": fits,
        "estimate_complete": known and all(row.get("coverage") == "quoted-contract" for row in required),
        "max_cost_usd": spec.max_cost,
        "weir_max_credits": effective.weir.task_credits,
        "payment_mode": effective.payment.mode,
        "min_balance_usd": effective.payment.min_balance_usd,
        "quote_mode": "offline-estimate" if offline else "on-demand",
        "price_ttl_seconds": effective.routing.price_ttl_seconds,
        "warnings": warnings,
    }
