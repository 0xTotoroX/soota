from __future__ import annotations

import math
import os
import selectors
import signal
import subprocess
import threading
import time
from dataclasses import dataclass

from soota.errors import SourceError
from soota.platform_support import WINDOWS, command_argv, utf8_environment
from soota.providers.process_pipes import collect_pipes


@dataclass
class ProcessResult:
    returncode: int
    stdout: str
    stderr: str


def run_process(argv: list[str], *, timeout: float, max_bytes: int, input_bytes: bytes | None = None) -> ProcessResult:
    """Bound both pipes while reading; kill the process group on timeout or cancellation."""
    if not math.isfinite(timeout) or timeout <= 0 or type(max_bytes) is not int or max_bytes <= 0:
        raise ValueError("timeout and max_bytes must be positive and finite")
    env = utf8_environment()
    env.pop("TIKHUB_API_KEY", None)
    argv = command_argv(argv)
    if WINDOWS:
        return _run_windows(argv, env, timeout=timeout, max_bytes=max_bytes, input_bytes=input_bytes)
    process = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        env=env,
    )
    output: dict[str, bytearray] = {"stdout": bytearray(), "stderr": bytearray()}
    deadline = time.monotonic() + timeout
    try:
        with selectors.DefaultSelector() as selector:
            remaining_input = memoryview(input_bytes or b"")
            if process.stdin is not None:
                os.set_blocking(process.stdin.fileno(), False)
                if remaining_input:
                    selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
                else:
                    process.stdin.close()
            for name, pipe in (("stdout", process.stdout), ("stderr", process.stderr)):
                assert pipe is not None
                selector.register(pipe, selectors.EVENT_READ, name)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise SourceError("外部命令超时，已终止本次调用。", details={"reason": "timeout"})
                for key, _ in selector.select(min(remaining, 0.2)):
                    if key.data == "stdin":
                        try:
                            written = os.write(key.fd, remaining_input[:65536])
                            remaining_input = remaining_input[written:]
                        except BlockingIOError:
                            continue
                        except BrokenPipeError:
                            remaining_input = memoryview(b"")
                        if not remaining_input:
                            selector.unregister(key.fileobj)
                            assert process.stdin is not None
                            process.stdin.close()
                        continue
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    output[key.data].extend(chunk)
                    if sum(map(len, output.values())) > max_bytes:
                        raise SourceError("外部命令输出超过上限，已终止本次调用。", details={"reason": "output_limit"})
            process.wait(timeout=max(0.01, deadline - time.monotonic()))
    except BaseException:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        raise
    finally:
        for pipe in (process.stdin, process.stdout, process.stderr):
            if pipe is not None:
                pipe.close()
    return ProcessResult(
        process.returncode, output["stdout"].decode("utf-8", "replace"), output["stderr"].decode("utf-8", "replace")
    )


def _run_windows(
    argv: list[str], env: dict[str, str], *, timeout: float, max_bytes: int, input_bytes: bytes | None
) -> ProcessResult:
    from soota.providers.windows_process import windows_process

    process = None
    threads: list[threading.Thread] = []
    try:
        with windows_process(argv, env) as process:
            wait, threads = collect_pipes(process, timeout=timeout, max_bytes=max_bytes, input_bytes=input_bytes)
            output = wait()
        return ProcessResult(
            process.returncode, output["stdout"].decode("utf-8", "replace"), output["stderr"].decode("utf-8", "replace")
        )
    finally:
        # The Job is closed first: blocked readers/writers wake when every child closes its pipes.
        for thread in threads:
            thread.join(timeout=5)
        if process is not None:
            for pipe in (process.stdin, process.stdout, process.stderr):
                if pipe is not None:
                    pipe.close()
