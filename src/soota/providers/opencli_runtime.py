from __future__ import annotations

import base64
import hashlib
import json
import re
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx

from soota.config import SootaSettings
from soota.errors import ContractError, InputError, SourceError
from soota.platform_support import npm_entry
from soota.providers.opencli_patches import apply_managed_fixes
from soota.providers.process import run_process
from soota.storage.atomic import atomic_write
from soota.storage.locking import lock_file, unlock_file

PACKAGE = "@jackwener/opencli"
REGISTRY = "https://registry.npmjs.org/@jackwener%2fopencli"
REQUIRED_COMMANDS = {
    "twitter/tweets": {"id", "text", "url"},
    "bilibili/video": {"field", "value"},
    "youtube/video": {"field", "value"},
    "bilibili/subtitle": {"index", "from", "to", "content"},
    "youtube/transcript": set(),
}


class RuntimeUnavailable(SourceError):
    code = "provider_unavailable"


class OpenCLIRuntime:
    command = "opencli"
    package = PACKAGE

    @property
    def configured_binary(self) -> str | None:
        return self.settings.opencli.binary

    def metadata(self, version: str) -> dict[str, Any]:
        return package_metadata(version)

    def __init__(self, settings: SootaSettings) -> None:
        self.settings = settings
        self.root = settings.data_dir / "runtimes" / self.command

    @contextmanager
    def lock(self, *, exclusive: bool = False) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / ".lock").open("a") as handle:
            try:
                lock_file(handle, exclusive=exclusive, blocking=False)
            except BlockingIOError as exc:
                raise SourceError("OpenCLI 正在执行或升级，请等本次操作结束后再试。") from exc
            try:
                yield
            finally:
                unlock_file(handle)

    def state(self) -> dict[str, Any]:
        path = self.root / "state.json"
        if not path.is_file():
            return {}
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(state, dict):
                raise ValueError
            for key in ("active", "previous"):
                if state.get(key) is not None:
                    validate_version(state[key])
            return state
        except (ValueError, InputError) as exc:
            raise ContractError("OpenCLI 运行时状态文件无效。") from exc

    def managed_binary(self, version: str) -> Path:
        validate_version(version)
        return npm_entry(self.root / "versions" / version, self.command)

    def resolve(self) -> tuple[str, str]:
        explicit = self.configured_binary
        active = self.state().get("active") if explicit is None else None
        candidate = explicit or (str(self.managed_binary(active)) if active else self.command)
        binary = shutil.which(candidate)
        if binary is None:
            raise RuntimeUnavailable(
                "未找到 OpenCLI。", hint="安装 OpenCLI 或用 soota runtime update --version 版本号。"
            )
        return str(Path(binary).absolute()), "configured" if explicit else ("managed" if active else "external")

    def status(self) -> dict[str, Any]:
        try:
            binary, owner = self.resolve()
        except RuntimeUnavailable:
            return {"available": False, "ownership": "missing", "browser_checked": False}
        with self.lock():
            result = run_process([binary, "--version"], timeout=15, max_bytes=65536)
        return {
            "available": result.returncode == 0,
            "binary": binary,
            "ownership": owner,
            "version": result.stdout.strip(),
            "profile": self.settings.opencli.profile,
            "browser_checked": False,
            "managed_state": self.state(),
        }

    def probe(self, binary: str) -> str:
        version = run_process([binary, "--version"], timeout=20, max_bytes=65536)
        if version.returncode:
            raise ContractError("OpenCLI 版本检查失败。")
        validate_version(version.stdout.strip())
        result = run_process([binary, "list", "-f", "json"], timeout=30, max_bytes=4_194_304)
        try:
            rows = json.loads(result.stdout)
            commands = {row["command"]: row for row in rows}
            for name, columns in REQUIRED_COMMANDS.items():
                row = commands[name]
                if row["access"] != "read" or not columns.issubset(row.get("columns") or []):
                    raise ValueError(name)
            for command, required in {
                "bilibili/subtitle": {"bvid", "lang"},
                "youtube/transcript": {"url", "lang", "mode"},
            }.items():
                if not required.issubset({arg["name"] for arg in commands[command]["args"]}):
                    raise ValueError(command)
            args = {arg["name"] for arg in commands["twitter/tweets"]["args"]}
            if not {"username", "limit"}.issubset(args) or result.returncode:
                raise ValueError("twitter/tweets args")
        except (ValueError, TypeError, KeyError) as exc:
            raise ContractError("OpenCLI 的只读命令或 JSON 契约不兼容，未切换运行时。") from exc
        return version.stdout.strip()

    def update(self, version: str) -> dict[str, Any]:
        validate_version(version)
        if self.configured_binary:
            raise InputError("已显式指定外部 binary；请先移除该配置再启用 Soota 管理的运行时。")
        npm = shutil.which("npm")
        if not npm:
            raise RuntimeUnavailable("升级需要 npm 和 Node.js >= 20.18.1。")
        with self.lock(exclusive=True):
            destination = self.managed_binary(version).parents[2]
            if destination.exists():
                apply_managed_fixes(destination)
                actual = self.probe(str(self.managed_binary(version)))
            else:
                metadata = self.metadata(version)
                self.root.mkdir(parents=True, exist_ok=True)
                with tempfile.TemporaryDirectory(prefix="stage-", dir=self.root) as folder:
                    stage = Path(folder)
                    tarball = stage / "opencli.tgz"
                    download_verified(metadata, tarball)
                    result = run_process(
                        [
                            npm,
                            "install",
                            "--prefix",
                            str(stage / "install"),
                            "--ignore-scripts",
                            "--no-audit",
                            "--no-fund",
                            "--omit=dev",
                            str(tarball),
                        ],
                        timeout=300,
                        max_bytes=4_194_304,
                    )
                    if result.returncode:
                        raise SourceError(
                            "OpenCLI 隔离安装失败；当前版本未切换。", details={"exit_code": result.returncode}
                        )
                    apply_managed_fixes(stage / "install")
                    actual = self.probe(str(npm_entry(stage / "install", self.command)))
                    if actual != version:
                        raise ContractError("安装后的 OpenCLI 版本与请求版本不同。")
                    atomic_write(
                        stage / "install" / "provenance.json",
                        json.dumps(
                            {
                                "package": self.package,
                                "version": version,
                                "dist": metadata["dist"],
                                "validation": "read-only CLI contract; browser not checked",
                            },
                            indent=2,
                        )
                        + "\n",
                    )
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    (stage / "install").rename(destination)
            if actual != version:
                raise ContractError("已安装运行时版本与目录不符。")
            old = self.state()
            if old.get("active") != version:
                atomic_write(
                    self.root / "state.json", json.dumps({"active": version, "previous": old.get("active")}) + "\n"
                )
        return self.status()

    def rollback(self) -> dict[str, Any]:
        if self.configured_binary:
            raise InputError("显式配置的外部 binary 不由 Soota 升级或回滚。")
        with self.lock(exclusive=True):
            old = self.state()
            if "previous" not in old:
                raise InputError("没有可回滚的 OpenCLI 版本。")
            previous = old["previous"]
            if previous:
                self.probe(str(self.managed_binary(previous)))
            atomic_write(
                self.root / "state.json", json.dumps({"active": previous, "previous": old.get("active")}) + "\n"
            )
        return self.status()


def validate_version(version: str) -> None:
    if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?", version):
        raise InputError("请指定完整版本号，例如 1.8.3；不接受 latest 或版本范围。")


def package_metadata(version: str = "latest", *, package: str = PACKAGE, registry: str = REGISTRY) -> dict[str, Any]:
    try:
        response = httpx.get(f"{registry}/{version}", timeout=30)
        response.raise_for_status()
        value = response.json()
        if not isinstance(value, dict) or value.get("name") != package:
            raise ValueError
        return value
    except (httpx.HTTPError, ValueError) as exc:
        raise SourceError("无法读取官方 npm OpenCLI 版本信息。") from exc


def download_verified(metadata: dict[str, Any], path: Path) -> None:
    try:
        dist = metadata["dist"]
        url = dist["tarball"]
        package = metadata.get("name", PACKAGE)
        if package not in {PACKAGE, "opencli-mcp"} or not url.startswith(f"https://registry.npmjs.org/{package}/-/"):
            raise ValueError("unexpected registry host")
        integrity = dist["integrity"]
        algorithm, expected = integrity.split("-", 1)
        if algorithm != "sha512":
            raise ValueError("unsupported integrity")
        digest = hashlib.sha512()
        size = 0
        with httpx.stream("GET", url, timeout=60) as response, path.open("wb") as target:
            response.raise_for_status()
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > 32 * 1024 * 1024:
                    raise ValueError("tarball too large")
                digest.update(chunk)
                target.write(chunk)
        if base64.b64encode(digest.digest()).decode() != expected:
            raise ValueError("integrity mismatch")
    except (httpx.HTTPError, KeyError, ValueError, TypeError) as exc:
        raise ContractError("OpenCLI 包来源或 SHA-512 完整性校验失败，未安装。") from exc
