"""[INPUT]: 当前 jobs 配置、原任务 ID 或绝对记录路径。
[OUTPUT]: JobStore、绑定原根的恢复 settings；不重定位全局账本或凭据。
[POS]: 任务记录和执行锁；冻结请求目录由 tasks/state 读取 spec_path。
[PROTOCOL]: 变更时更新此头部，然后检查 AGENTS.md
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from soota.config import SootaSettings
from soota.errors import InputError, SourceError, StorageError
from soota.secrets import redact_any
from soota.storage.atomic import atomic_write
from soota.storage.locking import lock_file, unlock_file


def _job_path(root: Path, job_id: str) -> Path:
    if not re.fullmatch(r"\d{8}T\d{6}-[0-9a-f]{8}", job_id):
        raise InputError("任务 ID 格式无效。", hint="用 soota jobs list 查看任务 ID。")
    return root / f"{job_id}.json"


def bind_job(settings: SootaSettings, job_id: str) -> tuple[SootaSettings, str]:
    """将恢复绑定到原任务记录；不移动全局账本、凭据或冻结请求。"""
    candidate = Path(job_id).expanduser()
    if candidate.is_absolute():
        if candidate.name == "request.json" or candidate.is_dir():
            folder = candidate.parent if candidate.name == "request.json" else candidate
            candidates = [folder.parent / f"{folder.name}.json", folder.parent.parent / "jobs" / f"{folder.name}.json"]
            candidate = next((path for path in candidates if path.is_file()), candidates[0])
        if not candidate.is_file() or candidate.suffix != ".json":
            raise InputError("找不到绝对路径对应的任务记录。")
        job_id = candidate.stem
    else:
        candidate = _job_path(settings.jobs_dir, job_id)
        legacy = settings.data_dir / "jobs" / f"{job_id}.json"
        if not candidate.is_file() and legacy.is_file():
            candidate = legacy
    if not candidate.is_file():
        raise InputError("找不到该任务。", hint="使用原任务记录的绝对路径恢复。")
    record = JobStore(candidate.parent).get(job_id)
    old_root = settings.data_dir / "jobs"
    old_spec = settings.data_dir / "tasks" / job_id / "request.json"
    legacy_layout = candidate.parent == old_root and record.get("spec_path") in {None, "", str(old_spec)}
    storage = settings.storage.model_copy(update={"jobs_dir": None if legacy_layout else candidate.parent})
    bound = settings.model_copy(update={"storage": storage})
    return bound, job_id


class JobStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        job_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex[:8]
        record = {
            "id": job_id,
            "created_at": datetime.now(UTC).isoformat(),
            "status": payload.get("status", "completed"),
            **payload,
        }
        record["id"] = job_id
        self.save(record)
        return record

    def _path(self, job_id: str) -> Path:
        return _job_path(self.root, job_id)

    def get(self, job_id: str) -> dict[str, Any]:
        path = self._path(job_id)
        if not path.is_file():
            raise InputError("找不到该任务。", hint="用 soota jobs list 查看任务 ID。")
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise StorageError("无法读取任务记录。") from exc
        if not isinstance(record, dict) or record.get("id") != job_id:
            raise StorageError("任务记录与任务 ID 不一致。")
        return record

    def save(self, record: dict[str, Any], *, redact: bool = True) -> None:
        path = self._path(str(record["id"]))
        record["updated_at"] = datetime.now(UTC).isoformat()
        try:
            payload = redact_any(record) if redact else record
            atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n")
        except OSError as exc:
            raise StorageError("无法保存任务进度。") from exc

    @contextmanager
    def lock(self, job_id: str) -> Iterator[None]:
        # Kernel locks are released on exit, including an interrupted process.
        with self._path(job_id).with_suffix(".lock").open("a") as handle:
            try:
                lock_file(handle, blocking=False)
            except BlockingIOError as exc:
                raise SourceError("该任务正在另一个进程中运行。", hint="等待原进程结束后再续跑。") from exc
            try:
                yield
            finally:
                unlock_file(handle)

    def list_jobs(self, limit: int | None = 50) -> list[dict[str, Any]]:
        files = sorted(self.root.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        jobs: list[dict[str, Any]] = []
        for path in files[:limit]:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                jobs.append(data)
        return jobs
