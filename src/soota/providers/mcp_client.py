from __future__ import annotations

import json
import queue
import re
import shutil
import subprocess
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any

from soota.config import OpenCLIMCPConfig
from soota.errors import ContractError, InputError, SourceError
from soota.platform_support import WINDOWS, command_argv, utf8_environment
from soota.providers.mcp_contract import validate_read_command
from soota.providers.opencli_runtime import RuntimeUnavailable


def require_enabled(config: OpenCLIMCPConfig) -> None:
    if not config.enabled:
        raise InputError("MCP 接入已停用；请使用 OpenCLI，或按费用策略使用 TikHub。")


class MCPToolError(SourceError):
    def __init__(self, code: str) -> None:
        self.upstream_code = code
        super().__init__("opencli-mcp 命令失败，未自动重试。", details={"upstream_code": code})


class StdioMCPClient:
    """A bounded, one-call MCP connection owned by this Soota invocation."""

    def __init__(self, config: OpenCLIMCPConfig) -> None:
        self.config = config

    @staticmethod
    def available(config: OpenCLIMCPConfig) -> bool:
        return config.enabled and bool(shutil.which(config.binary or config.runtime_binary or "opencli-mcp"))

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name != "sites_search":
            raise ContractError("内部客户端仅开放站点发现；站点执行必须经过只读契约检查。")
        return self._call(name, arguments)

    def call_site_readonly(
        self,
        site: str,
        command: str,
        args: dict[str, Any],
        *,
        expected_domain: str | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        self._assert_no_override(site, command)
        return self._call(
            "site_run",
            {"site": site, "command": command, "args": args},
            readonly_guard=(site, command, args, expected_domain),
            dry_run=dry_run,
        )

    @staticmethod
    def _assert_no_override(site: str, command: str) -> None:
        if not all(re.fullmatch(r"[a-zA-Z0-9_][a-zA-Z0-9_-]*", part) for part in (site, command)):
            raise ContractError("opencli-mcp 站点或命令名称无效。")
        # The Chrome-owned host loads this directory after built-ins, so a same-name user adapter wins.
        override = Path.home() / ".opencli-mcp" / "adapters" / site / f"{command}.js"
        if override.exists() or override.is_symlink():
            raise ContractError("opencli-mcp 内置只读命令被本地 adapter 覆盖；已拒绝自动调用。")

    @staticmethod
    def _assert_read_contract(
        payload: dict[str, Any], site: str, command: str, args: dict[str, Any], expected_domain: str | None
    ) -> None:
        validate_read_command(payload, site, command, args, expected_domain)

    def _call(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        readonly_guard: tuple[str, str, dict[str, Any], str | None] | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        require_enabled(self.config)
        binary = self.config.binary or self.config.runtime_binary or "opencli-mcp"
        resolved = shutil.which(binary)
        if resolved is None:
            raise RuntimeUnavailable("opencli-mcp 未安装。", hint="安装并运行 opencli-mcp setup --clients none。")
        with self._process(command_argv([resolved, "stdio"])) as process:
            return self._exchange(process, name, arguments, readonly_guard=readonly_guard, dry_run=dry_run)

    def _exchange(
        self,
        process: subprocess.Popen[bytes],
        name: str,
        arguments: dict[str, Any],
        *,
        readonly_guard: tuple[str, str, dict[str, Any], str | None] | None,
        dry_run: bool,
    ) -> dict[str, Any]:
        assert process.stdin is not None and process.stdout is not None
        messages: queue.Queue[bytes] = queue.Queue()
        limit = self.config.max_output_bytes

        def read_stdout() -> None:
            assert process.stdout is not None
            while line := process.stdout.readline(limit + 1):
                messages.put(line)
                if len(line) > limit or not line.endswith(b"\n"):
                    break
            messages.put(b"")

        threading.Thread(target=read_stdout, daemon=True).start()
        try:
            self._send(
                process,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "soota", "version": "0.1.0"},
                    },
                },
            )
            initialized = self._response(messages, 1, min(self.config.timeout_seconds, 15))
            handshake = initialized.get("result")
            if not isinstance(handshake, dict):
                raise ContractError("opencli-mcp 初始化契约不匹配。")
            server = handshake.get("serverInfo")
            if not isinstance(server, dict) or server.get("name") != "opencli-mcp":
                raise ContractError("连接的不是已核验的 opencli-mcp 服务。")
            if not isinstance(server.get("version"), str) or not server["version"]:
                raise ContractError("opencli-mcp 未提供有效的服务版本。")
            # Accept compatible releases; each operation is checked against its live read contract.
            self._send(process, {"jsonrpc": "2.0", "method": "notifications/initialized"})
            if readonly_guard is not None:
                site, command, args, expected_domain = readonly_guard
                self._send(
                    process,
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {
                            "name": "sites_search",
                            "arguments": {"query": f"{site} {command}", "limit": 100},
                        },
                    },
                )
                discovery = self._decode_tool(self._response(messages, 2, min(self.config.timeout_seconds, 15)))
                self._assert_read_contract(discovery, site, command, args, expected_domain)
                self._assert_no_override(site, command)
                if dry_run:
                    return {"ok": True, "site": site, "command": command, "args": args, "dispatched": False}
            call_id = 3 if readonly_guard is not None else 2
            self._send(
                process,
                {
                    "jsonrpc": "2.0",
                    "id": call_id,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": arguments},
                },
                dispatched=True,
            )
            response = self._response(messages, call_id, self.config.timeout_seconds, dispatched=True)
            return self._decode_tool(response)
        finally:
            with suppress(OSError):
                process.stdin.close()

    @staticmethod
    @contextmanager
    def _process(argv: list[str]) -> Iterator[subprocess.Popen[bytes]]:
        if WINDOWS:
            from soota.providers.windows_process import windows_process

            process = None
            try:
                with windows_process(argv, utf8_environment(), stderr=subprocess.DEVNULL) as process:
                    try:
                        yield process
                    finally:
                        assert process.stdin is not None
                        process.stdin.close()
                        # Let a completed stdio session handle EOF before closing its Job.
                        with suppress(subprocess.TimeoutExpired):
                            process.wait(timeout=3)
            finally:
                if process is not None:
                    assert process.stdin is not None and process.stdout is not None
                    process.stdin.close()
                    process.stdout.close()
            return
        try:
            process = subprocess.Popen(
                argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=utf8_environment()
            )
        except OSError as exc:
            raise RuntimeUnavailable("opencli-mcp 无法启动。") from exc
        assert process.stdin is not None and process.stdout is not None
        try:
            yield process
        finally:
            with suppress(OSError):
                process.stdin.close()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
            process.stdout.close()

    @staticmethod
    def _decode_tool(response: dict[str, Any]) -> dict[str, Any]:
        result = response.get("result")
        if not isinstance(result, dict):
            raise ContractError("opencli-mcp 工具结果契约不匹配。")
        content = result.get("content")
        if not isinstance(content, list) or len(content) != 1 or not isinstance(content[0], dict):
            raise ContractError("opencli-mcp 未返回单一文本结果。")
        block = content[0]
        if block.get("type") != "text" or not isinstance(block.get("text"), str):
            raise ContractError("opencli-mcp 文本结果契约不匹配。")
        try:
            payload = json.loads(block["text"])
        except ValueError as exc:
            raise ContractError("opencli-mcp 工具结果不是 JSON。") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("ok"), bool):
            raise ContractError("opencli-mcp 业务结果契约不匹配。")
        if result.get("isError") or not payload["ok"]:
            error = payload.get("error")
            code = error.get("code") if isinstance(error, dict) else None
            if not isinstance(code, str):
                raise ContractError("opencli-mcp 错误结果缺少代码。")
            if code in {"host_unavailable", "browser_unavailable", "extension_update_required"}:
                raise RuntimeUnavailable("opencli-mcp 浏览器宿主不可用。")
            raise MCPToolError(code)
        if payload.get("truncated"):
            raise ContractError("opencli-mcp 结果已截断，不能作为完整业务数据。")
        return payload

    @staticmethod
    def _send(process: subprocess.Popen[bytes], message: dict[str, Any], *, dispatched: bool = False) -> None:
        assert process.stdin is not None
        try:
            process.stdin.write(json.dumps(message, separators=(",", ":")).encode() + b"\n")
            process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            if dispatched:
                raise SourceError("opencli-mcp 发送请求时连接中断，结果未知；未自动重放或切换来源。") from exc
            raise RuntimeUnavailable("opencli-mcp 连接已断开。") from exc

    def _response(
        self, messages: queue.Queue[bytes], request_id: int, timeout: float, *, dispatched: bool = False
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            try:
                line = messages.get(timeout=max(0, deadline - time.monotonic()))
            except queue.Empty as exc:
                if dispatched:
                    raise SourceError("opencli-mcp 请求超时，结果未知；未自动重放或切换来源。") from exc
                raise RuntimeUnavailable("opencli-mcp 初始化超时。") from exc
            if not line:
                if dispatched:
                    raise SourceError("opencli-mcp 连接中断，结果未知；未自动重放或切换来源。")
                raise RuntimeUnavailable("opencli-mcp 初始化前已退出。")
            if len(line) > self.config.max_output_bytes or not line.endswith(b"\n"):
                raise ContractError("opencli-mcp 响应超过大小上限。")
            try:
                response = json.loads(line)
            except ValueError as exc:
                raise ContractError("opencli-mcp 协议响应不是 JSON。") from exc
            if not isinstance(response, dict) or response.get("jsonrpc") != "2.0":
                raise ContractError("opencli-mcp 协议响应无效。")
            if "id" not in response:
                continue  # Progress and other notifications do not answer this request.
            if response["id"] != request_id:
                raise ContractError("opencli-mcp 响应 ID 不匹配。")
            if "error" in response:
                raise ContractError("opencli-mcp 协议调用失败。")
            return response
