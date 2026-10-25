from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import typer

from soota.api import _credential_values, _protect
from soota.cli_output import run_settings
from soota.config import SootaSettings
from soota.errors import InputError
from soota.features import success
from soota.models import CommandResult
from soota.providers.mcp_client import StdioMCPClient, require_enabled
from soota.storage.atomic import atomic_write

app = typer.Typer(help="按需发现并执行上游 MCP 的只读站点命令，不建立批量映射。")


def discover(settings: SootaSettings, query: str, limit: int) -> CommandResult:
    require_enabled(settings.opencli_mcp)
    if not 1 <= limit <= 100:
        raise InputError("limit 必须是 1–100。")
    response = StdioMCPClient(settings.opencli_mcp).call("sites_search", {"query": query, "limit": limit})
    return success("已查询 MCP 当前站点命令；未执行站点业务请求。", {"response": _protect(response, [])})


def run_site(
    settings: SootaSettings, site: str, command: str, params_file: Path | None, output: Path | None, dry_run: bool
) -> CommandResult:
    require_enabled(settings.opencli_mcp)
    if output is not None and output.expanduser().exists() and not dry_run:
        raise InputError("输出文件已存在，请选择新路径。")
    params: dict[str, Any] = {}
    if params_file:
        try:
            params = json.loads(params_file.expanduser().read_text(encoding="utf-8"))
            if not isinstance(params, dict):
                raise ValueError
        except ValueError as exc:
            raise InputError("参数文件需要 JSON 对象。") from exc
    response = StdioMCPClient(settings.opencli_mcp).call_site_readonly(site, command, params, dry_run=dry_run)
    secrets = _credential_values(params)
    data = _protect({"provider": "opencli-mcp", "operation": f"{site}/{command}", "response": response}, secrets)
    if output is not None and not dry_run:
        target = output.expanduser().resolve()
        atomic_write(target, json.dumps(data.pop("response"), ensure_ascii=False, indent=2) + "\n")
        data["output"] = str(target)
    return success("参数契约已核对，未执行业务命令。" if dry_run else "已收到 MCP 原生结果。", data)


@app.command("search")
def search(
    query: Annotated[str, typer.Argument()] = "",
    limit: Annotated[int, typer.Option("--limit")] = 20,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    run_settings(json_output, discover, query, limit)


@app.command("run")
def run(
    site: Annotated[str, typer.Argument()],
    command: Annotated[str, typer.Argument()],
    params_file: Annotated[Path | None, typer.Option("--params-file")] = None,
    output: Annotated[Path | None, typer.Option("--output")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    run_settings(json_output, run_site, site, command, params_file, output, dry_run)
