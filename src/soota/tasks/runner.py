"""Execute and resume unified artifact tasks.

[INPUT]: TaskSpec/routing/Weir caps, shared goal dependencies, scheduler and artifact services.
[OUTPUT]: Source artifacts/checkpoints and one ordered CommandResult; old workflow requests are forwarded.
[POS]: tasks orchestration; applies selected routing, budget/quote scopes, job lock and recovery.
[PROTOCOL]: 变更时更新此头部，然后检查 AGENTS.md
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from contextlib import nullcontext
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from soota.artifacts.bundle import (
    file_sha256,
    load_bundle,
    save_bundle,
    verify_artifacts,
    write_artifact,
    write_bundle,
)
from soota.artifacts.models import BundleV2
from soota.config import SootaSettings
from soota.errors import AuthBudgetError, InputError, SootaError, SourceError, StorageError
from soota.media import MediaAsset, MediaService
from soota.models import CommandResult, MediaItem, ProcessingManifest, TranscriptMode, TranscriptSource
from soota.providers.quotes import online_prices
from soota.secrets import redact_any
from soota.storage.atomic import atomic_write
from soota.storage.jobs import JobStore
from soota.storage.task_budget import TaskBudget, task_budget_scope
from soota.tasks.acquire import collect_media, collect_text, native_operation, safe_item
from soota.tasks.enumeration import expand_inputs
from soota.tasks.inputs import file_fingerprint, source_for
from soota.tasks.models import InputSpec, StepState, TaskItem, TaskJob, TaskSpec
from soota.tasks.outputs import _export_subtitles, _images
from soota.tasks.scheduler import run_items
from soota.tasks.state import (
    _bundle_lock,
    create_job,
    load_spec,
    resolved_inputs,
    save_job,
    save_spec,
    set_inputs,
    task_folder,
)


def _folder(spec: TaskSpec, seed: InputSpec) -> Path:
    if seed.kind == "bundle":
        return Path(seed.value)
    source = source_for(seed)
    platform = re.sub(r"[^A-Za-z0-9._-]", "_", source.platform)
    ident = re.sub(r"[^A-Za-z0-9_-]", "_", source.id)
    return spec.output / platform / ident


def _error(exc: SootaError | OSError) -> dict[str, Any]:
    return redact_any((exc if isinstance(exc, SootaError) else StorageError("无法读写任务产物。")).to_dict())  # type: ignore[no-any-return]


def _step(settings: SootaSettings, job: TaskJob, row: TaskItem, name: str, action: Callable[[], Any]) -> Any:
    row.steps[name] = StepState(status="running")
    row.status = "running"
    save_job(settings, job, item=row)
    try:
        result = action()
        row.steps[name].status = "completed"
        save_job(settings, job, item=row)
        return result
    except (SootaError, OSError) as exc:
        error = _error(exc)
        row.steps[name] = StepState(
            status="blocked"
            if isinstance(exc, (AuthBudgetError, InputError)) or getattr(exc, "code", "") == "download_pending"
            else "failed",
            error=error,
        )
        row.errors.append({"step": name, **error})
        save_job(settings, job, item=row)
        return None


def _valid(folder: Path, bundle: BundleV2, kind: str) -> list[Any]:
    return [
        a
        for a in bundle.artifacts
        if a.kind == kind and (folder / a.path).is_file() and file_sha256(folder / a.path) == a.sha256
    ]


def _media_artifact(
    folder: Path, bundle: BundleV2, asset: MediaAsset, index: int, spec: TaskSpec, row: TaskItem
) -> None:
    suffix = asset.path.suffix or (".m4a" if asset.kind == "audio" else ".bin")
    path = f"attachments/{asset.kind}-{index:03d}{suffix}"
    details = {
        **asset.model_dump(mode="json", exclude={"path"}),
        "acquisition_context": spec.execution_context,
        "input_fingerprint": row.input_fingerprint,
    }
    full = asset.verification.completeness == "duration_matched"
    write_artifact(
        folder,
        bundle,
        path=path,
        kind=asset.kind,
        format=asset.format,
        content=asset.path,
        provider=asset.source_provider,
        verification="media",
        completeness="full" if full else "unknown",
        attributes=details,
        force=spec.force,
    )


def _cache_matches(artifact: Any, spec: TaskSpec, row: TaskItem) -> bool:
    if row.identity.startswith("local:bundle:"):
        return (
            artifact.kind not in {"body", "transcript", "context"}
            or not (spec.exclude_ranges or spec.exclude_promotions)
            or artifact.attributes.get("text_signature") == spec.text_signature
        )
    attrs = artifact.attributes
    if artifact.kind in {"body", "transcript", "context"} and attrs.get("text_signature") != spec.text_signature:
        return False
    return attrs.get("input_fingerprint") == row.input_fingerprint and (
        not spec.execution_context or attrs.get("acquisition_context") == spec.execution_context
    )


def _has_transcript(bundle: BundleV2, spec: TaskSpec) -> bool:
    if bundle.transcript.status.value != "ready" or bundle.transcript.segments < 1:
        return False
    if spec.transcript_mode == TranscriptMode.ASR and bundle.transcript.source != TranscriptSource.ASR:
        return False
    if spec.transcript_mode == TranscriptMode.CAPTIONS_ONLY and bundle.transcript.source == TranscriptSource.ASR:
        return False
    if spec.strict_language:
        actual = bundle.transcript.language
        if not actual or actual.split("-")[0].lower() != spec.language.split("-")[0].lower():
            return False
    return True


def _has_source(bundle: BundleV2, spec: TaskSpec) -> bool:
    return bool(bundle.content is not None and bundle.content.has_text) or _has_transcript(bundle, spec)


def _source_current(folder: Path, bundle: BundleV2, spec: TaskSpec, row: TaskItem) -> bool:
    return _has_source(bundle, spec) and any(_cache_matches(a, spec, row) for a in _valid(folder, bundle, "context"))


def _satisfied(folder: Path, bundle: BundleV2, goal: str, spec: TaskSpec, row: TaskItem) -> bool:
    if goal in {"context", "markdown"}:
        return _source_current(folder, bundle, spec, row)
    if goal == "transcript":
        return (
            _source_current(folder, bundle, spec, row)
            and _has_transcript(bundle, spec)
            and all(
                (folder / f"captions.{fmt}").is_file()
                and any(
                    a.path == f"captions.{fmt}" and a.sha256 == file_sha256(folder / a.path) for a in bundle.artifacts
                )
                for fmt in spec.subtitle_formats
            )
        )
    if goal == "images":
        return bool(_valid(folder, bundle, "image")) and row.steps.get("images", StepState()).status == "completed"
    matches = [a for a in _valid(folder, bundle, goal) if _cache_matches(a, spec, row)]
    if goal == "video" and spec.min_height is not None:
        matches = [
            a
            for a in matches
            if max(
                (
                    stream.get("height") or 0
                    for stream in a.attributes.get("streams", [])
                    if stream.get("type") == "video" and not stream.get("attached_picture")
                ),
                default=0,
            )
            >= spec.min_height
        ]
    if goal == "video" and not spec.allow_silent:
        matches = [
            a for a in matches if any(stream.get("type") == "audio" for stream in a.attributes.get("streams", []))
        ]
    return bool(matches)


def _run_item(settings: SootaSettings, spec: TaskSpec, job: TaskJob, row: TaskItem, seed: InputSpec) -> None:
    folder = _folder(spec, seed)
    with _bundle_lock(folder):
        _run_item_locked(settings, spec, job, row, seed, folder)


def _run_item_locked(
    settings: SootaSettings, spec: TaskSpec, job: TaskJob, row: TaskItem, seed: InputSpec, folder: Path
) -> None:
    from soota.content.native_bindings import validate_target_result
    from soota.content.native_types import NativeOperation
    from soota.tasks.dependencies import acquisition_goals

    row.bundle, row.errors = str(folder), []
    work = task_folder(settings, job.id) / row.item_id
    work.mkdir(parents=True, exist_ok=True)
    item = safe_item(MediaItem(source=source_for(seed)))
    bundle = load_bundle(folder) if (folder / "manifest.json").is_file() else write_bundle(folder, item)
    issues = verify_artifacts(folder, bundle)
    if issues:
        row.warnings = list(dict.fromkeys([*row.warnings, *issues]))
    operation = None
    raw: Any = None
    if seed.kind == "operation":
        cached_raw = work / "native.json"
        if cached_raw.is_file():
            saved = json.loads(cached_raw.read_text(encoding="utf-8"))
            operation = NativeOperation(
                raw=saved["raw"],
                provider=saved["provider"],
                operation=saved["operation"],
                workdir=Path(saved["workdir"]),
                files=[Path(p) for p in saved.get("files", [])],
                warnings=saved.get("warnings", []),
            )
            validate_target_result(seed.operation or "", seed.params, operation.raw)
        else:
            operation = _step(
                settings, job, row, "native", lambda: native_operation(settings, spec, seed, work / "native")
            )
            if operation:
                atomic_write(
                    cached_raw,
                    json.dumps(
                        {
                            "raw": operation.raw,
                            "provider": operation.provider,
                            "operation": operation.operation,
                            "workdir": str(operation.workdir),
                            "files": [str(p) for p in operation.files],
                            "warnings": operation.warnings,
                        },
                        ensure_ascii=False,
                    ),
                )
                row.steps["native"].provider = operation.provider
        raw = operation.raw if operation else None
    assets: list[MediaAsset] = []
    for kind in ("video", "audio", "image"):
        if _satisfied(folder, bundle, kind, spec, row):
            for artifact in _valid(folder, bundle, kind):
                if artifact.attributes.get("verification"):
                    assets.append(MediaAsset.model_validate({**artifact.attributes, "path": folder / artifact.path}))
    media_goals, text_goals = acquisition_goals(spec, seed, operation.files if operation else None)
    previous_media = {goal for goal in media_goals if _satisfied(folder, bundle, goal, spec, row)}
    if media_goals - previous_media:

        def fetch_media() -> None:
            nonlocal assets
            service = MediaService(settings, work / "media", max_bytes=spec.max_bytes)
            original = next((asset for asset in assets if asset.kind == "video"), None)
            only_audio_missing = (
                "audio" in media_goals
                and original is not None
                and all(goal == "audio" or _satisfied(folder, bundle, goal, spec, row) for goal in media_goals)
            )
            if only_audio_missing:
                assert original is not None
                acquired = [service.extract_audio(original)]
                errors: list[dict[str, Any]] = []
            else:
                batch = collect_media(settings, spec, seed, work, operation=operation)
                acquired, errors = batch.assets, batch.errors
                row.warnings = list(dict.fromkeys([*row.warnings, *batch.warnings]))
            counts: dict[str, int] = {}
            for asset in acquired:
                counts[asset.kind] = counts.get(asset.kind, 0) + 1
                _media_artifact(folder, bundle, asset, counts[asset.kind], spec, row)
                assets.append(asset)
            if "audio" in spec.wants and not any(asset.kind == "audio" for asset in assets):
                original = next((asset for asset in assets if asset.kind == "video"), None)
                if original is not None:
                    audio = service.extract_audio(original)
                    _media_artifact(folder, bundle, audio, 1, spec, row)
                    assets.append(audio)
            row.steps["media"].provider = acquired[0].source_provider if acquired else None
            if errors:
                raise SourceError("部分媒体未通过验证，已保留其余产物。", details={"media_errors": errors})

        _step(settings, job, row, "media", fetch_media)
    bundle = load_bundle(folder)
    if text_goals and (
        not _source_current(folder, bundle, spec, row)
        or "transcript" in spec.wants
        and not _has_transcript(bundle, spec)
    ):

        def fetch_text() -> None:
            nonlocal bundle, raw
            if seed.kind == "operation" and operation is None:
                raise SourceError("原生操作尚未成功，不能生成正文。")
            collection = collect_text(settings, spec, seed, work, media=next(iter(assets), None), operation=operation)
            if not (
                collection.content
                and collection.content.has_text
                or collection.document
                and collection.document.segments
            ):
                raise SourceError("尚未取得正文，状态占位不是有效 context。")
            if spec.strict_language and collection.document:
                actual = collection.document.language
                if not actual or actual.split("-")[0] != spec.language.split("-")[0]:
                    raise SourceError("字幕实际语言未知或不符合本次要求。")
            if (
                spec.transcript_mode == TranscriptMode.CAPTIONS_ONLY
                and collection.document
                and collection.document.source == TranscriptSource.ASR
            ):
                raise SourceError("已有转写不能满足原字幕要求。")
            item = safe_item(collection.item)
            processing = collection.processing or ProcessingManifest(
                provider=str(item.provenance.get("provider", "unknown"))
            )
            bundle = write_bundle(
                folder,
                item,
                document=collection.document,
                content=collection.content,
                processing=processing,
                force=spec.force,
            )
            if collection.original_document is not None:
                from soota.render.markdown import render_transcript

                write_artifact(
                    folder,
                    bundle,
                    path="transcript.original.md",
                    kind="transcript-original",
                    format="md",
                    content=render_transcript(item, collection.original_document),
                    provider=processing.provider,
                    force=spec.force,
                )
            for artifact in bundle.artifacts:
                if artifact.kind in {"body", "transcript", "context"}:
                    artifact.attributes.update(
                        acquisition_context=spec.execution_context,
                        input_fingerprint=row.input_fingerprint,
                        text_signature=spec.text_signature,
                    )
            bundle.metadata["input_fingerprint"] = row.input_fingerprint
            save_bundle(folder, bundle)
            if collection.raw_body is not None:
                atomic_write(
                    work / "references.json",
                    json.dumps(
                        {
                            "body": collection.raw_body,
                            "base": collection.reference_base or seed.value,
                            "attachments": [str(p) for p in collection.attachments],
                        },
                        ensure_ascii=False,
                    ),
                )
            row.steps["content"].provider = processing.provider
            row.data.update(collection.diagnostics)
            raw = collection.data if collection.data is not None else raw

        _step(settings, job, row, "content", fetch_text)
    bundle = load_bundle(folder)
    if "transcript" in spec.wants:

        def export_subtitles() -> None:
            if not _source_current(folder, bundle, spec, row):
                raise SourceError("当前源未满足本次采集条件，未把旧字幕作为新结果导出。")
            _export_subtitles(folder, bundle, spec)

        _step(settings, job, row, "subtitle-export", export_subtitles)
    if "data" in spec.wants and not _satisfied(folder, bundle, "data", spec, row):

        def save_data() -> None:
            nonlocal raw
            if seed.kind == "operation" and operation is None:
                raise SourceError("原生读取失败，未生成替代数据。")
            if raw is None and row.steps.get("content", StepState()).status in {"failed", "blocked"}:
                raise SourceError("正文获取失败，没有真实原始数据；未用包元数据替代。")
            if raw is None and (spec.provider == "weir" or bundle.processing.provider == "weir"):
                if not text_goals:
                    raise InputError("Weir 原始数据需要与 context/markdown 一起采集。")
                raw = collect_text(settings, spec, seed, work).data
                if raw is None:
                    raise SourceError("Weir 未交付真实原始数据。")
            if raw is None:
                if seed.kind == "file" and Path(seed.value).suffix.lower() == ".json":
                    raw = json.loads(Path(seed.value).read_text(encoding="utf-8"))
                elif seed.kind == "url" and not text_goals:
                    from soota.providers.router import inspect_item

                    fetched, records, warnings = inspect_item(
                        settings,
                        source_for(seed),
                        provider=spec.provider,
                        fallback=spec.fallback,
                        max_cost=spec.max_cost,
                        caption_metadata=False,
                    )
                    raw = {
                        "item": fetched.model_dump(mode="json"),
                        "requests": [r.model_dump(mode="json") for r in records],
                    }
                    row.warnings.extend(warnings)
                else:
                    raw = {
                        "source": bundle.source.model_dump(),
                        "metadata": bundle.metadata,
                        "artifacts": [asset.model_dump(mode="json") for asset in bundle.artifacts],
                    }
            write_artifact(
                folder,
                bundle,
                path="data.json",
                kind="data",
                format="json",
                content=json.dumps(redact_any(raw), ensure_ascii=False, indent=2) + "\n",
                provider=operation.provider if operation else bundle.processing.provider,
                force=spec.force,
                attributes={"acquisition_context": spec.execution_context, "input_fingerprint": row.input_fingerprint},
            )

        _step(settings, job, row, "data", save_data)
    if "images" in spec.wants:

        def save_images() -> None:
            if bundle.content is not None and re.search(r"!\[", bundle.content.body_md):
                _images(settings, spec, seed, work, folder, bundle, row)
                return
            if "images" in media_goals or seed.kind == "bundle" and _valid(folder, bundle, "image"):
                if row.steps.get("media", StepState()).status in {"failed", "blocked"}:
                    raise SourceError("原生图集尚未完整取得。")
                if not _valid(folder, bundle, "image"):
                    raise SourceError("没有取得图片文件。")
                return
            _images(settings, spec, seed, work, folder, bundle, row)

        _step(settings, job, row, "images", save_images)
    bundle = load_bundle(folder)
    bundle.metadata["task"] = {"task_id": job.id, "item_id": row.item_id}
    save_bundle(folder, bundle)
    bundle = load_bundle(folder)
    missing = [goal for goal in spec.required if not _satisfied(folder, bundle, goal, spec, row)]
    if row.steps.get("media", StepState()).status in {"failed", "blocked"}:
        missing = sorted(set(missing) | ((media_goals & spec.required) - previous_media))
    row.data.update(
        {
            "missing_outputs": sorted(missing),
            "artifacts": [a.model_dump(mode="json") for a in bundle.artifacts],
            "context": str(folder / "context.md") if (folder / "context.md").is_file() else None,
            "manifest": str(folder / "manifest.json"),
            "output": str(folder),
        }
    )
    if not missing and not any(error["step"] == "export" for error in row.errors):
        row.status = "completed"
    else:
        row.status = (
            "partial"
            if bundle.artifacts
            else "blocked"
            if any(step.status == "blocked" for step in row.steps.values())
            else "failed"
        )
    save_job(settings, job, item=row)


def _execute_item(settings: SootaSettings, spec: TaskSpec, job: TaskJob, row: TaskItem, seed: InputSpec) -> None:
    row.status, row.errors = "running", []
    save_job(settings, job, item=row)
    try:
        if seed.kind == "file" and file_fingerprint(Path(seed.value)) != row.input_fingerprint:
            raise InputError("本地输入自任务创建后已变化，请创建新任务或明确更新输入。")
        _run_item(settings, spec, job, row, seed)
    except Exception as exc:
        # Provider/format bugs are isolated to this item too. Do not expose raw
        # exception text, which may contain credentials or signed URLs.
        error = (
            _error(exc)
            if isinstance(exc, (SootaError, OSError))
            else SootaError("条目处理发生未预期错误。", details={"exception_type": type(exc).__name__}).to_dict()
        )
        row.status = "blocked" if isinstance(exc, (InputError, AuthBudgetError)) else "failed"
        row.errors.append(error)
        for step in row.steps.values():
            if step.status == "running":
                step.status, step.error = row.status, error
        save_job(settings, job, item=row)


@online_prices
def execute(
    settings: SootaSettings,
    spec: TaskSpec,
    *,
    job: TaskJob | None = None,
    analysis: Path | None = None,
    dry_run: bool = False,
    _locked: bool = False,
) -> CommandResult:
    from soota.workflow_compat import invoke, needs_workflow

    if analysis is not None or needs_workflow(spec.model_dump()):
        return invoke("execute", settings, spec, job=job, analysis=analysis, dry_run=dry_run, _locked=_locked)
    if dry_run:
        return CommandResult(
            ok=True,
            status="success",
            code="ok",
            exit_code=0,
            message="已生成离线任务计划，未创建任务或联网。",
            data={"plan": redact_any(spec.model_dump(mode="json")), "network_discovery": "not_run"},
        )
    from soota.tasks.policy import task_context, task_settings

    settings = task_settings(settings, spec)
    if spec.routing is not None:
        spec = spec.model_copy(update={"execution_context": task_context(settings, spec)})
    if not spec.execution_context:
        from soota.tasks.entry import execution_context

        spec = spec.model_copy(
            update={
                "execution_context": execution_context(settings, spec.provider, spec.inputs, scope=spec.execution_scope)
            }
        )
    job = job or create_job(settings, spec)
    budget = TaskBudget(task_folder(settings, job.id), spec.max_cost)
    guard = nullcontext() if _locked else JobStore(settings.jobs_dir).lock(job.id)
    with guard, task_budget_scope(budget):
        if spec.routing is not None:
            save_spec(settings, job.id, spec)
        try:
            job.status = "running"
            save_job(settings, job)
            if not job.expanded:
                set_inputs(settings, job, expand_inputs(settings, spec, job))
            inputs = resolved_inputs(settings, job)
            from soota.tasks.dependencies import task_stages

            for index, stage in enumerate(task_stages(spec)):
                # 整批必需目标完成后才派发可选获取；失败条目不在同次运行中自动重试。
                if index and any(row.status != "completed" for row in job.items[: spec.max_items]):
                    job.warnings.append("必需产物尚未全部完成，已保留剩余额度并跳过可选获取。")
                    break

                def acquire_stage(row: TaskItem, stage: TaskSpec = stage) -> None:
                    _execute_item(settings, stage, job, row, inputs[row.input_index])

                run_items(
                    job.items[: spec.max_items],
                    workers=spec.workers,
                    folder=lambda row: _folder(spec, inputs[row.input_index]),
                    action=acquire_stage,
                )
        except KeyboardInterrupt:
            # The scheduler has drained workers before this checkpoint/lock exit.
            for row in job.items:
                if row.status == "running":
                    row.status = "pending"
                for step in row.steps.values():
                    if step.status == "running":
                        step.status = "pending"
            job.status = "paused"
            save_job(settings, job)
            raise
        except (SootaError, OSError) as exc:
            job.status = "blocked"
            job.warnings.append(str(_error(exc)["message"]))
            save_job(settings, job)
        else:
            statuses = [row.status for row in job.items[: spec.max_items]]
            if statuses and all(status == "completed" for status in statuses):
                job.status = "completed"
            elif statuses and all(status in {"completed", "waiting_analysis"} for status in statuses):
                job.status = "waiting_analysis"
            elif any(status in {"completed", "partial", "waiting_analysis"} for status in statuses):
                job.status = "partial"
            else:
                job.status = "blocked" if "blocked" in statuses else "failed"
            if not statuses and job.expanded:
                job.status = "completed"
            blocked_enumeration = any(
                info.get("stop_reason") in {"budget_or_auth", "error", "storage", "interrupted"}
                for info in job.enumeration.values()
                if isinstance(info, dict)
            )
            if blocked_enumeration:
                job.status = "partial"
            save_job(settings, job)
    return result_for(settings, spec, job)


def result_for(settings: SootaSettings, spec: TaskSpec, job: TaskJob) -> CommandResult:
    budget = TaskBudget(task_folder(settings, job.id), spec.max_cost)
    selected = job.items[: spec.max_items]
    complete = job.status == "completed"
    data: dict[str, Any] = {
        "job_id": job.id,
        "job_status": job.status,
        "workers": spec.workers,
        "item_count": len(selected),
        "discovered": len(job.items),
        "limit_reached": len(job.items) > spec.max_items
        or any(
            info.get("stop_reason") in {"max_items", "max_pages", "max_items_or_unknown"}
            for info in job.enumeration.values()
            if isinstance(info, dict)
        ),
        "enumeration": job.enumeration,
        "items": [row.model_dump(mode="json") for row in selected],
        **budget.snapshot(),
        "actual_cost_usd": None,
    }
    from soota.storage.credit_budget import CreditBudget

    credits = CreditBudget(settings.data_dir / "billing" / "weir")
    if credits.path.exists() and (
        spec.provider == "weir" or any(step.provider == "weir" for row in selected for step in row.steps.values())
    ):
        with credits.locked():
            task_identity = hashlib.sha256(str(task_folder(settings, job.id).resolve()).encode()).hexdigest()
            data["weir_credits"] = credits.summary(task_identity)
    data["reserved_cost_usd"] = float(data["reserved_cost_usd"])
    if len(selected) == 1:
        data = {**selected[0].data, **data}
    return CommandResult(
        ok=complete,
        status="success" if complete else "partial",
        code="ok" if complete else job.status,
        exit_code=0 if complete else 2,
        message="已完成本次范围内的产物交付。" if complete else "任务尚未完成，进度已保存。",
        data=redact_any(data),
        warnings=job.warnings,
    )


def resume(
    settings: SootaSettings,
    job_id: str,
    *,
    max_items: int | None = None,
    max_pages: int | None = None,
    max_cost: float | None = None,
    weir_max_credits: int | None = None,
    workers: int | None = None,
    analysis: Path | None = None,
    force: bool = False,
) -> CommandResult:
    if workers is not None and (type(workers) is not int or not 1 <= workers <= 8):
        raise InputError("workers 必须为 1–8 的整数。")
    from soota.workflow_compat import invoke, job_needs_workflow

    if analysis is not None or job_needs_workflow(settings, job_id):
        return invoke(
            "resume",
            settings,
            job_id,
            max_items=max_items,
            max_pages=max_pages,
            max_cost=max_cost,
            **({"weir_max_credits": weir_max_credits} if weir_max_credits is not None else {}),
            workers=workers,
            analysis=analysis,
            force=force,
        )
    record = JobStore(settings.jobs_dir).get(job_id)
    if record.get("kind") != "content-task":
        from soota.catalog import resume_catalog

        if analysis is not None:
            raise InputError("旧目录任务不接受分析提交。")
        if weir_max_credits is not None:
            raise InputError("--weir-max-credits 仅适用于统一内容任务。")
        if workers is not None:
            raise InputError("--workers 仅适用于统一内容任务；目录枚举仍按顺序执行。")
        return resume_catalog(settings, job_id, max_items=max_items, max_pages=max_pages, max_cost=max_cost)
    with JobStore(settings.jobs_dir).lock(job_id):
        record = JobStore(settings.jobs_dir).get(job_id)
        job = TaskJob.model_validate(record)
        spec = load_spec(settings, job_id)
        updates = {
            key: value
            for key, value in {
                "max_items": max_items,
                "max_pages": max_pages,
                "max_cost": max_cost,
                "weir_max_credits": weir_max_credits,
                "workers": workers,
            }.items()
            if value is not None
        }
        incomplete_enumeration = any(
            info.get("stop_reason") in {"budget_or_auth", "error", "storage", "interrupted", "max_pages", "max_items"}
            for info in job.enumeration.values()
            if isinstance(info, dict)
        )
        if (
            job.enumeration
            and (updates.keys() - {"workers", "weir_max_credits"} or incomplete_enumeration)
            and analysis is None
        ):
            job.expanded = False
        from soota.tasks.entry import execution_context

        try:
            spec = TaskSpec.model_validate(
                {
                    **spec.model_dump(),
                    **updates,
                    "force": force,
                    "execution_context": spec.execution_context
                    if analysis is not None
                    else execution_context(settings, spec.provider, spec.inputs, scope=spec.execution_scope),
                }
            )
        except ValidationError as exc:
            raise InputError("续跑参数无效；请检查范围和费用上限。") from exc
        save_spec(settings, job_id, spec)
        settings = settings.model_copy(update={"default_language": spec.language})
        return execute(settings, spec, job=job, analysis=analysis, _locked=True)
