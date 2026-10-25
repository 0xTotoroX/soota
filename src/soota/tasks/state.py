"""Private task persistence and bundle exclusion.

[INPUT]: Task models, atomic storage, JobStore and resolved source identities.
[OUTPUT]: Request/input checkpoints, locked item merges and bundle file locks.
[POS]: tasks persistence; worker-local rows merge into one shared job snapshot.
[PROTOCOL]: 变更时更新此头部，然后检查 AGENTS.md
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from soota.config import SootaSettings
from soota.errors import InputError, SourceError
from soota.storage.atomic import atomic_write
from soota.storage.jobs import JobStore
from soota.storage.locking import lock_file, unlock_file
from soota.tasks.inputs import file_fingerprint, source_for
from soota.tasks.models import InputSpec, TaskItem, TaskJob, TaskSpec


def task_folder(settings: SootaSettings, job_id: str) -> Path:
    JobStore(settings.jobs_dir)._path(job_id)  # Validate before forming a path.
    return settings.data_dir / "tasks" / job_id


def create_job(settings: SootaSettings, spec: TaskSpec) -> TaskJob:
    store = JobStore(settings.jobs_dir)
    record = store.create({"kind": "content-task", "schema_version": 2, "status": "running", "spec_path": ""})
    folder = task_folder(settings, record["id"])
    folder.mkdir(parents=True, exist_ok=True)
    folder.chmod(0o700)
    path = folder / "request.json"
    # The resumable request may contain signed URLs. Keep it out of public
    # manifests/results, in the task's private 0700 directory and 0600 file.
    atomic_write(path, spec.model_dump_json(indent=2) + "\n")
    path.chmod(0o600)
    job = TaskJob(**{**record, "spec_path": str(path), "total_inputs": len(spec.inputs)})
    save_job(settings, job)
    return job


def load_spec(settings: SootaSettings, job_id: str) -> TaskSpec:
    path = task_folder(settings, job_id) / "request.json"
    try:
        return TaskSpec.model_validate_json(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise InputError("任务请求记录缺失或格式无效。") from exc


def save_spec(settings: SootaSettings, job_id: str, spec: TaskSpec) -> None:
    atomic_write(task_folder(settings, job_id) / "request.json", spec.model_dump_json(indent=2) + "\n")


def save_job(settings: SootaSettings, job: TaskJob, *, item: TaskItem | None = None) -> None:
    # Merge only the caller's row, then serialize and replace under one lock.
    # A detached snapshot prevents subsequent worker edits during serialization.
    with job._save_lock:
        if item is not None:
            index = next(i for i, row in enumerate(job.items) if row.item_id == item.item_id)
            job.items[index] = item.model_copy(deep=True)
        JobStore(settings.jobs_dir).save(job.model_dump(mode="json"))


def resolved_inputs(settings: SootaSettings, job: TaskJob) -> list[InputSpec]:
    path = task_folder(settings, job.id) / "inputs.json"
    try:
        return [InputSpec.model_validate(row) for row in json.loads(path.read_text(encoding="utf-8"))]
    except (ValueError, OSError) as exc:
        raise InputError("展开后的任务输入缺失或格式无效。") from exc


def set_inputs(settings: SootaSettings, job: TaskJob, items: list[InputSpec]) -> None:
    previous = {item.identity: item for item in job.items}
    unique: list[InputSpec] = []
    rows = []
    seen = set()
    for item in items:
        source = source_for(item)
        identity = source.cache_identity
        if identity in seen:
            continue
        seen.add(identity)
        index = len(unique)
        unique.append(item)
        fingerprint = file_fingerprint(Path(item.value)) if item.kind == "file" else None
        old = previous.get(identity)
        if old is not None and old.input_fingerprint == fingerprint:
            row = old.model_copy(update={"input_index": index})
        else:
            row = TaskItem(
                item_id="item-" + hashlib.sha256(identity.encode()).hexdigest()[:12],
                input_index=index,
                identity=identity,
                input_fingerprint=fingerprint,
            )
        rows.append(row)
    atomic_write(
        task_folder(settings, job.id) / "inputs.json",
        json.dumps([i.model_dump() for i in unique], ensure_ascii=False, indent=2) + "\n",
    )
    job.items, job.total_inputs, job.expanded = rows, len(unique), True
    save_job(settings, job)


@contextmanager
def _bundle_lock(folder: Path) -> Iterator[None]:
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / ".task.lock").open("a") as handle:
        try:
            lock_file(handle, blocking=False)
        except BlockingIOError as exc:
            raise SourceError("同一内容包正在另一个任务中处理。") from exc
        try:
            yield
        finally:
            unlock_file(handle)
