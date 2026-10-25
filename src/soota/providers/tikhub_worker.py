"""Standalone SDK bridge. Runs in the managed venv, without importing Soota."""

from __future__ import annotations

import contextlib
import importlib.metadata
import json
import sys
from io import TextIOWrapper
from typing import cast

import httpx
from tikhub import TikHub


def probe() -> None:
    seen = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"code": 200, "data": {"probe": True}})

    with TikHub(api_key="offline-probe", max_retries=0, transport=httpx.MockTransport(respond)) as client:
        response = client._request("POST", "/probe", params={"page": 1}, json={"probe": True})
    assert isinstance(response, dict) and len(seen) == 1
    assert seen[0].url.params["page"] == "1" and json.loads(seen[0].content) == {"probe": True}
    print(importlib.metadata.version("tikhub"))


def main() -> None:
    request = json.load(sys.stdin)
    try:
        # SDK diagnostics must never contaminate the single JSON response.
        with (
            contextlib.redirect_stdout(sys.stderr),
            TikHub(
                api_key=request["api_key"],
                base_url=request["base_url"],
                timeout=request["timeout"],
                max_retries=0,
            ) as client,
        ):
            response = client._request(
                request["method"], request["path"], params=request["params"], json=request["json"]
            )
        result = {"ok": True, "response": response}
    except Exception as exc:
        result = {"ok": False, "error": type(exc).__name__, "status_code": getattr(exc, "status_code", None)}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    # -I ignores PYTHON* variables; the worker protocol is always UTF-8, even when invoked directly.
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        cast(TextIOWrapper, stream).reconfigure(encoding="utf-8")
    if sys.argv[1:] == ["--probe"]:
        probe()
    else:
        main()
