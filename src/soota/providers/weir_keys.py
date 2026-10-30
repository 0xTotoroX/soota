"""Private, ordered Weir credentials; never serialize keys into command results.

[INPUT]: Environment override, legacy key file and named private credentials.
[OUTPUT]: Ordered enabled keys, redacted inventory and key-bound free-gift confirmations.
[POS]: Credential storage only; HTTP failover belongs to providers.weir.
[PROTOCOL]: 变更时更新此头部，然后检查 AGENTS.md
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from soota.config import SootaSettings
from soota.errors import InputError, StorageError
from soota.storage.atomic import atomic_write
from soota.storage.locking import lock_file, unlock_file


class WeirKey(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,47}$")
    key: SecretStr
    enabled: bool = True

    @property
    def cache_key(self) -> str:
        return "weir-body-" + hashlib.sha256(self.key.get_secret_value().encode()).hexdigest()[:24]


class KeyFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = 1
    keys: list[WeirKey] = Field(default_factory=list)


def _valid_key(key: str) -> bool:
    return bool(key) and not any(c.isspace() for c in key)


def _path(settings: SootaSettings) -> Path:
    return settings.config_dir / "credentials" / "weir-keys.json"


def _cursor_path(settings: SootaSettings) -> Path:
    return settings.config_dir / "credentials" / "weir-cursor.json"


def _read(settings: SootaSettings) -> KeyFile:
    path = _path(settings)
    if not path.exists():
        return KeyFile()
    try:
        pool = KeyFile.model_validate_json(path.read_text(encoding="utf-8"))
        names = [k.name for k in pool.keys]
        if (
            pool.version != 1
            or len(names) != len(set(names))
            or any(n in {"default", "environment"} for n in names)
            or any(not _valid_key(k.key.get_secret_value()) for k in pool.keys)
        ):
            raise ValueError("invalid credentials")
        return pool
    except (OSError, ValueError, ValidationError) as exc:
        raise StorageError("Weir 备用凭据文件无效；未重置或显示凭据。") from exc


def load_keys(settings: SootaSettings) -> list[WeirKey]:
    override = os.environ.get("WEIR_API_KEY")
    if override is not None:
        if not _valid_key(override):
            raise InputError("WEIR_API_KEY 无效；未回退到文件凭据。")
        return [WeirKey(name="environment", key=SecretStr(override))]
    pool = _read(settings)
    legacy = settings.config_dir / "credentials" / "weir.key"
    candidates = []
    if legacy.is_file():
        key = legacy.read_text(encoding="utf-8").strip()
        if not _valid_key(key):
            raise StorageError("Weir 主用凭据文件无效。")
        candidates.append(WeirKey(name="default", key=SecretStr(key)))
    candidates.extend(k for k in pool.keys if k.enabled)
    seen = set()
    unique = []
    for credential in candidates:
        value = credential.key.get_secret_value()
        if value not in seen:
            unique.append(credential)
            seen.add(value)
    return unique


def _gifts_path(settings: SootaSettings) -> Path:
    return settings.config_dir / "credentials" / "weir-gifts.json"


def _read_gifts(settings: SootaSettings) -> dict[str, int]:
    path = _gifts_path(settings)
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("schema_version") != 1:
            raise ValueError("invalid version")
        gifts = value["credits_by_key_sha256"]
        if not isinstance(gifts, dict) or any(
            not isinstance(key, str) or re.fullmatch(r"[0-9a-f]{64}", key) is None
            or type(amount) is not int or amount < 0 for key, amount in gifts.items()
        ):
            raise ValueError("invalid allowance")
        return gifts
    except (OSError, ValueError, KeyError) as exc:
        raise StorageError("Weir 赠送额度确认文件无效；未重置或放宽免费规则。") from exc


def gift_credits(settings: SootaSettings, credential: WeirKey) -> int:
    fingerprint = hashlib.sha256(credential.key.get_secret_value().encode()).hexdigest()
    return _read_gifts(settings).get(fingerprint, 0)


def confirm_gift(settings: SootaSettings, name: str, credits: int | None = None) -> dict:
    """Record a user's explicit gift declaration, bound to the current secret's hash."""
    credential = next((key for key in load_keys(settings) if key.name == name), None)
    if credential is None:
        raise InputError("赠送额度确认须指定当前启用的 Key 名称。")
    if credits is not None:
        if type(credits) is not int or credits < 0:
            raise InputError("赠送额度必须是非负整数；0 撤销确认。")
        with _locked(settings):
            gifts = _read_gifts(settings)
            fingerprint = hashlib.sha256(credential.key.get_secret_value().encode()).hexdigest()
            if credits:
                gifts[fingerprint] = credits
            else:
                gifts.pop(fingerprint, None)
            path = _gifts_path(settings)
            atomic_write(path, json.dumps({"schema_version": 1, "credits_by_key_sha256": gifts}) + "\n")
            path.chmod(0o600)
    return {"key_name": name, "confirmed_gift_credits": gift_credits(settings, credential),
            "scope": "当前 Key；仅允许已确认的赠送余额共存，消费仍限月度免费余额。"}


def next_start_index(settings: SootaSettings, count: int, *, advance_from: int | None = None) -> int:
    """Persist and return the next round-robin start under the pool lock."""
    if count <= 1:
        return 0
    with _locked(settings):
        if advance_from is None:
            try:
                value = json.loads(_cursor_path(settings).read_text(encoding="utf-8"))
                start = value["next_index"] if isinstance(value, dict) else 0
            except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
                start = 0
            start = start if isinstance(start, int) and not isinstance(start, bool) and start >= 0 else 0
        else:
            start = advance_from
        atomic_write(_cursor_path(settings), json.dumps({"next_index": (start + 1) % count}) + "\n")
        _cursor_path(settings).chmod(0o600)
        return start % count


@contextmanager
def _locked(settings: SootaSettings) -> Iterator[None]:
    root = _path(settings).parent
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / "weir-keys.lock").open("a") as handle:
        lock_file(handle)
        try:
            yield
        finally:
            unlock_file(handle)


def save_key(settings: SootaSettings, name: str, key: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,47}", name) or name in {"default", "environment"}:
        raise InputError("备用 Key 名称需为 1–48 位字母、数字、下划线或连字符；default/environment 为保留名称。")
    if not _valid_key(key):
        raise InputError("API Key 不能为空或包含空白。")
    with _locked(settings):
        pool = _read(settings)
        for item in pool.keys:
            if item.name == name:
                item.key = SecretStr(key)
                break
        else:
            pool.keys.append(WeirKey(name=name, key=SecretStr(key)))
        _save(settings, pool)


def _save(settings: SootaSettings, pool: KeyFile) -> None:
    payload = {
        "version": 1,
        "keys": [dict(name=k.name, key=k.key.get_secret_value(), enabled=k.enabled) for k in pool.keys],
    }
    atomic_write(_path(settings), json.dumps(payload, indent=2) + "\n")
    _path(settings).chmod(0o600)


def inventory(settings: SootaSettings, *, enable: str | None = None, disable: str | None = None) -> dict[str, object]:
    if enable and disable:
        raise InputError("一次只能启用或禁用一个备用 Key。")
    with _locked(settings):
        pool = _read(settings)
        name = enable or disable
        if name:
            item = next((k for k in pool.keys if k.name == name), None)
            if item is None:
                raise InputError("没有该备用 Key；主用 Key 沿用 providers auth weir。")
            item.enabled = enable is not None
            _save(settings, pool)
        effective = load_keys(settings)
        return {
            "environment_override": "WEIR_API_KEY" in os.environ,
            "effective_order": [k.name for k in effective],
            "backups": [dict(name=k.name, enabled=k.enabled) for k in pool.keys],
            "rotation": "round_robin",
            "failover_on": [401],
        }
