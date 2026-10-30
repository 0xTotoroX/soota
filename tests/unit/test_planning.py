from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from typer.testing import CliRunner

from soota.cli import app
from soota.config import PriorityRule
from soota.errors import InputError, SourceError
from soota.models import TranscriptMode
from soota.planning.models import Preferences, Preset
from soota.planning.routes import preview
from soota.planning.service import create_plan, manage_presets, run_plan, select_plan, show_plan
from soota.planning.store import load_plan, plan_path, presets, save_preset
from soota.providers.priority import ordered_names
from soota.providers.quotes import quote_scope
from soota.providers.tikhub.client import ENDPOINT_INFO
from soota.routing import recognize
from soota.storage.jobs import JobStore
from soota.tasks.entry import prepare_task
from soota.tasks.policy import routing_snapshot, task_settings
from soota.tasks.runner import resume
from soota.tasks.state import load_spec

URL = "https://www.youtube.com/watch?v=Example0001"
SECOND = "https://www.youtube.com/watch?v=Example0002"


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "first.md"
    path.write_text("# Example\n\nOriginal source paragraph.\n", encoding="utf-8")
    return path


def local_plan(settings, source, **kwargs):
    return create_plan(settings, "读取原文", str(source), want="context", **kwargs).data["plan"]


def choice(plan, name):
    return next(row for row in plan["options"] if row["id"] == name)


def test_local_choice_preset_reuse_and_real_execution(settings, source):
    plan = local_plan(settings, source)
    assert plan["status"] == "awaiting_choice"
    assert not presets(settings) and not JobStore(settings.jobs_dir).list_jobs()
    assert run_plan(settings, plan["id"]).code == "confirmation_required"
    assert select_plan(settings, plan["id"], "free").code == "confirmation_required"
    assert not presets(settings)
    selected = select_plan(settings, plan["id"], "free", confirm=True, save_as="正文采集")
    assert selected.data["plan"]["selected"] == "free"
    first = run_plan(settings, plan["id"])
    assert first.ok and "Original source paragraph" in Path(first.data["context"]).read_text()
    again = run_plan(settings, plan["id"])
    assert again.code == "task_exists" and again.data["job_id"] == first.data["job_id"]
    assert len(JobStore(settings.jobs_dir).list_jobs()) == 1
    second = source.with_name("second.md")
    second.write_text("Second input with its own content.\n")
    proposed = local_plan(settings, second, output=settings.output_dir / "second")
    assert proposed["matches"] == ["preset:正文采集"]
    assert proposed["status"] == "awaiting_preset_confirmation"
    assert "是否沿用" in proposed["question"]
    assert run_plan(settings, proposed["id"]).code == "confirmation_required"
    assert len(JobStore(settings.jobs_dir).list_jobs()) == 1
    select_plan(settings, proposed["id"], "preset:正文采集", confirm=True)
    result = run_plan(settings, proposed["id"])
    assert result.ok and "Second input" in Path(result.data["context"]).read_text()
    old = load_spec(settings, first.data["job_id"])
    new = load_spec(settings, result.data["job_id"])
    assert old.execution_scope != new.execution_scope
    assert new.output == settings.output_dir / "second" and new.inputs[0].value == str(second)
    assert resume(settings, result.data["job_id"]).ok


def test_presets_store_preferences_not_input_paths_quotes_or_ledgers(settings, source):
    plan = local_plan(settings, source, exclude_ranges=["10:20"])
    select_plan(settings, plan["id"], "free", confirm=True)
    data = (settings.config_dir / "presets.json").read_text()
    for forbidden in (
        str(source),
        "execution_scope",
        "execution_context",
        "exclude_ranges",
        "quoted_steps_usd",
        "output",
    ):
        assert forbidden not in data
    assert not (settings.config_dir / "providers.json").exists()
    assert not (settings.config_dir / "policy.json").exists()
    if __import__("os").name != "nt":
        assert (settings.config_dir / "presets.json").stat().st_mode & 0o777 == 0o600
        assert plan_path(settings, plan["id"]).parent.stat().st_mode & 0o777 == 0o700


def test_current_caps_and_explicit_language_tighten_preset(settings, cached_tikhub_prices):
    plan = create_plan(settings, "字幕", URL, want="transcript", max_cost=2, weir_max_credits=20).data["plan"]
    select_plan(settings, plan["id"], "configured", confirm=True, save_as="字幕")
    proposed = create_plan(
        settings, "取文字", SECOND, want="transcript", max_cost=0.1, weir_max_credits=3, language="en"
    ).data["plan"]
    match = choice(proposed, "preset:字幕")
    assert match["preferences"]["max_cost"] == 0.1
    assert match["preferences"]["weir_max_credits"] == 3
    assert match["preferences"]["language"] == "en" and match["preferences"]["strict_language"]
    assert match["changes"]["max_cost"] == {"saved": 2, "this_run": 0.1}
    assert "opencli" not in match["report"]["routes"][0]["order"]


def test_different_outputs_platforms_and_operations_do_not_match(settings, cached_tikhub_prices):
    plan = create_plan(settings, "字幕", URL, want="transcript").data["plan"]
    select_plan(settings, plan["id"], "free", confirm=True)
    for url, want in ((SECOND, "audio"), ("https://example.test/article", "transcript")):
        assert not create_plan(settings, "字幕", url, want=want, offline=True).data["plan"]["matches"]
    native = create_plan(settings, "查询", operation="opencli.youtube.search", want="data").data["plan"]
    select_plan(settings, native["id"], "free", confirm=True)
    different = create_plan(settings, "查询", operation="opencli.bilibili.search", want="data").data["plan"]
    assert not different["matches"]


def test_saved_manual_routing_survives_current_config_and_task_resume(settings, source, monkeypatch):
    settings.routing.priorities = {"youtube": {"captions": PriorityRule(mode="manual", order=["yt-dlp", "opencli"])}}
    plan = local_plan(settings, source)
    select_plan(settings, plan["id"], "free", confirm=True)
    settings.routing.priorities = {}
    seen = []
    original = __import__("soota.tasks.runner", fromlist=["collect_text"]).collect_text

    def collect(effective, *args, **kwargs):
        seen.append(effective.routing.priorities["youtube"]["captions"].order)
        return original(effective, *args, **kwargs)

    monkeypatch.setattr("soota.tasks.runner.collect_text", collect)
    result = run_plan(settings, plan["id"])
    assert result.ok and seen == [["yt-dlp", "opencli"]]
    assert load_spec(settings, result.data["job_id"]).routing.priorities["youtube"]["captions"].mode == "manual"
    assert resume(settings, result.data["job_id"]).ok
    assert settings.routing.priorities == {}


def test_current_disabled_providers_and_payment_are_not_overridden(settings, cached_tikhub_prices):
    settings.payment.mode = "ask"
    plan = create_plan(settings, "字幕", URL, want="transcript", max_cost=1).data["plan"]
    select_plan(settings, plan["id"], "provider:tikhub", confirm=True)
    settings.routing.disabled_providers.add("tikhub")
    stopped = run_plan(settings, plan["id"])
    assert stopped.code == "plan_changed" and not stopped.data["plan"]["selected"]
    assert not JobStore(settings.jobs_dir).list_jobs()
    assert settings.payment.mode == "ask"
    assert not settings.monid.enabled and not settings.treg.enabled


def test_policy_change_requires_new_choice(settings, source):
    plan = local_plan(settings, source)
    select_plan(settings, plan["id"], "free", confirm=True)
    settings.payment.mode = "ask"
    assert run_plan(settings, plan["id"]).code == "plan_changed"
    assert select_plan(settings, plan["id"], "free", confirm=True).code == "ok"
    assert run_plan(settings, plan["id"]).ok


def test_quotes_refresh_only_on_demand_reuse_all_alternatives_and_detect_increase(settings, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("soota.providers.quotes.time.time", lambda: now[0])
    monkeypatch.setattr("soota.billing.time.sleep", lambda delay: None)
    settings.routing.price_ttl_seconds = 10
    calls = []
    price = [0.001]

    def request(method, path, params):
        assert path == ENDPOINT_INFO, "planning must not fetch content, balance or paid endpoints"
        calls.append(params["endpoint"])
        return {"data": {"endpoint_uri": params["endpoint"], "endpoint_cost": price[0]}}

    with quote_scope(tikhub_requester=request):
        plan = create_plan(settings, "字幕", URL, want="transcript", transcript=TranscriptMode.CAPTIONS_ONLY).data[
            "plan"
        ]
        assert len(calls) == len(set(calls)) == 2
        select_plan(settings, plan["id"], "provider:tikhub", confirm=True)
        assert len(calls) == 2
        now[0] += 11
        price[0] = 0.005
        assert len(calls) == 2  # 时间流逝不触发轮询。
        show_plan(settings, plan["id"])
        manage_presets(settings)
        assert len(calls) == 2
        stopped = run_plan(settings, plan["id"])
        assert stopped.code == "plan_changed" and len(calls) == 4
        assert not JobStore(settings.jobs_dir).list_jobs()
        assert select_plan(settings, plan["id"], "provider:tikhub", confirm=True).code == "ok"
        assert len(calls) == 4


def test_price_refresh_failure_blocks_paid_selection(settings, monkeypatch):
    def request(*args):
        raise SourceError("quote unavailable")

    with quote_scope(tikhub_requester=request):
        plan = create_plan(settings, "字幕", URL, want="transcript", provider="tikhub").data["plan"]
        assert not choice(plan, "configured")["report"]["selectable"]
        with pytest.raises(InputError, match="没有满足"):
            select_plan(settings, plan["id"], "configured", confirm=True)
    assert not presets(settings) and not JobStore(settings.jobs_dir).list_jobs()


def test_offline_and_zero_budget_never_fetch_quotes(settings, monkeypatch):
    monkeypatch.setattr(
        "soota.providers.tikhub.client.TikHubProvider.endpoint_price", lambda *a, **k: pytest.fail("network")
    )
    for options in ({"offline": True}, {"max_cost": 0}):
        plan = create_plan(settings, "字幕", URL, want="transcript", **options).data["plan"]
        assert plan["options"] and not JobStore(settings.jobs_dir).list_jobs()
    zero = create_plan(settings, "字幕", URL, want="transcript", max_cost=0).data["plan"]
    for option in zero["options"]:
        assert option["preferences"]["max_cost"] == 0
        for route in option["report"]["routes"]:
            assert not set(route["order"]) & {"tikhub", "monid", "treg"}


def test_quote_order_is_existing_cost_or_manual_order(settings, cached_tikhub_prices):
    for mode in ("cost", "manual"):
        settings.routing.priorities = {"youtube": {"captions": PriorityRule(mode=mode, order=["tikhub", "yt-dlp"])}}
        plan = create_plan(settings, "字幕", URL, want="transcript", transcript=TranscriptMode.CAPTIONS_ONLY).data[
            "plan"
        ]
        report = choice(plan, "configured")["report"]["routes"][0]
        expected = ordered_names(
            settings, recognize(URL), "captions", ["opencli-mcp", "opencli", "yt-dlp", "tikhub", "monid", "treg"]
        )
        assert report["order"] == expected


def test_native_paid_zero_budget_is_not_a_free_option(settings):
    plan = create_plan(
        settings,
        "元数据",
        want="data",
        max_cost=0,
        operation="tikhub.bilibili.web.fetch_one_video",
        params=["url=https://bilibili.com/video/BV1Example001"],
    ).data["plan"]
    assert not any(option["report"]["selectable"] for option in plan["options"])


def test_presets_are_not_updated_by_preview_or_failed_confirmation(settings, source):
    plan = local_plan(settings, source)
    select_plan(settings, plan["id"], "free", confirm=True)
    before = (settings.config_dir / "presets.json").read_bytes()
    next_plan = local_plan(settings, source)
    select_plan(settings, next_plan["id"], "preset:读取原文")
    assert (settings.config_dir / "presets.json").read_bytes() == before
    manage_presets(settings, name="读取原文", delete=True)
    with pytest.raises(InputError, match="已修改或删除"):
        select_plan(settings, next_plan["id"], "preset:读取原文", confirm=True)


def test_concurrent_preset_saves_do_not_lose_records_or_overwrite_same_name(settings, source):
    spec = prepare_task(settings, str(source), want="context")
    spec.routing = routing_snapshot(settings)
    pref = Preferences.from_spec(spec)

    def save(name):
        return save_preset(settings, Preset(name=name, scope="test", preferences=pref, updated_at="test"))

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(save, ["A", "B", "C", "D"]))
    assert {row.name for row in presets(settings)} == {"A", "B", "C", "D"}
    with pytest.raises(InputError, match="同名"):
        save("A")


@pytest.mark.parametrize("value", ["../elsewhere", "/tmp/not-a-plan", "invalid"])
def test_invalid_plan_id_rejected(settings, value):
    with pytest.raises(InputError):
        show_plan(settings, value)


def test_corrupt_presets_not_replaced(settings, source):
    settings.config_dir.mkdir(parents=True)
    path = settings.config_dir / "presets.json"
    path.write_text('{"version":99}')
    with pytest.raises(InputError, match="presets.json"):
        local_plan(settings, source)
    assert path.read_text() == '{"version":99}'


def test_cli_whole_local_flow_and_confirmation(settings, source, monkeypatch):
    monkeypatch.setattr("soota.config.load_settings", lambda: settings)
    runner = CliRunner()
    made = runner.invoke(app, ["plan", "create", str(source), "--goal", "原文", "--want", "context", "--json"])
    assert made.exit_code == 0, made.output
    ident = json.loads(made.output)["data"]["plan"]["id"]
    pending = runner.invoke(app, ["plan", "select", ident, "--option", "free", "--json"])
    assert json.loads(pending.output)["code"] == "confirmation_required"
    accepted = runner.invoke(
        app, ["plan", "select", ident, "--option", "free", "--confirm", "--save-as", "我的预设", "--json"]
    )
    assert accepted.exit_code == 0, accepted.output
    done = runner.invoke(app, ["plan", "run", ident, "--json"])
    assert done.exit_code == 0, done.output
    listed = runner.invoke(app, ["presets", "list", "--json"])
    assert json.loads(listed.output)["data"]["plan"]["presets"][0]["name"] == "我的预设"


def test_snapshot_does_not_enable_newly_enabled_provider(settings, source):
    spec = prepare_task(settings, str(source), want="context")
    spec.routing = routing_snapshot(settings)
    settings.treg.enabled = True
    effective = task_settings(settings, spec)
    assert "treg" in effective.routing.disabled_providers
    assert "treg" not in settings.routing.disabled_providers


def test_video_goals_do_not_offer_paid_audio_as_video(settings, cached_tikhub_prices):
    plan = create_plan(settings, "视频", URL, want="video").data["plan"]
    assert not any(option["id"] == "provider:tikhub" for option in plan["options"])


def test_weir_preview_keeps_credits_separate_and_free_plan_excludes_it(settings):
    settings.weir.enabled = True
    settings.weir.task_credits = 10
    plan = create_plan(settings, "正文", "https://x.com/example/status/123456789", want="context").data["plan"]
    free = choice(plan, "free")["report"]
    assert all("weir" not in route["order"] for route in free["routes"])
    configured = choice(plan, "configured")["report"]
    assert configured["weir_max_credits"] == 10 and any("credits" in message for message in configured["warnings"])


def test_collection_estimate_does_not_claim_whole_batch_price(settings, cached_tikhub_prices):
    spec = prepare_task(settings, "https://www.youtube.com/@example", want="context")
    spec.routing = routing_snapshot(settings)
    report = preview(settings, spec)
    assert any("集合尚未枚举" in warning for warning in report["warnings"])
    assert not JobStore(settings.jobs_dir).list_jobs()


def test_raw_video_data_quotes_inspect_without_caption_requests(settings, monkeypatch):
    calls = []

    def request(method, path, params):
        assert path == ENDPOINT_INFO
        calls.append(params["endpoint"])
        return {"data": {"endpoint_uri": params["endpoint"], "endpoint_cost": 0.001}}

    with quote_scope(tikhub_requester=request):
        plan = create_plan(settings, "元数据", URL, want="data").data["plan"]
    assert len(calls) == 1 and "get_video_info" in calls[0]
    assert choice(plan, "configured")["report"]["routes"][0]["capability"] == "inspect"


def test_unbound_paid_native_text_rejected_before_quote(settings, monkeypatch):
    monkeypatch.setattr(
        "soota.providers.tikhub.client.TikHubProvider.endpoint_price", lambda *a, **k: pytest.fail("unneeded quote")
    )
    plan = create_plan(
        settings,
        "原文",
        want="context",
        operation="tikhub.bilibili.web.fetch_one_video",
        params=["url=https://bilibili.com/video/BV1Example001"],
    ).data["plan"]
    assert all(not option["report"]["selectable"] for option in plan["options"])
    assert not JobStore(settings.jobs_dir).list_jobs()


def test_optional_unavailable_native_target_does_not_block_required_data(settings):
    plan = create_plan(settings, "搜索", operation="opencli.youtube.search", want="data", optional="transcript").data[
        "plan"
    ]
    report = choice(plan, "free")["report"]
    assert report["selectable"]
    assert any(not row["selectable"] and row["required"] is False for row in report["routes"])


def test_images_only_preview_includes_body_acquisition(settings):
    plan = create_plan(settings, "图片", "https://example.test/article", want="images").data["plan"]
    assert [row["capability"] for row in choice(plan, "free")["report"]["routes"]] == ["body", "images"]


def test_multi_input_quote_subtotal_respects_whole_budget(settings, monkeypatch, tmp_path):
    inputs = tmp_path / "inputs.txt"
    inputs.write_text(URL + "\n" + SECOND + "\n")

    def request(method, path, params):
        assert path == ENDPOINT_INFO
        return {"data": {"endpoint_uri": params["endpoint"], "endpoint_cost": 0.001}}

    with quote_scope(tikhub_requester=request):
        plan = create_plan(
            settings, "元数据", want="data", inputs_file=inputs, provider="tikhub", max_cost=0.0015
        ).data["plan"]
    report = choice(plan, "configured")["report"]
    assert report["quoted_steps_usd"] == 0.002 and not report["selectable"]


def test_planned_context_is_bound_to_source_and_audio_choice(settings, cached_tikhub_prices):
    from soota.tasks.policy import task_context

    plan = create_plan(settings, "字幕", URL, want="transcript").data["plan"]
    saved = load_plan(settings, plan["id"])
    auto = next(option.spec for option in saved.options if option.id == "configured")
    fixed = next(option.spec for option in saved.options if option.id == "provider:tikhub")
    assert task_context(settings, auto) != task_context(settings, fixed)
    other_audio = auto.model_copy(update={"audio_provider": "tikhub"})
    assert task_context(settings, auto) != task_context(settings, other_audio)


@pytest.mark.parametrize("provider", ["http", "downie"])
def test_media_resume_honors_current_provider_disable(settings, provider, monkeypatch, tmp_path):
    from soota.tasks.acquire import collect_media

    spec = prepare_task(settings, "https://example.test/audio.mp3", want="audio", provider=provider)
    spec.routing = routing_snapshot(settings)
    settings.routing.disabled_providers.add(provider)
    monkeypatch.setattr("soota.media.MediaService.from_descriptor", lambda *a, **k: pytest.fail("downloaded"))
    monkeypatch.setattr("soota.providers.downie.DownieProvider.get_audio", lambda *a, **k: pytest.fail("downie"))
    with pytest.raises(InputError, match="停用"):
        collect_media(task_settings(settings, spec), spec, spec.inputs[0], tmp_path / "work")


def test_catalog_child_is_bound_before_first_request_and_resumed(settings, cached_tikhub_prices, monkeypatch):
    from soota.tasks.enumeration import expand_inputs
    from soota.tasks.state import create_job

    spec = prepare_task(
        settings, "https://www.youtube.com/channel/UCAAAAAAAAAAAAAAAAAAAAAA", want="context", max_cost=1
    )
    job = create_job(settings, spec)

    def interrupted(*args, **kwargs):
        checkpoint = JobStore(settings.jobs_dir).get(job.id)["enumeration"]["0"]
        assert checkpoint["job_id"] == args[1].id
        raise KeyboardInterrupt

    monkeypatch.setattr("soota.catalog._execute", interrupted)
    with pytest.raises(KeyboardInterrupt):
        expand_inputs(settings, spec, job)
    child = job.enumeration["0"]["job_id"]
    assert child and job.enumeration["0"]["resumable"]
    monkeypatch.setattr("soota.tasks.enumeration.catalog", lambda *a, **k: pytest.fail("new child"))
    resumed = []

    def capture(settings, job_id, **kwargs):
        resumed.append(job_id)
        raise SourceError("simulated still pending")

    monkeypatch.setattr("soota.tasks.enumeration.resume_catalog", capture)
    with pytest.raises(SourceError):
        expand_inputs(settings, spec, job)
    assert resumed == [child]


@pytest.mark.parametrize("count,workers", [(1, 1), (2, 1), (2, 2)])
def test_optional_audio_cannot_consume_required_caption_budget(settings, monkeypatch, tmp_path, count, workers):
    from soota.models import MediaItem, RequestRecord, TranscriptDocument, TranscriptSegment
    from soota.storage.task_budget import reserve_active_task
    from soota.tasks.acquire import Collection
    from soota.tasks.entry import ingest

    events = []
    inputs = tmp_path / "inputs.txt"
    inputs.write_text("\n".join([URL, SECOND][:count]))

    def text(settings, spec, seed, work, **kwargs):
        # 模拟字幕链 3 次扣费，原预算足够完成全批必需字幕。
        for _ in range(3):
            reserve_active_task(RequestRecord(endpoint="captions", billed=True, estimated_cost_usd=0.001))
        events.append("captions")
        return Collection(
            MediaItem(source=recognize(seed.value), title="Example"),
            document=TranscriptDocument(
                status="ready",
                source="official",
                language="en",
                segments=[
                    TranscriptSegment(
                        segment_id="seg-0001", start_ms=0, end_ms=1000, text="Source caption.", source="official"
                    )
                ],
            ),
        )

    def audio(*args, **kwargs):
        events.append("optional-audio")
        assert events.count("captions") == count
        reserve_active_task(RequestRecord(endpoint="audio", billed=True, estimated_cost_usd=0.001))
        pytest.fail("optional audio must be stopped by the exhausted shared budget")

    monkeypatch.setattr("soota.tasks.runner.collect_text", text)
    monkeypatch.setattr("soota.tasks.runner.collect_media", audio)
    result = ingest(
        settings,
        inputs_file=inputs,
        want="transcript",
        optional="audio",
        provider="tikhub",
        transcript=TranscriptMode.CAPTIONS_ONLY,
        max_cost=0.003 * count,
        workers=workers,
    )
    assert result.ok, result.model_dump()
    assert result.data["reserved_cost_usd"] == 0.003 * count
    assert all(row["status"] == "completed" and not row["data"]["missing_outputs"] for row in result.data["items"])


def test_failed_required_phase_skips_optional_without_automatic_retry(settings, source, monkeypatch):
    from soota.tasks.entry import ingest

    calls = []

    def text(*args, **kwargs):
        calls.append("text")
        raise SourceError("source missing")

    monkeypatch.setattr("soota.tasks.runner.collect_text", text)
    monkeypatch.setattr("soota.tasks.runner.collect_media", lambda *a, **k: pytest.fail("optional dispatched"))
    result = ingest(settings, str(source), want="context", optional="audio")
    assert not result.ok and calls == ["text"]


def test_optional_video_does_not_make_paid_required_audio_unselectable(settings, cached_tikhub_prices):
    plan = create_plan(settings, "音频", URL, want="audio", optional="video", provider="tikhub", max_cost=0.1).data[
        "plan"
    ]
    report = choice(plan, "configured")["report"]
    assert report["selectable"]
    assert next(row for row in report["routes"] if row["capability"] == "audio")["required"]
    assert not next(row for row in report["routes"] if row["capability"] == "video")["required"]


def test_failed_optional_video_keeps_already_verified_required_audio(settings, monkeypatch, tmp_path):
    import hashlib

    from soota.media import MediaAsset
    from soota.tasks.acquire import MediaBatch
    from soota.tasks.entry import ingest

    path = tmp_path / "verified-audio.wav"
    path.write_bytes(b"synthetic verified media fixture")
    calls = []

    def media(settings, spec, *args, **kwargs):
        calls.append(spec.wants)
        if "video" in spec.wants:
            raise SourceError("optional video failed")
        return MediaBatch(
            [
                MediaAsset(
                    path=path,
                    kind="audio",
                    format="wav",
                    size=path.stat().st_size,
                    sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                    streams=[{"index": 0, "type": "audio"}],
                    verification={"decoded": True},
                    source_provider="yt-dlp",
                )
            ]
        )

    monkeypatch.setattr("soota.tasks.runner.collect_media", media)
    result = ingest(settings, URL, want="audio", optional="video", max_cost=0)
    assert result.ok, result.model_dump()
    assert calls == [{"audio"}, {"audio", "video"}]
    assert not result.data["missing_outputs"]
