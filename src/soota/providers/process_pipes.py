"""Bounded pipe pumps for platforms whose selectors cannot monitor subprocess pipes.

[INPUT]: Job 管理的进程、stdin 字节、时间和合计输出上限。
[OUTPUT]: 并发管道结果或明确超限／超时；调用方清理进程树后回收线程。
[POS]: process.py 的 Windows 管道读写适配。
[PROTOCOL]: 变更时更新此头部，然后检查 AGENTS.md
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from collections.abc import Callable

from soota.errors import SourceError


def collect_pipes(
    process: subprocess.Popen[bytes], *, timeout: float, max_bytes: int, input_bytes: bytes | None
) -> tuple[Callable[[], dict[str, bytearray]], list[threading.Thread]]:
    """The caller owns tree termination, then joins the returned pumps before closing pipes."""
    assert process.stdin is not None and process.stdout is not None and process.stderr is not None
    output = {"stdout": bytearray(), "stderr": bytearray()}
    mutex, changed = threading.Lock(), threading.Event()
    errors: list[BaseException] = []
    complete: set[str] = set()
    stopped = threading.Event()

    def finish(name: str) -> None:
        with mutex:
            complete.add(name)
        changed.set()

    def read(name: str) -> None:
        pipe = getattr(process, name)
        try:
            while not stopped.is_set():
                chunk = os.read(pipe.fileno(), 65536)
                if not chunk:
                    break
                with mutex:
                    if sum(map(len, output.values())) + len(chunk) > max_bytes:
                        errors.append(
                            SourceError("外部命令输出超过上限，已终止本次调用。", details={"reason": "output_limit"})
                        )
                        break
                    output[name].extend(chunk)
        except OSError as exc:
            with mutex:
                errors.append(exc)
        finally:
            finish(name)

    def write() -> None:
        assert process.stdin is not None
        try:
            data = memoryview(input_bytes or b"")
            while data and not stopped.is_set():
                count = os.write(process.stdin.fileno(), data[:65536])
                data = data[count:]
        except BrokenPipeError:
            pass
        except OSError as exc:
            with mutex:
                errors.append(exc)
        finally:
            try:
                process.stdin.close()
            finally:
                finish("stdin")

    threads = [threading.Thread(target=read, args=(name,), daemon=True) for name in ("stdout", "stderr")] + [
        threading.Thread(target=write, daemon=True)
    ]
    for thread in threads:
        thread.start()

    def wait() -> dict[str, bytearray]:
        deadline = time.monotonic() + timeout
        try:
            while True:
                with mutex:
                    if errors:
                        raise errors[0]
                    if len(complete) == 3 and process.poll() is not None:
                        return output
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise SourceError("外部命令超时，已终止本次调用。", details={"reason": "timeout"})
                changed.wait(min(remaining, 0.05))
                changed.clear()
        finally:
            stopped.set()

    return wait, threads
