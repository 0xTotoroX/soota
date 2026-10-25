from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest

from soota import platform_support
from soota.errors import SourceError
from soota.media import probe
from soota.providers import process, windows_process
from soota.storage import locking


def test_public_imports_without_unix_modules(tmp_path):
    script = """
import importlib.abc, sys
class NoUnix(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, *args):
        if fullname in {'fcntl', 'resource', 'termios'}:
            raise ImportError('not on Windows')
sys.meta_path.insert(0, NoUnix())
import soota.cli, soota.providers.downie
print('portable imports')
"""
    env = platform_support.utf8_environment()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src")
    result = subprocess.run([sys.executable, "-B", "-c", script], capture_output=True, env=env, timeout=15)
    assert result.returncode == 0, result.stderr.decode()
    assert b"portable imports" in result.stdout


def test_windows_venv_and_npm_paths(monkeypatch, tmp_path):
    monkeypatch.setattr(platform_support, "WINDOWS", True)
    assert platform_support.venv_python(tmp_path) == tmp_path / "Scripts" / "python.exe"
    assert platform_support.npm_entry(tmp_path, "opencli").name == "opencli.cmd"


def test_console_entry_help_is_utf8_under_ascii_locale():
    env = {**os.environ, "PYTHONIOENCODING": "ascii", "PYTHONUTF8": "0"}
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src")
    result = subprocess.run(
        [sys.executable, "-B", "-c", "from soota.cli import app; app()", "--help"],
        env=env,
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    assert "采集正文" in result.stdout.decode("utf-8")


@pytest.mark.parametrize("local", [True, False])
@pytest.mark.parametrize(
    "name,package", [("opencli", "@jackwener/opencli"), ("opencli-mcp", "opencli-mcp"), ("npm", "npm")]
)
def test_windows_npm_shim_uses_node_without_shell(monkeypatch, tmp_path, local, name, package):
    root = tmp_path / "运行时 with space"
    shim = root / ("node_modules/.bin" if local else "") / (name + ".cmd")
    shim.parent.mkdir(parents=True)
    shim.write_text("must never be executed by cmd.exe", encoding="utf-8")
    package_root = root / "node_modules" / package
    package_root.mkdir(parents=True)
    entry = package_root / "cli.js"
    entry.write_text("", encoding="utf-8")
    (package_root / "package.json").write_text(json.dumps({"name": package, "bin": {name: "cli.js"}}), encoding="utf-8")
    monkeypatch.setattr(platform_support, "WINDOWS", True)
    monkeypatch.setattr(platform_support.shutil, "which", lambda value: "node.exe" if value == "node" else None)
    arguments = ["中文 & %PATH% | ^", "$(not-a-command)"]
    assert platform_support.command_argv([str(shim), *arguments]) == ["node.exe", str(entry), *arguments]


def test_windows_unknown_batch_entry_is_not_executed(monkeypatch):
    monkeypatch.setattr(platform_support, "WINDOWS", True)
    monkeypatch.setattr(platform_support.shutil, "which", lambda value: "node.exe" if value == "node" else None)
    with pytest.raises(SourceError, match="批处理"):
        platform_support.command_argv(["custom.cmd", "& unwanted"])


def test_real_shared_exclusive_locks_and_release(tmp_path):
    path = tmp_path / "state.lock"
    path.write_text("1700000000.0", encoding="utf-8")
    script = """
import sys
from soota.storage.locking import lock_file
with open(sys.argv[1], 'a+b') as handle:
    try:
        lock_file(handle, exclusive=sys.argv[2]=='exclusive', blocking=False)
    except BlockingIOError:
        sys.exit(23)
"""

    def attempt(mode):
        return subprocess.run(
            [sys.executable, "-B", "-c", script, str(path), mode],
            timeout=10,
            capture_output=True,
            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src")},
        ).returncode

    with path.open("a+b") as handle:
        locking.lock_file(handle, exclusive=False)
        try:
            assert attempt("shared") == 0
            assert attempt("exclusive") == 23
        finally:
            locking.unlock_file(handle)
        locking.lock_file(handle)
        assert attempt("shared") == 23
    assert attempt("exclusive") == 0  # Closing the owner releases the kernel lock.
    assert path.read_text(encoding="utf-8") == "1700000000.0"


@pytest.fixture
def portable_windows_job(monkeypatch):
    """Exercise Windows pipe orchestration on every OS; native Job tests run on Windows."""
    if sys.platform == "win32":
        return

    @contextmanager
    def local_job(argv, env, **kwargs):
        child = subprocess.Popen(
            argv,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=kwargs.get("stderr", subprocess.PIPE),
            start_new_session=True,
        )
        try:
            yield child
        finally:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGKILL)
            child.wait(timeout=5)

    monkeypatch.setattr(windows_process, "windows_process", local_job)


def test_windows_pumps_handle_full_duplex_utf8(monkeypatch, portable_windows_job):
    monkeypatch.setattr(process, "WINDOWS", True)
    text = "中文📝" * 50000
    script = "import os,sys; os.write(2,b'e'*150000); sys.stdout.buffer.write(sys.stdin.buffer.read())"
    result = process.run_process(
        [sys.executable, "-B", "-c", script], timeout=10, max_bytes=1000000, input_bytes=text.encode()
    )
    assert result.returncode == 0
    assert result.stdout == text
    assert result.stderr == "e" * 150000


@pytest.mark.parametrize("reason", ["timeout", "output_limit"])
def test_windows_pumps_stop_blocked_input_and_join(monkeypatch, portable_windows_job, reason):
    monkeypatch.setattr(process, "WINDOWS", True)
    before = set(threading.enumerate())
    script = (
        "import time; time.sleep(30)"
        if reason == "timeout"
        else "import os,time; os.write(2,b'x'*10000); time.sleep(30)"
    )
    with pytest.raises(SourceError) as caught:
        process.run_process([sys.executable, "-B", "-c", script], timeout=1, max_bytes=100, input_bytes=b"x" * 200000)
    assert caught.value.details["reason"] == reason
    assert set(threading.enumerate()) <= before


def test_windows_pumps_cleanup_on_keyboard_interrupt(monkeypatch, portable_windows_job):
    monkeypatch.setattr(process, "WINDOWS", True)
    original = process.collect_pipes
    before = set(threading.enumerate())

    def interrupted(*args, **kwargs):
        _, threads = original(*args, **kwargs)

        def cancel():
            raise KeyboardInterrupt

        return cancel, threads

    monkeypatch.setattr(process, "collect_pipes", interrupted)
    with pytest.raises(KeyboardInterrupt):
        process.run_process(
            [sys.executable, "-B", "-c", "import time; time.sleep(30)"],
            timeout=10,
            max_bytes=1024,
            input_bytes=b"x" * 200000,
        )
    assert set(threading.enumerate()) <= before


def test_windows_gate_does_not_dispatch_before_assignment(monkeypatch, tmp_path):
    target = tmp_path / "must-not-exist"

    class DeniedJob:
        def assign(self, child):
            raise OSError("assignment denied")

        def close(self, **kwargs):
            pass

    monkeypatch.setattr(windows_process, "WindowsJob", DeniedJob)
    with (
        pytest.raises(SourceError) as caught,
        windows_process.windows_process(
            [sys.executable, "-B", "-c", f"open({str(target)!r}, 'w').close()"], platform_support.utf8_environment()
        ),
    ):
        pytest.fail("must not dispatch without process tree containment")
    assert caught.value.details["dispatched"] is False
    assert not target.exists()


def test_windows_gate_preserves_stdin_bytes(monkeypatch):
    class TestJob:
        def assign(self, child):
            pass

        def close(self, **kwargs):
            pass

    monkeypatch.setattr(windows_process, "WindowsJob", TestJob)
    payload = "中文📝".encode()
    with windows_process.windows_process(
        [sys.executable, "-c", "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"],
        platform_support.utf8_environment(),
    ) as child:
        stdout, stderr = child.communicate(payload, timeout=10)
        assert child.returncode == 0
        assert stdout == payload and not stderr


def test_windows_file_cap_fails_before_dispatch(monkeypatch):
    monkeypatch.setattr(probe, "WINDOWS", True)
    monkeypatch.setattr(probe, "run_process", lambda *a, **k: pytest.fail("must not run without a hard file limit"))
    with pytest.raises(SourceError) as caught:
        probe.command(["ffmpeg", "synthetic"], timeout=1, limit_file_bytes=1024)
    assert caught.value.details == {"reason": "file_limit_unavailable", "dispatched": False}


@pytest.mark.skipif(sys.platform != "win32", reason="requires the real Windows kernel")
def test_native_windows_job_terminates_descendants(tmp_path):
    pid_file = tmp_path / "descendant.pid"
    child_code = "import time; time.sleep(30)"
    parent_code = (
        f"import subprocess,sys,time; p=subprocess.Popen([sys.executable,'-c',{child_code!r}]); "
        f"open({str(pid_file)!r},'w').write(str(p.pid)); time.sleep(30)"
    )
    with pytest.raises(SourceError):
        process.run_process([sys.executable, "-B", "-c", parent_code], timeout=2, max_bytes=1024)
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x100000, False, int(pid_file.read_text(encoding="utf-8")))
    if handle:
        try:
            assert kernel.WaitForSingleObject(handle, 5000) == 0
        finally:
            kernel.CloseHandle(handle)
