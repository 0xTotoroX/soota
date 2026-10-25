from __future__ import annotations

from typing import Any

from soota.errors import ContractError, SourceError
from soota.providers.managed import WORKER
from soota.providers.process import run_process


class ManagedClient:
    def __init__(self, python: str, *, api_key: str, base_url: str, timeout: float) -> None:
        self.python = python
        self.timeout = timeout
        self.options = {"api_key": api_key, "base_url": base_url, "timeout": timeout}

    def _request(self, method: str, path: str, *, params: dict[str, Any], json: Any) -> Any:
        import json as codec

        from soota.providers.tikhub.client import _map_tikhub_error

        payload = {**self.options, "method": method, "path": path, "params": params, "json": json}
        result = run_process(
            [self.python, "-I", "-X", "utf8", str(WORKER)],
            timeout=self.timeout + 15,
            max_bytes=32_000_000,
            input_bytes=codec.dumps(payload).encode(),
        )
        if result.returncode:
            raise SourceError("TikHub SDK 子进程失败；未自动重试。")
        try:
            value = codec.loads(result.stdout)
            if value["ok"]:
                return value["response"]
            name = value["error"]
            if not isinstance(name, str) or not name.startswith("TikHub"):
                raise SourceError("TikHub SDK 请求失败；未自动重试。")
            error = type(name, (Exception,), {})("TikHub 请求失败")
            error.status_code = value.get("status_code")
            raise _map_tikhub_error(error)
        except (ValueError, TypeError, KeyError) as exc:
            raise ContractError("TikHub SDK 返回格式无效；未自动重试。") from exc
