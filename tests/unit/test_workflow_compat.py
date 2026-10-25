"""旧工作流入口只转发参数；采集安装不绑定产品或另一份受管运行时。"""

import json
import subprocess
import sys

import pytest

from soota import workflow_compat


@pytest.fixture
def standalone(monkeypatch):
    for key in (
        "LORELAB_RUNTIME_FILE",
        "LOREHUB_RUNTIME_FILE",
        "SOOTA_KNOWLEDGE_RUNTIME_FILE",
        "LORELAB_ROOT",
        "LOREHUB_ROOT",
        "SOOTA_KNOWLEDGE_COMPONENTS_LOCK",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(workflow_compat.importlib.util, "find_spec", lambda name: None)


def test_missing_product_reports_migration_without_running_workflow(standalone, monkeypatch, capsys):
    monkeypatch.setattr(workflow_compat.shutil, "which", lambda name: None)
    assert workflow_compat.forward(["knowledge", "status", "/old task"]) == 1
    result = capsys.readouterr()
    assert not result.out
    assert "LoreLab" in json.loads(result.err)["error"]


def test_forward_preserves_arguments_output_and_exit_code(standalone, monkeypatch, tmp_path, capsys):
    executable = tmp_path / "lorelab with spaces"
    executable.write_text(
        f"#!{sys.executable}\nimport json,sys\nprint(json.dumps(sys.argv[1:], ensure_ascii=False))\n"
        "sys.stderr.write('状态明细')\nsys.exit(17)\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    monkeypatch.setattr(workflow_compat.shutil, "which", lambda name: str(executable))
    run = subprocess.run

    def launch(command, **kwargs):
        assert command[0] == str(executable)
        return run([sys.executable, str(executable), *command[1:]], **kwargs)

    monkeypatch.setenv("PYTHONIOENCODING", "ascii")
    monkeypatch.setattr(workflow_compat.subprocess, "run", launch)
    args = ["knowledge", "status", "/旧任务/$(literal); no-shell"]
    assert workflow_compat.forward(args) == 17
    result = capsys.readouterr()
    assert json.loads(result.out) == args
    assert result.err == "状态明细"


@pytest.mark.parametrize(
    "key",
    [
        "LORELAB_RUNTIME_FILE",
        "LOREHUB_RUNTIME_FILE",
        "SOOTA_KNOWLEDGE_RUNTIME_FILE",
        "LORELAB_ROOT",
        "LOREHUB_ROOT",
        "SOOTA_KNOWLEDGE_COMPONENTS_LOCK",
    ],
)
def test_damaged_managed_install_never_uses_another_product(standalone, monkeypatch, capsys, key):
    monkeypatch.setenv(key, "/managed/binding")
    monkeypatch.setattr(workflow_compat.shutil, "which", lambda name: pytest.fail("must not consult PATH"))
    assert workflow_compat.forward(["study", "status", "/old task"]) == 1
    assert not capsys.readouterr().out
