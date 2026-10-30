from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from typer.testing import CliRunner

from soota.cli import app
from soota.content.native_types import NativeContent, NativeOperation
from soota.errors import AuthBudgetError, InputError, SourceError
from soota.models import RequestRecord
from soota.storage.jobs import JobStore
from soota.storage.task_budget import TaskBudget
from soota.tasks.entry import ingest
from soota.tasks.inputs import parse_inputs
from soota.tasks.runner import resume


@pytest.fixture
def article(tmp_path):
    path = tmp_path / "source.md"
    path.write_text("# Source title\n\nFirst paragraph.\n\nRepeated.\n\nRepeated.\n")
    return path


@pytest.fixture(scope="module")
def media_samples(tmp_path_factory):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg/ffprobe required")
    folder = tmp_path_factory.mktemp("task-media")
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x180:rate=10:duration=1",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=1",
            "-c:v",
            "mpeg4",
            "-c:a",
            "aac",
            str(folder / "sample.mp4"),
        ],
        check=True,
    )
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-i",
            str(folder / "sample.mp4"),
            "-frames:v",
            "1",
            str(folder / "image.png"),
        ],
        check=True,
    )
    return folder


def test_local_context_is_v2_and_does_not_create_article(settings, article):
    result = ingest(settings, str(article), want="context")
    assert result.ok
    manifest = json.loads(Path(result.data["manifest"]).read_text())
    assert manifest["schema_version"] == 2 and manifest["content"]["scope"] == "full"
    assert "First paragraph" in Path(result.data["context"]).read_text()
    folder = Path(result.data["output"])
    assert not (folder / "content.md").exists() and not (folder / "transcript.md").exists()
    assert manifest["analysis"]["status"] == "not_requested"
    assert len(JobStore(settings.jobs_dir).list_jobs()) == 1


def test_local_source_change_is_not_old_context_cache(settings, article):
    first = ingest(settings, str(article), want="context")
    article.write_text("Completely changed source.\n")
    second = ingest(settings, str(article), want="context")
    assert first.ok and second.ok and first.data["output"] == second.data["output"]
    assert "Completely changed" in Path(second.data["context"]).read_text()
    assert "First paragraph" not in Path(second.data["context"]).read_text()


def test_resume_does_not_reacquire_valid_content(settings, article, monkeypatch):
    first = ingest(settings, str(article), want="context")
    monkeypatch.setattr("soota.tasks.runner.collect_text", lambda *a, **k: pytest.fail("unexpected acquisition"))
    assert resume(settings, first.data["job_id"]).ok


def test_local_caption_exports_and_no_native_requests(settings, tmp_path):
    source = tmp_path / "captions.vtt"
    source.write_text("WEBVTT\n\n00:00.125 --> 00:01.750\n甲\n乙\n")
    result = ingest(settings, str(source), want="transcript,context", subtitle_formats="srt,vtt,txt,json")
    assert result.ok
    folder = Path(result.data["output"])
    assert "00:00:00,125 --> 00:00:01,750" in (folder / "captions.srt").read_text()
    assert (folder / "captions.txt").read_text() == "甲\n乙\n"
    assert result.data["reserved_cost_usd"] == 0


def test_plain_txt_does_not_invent_srt_times(settings, tmp_path):
    source = tmp_path / "captions.txt"
    source.write_text("Untimed words.")
    result = ingest(settings, str(source), want="transcript,context", input_format="txt")
    assert not result.ok and "transcript" in result.data["missing_outputs"]
    assert Path(result.data["context"]).is_file()
    assert not (Path(result.data["output"]) / "captions.srt").exists()


def test_native_failure_is_not_fake_data_success(settings, monkeypatch):
    monkeypatch.setattr(
        "soota.tasks.runner.native_operation", lambda *a, **k: (_ for _ in ()).throw(SourceError("failed read"))
    )
    result = ingest(settings, operation="opencli.youtube.search", params=["query=test"], want="data")
    assert not result.ok and "data" in result.data["missing_outputs"]
    assert not (Path(result.data["output"]) / "data.json").exists()


def test_native_data_and_resume_only_read_once(settings, monkeypatch):
    calls = []

    def capture(*args, **kwargs):
        calls.append(1)
        return NativeOperation(
            raw=[{"title": "item", "url": "https://example.test/one"}],
            provider="opencli",
            operation="youtube/search",
            workdir=settings.data_dir,
        )

    monkeypatch.setattr("soota.tasks.runner.native_operation", capture)
    first = ingest(settings, operation="opencli.youtube.search", params=["query=test"], want="data")
    assert first.ok and resume(settings, first.data["job_id"]).ok
    assert len(calls) == 1
    assert json.loads((Path(first.data["output"]) / "data.json").read_text())[0]["title"] == "item"


def test_native_transient_resume_reuses_success_and_keeps_typed_errors(settings, tmp_path, monkeypatch):
    from soota.providers import opencli_commands
    from soota.providers.opencli_runtime import OpenCLIRuntime
    from soota.providers.process import ProcessResult

    row = {
        "command": "youtube/search",
        "site": "youtube",
        "name": "search",
        "access": "read",
        "browser": False,
        "args": [{"name": "query", "type": "str"}],
    }
    monkeypatch.setattr(opencli_commands, "commands", lambda settings: [row])
    monkeypatch.setattr(OpenCLIRuntime, "resolve", lambda self: ("fixture", "managed"))
    calls = []
    recovered = False

    def process(argv, **kwargs):
        query = next(arg.split("=", 1)[1] for arg in argv if arg.startswith("--query="))
        calls.append(query)
        if query == "pending" and not recovered:
            return ProcessResult(1, "", "error:\n  code: COMMAND_EXEC\n  message: Navigation rejected")
        return ProcessResult(0, json.dumps([{"title": query, "url": "https://example.test/" + query}]), "")

    monkeypatch.setattr(opencli_commands, "run_process", process)
    inputs = tmp_path / "inputs.json"
    inputs.write_text(
        json.dumps(
            {
                "inputs": [
                    {"kind": "operation", "operation": "opencli.youtube.search", "params": {"query": query}}
                    for query in ("complete", "pending")
                ]
            }
        )
    )
    first = ingest(settings, inputs_file=inputs, want="data", provider="opencli", max_cost=0)
    assert not first.ok and first.data["job_status"] == "partial"
    assert calls == ["complete", "pending"]
    error = first.data["items"][1]["steps"]["native"]["error"]
    assert error["details"]["reason"] == "navigation_rejected" and error["details"]["retryable"]
    assert error["details"]["attempts"] == 1
    completed = Path(first.data["items"][0]["bundle"]) / "data.json"
    before = (completed.read_bytes(), completed.stat().st_mtime_ns)
    failed = Path(first.data["items"][1]["bundle"])
    assert not (failed / "data.json").exists()
    recovered = True
    second = resume(settings, first.data["job_id"], max_cost=0)
    assert second.ok and calls == ["complete", "pending", "pending"]
    assert (completed.read_bytes(), completed.stat().st_mtime_ns) == before
    assert first.data["reserved_cost_usd"] == second.data["reserved_cost_usd"] == 0


def test_resume_rejects_historical_thread_cache_without_target(settings, monkeypatch):
    # Model a pre-fix job which incorrectly cached a parent post as success.
    def capture(*args, **kwargs):
        return NativeOperation(
            raw=[{"id": "100", "text": "Parent"}],
            provider="opencli",
            operation="twitter/thread",
            workdir=settings.data_dir,
        )

    monkeypatch.setattr("soota.tasks.runner.native_operation", capture)
    first = ingest(settings, operation="opencli.twitter.thread", params=["tweet-id=101", "limit=1"], want="data")
    assert first.ok
    monkeypatch.setattr("soota.tasks.runner.native_operation", lambda *a, **kw: pytest.fail("must not replay"))
    second = resume(settings, first.data["job_id"])
    assert not second.ok
    assert second.data["items"][0]["errors"][0]["details"]["reason"] == "target_not_returned"


def test_signed_content_references_are_private_and_creator_survives(settings, monkeypatch):
    signed = "https://example.test/article?token=fixture-secret-access"

    def capture(*args, **kwargs):
        return NativeContent(
            raw=[{"title": "Article", "author": "Example author"}],
            body="Body.\n\nSource: " + signed,
            title="Article",
            source_url=signed,
            provider="opencli",
            scope="page",
            operation="web/read",
        )

    monkeypatch.setattr("soota.tasks.acquire.capture_content", capture)
    result = ingest(settings, signed, want="context")
    assert result.ok
    assert "fixture-secret-access" not in Path(result.data["context"]).read_text()
    manifest = Path(result.data["manifest"]).read_text()
    assert "fixture-secret-access" not in manifest
    assert json.loads(manifest)["metadata"]["creator"] == "Example author"
    assert json.loads(manifest)["processing"]["provider"] == "opencli"


def test_required_images_cannot_succeed_with_none(settings, monkeypatch):
    monkeypatch.setattr(
        "soota.tasks.acquire.capture_content",
        lambda *a, **k: NativeContent(
            raw=[],
            body="No images.",
            title="Article",
            source_url="https://example.test/page",
            provider="opencli",
            scope="page",
            operation="web/read",
        ),
    )
    result = ingest(settings, "https://example.test/page", want="images,context")
    assert not result.ok and result.data["missing_outputs"] == ["images"]
    assert Path(result.data["context"]).is_file()


def test_local_markdown_images_are_copied_and_relinked(settings, tmp_path, media_samples):
    source = tmp_path / "source.md"
    (tmp_path / "pictures").mkdir()
    shutil.copyfile(media_samples / "image.png", tmp_path / "pictures" / "image.png")
    source.write_text("# With image\n\n![sample](pictures/image.png)\n")
    result = ingest(settings, str(source), want="context,images")
    assert result.ok, result.model_dump()
    folder = Path(result.data["output"])
    assert list((folder / "attachments").glob("image-*"))
    assert "attachments/image-" in Path(result.data["context"]).read_text()
    assert "pictures/image.png" in source.read_text()


def test_partial_images_preserve_success_and_report_missing(settings, tmp_path, media_samples):
    source = tmp_path / "source.md"
    shutil.copyfile(media_samples / "image.png", tmp_path / "image.png")
    source.write_text("![one](image.png)\n\n![two](missing.png)\n")
    result = ingest(settings, str(source), want="context,images")
    assert not result.ok and "images" in result.data["missing_outputs"]
    assert list((Path(result.data["output"]) / "attachments").glob("image-*"))


def test_local_image_goal_never_runs_asr(settings, media_samples, monkeypatch):
    monkeypatch.setattr("soota.tasks.acquire.resolve_engine", lambda *a: pytest.fail("ASR on an image"))
    result = ingest(settings, str(media_samples / "image.png"), want="images")
    assert result.ok, result.model_dump()
    assert not (Path(result.data["output"]) / "transcript.md").exists()


def test_video_and_audio_share_one_local_source_and_preserve_neighbors(settings, tmp_path, media_samples):
    source = tmp_path / "original.mp4"
    shutil.copyfile(media_samples / "sample.mp4", source)
    neighbor = tmp_path / "original.m4a"
    neighbor.write_bytes(b"user-owned audio")
    original_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    result = ingest(settings, str(source), want="video,audio")
    assert result.ok, result.model_dump()
    kinds = {a["kind"] for a in result.data["artifacts"]}
    assert {"video", "audio"} <= kinds
    assert neighbor.read_bytes() == b"user-owned audio"
    assert hashlib.sha256(source.read_bytes()).hexdigest() == original_hash
    assert not (Path(result.data["output"]) / "context.md").exists()


def test_manifest_is_bundle_and_unrelated_directory_is_rejected(settings, article, tmp_path):
    result = ingest(settings, str(article), want="context")
    assert parse_inputs(result.data["manifest"], None)[0].kind == "bundle"
    empty = tmp_path / "unrelated"
    empty.mkdir()
    with pytest.raises(InputError):
        parse_inputs(str(empty), None)
    assert list(empty.iterdir()) == []


def test_batch_limit_and_resume_reuse_completed_sources(settings, tmp_path):
    files = []
    for index in range(3):
        path = tmp_path / f"source-{index}.md"
        path.write_text(f"Text {index}")
        files.append(str(path))
    inputs = tmp_path / "inputs.json"
    inputs.write_text(json.dumps(files + [files[0]]))
    first = ingest(settings, inputs_file=inputs, want="context", max_items=1)
    assert first.ok and first.data["discovered"] == 3 and first.data["limit_reached"]
    second = resume(settings, first.data["job_id"], max_items=3)
    assert second.ok and len(second.data["items"]) == 3
    assert len(JobStore(settings.jobs_dir).list_jobs()) == 1


def test_atomic_budget_reservations_survive_instances_and_do_not_run_child_on_rejection(tmp_path):
    root = tmp_path / "budget"
    budget = TaskBudget(root, 1)

    def reserve_one(index):
        try:
            TaskBudget(root, 1).reserve(RequestRecord(endpoint=f"request-{index}", billed=True, estimated_cost_usd=0.2))
            return True
        except AuthBudgetError:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(reserve_one, range(10))) == 5
    assert TaskBudget(root, 1).snapshot()["reserved_cost_usd"] == "1.0"
    calls = []
    with pytest.raises(AuthBudgetError):
        budget.reserve(
            RequestRecord(endpoint="extra", billed=True, estimated_cost_usd=0.2), lambda record: calls.append(1)
        )
    assert not calls


def test_new_cli_local_input_does_not_invoke_updater(settings, article, monkeypatch):
    settings.updates.enabled = True
    monkeypatch.setattr("soota.config.load_settings", lambda: settings)
    monkeypatch.setattr(
        "soota.auto_updates.automatic_update", lambda *a, **k: pytest.fail("network update for local file")
    )
    result = CliRunner().invoke(app, ["ingest", str(article), "--want", "context", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["ok"]


def test_asr_and_filter_options_cannot_reuse_old_context(settings, monkeypatch):
    from soota.models import MediaItem, TranscriptDocument, TranscriptSegment, TranscriptSource
    from soota.tasks.acquire import Collection
    from soota.tasks.inputs import source_for

    calls = []

    def collect(config, spec, seed, work, **kwargs):
        calls.append((spec.transcript_mode.value, spec.exclude_ranges))
        kind = TranscriptSource.ASR if spec.transcript_mode.value == "asr" else TranscriptSource.OFFICIAL
        doc = TranscriptDocument(
            status="ready",
            source=kind,
            language="zh",
            segments=[
                TranscriptSegment(
                    segment_id="seg-000001", start_ms=0, end_ms=1000, text=f"source {len(calls)}", source=kind
                )
            ],
        )
        return Collection(MediaItem(source=source_for(seed)), document=doc)

    monkeypatch.setattr("soota.tasks.runner.collect_text", collect)
    url = "https://www.youtube.com/watch?v=Example0001"
    assert ingest(settings, url, want="context").ok
    from soota.models import TranscriptMode

    assert ingest(settings, url, want="context", transcript=TranscriptMode.ASR).ok
    assert ingest(settings, url, want="context", exclude_ranges=["0:1"]).ok
    assert len(calls) == 3
    monkeypatch.setattr(
        "soota.tasks.runner.collect_text", lambda *a, **k: (_ for _ in ()).throw(SourceError("fetch failed"))
    )
    failed = ingest(settings, url, want="transcript", exclude_ranges=["0:2"])
    assert not failed.ok and "transcript" in failed.data["missing_outputs"]


def test_profile_alias_rebinding_invalidates_task_cache(settings, monkeypatch):
    from soota.artifacts.bundle import content_from_markdown
    from soota.models import MediaItem
    from soota.tasks.acquire import Collection
    from soota.tasks.inputs import source_for

    settings.opencli.profile = "work"
    profile = {"aliases": {"work": "account-a"}}
    monkeypatch.setattr("soota.providers.opencli_context.profile_config", lambda: profile)
    calls = []

    def collect(config, spec, seed, work, **kwargs):
        actual = profile["aliases"]["work"]
        calls.append(actual)
        source = source_for(seed)
        return Collection(MediaItem(source=source), content=content_from_markdown(source, actual))

    monkeypatch.setattr("soota.tasks.runner.collect_text", collect)
    first = ingest(settings, "https://example.test/article", want="context")
    profile["aliases"]["work"] = "account-b"
    second = ingest(settings, "https://example.test/article", want="context")
    assert first.ok and second.ok and calls == ["account-a", "account-b"]
    assert "account-b" in Path(second.data["context"]).read_text()


@pytest.mark.parametrize("provider", ["opencli-mcp", "yt-dlp", "http"])
def test_disabled_sources_cannot_be_reached_by_alternate_task_paths(settings, monkeypatch, tmp_path, provider):
    settings.routing.disabled_providers.add(provider)
    if provider == "opencli-mcp":
        settings.opencli_mcp.enabled = True
        monkeypatch.setattr(
            "soota.providers.mcp_client.StdioMCPClient.call_site_readonly",
            lambda *a, **k: pytest.fail("disabled MCP ran"),
        )
        result = ingest(settings, operation="opencli-mcp.example.read", want="data")
    elif provider == "yt-dlp":
        monkeypatch.setattr("soota.tasks.enumeration.run_process", lambda *a, **k: pytest.fail("disabled yt-dlp ran"))
        result = ingest(settings, "https://www.youtube.com/playlist?list=ExampleList", want="context")
    else:
        source = tmp_path / "images.md"
        source.write_text("![one](https://example.test/image.png)")
        monkeypatch.setattr(
            "soota.media.MediaService.from_descriptor", lambda *a, **k: pytest.fail("disabled HTTP ran")
        )
        result = ingest(settings, str(source), want="images")
    assert not result.ok


@pytest.mark.parametrize(
    "body,provider",
    [
        ({"url": "https://example.test/video.mp4"}, "local"),
        ({"url": "https://example.test/video.mp4", "video_url": "https://example.test/track.mp4"}, "auto"),
    ],
)
def test_descriptor_guards_are_reported_as_item_failures(settings, monkeypatch, tmp_path, body, provider):
    source = tmp_path / "media.json"
    source.write_text(json.dumps({"media": body}))
    monkeypatch.setattr(
        "soota.media.MediaService.from_descriptor", lambda *a, **k: pytest.fail("invalid descriptor dispatched")
    )
    result = ingest(settings, str(source), want="video", provider=provider)
    assert not result.ok and result.data["items"][0]["errors"][0]["code"] == "input"


@pytest.mark.parametrize("downloaded", [False, True])
def test_native_article_images_have_valid_relative_links(settings, monkeypatch, media_samples, downloaded):
    from soota.media import MediaService

    asset = MediaService(settings).from_file(media_samples / "image.png", kind="image")

    def operation(config, spec, seed, work):
        work.mkdir(parents=True, exist_ok=True)
        path = work / "article.md"
        files = [path]
        if downloaded:
            (work / "images").mkdir()
            image = work / "images" / "source.png"
            shutil.copyfile(media_samples / "image.png", image)
            files.append(image)
            path.write_text("![photo](images/source.png)")
        else:
            path.write_text("![photo](https://example.test/photo.png?token=fixture-secret)")
        return NativeOperation(
            raw=[{"title": "Page", "status": "success", "saved": str(path)}],
            provider="opencli",
            operation="web/read",
            workdir=work,
            files=files,
        )

    monkeypatch.setattr("soota.tasks.runner.native_operation", operation)
    monkeypatch.setattr("soota.media.MediaService.from_descriptor", lambda *a, **k: asset)
    result = ingest(
        settings, operation="opencli.web.read", params=["url=https://example.test/article"], want="context,images"
    )
    assert result.ok, result.model_dump()
    folder = Path(result.data["output"])
    assert "attachments/image-001.png" in (folder / "body.md").read_text()
    assert (folder / "attachments/image-001.png").is_file()
    assert "fixture-secret" not in (folder / "body.md").read_text()


def test_old_bundle_images_are_reused_without_network(settings, tmp_path, media_samples, monkeypatch):
    source = tmp_path / "article.md"
    shutil.copyfile(media_samples / "image.png", tmp_path / "image.png")
    source.write_text("![one](image.png)")
    first = ingest(settings, str(source), want="context,images")
    assert first.ok
    monkeypatch.setattr("soota.media.MediaService.from_descriptor", lambda *a, **k: pytest.fail("unnecessary network"))
    second = ingest(settings, first.data["output"], want="images", provider="local")
    assert second.ok, second.model_dump()


def test_video_constraint_does_not_discard_native_images(settings, monkeypatch, media_samples):
    def operation(config, spec, seed, work):
        work.mkdir(parents=True, exist_ok=True)
        path = work / "image.png"
        shutil.copyfile(media_samples / "image.png", path)
        return NativeOperation(raw=[], provider="opencli", operation="example/download", workdir=work, files=[path])

    monkeypatch.setattr("soota.tasks.runner.native_operation", operation)
    result = ingest(settings, operation="opencli.example.download", want="video,images", min_height=1080)
    assert not result.ok and result.data["missing_outputs"] == ["video"]
    assert any(a["kind"] == "image" for a in result.data["artifacts"])


def test_native_text_redaction_preserves_following_paragraphs():
    from soota.secrets import redact_any

    body = "Intro.\nhttps://example.test/media?token=synthetic-sensitive\n\nKeep the last paragraph."
    safe = redact_any({"body": body})["body"]
    assert "synthetic-sensitive" not in safe
    assert safe.endswith("Keep the last paragraph.") and safe.startswith("Intro.")


def test_task_budget_is_used_by_two_independent_tikhub_clients(settings, tmp_path):
    from soota.billing import PaymentGate
    from soota.providers.tikhub.client import TikHubProvider
    from soota.storage.cache import CacheStore
    from soota.storage.task_budget import task_budget_scope

    requests = []

    def requester(*args, **kwargs):
        requests.append(args)
        raise SourceError("uncertain transport outcome")

    def client():
        result = TikHubProvider(
            cache=CacheStore(tmp_path / "cache"),
            api_key="synthetic",
            requester=requester,
            payment_gate=PaymentGate(settings, 1),
        )
        result.endpoint_price = lambda endpoint: 0.6
        return result

    with task_budget_scope(TaskBudget(tmp_path / "budget", 1)):
        with pytest.raises(SourceError):
            client()._call("/api/v1/bilibili/web/fetch_one_video_v3", {}, billed=True)
    with task_budget_scope(TaskBudget(tmp_path / "budget", 1)):
        with pytest.raises(AuthBudgetError):
            client()._call("/api/v1/bilibili/web/fetch_one_video_v3", {}, billed=True)
    assert len(requests) == 1
    assert TaskBudget(tmp_path / "budget", 1).snapshot()["reserved_cost_usd"] == "0.6"


def test_local_mode_cannot_fetch_remote_images(settings, article, monkeypatch):
    article.write_text("![remote](https://example.test/image.png)")
    monkeypatch.setattr("soota.media.MediaService.from_descriptor", lambda *a, **k: pytest.fail("local mode network"))
    result = ingest(settings, str(article), want="images", provider="local")
    assert not result.ok and result.data["missing_outputs"] == ["images"]


def test_local_and_bundle_time_filter_preserve_original_evidence(settings, tmp_path):
    source = tmp_path / "captions.vtt"
    source.write_text("WEBVTT\n\n00:00.000 --> 00:01.000\nRemove this\n\n00:01.000 --> 00:02.000\nKeep this\n")
    original = ingest(settings, str(source), want="context")
    filtered = ingest(settings, original.data["output"], want="context", exclude_ranges=["0:1"])
    assert filtered.ok, filtered.model_dump()
    folder = Path(filtered.data["output"])
    assert "Remove this" not in (folder / "context.md").read_text()
    assert "Keep this" in (folder / "context.md").read_text()
    assert "Remove this" in (folder / "transcript.original.md").read_text()
    changed = ingest(settings, str(folder), want="context", exclude_ranges=["1:2"])
    assert changed.ok
    assert "Remove this" in (folder / "context.md").read_text()
    assert "Keep this" not in (folder / "context.md").read_text()


def test_local_caption_without_language_metadata_stays_unknown(settings, tmp_path):
    source = tmp_path / "source.srt"
    source.write_text("1\n00:00:00,000 --> 00:00:01,000\nUnknown source language\n")
    result = ingest(settings, str(source), want="context")
    assert result.ok
    assert json.loads(Path(result.data["manifest"]).read_text())["transcript"]["language"] is None
