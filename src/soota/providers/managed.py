"""Isolated provider packages; installing MCP never enables its browser connection."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Any

from soota.config import SootaSettings
from soota.errors import ContractError, SourceError
from soota.platform_support import venv_python
from soota.providers.opencli_runtime import OpenCLIRuntime, package_metadata, validate_version
from soota.providers.process import run_process
from soota.storage.atomic import atomic_write

WORKER = Path(__file__).with_name("tikhub_worker.py")


class MCPRuntime(OpenCLIRuntime):
    command = "opencli-mcp"
    package = "opencli-mcp"

    @property
    def configured_binary(self) -> str | None:
        return self.settings.opencli_mcp.binary

    def metadata(self, version: str) -> dict[str, Any]:
        return package_metadata(version, package=self.package, registry="https://registry.npmjs.org/opencli-mcp")

    def probe(self, binary: str) -> str:
        result = run_process([binary, "--version"], timeout=20, max_bytes=65536)
        help_result = run_process([binary, "--help"], timeout=20, max_bytes=65536)
        if result.returncode or help_result.returncode or "stdio" not in help_result.stdout:
            raise ContractError("opencli-mcp 启动入口不兼容；当前版本未切换。")
        validate_version(result.stdout.strip())
        return result.stdout.strip()


class TikHubRuntime(OpenCLIRuntime):
    command = "tikhub"
    package = "tikhub"

    @property
    def configured_binary(self) -> None:
        return None

    def managed_binary(self, version: str) -> Path:
        validate_version(version)
        return venv_python(self.root / "versions" / version)

    def probe(self, binary: str) -> str:
        result = run_process([binary, "-I", str(WORKER), "--probe"], timeout=30, max_bytes=65536)
        if result.returncode:
            raise ContractError("TikHub SDK 离线接口验证失败；当前版本未切换。")
        validate_version(result.stdout.strip())
        return result.stdout.strip()

    def update(self, version: str) -> dict[str, Any]:
        validate_version(version)
        uv = shutil.which("uv")
        if not uv:
            raise SourceError("TikHub 独立运行环境更新需要 uv；现有 SDK 继续保留。")
        with self.lock(exclusive=True):
            binary = self.managed_binary(version)
            destination = binary.parent.parent
            # Venv scripts contain absolute paths: build at its final location, activate only after validation.
            if not binary.exists():
                destination.parent.mkdir(parents=True, exist_ok=True)
                result = run_process(
                    [uv, "venv", "--python", sys.executable, str(destination)], timeout=120, max_bytes=1_048_576
                )
                if result.returncode:
                    raise SourceError("TikHub 独立环境创建失败；当前版本未切换。")
            if not (destination / "validated.json").exists():
                result = run_process(
                    [
                        uv,
                        "pip",
                        "install",
                        "--python",
                        str(binary),
                        "--default-index",
                        "https://pypi.org/simple",
                        "--only-binary",
                        ":all:",
                        f"tikhub=={version}",
                    ],
                    timeout=300,
                    max_bytes=4_194_304,
                )
                if result.returncode:
                    raise SourceError("TikHub SDK 安装失败；当前版本未切换。")
            if self.probe(str(binary)) != version:
                raise ContractError("TikHub SDK 安装版本不一致；当前版本未切换。")
            atomic_write(destination / "validated.json", json.dumps({"version": version, "probe": "offline"}))
            old = self.state()
            if old.get("active") != version:
                atomic_write(self.root / "state.json", json.dumps({"active": version, "previous": old.get("active")}))
        return {"version": version, "ownership": "managed", "available": True}


def bind_runtimes(settings: SootaSettings) -> None:
    for runtime, config, field in (
        (MCPRuntime(settings), settings.opencli_mcp, "runtime_binary"),
        (TikHubRuntime(settings), settings.tikhub, "runtime_python"),
    ):
        active = runtime.state().get("active")
        binary = runtime.managed_binary(active) if active else None
        setattr(config, field, str(binary) if binary and binary.is_file() else None)
