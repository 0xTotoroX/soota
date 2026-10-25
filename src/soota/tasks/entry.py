"""Task entry and offline capability discovery.

[INPUT]: CLI options, settings, input parsing and provider selection.
[OUTPUT]: ingest/capabilities; validated persistent task settings or legacy ingest.
[POS]: tasks entry; runner executes explicit goals and bounded batches.
[PROTOCOL]: 变更时更新此头部，然后检查 AGENTS.md
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError

from soota.billing import selected_provider
from soota.config import SootaSettings
from soota.errors import InputError, SourceError
from soota.features import read_params
from soota.models import CommandResult, TranscriptMode
from soota.routing import recognize
from soota.tasks.inputs import parse_inputs, parse_outputs
from soota.tasks.models import InputSpec, TaskSpec
from soota.tasks.runner import execute


def execution_context(
    settings: SootaSettings, provider: str, inputs: list[InputSpec] | None = None, *, scope: str | None = None
) -> str:
    from soota.providers.opencli_context import profile_config

    local_only = inputs is not None and all(item.kind in {"file", "bundle"} for item in inputs)
    profile = {} if local_only else profile_config()
    name = settings.opencli.profile or os.environ.get("OPENCLI_PROFILE") or profile.get("defaultContextId")
    aliases = profile.get("aliases", {})
    actual = aliases.get(name, name) if isinstance(aliases, dict) and isinstance(name, str) else None
    # No daemon call during planning/local work. An unbound browser identity
    # cannot authorize reusing an account-sensitive cache in a different task.
    actual = (
        "local-input"
        if local_only
        else actual or ("unbound-" + (scope or uuid4().hex) if provider in {"auto", "opencli", "opencli-mcp"} else None)
    )
    identity = [
        provider,
        actual,
        settings.local.cookies_browser,
        settings.local.binary,
        settings.asr.model_dump(),
    ]
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def ingest(
    settings: SootaSettings,
    url: str | None = None,
    *,
    want: str | None = None,
    optional: str | None = None,
    inputs_file: Path | None = None,
    operation: str | None = None,
    params: list[str] | None = None,
    params_file: Path | None = None,
    expand: bool = False,
    transcript: TranscriptMode = TranscriptMode.AUTO,
    audio_provider: str | None = None,
    provider: str | None = None,
    fallback: str | None = None,
    output: Path | None = None,
    max_cost: float | None = None,
    weir_max_credits: int | None = None,
    max_items: int | None = None,
    max_pages: int = 10,
    workers: int = 1,
    max_bytes: int = 2 * 1024**3,
    min_height: int | None = None,
    allow_silent: bool = False,
    language: str | None = None,
    input_format: str | None = None,
    subtitle_formats: str = "srt",
    destination: Path | None = None,
    vault: Path | None = None,
    subdir: str = "Soota",
    dry_run: bool = False,
    force: bool = False,
    prefer_youtube: bool | None = None,
    exclude_promotions: bool | None = None,
    exclude_ranges: list[str] | None = None,
) -> CommandResult:
    from soota.workflow_compat import ingest_needs_workflow, invoke

    if ingest_needs_workflow(want, optional, destination, vault):
        return invoke(
            "ingest",
            settings,
            url,
            want=want,
            optional=optional,
            inputs_file=inputs_file,
            operation=operation,
            params=params,
            params_file=params_file,
            expand=expand,
            transcript=transcript,
            audio_provider=audio_provider,
            provider=provider,
            fallback=fallback,
            output=output,
            max_cost=max_cost,
            weir_max_credits=weir_max_credits if weir_max_credits is not None else settings.weir.task_credits,
            max_items=max_items,
            max_pages=max_pages,
            workers=workers,
            max_bytes=max_bytes,
            min_height=min_height,
            allow_silent=allow_silent,
            language=language,
            input_format=input_format,
            subtitle_formats=subtitle_formats,
            destination=destination,
            vault=vault,
            subdir=subdir,
            dry_run=dry_run,
            force=force,
            prefer_youtube=prefer_youtube,
            exclude_promotions=exclude_promotions,
            exclude_ranges=exclude_ranges,
        )
    if type(workers) is not int or not 1 <= workers <= 8:
        raise InputError("workers 必须为 1–8 的整数。")
    # Keep the established single-video default command contract. Explicit
    # goals and new input types use the shared artifact task executor.
    legacy = (
        want is None
        and optional is None
        and inputs_file is None
        and operation is None
        and language is None
        and destination is None
        and vault is None
        and max_items in {None, 1}
        and input_format is None
        and workers == 1
    )
    if legacy and url:
        try:
            source = recognize(url)
            legacy = source.kind in {"video", "shortlink"}
        except (InputError, SourceError):
            legacy = False
        if legacy:
            from soota.pipeline import ingest as legacy_ingest

            return legacy_ingest(
                settings,
                url,
                provider=provider,
                fallback=fallback,
                transcript=transcript,
                audio_provider=audio_provider,
                output=output,
                max_cost=max_cost,
                dry_run=dry_run,
                force=force,
                prefer_youtube=prefer_youtube,
                exclude_promotions=exclude_promotions,
                exclude_ranges=exclude_ranges,
            )
    spec = prepare_task(
        settings,
        url,
        want=want,
        optional=optional,
        inputs_file=inputs_file,
        operation=operation,
        params=params,
        params_file=params_file,
        expand=expand,
        transcript=transcript,
        audio_provider=audio_provider,
        provider=provider,
        fallback=fallback,
        output=output,
        max_cost=max_cost,
        weir_max_credits=weir_max_credits,
        max_items=max_items,
        max_pages=max_pages,
        workers=workers,
        max_bytes=max_bytes,
        min_height=min_height,
        allow_silent=allow_silent,
        language=language,
        input_format=input_format,
        subtitle_formats=subtitle_formats,
        destination=destination,
        vault=vault,
        subdir=subdir,
        dry_run=dry_run,
        force=force,
        prefer_youtube=prefer_youtube,
        exclude_promotions=exclude_promotions,
        exclude_ranges=exclude_ranges,
    )
    settings = settings.model_copy(update={"default_language": spec.language})
    return execute(settings, spec, dry_run=dry_run)


def prepare_task(
    settings: SootaSettings,
    url: str | None = None,
    *,
    want: str | None = None,
    optional: str | None = None,
    inputs_file: Path | None = None,
    operation: str | None = None,
    params: list[str] | None = None,
    params_file: Path | None = None,
    expand: bool = False,
    transcript: TranscriptMode = TranscriptMode.AUTO,
    audio_provider: str | None = None,
    provider: str | None = None,
    fallback: str | None = None,
    output: Path | None = None,
    max_cost: float | None = None,
    weir_max_credits: int | None = None,
    max_items: int | None = None,
    max_pages: int = 10,
    workers: int = 1,
    max_bytes: int = 2 * 1024**3,
    min_height: int | None = None,
    allow_silent: bool = False,
    language: str | None = None,
    input_format: str | None = None,
    subtitle_formats: str = "srt",
    destination: Path | None = None,
    vault: Path | None = None,
    subdir: str = "Soota",
    dry_run: bool = False,
    force: bool = False,
    prefer_youtube: bool | None = None,
    exclude_promotions: bool | None = None,
    exclude_ranges: list[str] | None = None,
) -> TaskSpec:
    if type(workers) is not int or not 1 <= workers <= 8:
        raise InputError("workers 必须为 1–8 的整数。")
    if operation is not None:
        if inputs_file is not None:
            raise InputError("--operation 与 --inputs-file 不能同时指定；多操作请写入 JSON 输入清单。")
        inputs = [
            InputSpec(
                kind="operation",
                value=url or "",
                operation=operation,
                params=read_params(params, params_file),
                expand=expand,
            )
        ]
    else:
        if params or params_file is not None:
            raise InputError("原生参数需要 --operation。")
        inputs = parse_inputs(url, inputs_file)
    chosen = selected_provider(settings, provider)
    try:
        spec = TaskSpec(
            inputs=inputs,
            outputs=parse_outputs(want, optional),
            output=(output or settings.output_dir).expanduser().resolve(),
            provider=chosen,
            fallback=fallback,
            audio_provider=audio_provider,
            language=language or settings.default_language,
            strict_language=language is not None,
            transcript_mode=transcript,
            subtitle_formats=subtitle_formats.split(","),  # type: ignore[arg-type]
            input_format=input_format,
            max_items=max_items if max_items is not None else 100,
            max_pages=max_pages,
            workers=workers,
            max_cost=max_cost,
            weir_max_credits=weir_max_credits if weir_max_credits is not None else settings.weir.task_credits,
            max_bytes=max_bytes,
            min_height=min_height,
            allow_silent=allow_silent,
            force=force,
            prefer_youtube=settings.content_selection.prefer_youtube if prefer_youtube is None else prefer_youtube,
            exclude_promotions=(settings.content_selection.prefer_youtube if prefer_youtube is None else prefer_youtube)
            if exclude_promotions is None
            else exclude_promotions,
            exclude_ranges=exclude_ranges or [],
            destination=destination.expanduser().resolve() if destination else None,
            vault=vault.expanduser().resolve() if vault else None,
            subdir=subdir,
        )
    except ValidationError as exc:
        raise InputError("任务参数无效；请检查目标、格式、范围和输出位置。") from exc
    spec.execution_context = execution_context(settings, chosen, inputs, scope=spec.execution_scope)
    return spec


def capabilities(
    settings: SootaSettings, *, search: str | None = None, provider: str | None = None, limit: int = 50
) -> CommandResult:
    from soota.content.discovery import discover_capabilities

    if not 1 <= limit <= 5000:
        raise InputError("limit 必须为 1–5000。")
    result = discover_capabilities(settings)
    rows = [
        row
        for row in result["capabilities"]
        if (provider is None or row["provider"] == provider)
        and (search is None or search.casefold() in f"{row['id']} {row.get('description', '')}".casefold())
    ]
    result.update(capabilities=rows[:limit], total=len(rows), limit=limit)
    return CommandResult(
        ok=True,
        status="success",
        code="ok",
        exit_code=0,
        message="已读取能力清单；发现不等于真实产物已验证。",
        data=result,
    )
