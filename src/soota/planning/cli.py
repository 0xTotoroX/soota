"""规划与预设命令。

[INPUT]: 已归一的目标参数、方案 ID 和用户确认。
[OUTPUT]: plan create/show/select/run，presets list/show/delete。
[POS]: 纯 CLI 适配；自然语言解释与确认对话由 Soota Skill 指导 Agent。
[PROTOCOL]: 变更时更新此头部，然后检查 AGENTS.md
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from soota.cli_output import run_settings
from soota.models import TranscriptMode
from soota.planning.service import create_plan, manage_presets, run_plan, select_plan, show_plan

app = typer.Typer(help="目标规划、方案选择和执行；报价按需刷新")
presets_app = typer.Typer(help="查看和删除命名预设；匹配后仍需确认")


@app.command("create")
def create(
    goal: Annotated[str, typer.Option("--goal", help="用户目标的简短说明")],
    want: Annotated[str, typer.Option("--want", help="采集目标，逗号分隔")],
    url: Annotated[str | None, typer.Argument(help="链接、分享文本、本地文件或内容包")] = None,
    optional: Annotated[str | None, typer.Option("--optional")] = None,
    inputs_file: Annotated[Path | None, typer.Option("--inputs-file")] = None,
    operation: Annotated[str | None, typer.Option("--operation")] = None,
    param: Annotated[list[str] | None, typer.Option("--param")] = None,
    params_file: Annotated[Path | None, typer.Option("--params-file")] = None,
    expand: Annotated[bool | None, typer.Option("--expand/--no-expand")] = None,
    provider: Annotated[str | None, typer.Option("--provider")] = None,
    fallback: Annotated[str | None, typer.Option("--fallback")] = None,
    audio_provider: Annotated[str | None, typer.Option("--audio-provider")] = None,
    transcript: Annotated[TranscriptMode | None, typer.Option("--transcript")] = None,
    language: Annotated[str | None, typer.Option("--language")] = None,
    subtitle_formats: Annotated[str | None, typer.Option("--subtitle-format")] = None,
    input_format: Annotated[str | None, typer.Option("--input-format")] = None,
    min_height: Annotated[int | None, typer.Option("--min-height")] = None,
    allow_silent: Annotated[bool | None, typer.Option("--allow-silent/--require-audio")] = None,
    output: Annotated[Path | None, typer.Option("--output")] = None,
    max_cost: Annotated[float | None, typer.Option("--max-cost")] = None,
    weir_max_credits: Annotated[int | None, typer.Option("--weir-max-credits")] = None,
    max_items: Annotated[int | None, typer.Option("--max-items")] = None,
    max_pages: Annotated[int | None, typer.Option("--max-pages")] = None,
    workers: Annotated[int | None, typer.Option("--workers")] = None,
    max_bytes: Annotated[int | None, typer.Option("--max-bytes")] = None,
    prefer_youtube: Annotated[bool | None, typer.Option("--prefer-youtube/--no-prefer-youtube")] = None,
    exclude_promotions: Annotated[bool | None, typer.Option("--exclude-promotions/--keep-promotions")] = None,
    exclude_range: Annotated[list[str] | None, typer.Option("--exclude-range")] = None,
    offline: Annotated[bool, typer.Option("--offline", help="只读本地报价估计；确认前仍会核价")] = False,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    values = {key: value for key, value in locals().items() if value is not None}
    for key in ("goal", "url", "offline", "json_output"):
        values.pop(key, None)
    if "param" in values:
        values["params"] = values.pop("param")
    if "exclude_range" in values:
        values["exclude_ranges"] = values.pop("exclude_range")
    run_settings(json_output, create_plan, goal, url, offline=offline, **values)


@app.command("show")
def show(plan_id: str, json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
    run_settings(json_output, show_plan, plan_id)


@app.command("select")
def select(
    plan_id: str,
    option: Annotated[str, typer.Option("--option", help="create/show 返回的方案 ID")],
    confirm: Annotated[bool, typer.Option("--confirm", help="仅在用户已确认本方案后传入")] = False,
    save_as: Annotated[str | None, typer.Option("--save-as", help="预设名称，默认沿用旧名称或目标说明")] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    run_settings(json_output, select_plan, plan_id, option, confirm=confirm, save_as=save_as)


@app.command("run")
def run(plan_id: str, json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
    run_settings(json_output, run_plan, plan_id)


@presets_app.command("list")
def list_presets(json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
    run_settings(json_output, manage_presets)


@presets_app.command("show")
def show_preset(name: str, json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
    run_settings(json_output, manage_presets, name=name)


@presets_app.command("delete")
def remove_preset(name: str, json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
    run_settings(json_output, manage_presets, name=name, delete=True)
