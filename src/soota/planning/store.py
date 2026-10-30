"""私有规划及预设存储。

[INPUT]: 带版本模型、用户配置/状态目录。
[OUTPUT]: 加锁草稿读写、命名预设的乐观并发更新。
[POS]: 使用现有原子写入与跨平台文件锁，不改变 providers.json。
[PROTOCOL]: 变更时更新此头部，然后检查 AGENTS.md
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from soota.config import SootaSettings
from soota.errors import InputError
from soota.planning.models import Plan, Preset, PresetFile
from soota.storage.atomic import atomic_write
from soota.storage.locking import lock_file, unlock_file


@contextmanager
def locked(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        path.chmod(0o600)
        lock_file(handle)
        try:
            yield
        finally:
            unlock_file(handle)


def plan_path(settings: SootaSettings, plan_id: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{32}", plan_id):
        raise InputError("无效的规划 ID。")
    return settings.data_dir / "plans" / plan_id / "plan.json"


@contextmanager
def plan_lock(settings: SootaSettings, plan_id: str) -> Iterator[None]:
    path = plan_path(settings, plan_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    with locked(path.with_suffix(".lock")):
        yield


def save_plan(settings: SootaSettings, plan: Plan) -> None:
    path = plan_path(settings, plan.id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    atomic_write(path, plan.model_dump_json(indent=2) + "\n")


def load_plan(settings: SootaSettings, plan_id: str) -> Plan:
    try:
        plan = Plan.model_validate_json(plan_path(settings, plan_id).read_text(encoding="utf-8"))
        if plan.id != plan_id:
            raise ValueError("identity mismatch")
        return plan
    except (OSError, ValueError) as exc:
        raise InputError("规划不存在或记录无效；请重新规划。") from exc


def presets(settings: SootaSettings) -> list[Preset]:
    path = settings.config_dir / "presets.json"
    if not path.exists():
        return []
    try:
        rows = PresetFile.model_validate_json(path.read_text(encoding="utf-8")).presets
        if len({row.name for row in rows}) != len(rows):
            raise ValueError("duplicate preset")
        return rows
    except (OSError, ValueError) as exc:
        raise InputError("presets.json 无效；未覆盖现有预设。") from exc


def save_preset(settings: SootaSettings, preset: Preset, *, expected_revision: int | None = None) -> Preset:
    with locked(settings.config_dir / "presets.lock"):
        rows = presets(settings)
        previous = next((row for row in rows if row.name == preset.name), None)
        if (previous.revision if previous else None) != expected_revision:
            raise InputError("同名预设已存在或已被修改；请重新规划，或使用新的 --save-as 名称。")
        preset = preset.model_copy(update={"revision": previous.revision + 1 if previous else 1})
        rows = [row for row in rows if row.name != preset.name] + [preset]
        atomic_write(settings.config_dir / "presets.json", PresetFile(presets=rows).model_dump_json(indent=2) + "\n")
        return preset


def delete_preset(settings: SootaSettings, name: str) -> None:
    with locked(settings.config_dir / "presets.lock"):
        rows = presets(settings)
        if not any(row.name == name for row in rows):
            raise InputError("预设不存在。")
        atomic_write(
            settings.config_dir / "presets.json",
            PresetFile(presets=[row for row in rows if row.name != name]).model_dump_json(indent=2) + "\n",
        )
