"""[INPUT]: 旧知识命令、语义参数、交付目标与当前安装环境。
[OUTPUT]: 转交 LoreLab 的结果；不实现分析、交付或改写任务。
[POS]: Soota 的旧命令兼容边界；普通采集不依赖 LoreLab。
[PROTOCOL]: 变更时更新此头部，然后检查 AGENTS.md
"""

import importlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Literal, overload

from soota.config import SootaSettings
from soota.models import CommandResult
from soota.platform_support import utf8_environment

SEMANTIC_OUTPUTS = {"analysis", "note", "article", "outline"}


def ingest_needs_workflow(
    want: str | None, optional: str | None = None, destination: Path | None = None, vault: Path | None = None
) -> bool:
    names = {name.strip() for value in (want, optional) for name in (value or "").split(",")}
    return bool(names & SEMANTIC_OUTPUTS or destination is not None or vault is not None)


def needs_workflow(request: dict[str, Any]) -> bool:
    """识别旧请求；只负责路由，不执行分析或导出。"""
    targets = request.get("outputs", [])
    targets = targets if isinstance(targets, list) else []
    return bool(
        request.get("owner") == "lorelab"
        or any(isinstance(row, dict) and row.get("kind") in SEMANTIC_OUTPUTS for row in targets)
        or request.get("destination") is not None
        or request.get("vault") is not None
    )


def job_needs_workflow(settings: SootaSettings, job_id: str) -> bool:
    from soota.storage.jobs import JobStore
    from soota.tasks.state import task_folder

    record = JobStore(settings.jobs_dir).get(job_id)
    if record.get("kind") != "content-task":
        return False
    path = task_folder(settings, job_id) / "request.json"
    try:
        request = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return False  # 由原任务加载器报告损坏；不创建或改写任务。
    return isinstance(request, dict) and needs_workflow(request)


@overload
def invoke(action: Literal["write_legacy"], *args: Any, **kwargs: Any) -> Path: ...


@overload
def invoke(action: Literal["ingest", "execute", "resume", "render"], *args: Any, **kwargs: Any) -> CommandResult: ...


def invoke(action: str, *args: Any, **kwargs: Any) -> Any:
    """旧 Python/CLI 接口仅转交同一环境中的 LoreLab。"""
    from soota.errors import InputError

    if importlib.util.find_spec("lorelab") is None:
        raise InputError(
            "分析、笔记、文章与交付已迁至 LoreLab；原任务与文件保持不变。",
            hint="安装 LoreLab 后使用 lorelab content 继续；单纯采集仍可独立使用 Soota。",
        )
    api = importlib.import_module("lorelab.content.api")

    if action not in {"ingest", "execute", "resume", "render", "write_legacy"}:
        raise InputError("未知的旧内容流程入口。")
    return getattr(api, action)(*args, **kwargs)


def forward(args: list[str]) -> int:
    if not args or args[0] not in {"knowledge", "study"}:
        raise ValueError("仅兼容旧 knowledge/study 命令。")
    if importlib.util.find_spec("lorelab") is not None:
        command = [sys.executable, "-m", "lorelab.cli"]
    elif any(
        os.environ.get(key)
        for key in (
            "LORELAB_RUNTIME_FILE",
            "LOREHUB_RUNTIME_FILE",
            "SOOTA_KNOWLEDGE_RUNTIME_FILE",
            "LORELAB_ROOT",
            "LOREHUB_ROOT",
            "SOOTA_KNOWLEDGE_COMPONENTS_LOCK",
        )
    ):
        command = None  # 受管安装损坏时不能混用 PATH 上的另一份产品。
    else:
        executable = shutil.which("lorelab")
        command = [executable] if executable else None
    if command is None:
        error = "知识整理与学习流程已迁至 LoreLab。请安装或修复 LoreLab，再用 lorelab knowledge/study 继续原任务。"
        print(json.dumps({"ok": False, "error": error}, ensure_ascii=False), file=sys.stderr)
        return 1
    try:
        result = subprocess.run(
            command + args, capture_output=True, text=True, encoding="utf-8", env=utf8_environment(), check=False
        )
    except OSError as exc:
        print(json.dumps({"ok": False, "error": f"无法启动 LoreLab：{exc}"}, ensure_ascii=False), file=sys.stderr)
        return 1
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    return result.returncode
