from pathlib import Path
from typing import Annotated

import typer

from soota.api import call_endpoint, list_endpoints, show_endpoint
from soota.cli_output import run, run_settings

app = typer.Typer(help="查询 TikHub 各平台内容接口；同功能优先当前最高版本", no_args_is_help=True)


@app.command("list")
def api_list(
    platform: Annotated[str | None, typer.Option("--platform")] = None,
    task: Annotated[str | None, typer.Option("--task", help="detail/search/catalog/comments/media 等")] = None,
    search: Annotated[str | None, typer.Option("--search", help="搜索接口名称或说明")] = None,
    all_versions: Annotated[bool, typer.Option("--all-versions", help="列出历史版本；默认每组仅列首选版本")] = False,
    limit: Annotated[int, typer.Option("--limit")] = 50,
    offset: Annotated[int, typer.Option("--offset")] = 0,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    run(
        json_output,
        list_endpoints,
        platform=platform,
        task=task,
        search=search,
        all_versions=all_versions,
        limit=limit,
        offset=offset,
    )


@app.command("show")
def api_show(
    endpoint: Annotated[str, typer.Argument(help="api list 中的 key 或完整接口路径")],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    run(json_output, show_endpoint, endpoint)


@app.command("call")
def api_call(
    endpoint: Annotated[str, typer.Argument(help="接口 key 或路径；旧路径自动选择同功能最新版本")],
    provider: Annotated[str, typer.Option("--provider", help="auto/tikhub/monid/treg")] = "auto",
    param: Annotated[list[str] | None, typer.Option("--param", "-p", help="name=value，可重复")] = None,
    params_file: Annotated[Path | None, typer.Option("--params-file", help="查询参数 JSON 对象文件")] = None,
    body: Annotated[
        Path | None, typer.Option("--body", help="POST JSON 请求体文件；按契约提供对象、数组或字符串")
    ] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="离线选版本、校验请求，不调用接口")] = False,
    max_cost: Annotated[float | None, typer.Option("--max-cost", help="本次查询费用上限 USD，可选附加上限")] = None,
    output: Annotated[Path | None, typer.Option("--output", help="保存脱敏 JSON 到新文件")] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    run_settings(
        json_output,
        call_endpoint,
        endpoint,
        params=param,
        params_file=params_file,
        body_file=body,
        dry_run=dry_run,
        max_cost=max_cost,
        provider_name=provider,
        output=output,
    )
