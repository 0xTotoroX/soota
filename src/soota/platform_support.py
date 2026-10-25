"""[INPUT]: 当前平台、独立 CLI 路径及 npm 包元数据。
[OUTPUT]: 无 shell 的 argv、平台入口路径和 UTF-8 进程环境。
[POS]: 独立组件内部的平台适配，不依赖 LoreLab 安装器。
[PROTOCOL]: 变更时更新此头部，然后检查 AGENTS.md
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

from soota.errors import SourceError

WINDOWS = sys.platform == "win32"


def venv_python(root: Path) -> Path:
    return root / ("Scripts/python.exe" if WINDOWS else "bin/python")


def npm_entry(root: Path, name: str) -> Path:
    return root / "node_modules" / ".bin" / (name + ".cmd" if WINDOWS else name)


def utf8_environment() -> dict[str, str]:
    return {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}


def configure_stdio() -> None:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")


def command_argv(argv: list[str]) -> list[str]:
    """Resolve Windows npm shims to node + package bin; never pass data through cmd.exe."""
    if not WINDOWS:
        return argv
    executable = Path(shutil.which(argv[0]) or argv[0])
    suffix = executable.suffix.lower()
    if suffix not in {".cmd", ".bat", ".js", ".mjs", ".cjs"}:
        return [str(executable), *argv[1:]]
    node = executable.parent / "node.exe"
    node_binary = str(node) if node.is_file() else shutil.which("node")
    if not node_binary:
        raise SourceError("该运行时需要 Node.js；未执行命令。")
    if suffix in {".js", ".mjs", ".cjs"}:
        return [node_binary, str(executable), *argv[1:]]
    packages = {"npm": "npm", "opencli": "@jackwener/opencli", "opencli-mcp": "opencli-mcp"}
    name = executable.stem.lower()
    package = packages.get(name)
    if package is None:
        raise SourceError("不支持自定义 Windows 批处理入口；请指定 .exe 或 Node.js 脚本。")
    modules = executable.parent.parent if executable.parent.name == ".bin" else executable.parent / "node_modules"
    root = modules / package
    try:
        metadata = json.loads((root / "package.json").read_text(encoding="utf-8"))
        entry = metadata["bin"]
        entry = entry.get(name) if isinstance(entry, dict) else entry
        if metadata.get("name") != package or not isinstance(entry, str):
            raise ValueError("invalid package bin")
        target = (root / entry).resolve()
        if not target.is_relative_to(root.resolve()) or not target.is_file():
            raise ValueError("missing package bin")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise SourceError("无法定位 Windows npm 包入口；请修复该运行时。") from exc
    return [node_binary, str(target), *argv[1:]]
