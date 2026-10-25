"""Windows Job Object owns a gated command and its descendants.

[INPUT]: 无 shell argv；标准库 ctypes 的 Windows Job Object API。
[OUTPUT]: 加入 Job 后才允许派发的进程；异常退出关闭整棵进程树。
[POS]: process.py 的 Windows 生命周期适配，独立于 LoreLab。
[PROTOCOL]: 变更时更新此头部，然后检查 AGENTS.md
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, cast

from soota.errors import SourceError

# The isolated helper performs no user command until the parent has assigned its Job.
# os.read avoids buffering any of the command's input together with the one-byte gate.
GATE = (
    "import json,os,subprocess,sys; "
    "token=os.read(0,1); "
    "sys.exit(subprocess.call(json.loads(sys.argv[1])) if token==b'\\0' else 125)"
)


class WindowsJob:
    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        class BasicLimits(ctypes.Structure):
            _fields_ = [
                ("ProcessTime", ctypes.c_int64),
                ("JobTime", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimits),
                ("IoInfo", ctypes.c_uint64 * 6),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        self.ctypes: Any = ctypes  # Windows-only members are absent from non-Windows type stubs.
        self.limits = ExtendedLimits()
        self.kernel = self.ctypes.WinDLL("kernel32", use_last_error=True)
        for name, args, result in (
            ("CreateJobObjectW", [ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
            (
                "SetInformationJobObject",
                [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD],
                wintypes.BOOL,
            ),
            ("AssignProcessToJobObject", [wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
            ("CloseHandle", [wintypes.HANDLE], wintypes.BOOL),
        ):
            fn = getattr(self.kernel, name)
            fn.argtypes, fn.restype = args, result
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise self.ctypes.WinError(self.ctypes.get_last_error())
        try:
            self._set_limits(0x2000)  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        except BaseException:
            self.kernel.CloseHandle(self.handle)
            self.handle = None
            raise

    def _set_limits(self, flags: int) -> None:
        self.limits.BasicLimitInformation.LimitFlags = flags
        if not self.kernel.SetInformationJobObject(
            self.handle, 9, self.ctypes.byref(self.limits), self.ctypes.sizeof(self.limits)
        ):
            raise self.ctypes.WinError(self.ctypes.get_last_error())

    def assign(self, process: subprocess.Popen[bytes]) -> None:
        if not self.kernel.AssignProcessToJobObject(self.handle, int(cast(Any, process)._handle)):
            raise self.ctypes.WinError(self.ctypes.get_last_error())

    def close(self, *, success: bool = False) -> None:
        if self.handle is not None:
            try:
                if success:
                    # A successful daemon-start command may deliberately leave its daemon running.
                    self._set_limits(0)
            finally:
                self.kernel.CloseHandle(self.handle)
                self.handle = None


@contextmanager
def windows_process(
    argv: list[str], env: dict[str, str], *, stderr: int = subprocess.PIPE
) -> Iterator[subprocess.Popen[bytes]]:
    try:
        job = WindowsJob()
    except OSError as exc:
        raise SourceError("无法建立 Windows 进程树保护，未派发命令。", details={"dispatched": False}) from exc
    process = None
    success = False
    yielded = False
    try:
        process = subprocess.Popen(
            [sys.executable, "-I", "-S", "-X", "utf8", "-c", GATE, json.dumps(argv)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr,
            env=env,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
        try:
            job.assign(process)
        except OSError as exc:
            raise SourceError("无法绑定 Windows 进程树保护，未派发命令。", details={"dispatched": False}) from exc
        assert process.stdin is not None
        process.stdin.write(b"\0")
        process.stdin.flush()
        yielded = True
        yield process
        success = process.poll() == 0
    finally:
        try:
            job.close(success=success)
        finally:
            if process is not None:
                if process.poll() is None:
                    process.kill()  # Also handles assignment failure: the gate never opened.
                process.wait(timeout=5)
                if not yielded:
                    for pipe in (process.stdin, process.stdout, process.stderr):
                        if pipe is not None:
                            pipe.close()
