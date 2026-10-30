from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from soota.cli_output import run_settings
from soota.features import call_feature, list_features, show_feature

app = typer.Typer(help="查看和执行 Soota 业务入口、已核验 MCP 能力与 TikHub 封装。")


@app.command("list")
def listing(
    provider: Annotated[str | None, typer.Option("--provider")] = None,
    site: Annotated[str | None, typer.Option("--site")] = None,
    search: Annotated[str | None, typer.Option("--search")] = None,
    limit: Annotated[int, typer.Option("--limit")] = 50,
    offset: Annotated[int, typer.Option("--offset")] = 0,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    run_settings(
        json_output,
        list_features,
        provider=provider,
        site=site,
        search=search,
        limit=limit,
        offset=offset,
    )


@app.command()
def show(
    feature: Annotated[str, typer.Argument()], json_output: Annotated[bool, typer.Option("--json")] = False
) -> None:
    run_settings(json_output, show_feature, feature)


@app.command()
def call(
    feature: Annotated[str, typer.Argument()],
    param: Annotated[list[str] | None, typer.Option("--param", help="name=value，可重复；ID 保持字符串")] = None,
    params_file: Annotated[Path | None, typer.Option("--params-file")] = None,
    body_file: Annotated[Path | None, typer.Option("--body")] = None,
    output: Annotated[Path | None, typer.Option("--output", help="保存脱敏原生结果到新 JSON 文件")] = None,
    provider: Annotated[
        str | None, typer.Option("--provider", help="auto/opencli-mcp/opencli/yt-dlp/tikhub/monid/treg")
    ] = None,
    max_cost: Annotated[float | None, typer.Option("--max-cost", help="本次额外费用上限，USD")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    allow_write: Annotated[bool, typer.Option("--allow-write", help="明确允许 TikHub 辅助接口")] = False,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    run_settings(
        json_output,
        call_feature,
        feature,
        params=param,
        params_file=params_file,
        body_file=body_file,
        output=output,
        provider=provider,
        max_cost=max_cost,
        dry_run=dry_run,
        allow_write=allow_write,
    )
