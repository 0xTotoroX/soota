"""Public Typer entry points.

[INPUT]: Command arguments, settings and application command functions.
[OUTPUT]: 目标规划、预设、采集与恢复命令；旧知识命令仅转交 LoreLab。
[POS]: soota CLI adapter; task execution contracts live in tasks/ and docs/tasks.md.
[PROTOCOL]: 变更时更新此头部，然后检查 AGENTS.md
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

import typer
from typer.core import TyperGroup

from soota.api import platforms as platforms_cmd
from soota.api_cli import app as api_app
from soota.catalog import DEFAULT_MAX_PAGES
from soota.catalog import catalog as catalog_cmd
from soota.cli_output import run as _run
from soota.cli_output import run_settings
from soota.features_cli import app as features_app
from soota.mcp_cli import app as mcp_app
from soota.models import TranscriptMode
from soota.pipeline import doctor as doctor_cmd
from soota.pipeline import estimate as estimate_cmd
from soota.pipeline import inspect as inspect_cmd
from soota.pipeline import list_jobs as list_jobs_cmd
from soota.pipeline import render as render_cmd
from soota.planning.cli import app as plan_app
from soota.planning.cli import presets_app
from soota.platform_support import configure_stdio
from soota.policy_cli import app as policy_app
from soota.providers_cli import app as providers_app
from soota.runtime_cli import app as runtime_app
from soota.tasks.entry import capabilities as capabilities_cmd
from soota.tasks.entry import ingest as ingest_cmd
from soota.tasks.runner import resume as resume_task_cmd
from soota.updates_cli import app as updates_app


class UTF8Group(TyperGroup):
    def main(self, *args: Any, **kwargs: Any) -> Any:
        # Configure before eager --help/usage handling, including the :app console entry.
        configure_stdio()
        return super().main(*args, **kwargs)


app = typer.Typer(
    cls=UTF8Group,
    add_completion=False,
    no_args_is_help=True,
    help="Soota：采集正文、字幕和媒体，解析为可追溯的 context。",
)
jobs_app = typer.Typer(help="查看任务与续跑目录采集")
app.add_typer(jobs_app, name="jobs")
app.add_typer(api_app, name="api")
app.add_typer(runtime_app, name="runtime")
app.add_typer(policy_app, name="policy")
app.add_typer(providers_app, name="providers")
app.add_typer(features_app, name="features")
app.add_typer(updates_app, name="updates")
app.add_typer(mcp_app, name="mcp")
app.add_typer(plan_app, name="plan")
app.add_typer(presets_app, name="presets")


@app.command(
    "knowledge",
    hidden=True,
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True, "help_option_names": []},
)
def knowledge(ctx: typer.Context) -> None:
    """兼容旧命令，将原参数转交 LoreLab knowledge。"""
    from soota.workflow_compat import forward

    raise typer.Exit(forward(["knowledge", *ctx.args]))


@app.command(
    "study",
    hidden=True,
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True, "help_option_names": []},
)
def study(ctx: typer.Context) -> None:
    """兼容旧命令，将原参数转交 LoreLab study。"""
    from soota.workflow_compat import forward

    raise typer.Exit(forward(["study", *ctx.args]))


@app.command()
def platforms(
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    _run(json_output, platforms_cmd)


@app.command()
def doctor(
    json_output: Annotated[bool, typer.Option("--json", help="stdout 只输出一个 JSON 文档")] = False,
) -> None:
    run_settings(
        json_output,
        doctor_cmd,
    )


@app.command()
def inspect(
    url: Annotated[str, typer.Argument(help="YouTube 或 Bilibili URL")],
    provider: Annotated[
        str | None, typer.Option("--provider", help="auto/opencli-mcp/opencli/yt-dlp/tikhub/monid/treg")
    ] = None,
    fallback: Annotated[str | None, typer.Option("--fallback", help="显式允许 tikhub 兜底；仅已验证能力")] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
    max_cost: Annotated[float | None, typer.Option("--max-cost", help="可选 USD 费用上限；0 禁止计费请求")] = None,
    offline: Annotated[bool, typer.Option("--offline", help="只识别 URL，不调用 Provider")] = False,
) -> None:
    run_settings(
        json_output,
        inspect_cmd,
        url,
        fetch=not offline,
        provider=provider,
        fallback=fallback,
        max_cost=max_cost,
    )


@app.command()
def estimate(
    url: Annotated[str, typer.Argument()],
    provider: Annotated[str | None, typer.Option("--provider")] = None,
    mode: Annotated[str, typer.Option("--mode")] = "ingest",
    transcript: Annotated[TranscriptMode, typer.Option("--transcript")] = TranscriptMode.AUTO,
    audio_provider: Annotated[
        str | None, typer.Option("--audio-provider", help="音频来源：auto/yt-dlp/downie/tikhub/monid/treg")
    ] = None,
    max_items: Annotated[int, typer.Option("--max-items", help="目录任务的条数上限")] = 100,
    max_pages: Annotated[int, typer.Option("--max-pages", help="目录任务的分页上限")] = DEFAULT_MAX_PAGES,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    run_settings(
        json_output,
        estimate_cmd,
        url,
        mode=mode,
        provider=provider,
        transcript=transcript,
        audio_provider=audio_provider,
        max_items=max_items,
        max_pages=max_pages,
    )


@app.command()
def catalog(
    url: Annotated[str, typer.Argument(help="X、YouTube 频道或 Bilibili UP 主主页")],
    provider: Annotated[
        str | None, typer.Option("--provider", help="auto/opencli-mcp/opencli/yt-dlp/tikhub/monid/treg")
    ] = None,
    fallback: Annotated[str | None, typer.Option("--fallback", help="显式允许 tikhub 兜底；仅已验证能力")] = None,
    max_items: Annotated[int, typer.Option("--max-items", help="本任务累计收录的条数上限")] = 100,
    max_pages: Annotated[int, typer.Option("--max-pages", help="本任务累计读取的页数上限")] = DEFAULT_MAX_PAGES,
    max_cost: Annotated[
        float | None, typer.Option("--max-cost", help="任务累计费用上限，单位 USD；可选附加上限")
    ] = None,
    output: Annotated[Path | None, typer.Option("--output")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="离线预估，不联网或创建任务")] = False,
    incremental: Annotated[bool, typer.Option("--incremental", help="跳过已收录条目，整页无新增时停止")] = False,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    run_settings(
        json_output,
        catalog_cmd,
        url,
        provider=provider,
        fallback=fallback,
        max_items=max_items,
        max_pages=max_pages,
        max_cost=max_cost,
        output=output,
        dry_run=dry_run,
        incremental=incremental,
    )


@app.command()
def ingest(
    url: Annotated[str | None, typer.Argument(help="链接、分享文本、本地文件或内容包")] = None,
    want: Annotated[
        str | None,
        typer.Option(
            "--want",
            help="目标产物，逗号分隔：video/audio/transcript/context/markdown/images/data",
        ),
    ] = None,
    optional: Annotated[str | None, typer.Option("--optional", help="可选产物，失败不影响必需目标")] = None,
    inputs_file: Annotated[
        Path | None, typer.Option("--inputs-file", help="链接清单、目录 JSON 或 InputSpec 数组")
    ] = None,
    operation: Annotated[
        str | None, typer.Option("--operation", help="显式原生读取操作，如 opencli.youtube.search")
    ] = None,
    param: Annotated[list[str] | None, typer.Option("--param")] = None,
    params_file: Annotated[Path | None, typer.Option("--params-file")] = None,
    expand: Annotated[bool, typer.Option("--expand", help="把已绑定的搜索结果展开为内容任务")] = False,
    language: Annotated[str | None, typer.Option("--language", help="要求的实际字幕语言")] = None,
    subtitle_formats: Annotated[str, typer.Option("--subtitle-format", help="srt/vtt/txt/json，可逗号组合")] = "srt",
    input_format: Annotated[str | None, typer.Option("--input-format", help="本地字幕 wire 格式")] = None,
    min_height: Annotated[int | None, typer.Option("--min-height", help="视频最低高度，不降清晰度交付")] = None,
    allow_silent: Annotated[bool, typer.Option("--allow-silent", help="允许无音轨的视频")] = False,
    max_bytes: Annotated[int, typer.Option("--max-bytes", help="单个媒体文件字节上限")] = 2 * 1024**3,
    max_pages: Annotated[int, typer.Option("--max-pages")] = 10,
    workers: Annotated[int, typer.Option("--workers", min=1, max=8, help="独立条目并发数；枚举仍有序")] = 1,
    destination: Annotated[Path | None, typer.Option("--destination", hidden=True, help="另外导出到指定目录")] = None,
    vault: Annotated[Path | None, typer.Option("--vault", hidden=True, help="显式 Obsidian Vault 目录")] = None,
    subdir: Annotated[str, typer.Option("--subdir", hidden=True, help="Vault 内目录，不猜测分类")] = "Soota",
    provider: Annotated[
        str | None, typer.Option("--provider", help="auto/opencli/yt-dlp/tikhub/monid/treg/weir/http/local/downie")
    ] = None,
    fallback: Annotated[str | None, typer.Option("--fallback", help="显式允许 tikhub 兜底；仅已验证能力")] = None,
    transcript: Annotated[TranscriptMode, typer.Option("--transcript")] = TranscriptMode.AUTO,
    audio_provider: Annotated[
        str | None, typer.Option("--audio-provider", help="转写用的音频来源：auto/yt-dlp/downie/tikhub/monid/treg")
    ] = None,
    output: Annotated[Path | None, typer.Option("--output")] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
    max_cost: Annotated[float | None, typer.Option("--max-cost")] = None,
    weir_max_credits: Annotated[
        int | None, typer.Option("--weir-max-credits", min=0, help="本任务 Weir credits 总上限")
    ] = None,
    max_items: Annotated[int | None, typer.Option("--max-items")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    force: Annotated[bool, typer.Option("--force")] = False,
    prefer_youtube: Annotated[
        bool | None,
        typer.Option(
            "--prefer-youtube/--no-prefer-youtube", help="B站视频检查同名YouTube频道及同一期，匹配后优先YouTube字幕"
        ),
    ] = None,
    exclude_promotions: Annotated[
        bool | None,
        typer.Option("--exclude-promotions/--keep-promotions", help="过滤已标注推广；跨平台开关开启时默认启用"),
    ] = None,
    exclude_range: Annotated[
        list[str] | None, typer.Option("--exclude-range", help="人工或Agent核实的推广区间，起秒:止秒，可重复")
    ] = None,
) -> None:
    if max_items is not None and max_items < 1:
        raise typer.Exit(3)
    run_settings(
        json_output,
        ingest_cmd,
        url,
        want=want,
        optional=optional,
        inputs_file=inputs_file,
        operation=operation,
        params=param,
        params_file=params_file,
        expand=expand,
        language=language,
        subtitle_formats=subtitle_formats,
        input_format=input_format,
        min_height=min_height,
        allow_silent=allow_silent,
        max_bytes=max_bytes,
        max_pages=max_pages,
        workers=workers,
        max_items=max_items,
        destination=destination,
        vault=vault,
        subdir=subdir,
        provider=provider,
        fallback=fallback,
        transcript=transcript,
        audio_provider=audio_provider,
        output=output,
        max_cost=max_cost,
        weir_max_credits=weir_max_credits,
        dry_run=dry_run,
        force=force,
        prefer_youtube=prefer_youtube,
        exclude_promotions=exclude_promotions,
        exclude_ranges=exclude_range,
    )


@app.command(hidden=True)
def render(
    bundle: Annotated[Path, typer.Option("--bundle", help="manifest.json 或内容包目录")],
    analysis: Annotated[Path, typer.Option("--analysis", help="Analysis JSON")],
    json_output: Annotated[bool, typer.Option("--json")] = False,
    force: Annotated[bool, typer.Option("--force")] = False,
) -> None:
    run_settings(json_output, render_cmd, bundle=bundle, analysis=analysis, force=force)


@jobs_app.command("list")
def jobs_list(
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    run_settings(
        json_output,
        list_jobs_cmd,
    )


@jobs_app.command("resume")
def jobs_resume(
    job_id: Annotated[str, typer.Argument()],
    max_items: Annotated[int | None, typer.Option("--max-items", help="新的累计条数上限")] = None,
    max_pages: Annotated[int | None, typer.Option("--max-pages", help="新的累计页数上限")] = None,
    max_cost: Annotated[float | None, typer.Option("--max-cost", help="新的总费用上限，已有预留不会清零")] = None,
    weir_max_credits: Annotated[
        int | None, typer.Option("--weir-max-credits", min=0, help="新的 Weir credits 总上限，不清零历史用量")
    ] = None,
    workers: Annotated[
        int | None, typer.Option("--workers", min=1, max=8, help="覆盖并保存条目并发数；默认沿用任务设置")
    ] = None,
    analysis: Annotated[Path | None, typer.Option("--analysis", hidden=True, help="兼容旧任务，转交 LoreLab")] = None,
    force: Annotated[bool, typer.Option("--force")] = False,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    run_settings(
        json_output,
        resume_task_cmd,
        job_id,
        max_items=max_items,
        max_pages=max_pages,
        max_cost=max_cost,
        weir_max_credits=weir_max_credits,
        workers=workers,
        analysis=analysis,
        force=force,
    )


@app.command("capabilities")
def capabilities(
    search: Annotated[str | None, typer.Option("--search")] = None,
    provider: Annotated[str | None, typer.Option("--provider")] = None,
    limit: Annotated[int, typer.Option("--limit")] = 50,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    run_settings(json_output, capabilities_cmd, search=search, provider=provider, limit=limit)


def main() -> None:
    configure_stdio()
    app()


if __name__ == "__main__":
    main()
