"""分离存储的离线集成、路径门禁与历史任务恢复。"""

import base64
import importlib.util
import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from soota.artifacts.bundle import (
    artifact_path,
    content_from_markdown,
    load_bundle,
    load_transcript,
    verify_artifacts,
    write_artifact,
    write_bundle,
)
from soota.artifacts.models import BundleV3
from soota.cli import app
from soota.config import StorageConfig, load_settings
from soota.errors import ContractError, InputError
from soota.models import MediaItem, RequestRecord
from soota.routing import recognize
from soota.storage.jobs import JobStore, bind_job
from soota.storage.task_budget import TaskBudget
from soota.tasks.entry import ingest, prepare_task
from soota.tasks.runner import resume
from soota.tasks.state import load_spec, task_folder


@pytest.fixture
def split(settings, tmp_path):
    settings.storage = StorageConfig(
        control_dir=tmp_path / ".lorelab" / "packages", jobs_dir=tmp_path / ".lorelab" / "jobs"
    )
    return settings


def local_source(tmp_path):
    path = tmp_path / "input.md"
    path.write_text("# Original\n\nOne paragraph.\n")
    return path


def split_bundle(tmp_path):
    folder, raw = tmp_path / "control", tmp_path / "raw"
    item = MediaItem(source=recognize("https://www.youtube.com/watch?v=Example0001"))
    bundle = write_bundle(folder, item, content=content_from_markdown(item.source, "Original body."), raw_root=raw)
    return folder, raw, bundle


def test_optional_preferences_overlay_is_sparse_and_environment_wins(tmp_path, monkeypatch):
    config, data, cache = (tmp_path / part for part in ("config", "state", "cache"))
    config.mkdir()
    preferences = tmp_path / "user" / "preferences.toml"
    preferences.parent.mkdir()
    original = """[soota]
preferences_file = "../user/preferences.toml"
default_language = "en"
[asr]
model = "global-model"
device = "cpu"
[routing]
default_provider = "yt-dlp"
disabled_providers = ["tikhub"]
[routing.priorities.youtube.transcript]
mode = "manual"
order = ["yt-dlp", "opencli"]
"""
    (config / "config.toml").write_text(original)
    (config / "providers.json").write_text(
        json.dumps({"routing": {"default_provider": "auto"}, "weir": {"enabled": True}})
    )
    (config / "credentials.json").write_text('{"synthetic": "preserve-me"}')
    preferences.write_text("""[soota]
output_dir = "raw"
data_dir = "must-not-move"
config_dir = "must-not-move"
[storage]
control_dir = "control"
jobs_dir = "jobs"
[asr]
device = "cuda"
[routing]
default_provider = "opencli"
[routing.priorities.youtube.transcript]
mode = "cost"
[weir]
task_credits = 7
[opencli]
profile = "file-profile"
""")
    monkeypatch.setenv("SOOTA_CONFIG_DIR", str(config))
    monkeypatch.setenv("SOOTA_DATA_DIR", str(data))
    monkeypatch.setenv("SOOTA_CACHE_DIR", str(cache))
    monkeypatch.setenv("SOOTA_OUTPUT_DIR", str(tmp_path / "env-raw"))
    monkeypatch.setenv("SOOTA_JOBS_DIR", str(tmp_path / "env-jobs"))
    monkeypatch.setenv("OPENCLI_PROFILE", "env-profile")
    monkeypatch.delenv("SOOTA_PREFERENCES_FILE", raising=False)
    monkeypatch.delenv("SOOTA_CONTROL_DIR", raising=False)
    settings = load_settings()
    assert settings.preferences_path == preferences
    assert settings.output_dir == tmp_path / "env-raw"
    assert settings.storage.control_dir == preferences.parent / "control"
    assert settings.jobs_dir == tmp_path / "env-jobs"
    assert (settings.config_dir, settings.data_dir, settings.cache_dir) == (config, data, cache)
    assert settings.ledger_path == data / "ledger.jsonl"
    assert settings.default_language == "en"
    assert (settings.asr.model, settings.asr.device) == ("global-model", "cuda")
    assert settings.opencli.profile == "env-profile"
    assert settings.routing.default_provider == "opencli"
    assert settings.routing.disabled_providers == {"tikhub"}
    priority = settings.routing.priorities["youtube"]["transcript"]
    assert priority.mode == "cost" and priority.order == ["yt-dlp", "opencli"]
    assert settings.weir.enabled and settings.weir.task_credits == 7
    assert (config / "config.toml").read_text() == original
    assert (config / "credentials.json").read_text() == '{"synthetic": "preserve-me"}'
    assert not settings.jobs_dir.exists() and not settings.storage.control_dir.exists()


def test_missing_preferences_are_optional_but_invalid_toml_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("SOOTA_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("SOOTA_DATA_DIR", str(tmp_path / "data"))
    for name in ("SOOTA_JOBS_DIR", "SOOTA_CONTROL_DIR"):
        monkeypatch.delenv(name, raising=False)
    path = tmp_path / "optional.toml"
    monkeypatch.setenv("SOOTA_PREFERENCES_FILE", str(path))
    settings = load_settings()
    assert settings.preferences_path == path
    assert settings.storage == StorageConfig()
    assert settings.jobs_dir == settings.data_dir / "jobs"
    path.write_text("[invalid")
    with pytest.raises(InputError, match="TOML"):
        load_settings()


def test_v3_raw_and_control_are_distinct_reloadable_and_hashed(tmp_path):
    folder, raw, bundle = split_bundle(tmp_path)
    assert isinstance(bundle, BundleV3)
    assert {p.name for p in raw.iterdir()} == {"body.md"}
    assert {p.name for p in folder.iterdir()} == {"context.md", "content.json", "manifest.json"}
    assert artifact_path(folder, bundle, "body.md") == raw / "body.md"
    assert artifact_path(folder, bundle, "context.md") == folder / "context.md"
    reloaded = load_bundle(folder / "manifest.json")
    assert reloaded.model_dump() == bundle.model_dump()
    assert verify_artifacts(folder, reloaded) == []
    (raw / "body.md").write_text("tampered body")
    assert verify_artifacts(folder, reloaded) == ["changed:body.md"]
    with pytest.raises(InputError, match="修改"):
        write_artifact(folder, reloaded, path="body.md", kind="body", format="md", content="replacement")


@pytest.mark.parametrize(
    "mutation", ["relative-root", "missing-storage", "wrong-storage", "escape", "unregistered", "invalid-artifacts"]
)
def test_v3_rejects_ambiguous_or_unsafe_manifest(tmp_path, mutation):
    folder, _, _ = split_bundle(tmp_path)
    path = folder / "manifest.json"
    payload = json.loads(path.read_text())
    if mutation == "relative-root":
        payload["raw_root"] = "../raw"
    elif mutation == "missing-storage":
        payload["artifacts"][0].pop("storage")
    elif mutation == "wrong-storage":
        payload["artifacts"][0]["storage"] = "control"
    elif mutation == "escape":
        payload["artifacts"][0]["path"] = "../escape.md"
    elif mutation == "invalid-artifacts":
        payload["artifacts"] = None
    else:
        payload["files"]["unknown"] = "unknown.md"
    path.write_text(json.dumps(payload))
    with pytest.raises(ContractError):
        load_bundle(folder)


def test_v3_rejects_symlink_escape_and_root_retargeting(tmp_path):
    folder, raw, bundle = split_bundle(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (raw / "attachments").symlink_to(outside, target_is_directory=True)
    with pytest.raises(InputError, match="符号链接"):
        write_artifact(folder, bundle, path="attachments/escape.md", kind="body", format="md", content="no")
    assert not list(outside.iterdir())
    raw.rename(tmp_path / "moved-raw")
    raw.symlink_to(tmp_path / "moved-raw", target_is_directory=True)
    with pytest.raises(ContractError):
        load_bundle(folder)


def test_existing_v2_cannot_be_relabelled_or_relocated(tmp_path):
    folder = tmp_path / "v2"
    item = MediaItem(source=recognize("https://www.youtube.com/watch?v=Example0001"))
    write_bundle(folder, item, content=content_from_markdown(item.source, "old"))
    before = {p.name: p.read_bytes() for p in folder.iterdir()}
    with pytest.raises(InputError, match="存储契约"):
        write_bundle(folder, item, raw_root=tmp_path / "raw", force=True)
    assert {p.name: p.read_bytes() for p in folder.iterdir()} == before
    assert not (tmp_path / "raw").exists()


def test_standalone_soota_uses_frozen_roots_after_configuration_change(split, tmp_path, monkeypatch):
    original = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, "find_spec", lambda name, *a: None if name == "lorelab" else original(name, *a))
    result = ingest(split, str(local_source(tmp_path)), want="context", provider="local")
    assert result.ok, result.model_dump()
    folder, raw = Path(result.data["output"]), Path(result.data["raw"])
    assert folder.is_relative_to(split.storage.control_dir) and raw.is_relative_to(split.output_dir)
    job_id = result.data["job_id"]
    record_path = split.jobs_dir / f"{job_id}.json"
    request = load_spec(split, job_id)
    assert request.control_dir == split.storage.control_dir
    assert task_folder(split, job_id).is_relative_to(split.jobs_dir)
    changed = split.model_copy(deep=True)
    changed.output_dir = tmp_path / "new-raw"
    changed.storage = StorageConfig(control_dir=tmp_path / "new-control", jobs_dir=tmp_path / "new-jobs")
    monkeypatch.setattr("soota.tasks.runner.collect_text", lambda *a, **k: pytest.fail("reacquired frozen source"))
    before = (raw / "body.md").read_bytes()
    done = resume(changed, str(record_path))
    assert done.ok and done.data["job_id"] == job_id
    assert Path(done.data["output"]) == folder
    assert (raw / "body.md").read_bytes() == before
    assert not changed.jobs_dir.exists() and not changed.output_dir.exists()
    assert not changed.storage.control_dir.exists()
    assert changed.ledger_path == split.ledger_path


def test_new_jobs_setting_does_not_move_legacy_budget_or_artifacts(settings, tmp_path, monkeypatch):
    result = ingest(settings, str(local_source(tmp_path)), want="context", provider="local", max_cost=2)
    job_id = result.data["job_id"]
    original_folder = task_folder(settings, job_id)
    assert "control_dir" not in json.loads((original_folder / "request.json").read_text())
    budget = TaskBudget(original_folder, 2)
    budget.reserve(RequestRecord(provider="synthetic", endpoint="offline", estimated_cost_usd=0.25, billed=True))
    reserved = budget.snapshot()
    folder = Path(result.data["output"])
    context = (folder / "context.md").read_bytes()
    record_path = settings.jobs_dir / f"{job_id}.json"
    settings.storage = StorageConfig(control_dir=tmp_path / "new-control", jobs_dir=tmp_path / "new-jobs")
    monkeypatch.setattr("soota.tasks.runner.collect_text", lambda *a, **k: pytest.fail("reacquired legacy source"))
    for reference in (str(record_path), str(original_folder / "request.json"), job_id):
        done = resume(settings, reference)
        assert done.ok and done.data["job_id"] == job_id
        bound, identity = bind_job(settings, reference)
        assert task_folder(bound, identity) == original_folder
        assert TaskBudget(original_folder, 2).snapshot() == reserved
        assert "control_dir" not in json.loads((original_folder / "request.json").read_text())
    assert (folder / "context.md").read_bytes() == context
    assert load_bundle(folder).schema_version == 2
    assert not settings.storage.control_dir.exists()
    assert not settings.jobs_dir.exists()
    assert len(JobStore(record_path.parent).list_jobs()) == 1


def test_v3_subtitle_exports_and_resume_use_raw_paths(split, tmp_path, monkeypatch):
    source = tmp_path / "captions.srt"
    source.write_text("1\n00:00:00,000 --> 00:00:01,000\nOriginal sentence.\n")
    result = ingest(split, str(source), want="transcript", provider="local", subtitle_formats="srt,vtt,txt,json")
    assert result.ok, result.model_dump()
    folder, raw = Path(result.data["output"]), Path(result.data["raw"])
    bundle = load_bundle(folder)
    assert load_transcript(folder, bundle).segments[0].text == "Original sentence."
    assert (raw / "transcript.md").exists() and (folder / "transcript.json").exists()
    assert all((raw / f"captions.{fmt}").is_file() for fmt in ("srt", "vtt", "txt", "json"))
    assert not (raw / "manifest.json").exists() and not (raw / "context.md").exists()
    monkeypatch.setattr("soota.tasks.runner.collect_text", lambda *a, **k: pytest.fail("reacquired subtitles"))
    assert resume(split, result.data["job_id"]).ok


def test_overlapping_storage_roots_fail_before_task_writes(split, tmp_path):
    split.storage.control_dir = split.output_dir / ".control"
    with pytest.raises(InputError):
        prepare_task(split, str(local_source(tmp_path)), provider="local")
    assert not split.jobs_dir.exists() and not split.output_dir.exists()


@pytest.mark.parametrize("root_name", ["raw", "control"])
def test_v3_source_subdirectory_cannot_escape_configured_root(split, tmp_path, root_name):
    source = local_source(tmp_path)
    root = split.output_dir if root_name == "raw" else split.storage.control_dir
    root.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "local").symlink_to(outside, target_is_directory=True)
    result = ingest(split, str(source), want="context", provider="local")
    assert not result.ok
    assert not list(outside.iterdir())


def test_standalone_cli_loads_the_same_explicit_preferences(tmp_path, monkeypatch):
    config, data = tmp_path / "config", tmp_path / "global-data"
    config.mkdir()
    (config / "config.toml").write_text('[soota]\npreferences_file = "../preferences.toml"\n')
    (tmp_path / "preferences.toml").write_text(
        '[soota]\noutput_dir = "raw"\n[storage]\ncontrol_dir = ".lorelab/packages"\njobs_dir = ".lorelab/jobs"\n'
    )
    monkeypatch.setenv("SOOTA_CONFIG_DIR", str(config))
    monkeypatch.setenv("SOOTA_DATA_DIR", str(data))
    monkeypatch.setenv("SOOTA_CACHE_DIR", str(tmp_path / "cache"))
    for key in ("SOOTA_PREFERENCES_FILE", "SOOTA_OUTPUT_DIR", "SOOTA_CONTROL_DIR", "SOOTA_JOBS_DIR"):
        monkeypatch.delenv(key, raising=False)
    original = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, "find_spec", lambda name, *a: None if name == "lorelab" else original(name, *a))
    result = CliRunner().invoke(
        app, ["ingest", str(local_source(tmp_path)), "--want", "context", "--provider", "local", "--json"]
    )
    assert result.exit_code == 0, result.output
    value = json.loads(result.stdout)["data"]
    assert Path(value["output"]).is_relative_to(tmp_path / ".lorelab" / "packages")
    assert Path(value["raw"]).is_relative_to(tmp_path / "raw")
    assert (tmp_path / ".lorelab" / "jobs" / (value["job_id"] + ".json")).is_file()
    assert not (data / "jobs").exists()
    assert load_settings().ledger_path == data / "ledger.jsonl"


def test_v3_local_images_and_bundle_reuse_resolve_raw_attachments(split, tmp_path):
    if not shutil.which("ffprobe"):
        pytest.skip("ffprobe required for actual image validation")
    image = tmp_path / "pixel.png"
    image.write_bytes(
        base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j9xkAAAAASUVORK5CYII=")
    )
    article = tmp_path / "image.md"
    article.write_text("# Body\n\nOriginal paragraph.\n\n![Pixel](pixel.png)\n")
    result = ingest(split, str(article), want="context,images", provider="local")
    assert result.ok, result.model_dump()
    folder = Path(result.data["output"])
    bundle = load_bundle(folder)
    attachment = next(a for a in bundle.artifacts if a.kind == "image")
    assert artifact_path(folder, bundle, attachment.path).read_bytes() == image.read_bytes()
    assert f"]({attachment.path})" in (bundle.raw_root / "body.md").read_text()
    assert ingest(split, str(folder), want="context,images", provider="local").ok
    assert verify_artifacts(folder, load_bundle(folder)) == []
