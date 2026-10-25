"""跨平台内核文件锁；保留共享、排他、等待和立即失败语义。

[INPUT]: 已打开的锁文件；Unix flock 或 Windows LockFileEx。
[OUTPUT]: lock_file / unlock_file，不创建无操作锁或替代预算事务。
[POS]: storage、任务、计费和 Provider 运行时共用的进程互斥边界。
[PROTOCOL]: 变更时更新此头部，然后检查 AGENTS.md
"""

from __future__ import annotations

import errno
import sys
from typing import IO, Any

WINDOWS = sys.platform == "win32"


def _windows_lock(fd: int, *, exclusive: bool, blocking: bool, unlock: bool = False) -> None:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class Overlapped(ctypes.Structure):
        _fields_ = [
            ("Internal", ctypes.c_size_t),
            ("InternalHigh", ctypes.c_size_t),
            ("Offset", wintypes.DWORD),
            ("OffsetHigh", wintypes.DWORD),
            ("hEvent", wintypes.HANDLE),
        ]

    winapi: Any = ctypes  # Windows-only APIs are absent from non-Windows type stubs.
    crt: Any = msvcrt
    kernel = winapi.WinDLL("kernel32", use_last_error=True)
    overlap = Overlapped()
    handle = crt.get_osfhandle(fd)
    args: tuple[Any, ...]
    if unlock:
        fn = kernel.UnlockFileEx
        fn.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(Overlapped)]
        args = (handle, 0, 1, 0, ctypes.byref(overlap))
    else:
        fn = kernel.LockFileEx
        fn.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(Overlapped),
        ]
        flags = (2 if exclusive else 0) | (0 if blocking else 1)
        args = (handle, flags, 0, 1, 0, ctypes.byref(overlap))
    fn.restype = wintypes.BOOL
    # LockFileEx permits a range beyond EOF; never write a placeholder over rate-limit state.
    if not fn(*args):
        error = winapi.get_last_error()
        if error == 33 and not unlock:  # ERROR_LOCK_VIOLATION
            raise BlockingIOError(errno.EAGAIN, "文件锁已占用。")
        raise winapi.WinError(error)


def lock_file(handle: IO[Any], *, exclusive: bool = True, blocking: bool = True) -> None:
    if WINDOWS:
        _windows_lock(handle.fileno(), exclusive=exclusive, blocking=blocking)
    else:
        import fcntl

        flags = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        fcntl.flock(handle, flags | (0 if blocking else fcntl.LOCK_NB))


def unlock_file(handle: IO[Any]) -> None:
    if WINDOWS:
        _windows_lock(handle.fileno(), exclusive=True, blocking=True, unlock=True)
    else:
        import fcntl

        fcntl.flock(handle, fcntl.LOCK_UN)
