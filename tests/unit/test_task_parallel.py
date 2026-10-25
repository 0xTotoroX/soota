from __future__ import annotations

import json
import signal
import threading
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from pathlib import Path

import pytest
from typer.testing import CliRunner

from soota.cli import app
from soota.content.native_types import NativeContent, NativeOperation
from soota.errors import InputError, SourceError
from soota.models import RequestRecord
from soota.storage.jobs import JobStore
from soota.storage.task_budget import TaskBudget, active_task_identity, reserve_active_task
from soota.tasks import runner, scheduler
from soota.tasks.entry import ingest
from soota.tasks.models import TaskJob
from soota.tasks.runner import resume
from soota.tasks.state import load_spec, save_job, task_folder


def local_batch(tmp_path, count):
    paths = [tmp_path / f"source-{i}.txt" for i in range(count)]
    for i, path in enumerate(paths):
        path.write_text(f"Source {i}.\n\nEvidence {i}.\n")
    inputs = tmp_path / "inputs.json"
    inputs.write_text(json.dumps([str(path) for path in paths]))
    return inputs, paths


@pytest.mark.parametrize("workers", [1, 2, 4, 8])
def test_barrier_proves_parallelism_bound_and_order(settings, tmp_path, monkeypatch, workers):
    inputs, paths = local_batch(tmp_path, workers * 2)
    original = runner.collect_text
    barrier = threading.Barrier(workers, timeout=10)
    lock = threading.Lock()
    active = peak = 0
    observed_context = ContextVar("parallel_test_context", default=None)
    observed_context.set("parent-value")
    main = threading.get_ident()
    identities = set()
    threads = set()

    def collect(*args, **kwargs):
        nonlocal active, peak
        assert observed_context.get() == "parent-value"
        identity = active_task_identity()
        assert identity is not None
        with lock:
            identities.add(identity)
            threads.add(threading.get_ident())
            active += 1
            peak = max(peak, active)
        try:
            # A serial implementation with workers > 1 cannot pass this gate.
            barrier.wait()
            result = original(*args, **kwargs)
            barrier.wait()
            return result
        finally:
            with lock:
                active -= 1

    expand = runner.expand_inputs

    def ordered_enumeration(*args):
        assert threading.get_ident() == main
        return expand(*args)

    monkeypatch.setattr(runner, "expand_inputs", ordered_enumeration)
    monkeypatch.setattr(runner, "collect_text", collect)
    result = ingest(settings, inputs_file=inputs, want="context", workers=workers)
    assert result.ok, result.model_dump()
    assert peak == workers and active == 0
    assert len(threads) == workers and len(identities) == 1
    assert (main in threads) == (workers == 1)
    assert result.data["workers"] == workers
    assert [row["input_index"] for row in result.data["items"]] == list(range(len(paths)))
    for i, row in enumerate(result.data["items"]):
        assert f"Evidence {i}." in Path(row["data"]["context"]).read_text()
        assert row["status"] == "completed" and row["steps"]["content"]["status"] == "completed"
    saved = JobStore(settings.jobs_dir).get(result.data["job_id"])
    assert saved["items"] == result.data["items"]
    assert saved["status"] == "completed"
    assert load_spec(settings, saved["id"]).workers == workers


def test_checkpoint_merges_rows_and_serializes_disk_writes(settings, tmp_path, monkeypatch):
    inputs, _ = local_batch(tmp_path, 2)
    first = ingest(settings, inputs_file=inputs, want="context")
    job = TaskJob.model_validate(JobStore(settings.jobs_dir).get(first.data["job_id"]))
    rows = [row.model_copy(deep=True) for row in job.items]
    for i, row in enumerate(rows):
        row.data["checkpoint"] = i
    entered = threading.Event()
    release = threading.Event()
    second_entered = threading.Event()
    second_started = threading.Event()
    original = JobStore.save
    calls = 0

    def slow_save(store, record, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            assert release.wait(5)
        else:
            second_entered.set()
        original(store, record, **kwargs)

    monkeypatch.setattr(JobStore, "save", slow_save)

    def second():
        second_started.set()
        save_job(settings, job, item=rows[1])

    with ThreadPoolExecutor(max_workers=2) as pool:
        one = pool.submit(save_job, settings, job, item=rows[0])
        assert entered.wait(5)
        two = pool.submit(second)
        assert second_started.wait(5)
        try:
            assert not second_entered.wait(0.05)
        finally:
            release.set()
        one.result()
        two.result()
    rows[0].data["checkpoint"] = "later private edit"
    saved = JobStore(settings.jobs_dir).get(job.id)
    assert [row["data"]["checkpoint"] for row in saved["items"]] == [0, 1]
    assert [row.data["checkpoint"] for row in job.items] == [0, 1]


def test_errors_and_changed_inputs_only_block_their_own_items(settings, tmp_path, monkeypatch):
    inputs, paths = local_batch(tmp_path, 4)
    original = runner.collect_text

    def collect(config, spec, seed, *args, **kwargs):
        if seed.value == str(paths[0]):
            raise InputError("known input failure")
        if seed.value == str(paths[1]):
            raise RuntimeError("synthetic-secret-must-not-be-published")
        return original(config, spec, seed, *args, **kwargs)

    monkeypatch.setattr(runner, "collect_text", collect)
    first = ingest(settings, inputs_file=inputs, want="context", workers=3)
    assert first.data["job_status"] == "partial"
    assert [row["status"] for row in first.data["items"]] == ["blocked", "failed", "completed", "completed"]
    assert "synthetic-secret" not in first.model_dump_json()
    assert first.data["items"][1]["steps"]["content"]["error"]["details"]["exception_type"] == "RuntimeError"
    paths[0].write_text("Changed after initial task.")
    calls = []

    def recover(config, spec, seed, *args, **kwargs):
        calls.append(seed.value)
        return original(config, spec, seed, *args, **kwargs)

    monkeypatch.setattr(runner, "collect_text", recover)
    second = resume(settings, first.data["job_id"], workers=2)
    assert [row["status"] for row in second.data["items"]] == ["blocked", "completed", "completed", "completed"]
    assert calls == [str(paths[1])]
    assert "已变化" in second.data["items"][0]["errors"][0]["message"]


def test_budget_competition_reserves_before_send_and_resume_does_not_rebill(settings, tmp_path, monkeypatch):
    inputs = tmp_path / "native-inputs.json"
    inputs.write_text(
        json.dumps([{"kind": "operation", "operation": "tikhub.synthetic", "params": {"id": i}} for i in range(4)])
    )
    barrier = threading.Barrier(4, timeout=10)
    sent = []
    first_run = True
    lock = threading.Lock()

    def request(config, spec, seed, work):
        if first_run:
            barrier.wait()
        record = RequestRecord(endpoint=f"synthetic-{seed.params['id']}", billed=True, estimated_cost_usd=0.1)
        reserve_active_task(record)
        # Read the actual file at the simulated send boundary, not just memory.
        identity = active_task_identity()
        assert identity is not None
        saved = TaskBudget(Path(identity), spec.max_cost).snapshot()
        assert any(row["endpoint"] == record.endpoint for row in saved["requests"])
        assert float(saved["reserved_cost_usd"]) <= spec.max_cost
        with lock:
            sent.append(seed.params["id"])
        return NativeOperation(raw={"id": seed.params["id"]}, provider="tikhub", operation=seed.operation, workdir=work)

    monkeypatch.setattr(runner, "native_operation", request)
    first = ingest(settings, inputs_file=inputs, want="data", provider="tikhub", workers=4, max_cost=0.3)
    assert first.data["job_status"] == "partial" and len(sent) == 3
    assert first.data["reserved_cost_usd"] == 0.3
    assert sum(row["status"] == "completed" for row in first.data["items"]) == 3
    first_run = False
    second = resume(settings, first.data["job_id"], workers=2)
    assert second.data["reserved_cost_usd"] == 0.3 and len(sent) == 3
    assert not second.ok
    third = resume(settings, first.data["job_id"], max_cost=0.4, workers=4)
    assert third.ok and third.data["reserved_cost_usd"] == 0.4
    assert sorted(sent) == list(range(4))
    assert resume(settings, first.data["job_id"]).ok and len(sent) == 4


def test_worker_override_is_persisted_and_old_requests_default_to_one(settings, tmp_path, monkeypatch):
    inputs, _ = local_batch(tmp_path, 2)
    first = ingest(settings, inputs_file=inputs, want="context", workers=2)
    request = task_folder(settings, first.data["job_id"]) / "request.json"
    old = json.loads(request.read_text())
    old.pop("workers")
    request.write_text(json.dumps(old))
    monkeypatch.setattr(runner, "collect_text", lambda *a, **k: pytest.fail("valid source reacquired"))
    assert resume(settings, first.data["job_id"]).data["workers"] == 1
    assert resume(settings, first.data["job_id"], workers=3).data["workers"] == 3
    assert resume(settings, first.data["job_id"]).data["workers"] == 3
    saved = request.read_bytes()
    for invalid in (0, 9, -1, True, 2.5):
        with pytest.raises(InputError):
            resume(settings, first.data["job_id"], workers=invalid)
        with pytest.raises(InputError):
            ingest(settings, inputs_file=inputs, want="context", workers=invalid)
        assert request.read_bytes() == saved


def test_workers_only_resume_does_not_repeat_completed_enumeration(settings, tmp_path, monkeypatch):
    inputs, _ = local_batch(tmp_path, 2)
    first = ingest(settings, inputs_file=inputs, want="context", workers=2)
    job = TaskJob.model_validate(JobStore(settings.jobs_dir).get(first.data["job_id"]))
    job.enumeration = {"0": {"stop_reason": "end", "completeness": "complete"}}
    save_job(settings, job)
    monkeypatch.setattr(runner, "expand_inputs", lambda *a: pytest.fail("enumeration repeated"))
    assert resume(settings, job.id, workers=4).ok


def test_unbound_context_survives_resume_but_not_a_new_task(settings, monkeypatch):
    monkeypatch.delenv("OPENCLI_PROFILE", raising=False)
    calls = []

    def capture(*args, **kwargs):
        calls.append(1)
        return NativeContent(
            raw=[],
            body="Actual source body.",
            title="Synthetic page",
            source_url="https://example.test/page",
            provider="opencli",
            scope="page",
            operation="web/read",
        )

    monkeypatch.setattr("soota.tasks.acquire.capture_content", capture)
    first = ingest(settings, "https://example.test/page", want="context", workers=2)
    assert first.ok
    assert resume(settings, first.data["job_id"], workers=4).ok
    assert resume(settings, first.data["job_id"]).ok
    assert len(calls) == 1
    # A separate unbound task must not inherit an unverifiable account cache.
    assert ingest(settings, "https://example.test/page", want="context").ok
    assert len(calls) == 2


def test_duplicate_sources_and_bundle_aliases_never_write_together(settings, tmp_path, monkeypatch):
    inputs, paths = local_batch(tmp_path, 2)
    first = ingest(settings, str(paths[0]), want="context")
    folder = Path(first.data["output"])
    alias = tmp_path / "bundle-alias"
    alias.symlink_to(folder, target_is_directory=True)
    inputs.write_text(json.dumps([str(paths[0]), str(alias), str(paths[0]), str(folder), str(paths[1])]))
    original = runner._run_item_locked
    barrier = threading.Barrier(2, timeout=10)
    active = set()
    order = []
    lock = threading.Lock()

    def run(config, spec, job, row, seed, target):
        key = target.resolve()
        with lock:
            assert key not in active
            active.add(key)
            order.append((key, seed.kind))
        try:
            if seed.kind == "file":
                barrier.wait()
            return original(config, spec, job, row, seed, target)
        finally:
            with lock:
                active.remove(key)

    monkeypatch.setattr(runner, "_run_item_locked", run)
    result = ingest(settings, inputs_file=inputs, want="context", workers=3)
    assert result.ok, result.model_dump()
    assert result.data["item_count"] == 3
    assert [kind for key, kind in order if key == folder.resolve()] == ["file", "bundle"]
    assert not active


def test_interrupt_drains_workers_before_pause_and_resume_lock_release(settings, tmp_path, monkeypatch):
    inputs, _ = local_batch(tmp_path, 5)
    started = threading.Barrier(3, timeout=10)
    release = threading.Event()
    original = runner.collect_text
    original_wait = scheduler.wait
    original_shutdown = ThreadPoolExecutor.shutdown
    sent = []
    finished = []

    def collect(config, spec, seed, *args, **kwargs):
        sent.append(seed.value)
        started.wait()
        assert release.wait(10)
        result = original(config, spec, seed, *args, **kwargs)
        finished.append(seed.value)
        return result

    def interrupt(*args, **kwargs):
        started.wait()
        raise KeyboardInterrupt

    def drain(pool, *args, **kwargs):
        assert signal.getsignal(signal.SIGINT) == signal.SIG_IGN
        # Repeated Ctrl-C during draining cannot let the caller escape early.
        signal.raise_signal(signal.SIGINT)
        job = JobStore(settings.jobs_dir).list_jobs()[0]
        with pytest.raises(SourceError, match="正在"):
            resume(settings, job["id"])
        release.set()
        return original_shutdown(pool, *args, **kwargs)

    monkeypatch.setattr(runner, "collect_text", collect)
    monkeypatch.setattr(scheduler, "wait", interrupt)
    monkeypatch.setattr(ThreadPoolExecutor, "shutdown", drain)
    old_handler = signal.getsignal(signal.SIGINT)
    with pytest.raises(KeyboardInterrupt):
        ingest(settings, inputs_file=inputs, want="context", workers=2)
    assert signal.getsignal(signal.SIGINT) == old_handler
    assert len(sent) == len(finished) == 2
    assert not any(t.name.startswith("soota-item") for t in threading.enumerate())
    job = JobStore(settings.jobs_dir).list_jobs()[0]
    assert job["status"] == "paused"
    assert [row["status"] for row in job["items"]] == ["completed", "completed", "pending", "pending", "pending"]
    monkeypatch.setattr(scheduler, "wait", original_wait)
    monkeypatch.setattr(ThreadPoolExecutor, "shutdown", original_shutdown)

    def remaining(config, spec, seed, *args, **kwargs):
        assert seed.value not in sent
        sent.append(seed.value)
        return original(config, spec, seed, *args, **kwargs)

    monkeypatch.setattr(runner, "collect_text", remaining)
    assert resume(settings, job["id"]).ok
    assert len(sent) == 5


def test_cli_workers_validation_plan_and_resume(settings, tmp_path, monkeypatch):
    inputs, _ = local_batch(tmp_path, 2)
    monkeypatch.setattr("soota.config.load_settings", lambda: settings)
    cli = CliRunner()
    base = ["ingest", "--inputs-file", str(inputs), "--want", "context", "--json"]
    plan = cli.invoke(app, [*base, "--workers", "2", "--dry-run"])
    assert plan.exit_code == 0, plan.output
    assert json.loads(plan.stdout)["data"]["plan"]["workers"] == 2
    for invalid in ("0", "9"):
        assert cli.invoke(app, [*base, "--workers", invalid]).exit_code != 0
    first = cli.invoke(app, [*base, "--workers", "2"])
    assert first.exit_code == 0, first.output
    job_id = json.loads(first.stdout)["data"]["job_id"]
    result = cli.invoke(app, ["jobs", "resume", job_id, "--workers", "3", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["data"]["workers"] == 3
