"""
[INPUT]: Runtime capability registry, URL and explicit provider.
[OUTPUT]: Reviewed native operations, body and media results.
[POS]: Acquisition routing; Weir body fallback only before OpenCLI dispatch.
[PROTOCOL]: 变更时更新此头部，然后检查 AGENTS.md
"""

from __future__ import annotations

import math
import re
import tempfile
from pathlib import Path
from typing import Any

from soota.config import SootaSettings
from soota.content.native_bindings import (
    CONTENT_BINDINGS,
    content_candidates,
    media_binding,
    normalize_rows,
    validate_target_result,
)
from soota.content.native_types import NativeContent, NativeMedia, NativeOperation
from soota.errors import ContractError, InputError, SourceError
from soota.providers import opencli_commands

MEDIA_SUFFIXES = {
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".gif",
    ".avif",
    ".svg",
    ".bmp",
    ".tiff",
    ".heic",
    ".mp4",
    ".mkv",
    ".webm",
    ".mov",
    ".m4v",
    ".mp3",
    ".m4a",
    ".aac",
    ".wav",
    ".flac",
    ".ogg",
    ".opus",
}


def _operation(selector: str) -> str:
    operation = selector.removeprefix("opencli.").replace(".", "/")
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_-]*/[A-Za-z0-9_][A-Za-z0-9_-]*", operation):
        raise InputError("原生操作需要 OpenCLI 的 site/command 或 opencli.site.command 标识。")
    return operation


def _command(settings: SootaSettings, operation: str) -> dict[str, Any]:
    found = next((row for row in opencli_commands.commands(settings) if row["command"] == operation), None)
    if found is None:
        raise SourceError("当前 OpenCLI 运行时没有该命令。")
    if found["access"] != "read":
        raise InputError("内容获取只接受声明为 read 的命令。")
    return found


def _safe_file(path: Path, directory: Path) -> Path:
    resolved = path.resolve()
    if not resolved.is_relative_to(directory.resolve()) or path.is_symlink() or not resolved.is_file():
        raise ContractError("工具产物路径不在本次隔离输出目录内，或不是普通文件。")
    if resolved.stat().st_size == 0:
        raise ContractError("工具返回了空文件。")
    return resolved


def _files(directory: Path) -> list[Path]:
    paths = sorted(
        directory.rglob("*"),
        key=lambda p: re.sub(r"\d+", lambda match: match[0].zfill(20), p.relative_to(directory).as_posix()),
    )
    found: list[Path] = []
    for path in paths:
        if path.is_symlink():
            raise ContractError("工具输出包含符号链接，未纳入产物。")
        if path.is_file() and path.stat().st_size:
            found.append(_safe_file(path, directory))
    return found


def _dispatch(
    settings: SootaSettings, command: dict[str, Any], params: dict[str, Any], workdir: Path
) -> NativeOperation:
    # Each call gets a fresh directory: title collisions cannot overwrite another source.
    workdir = workdir.expanduser().resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="opencli-", dir=workdir))
    arguments = dict(params)
    names = {arg["name"] for arg in command["args"]}
    if "output" in names:
        arguments["output"] = str(directory)
    if "stdout" in names and arguments.get("stdout") not in (None, False, "false"):
        raise InputError("原生 JSON 通道不接受 stdout 文本模式。")
    if "download-images" in names and "download-images" not in arguments:
        arguments["download-images"] = False
    raw = opencli_commands.execute(settings, command, arguments, allow_write=False)
    validate_target_result(command["command"], arguments, raw)
    return NativeOperation(
        raw=raw,
        provider="opencli",
        operation=command["command"],
        workdir=directory,
        files=_files(directory),
        entries=normalize_rows(command["command"], raw),
    )


def capture_operation(
    settings: SootaSettings, selector: str, params: dict[str, Any], workdir: Path, *, max_cost: float | None = None
) -> NativeOperation:
    """Run an explicitly requested native read; no paid-provider fallback or semantic inference."""
    if max_cost is not None and (isinstance(max_cost, bool) or not math.isfinite(max_cost) or max_cost < 0):
        raise InputError("max_cost 必须是非负有限数。")
    operation = _operation(selector)
    return _dispatch(settings, _command(settings, operation), params, workdir)


def _single_row(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, list) or len(raw) != 1 or not isinstance(raw[0], dict):
        raise ContractError("正文结果需要单条对象记录。")
    return raw[0]


def _body(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractError("正文结果缺少非空文字。")
    return value


def convert_content(result: NativeOperation, source_url: str, *, result_schema: str | None = None) -> NativeContent:
    """Bind a reviewed output contract; provider acquisition stays independent of conversion."""
    schema = result_schema or CONTENT_BINDINGS.get(result.operation)
    if schema == "field-value-text":
        if not isinstance(result.raw, list):
            raise ContractError("正文结果需要 field/value 数组。")
        row: dict[str, Any] = {}
        for item in result.raw:
            if not isinstance(item, dict) or not isinstance(item.get("field"), str) or "value" not in item:
                raise ContractError("正文 field/value 契约无效。")
            if item["field"] in row:
                raise ContractError("正文 field/value 有重复字段。")
            row[item["field"]] = item["value"]
        body = _body(row.get("content"))
    elif schema in {"saved-markdown", "inline-markdown"}:
        row = _single_row(result.raw)
        if schema == "saved-markdown":
            if row.get("status") != "success":
                raise SourceError("上游未成功保存文章正文。")
            saved = row.get("saved")
            if not isinstance(saved, str) or not saved or saved == "-":
                raise ContractError("文章结果缺少 saved 文件路径。")
            path = Path(saved)
            if not path.is_absolute():
                path = result.workdir / path
            path = _safe_file(path, result.workdir)
            if path.suffix.lower() != ".md":
                raise ContractError("已保存正文不是 Markdown 文件。")
            try:
                body = _body(path.read_text(encoding="utf-8"))
            except UnicodeError as exc:
                raise ContractError("Markdown 正文不是 UTF-8。") from exc
        else:
            body = _body(row.get("content"))
    else:
        raise InputError("该命令尚无已核实的正文结果契约；原生数据仍可保存。")
    title = row.get("title")
    if not isinstance(title, str):
        raise ContractError("正文标题必须是字符串，允许空标题。")
    return NativeContent(
        raw=result.raw,
        body=body,
        title=title,
        source_url=source_url,
        provider=result.provider,
        scope="page" if result.operation == "web/read" else "item",
        operation=result.operation,
        attachments=[path for path in result.files if path.suffix.lower() in MEDIA_SUFFIXES],
        format="text" if schema == "field-value-text" else "markdown",
        warnings=list(result.warnings),
    )


def capture_content(
    settings: SootaSettings, url: str, workdir: Path, *, provider: str = "auto", allow_credits: bool = True
) -> NativeContent:
    from soota.billing import provider_enabled
    from soota.content.weir import capture as capture_weir
    from soota.content.weir import supports as weir_supports
    from soota.providers.opencli_runtime import RuntimeUnavailable

    if provider == "weir":
        return capture_weir(settings, url, allow_credits=allow_credits)
    if provider not in {"auto", "opencli"}:
        raise InputError("正文采集支持 auto/opencli/weir；其他 Provider 可走原生操作通道。")
    commands = {}
    if provider == "opencli" or provider_enabled(settings, "opencli"):
        try:
            commands = {row["command"]: row for row in opencli_commands.commands(settings)}
        except RuntimeUnavailable:
            if provider == "opencli":
                raise
    for operation, params in content_candidates(url):
        command = commands.get(operation)
        if command is not None and command["access"] == "read":
            # Capability fallback only; never retry a dispatched failure elsewhere.
            result = _dispatch(settings, command, params, workdir)
            return convert_content(result, url)
    if provider == "auto" and provider_enabled(settings, "weir") and weir_supports(url):
        return capture_weir(settings, url, allow_credits=allow_credits)
    raise SourceError("当前没有可用的正文命令；可显式启用并选择 Weir 获取 X/Reddit 正文。")


def capture_media(settings: SootaSettings, url: str, workdir: Path, *, provider: str = "auto") -> NativeMedia:
    if provider not in {"auto", "opencli"}:
        raise InputError("此原生媒体入口只支持 auto/opencli；其他来源由共享媒体下载器处理。")
    operation, params = media_binding(url)
    result = _dispatch(settings, _command(settings, operation), params, workdir)
    if not isinstance(result.raw, list) or not result.raw or any(not isinstance(row, dict) for row in result.raw):
        raise ContractError("媒体下载结果需要非空对象数组。")
    successes = sum(row.get("status") == "success" for row in result.raw)
    if not successes:
        raise SourceError("上游未成功下载媒体文件。")
    files = [path for path in result.files if path.suffix.lower() in MEDIA_SUFFIXES]
    if not files:
        raise ContractError("上游报告下载成功，但本次输出目录没有非空媒体文件。")
    warnings = ["文件存在尚不代表媒体可解码、时长完整或来源完整；交付前需要媒体验证。"]
    if successes != len(result.raw) or len(files) != successes:
        warnings.append("上游下载结果部分失败或文件数不匹配；保留现有文件，完整性未知。")
    return NativeMedia(
        raw=result.raw, files=files, source_url=url, provider=result.provider, operation=operation, warnings=warnings
    )
