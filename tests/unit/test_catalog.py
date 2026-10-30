import json
from pathlib import Path

import pytest

from soota.catalog import catalog, estimate_catalog, resume_catalog
from soota.errors import AuthBudgetError, InputError, SourceError
from soota.models import CatalogJob
from soota.providers.tikhub.catalog import BILIBILI_USER_VIDEOS, YOUTUBE_CHANNEL_ID, YOUTUBE_CHANNEL_VIDEOS
from soota.storage.jobs import JobStore

pytestmark = pytest.mark.usefixtures("cached_tikhub_prices")

CHANNEL_ID = "UCAAAAAAAAAAAAAAAAAAAAAA"
CHANNEL = f"https://www.youtube.com/channel/{CHANNEL_ID}"
IDS = ["dQw4w9WgXcQ", "Example0001", "Example0002", "Example0003"]


@pytest.fixture(autouse=True)
def no_live_key(monkeypatch):
    monkeypatch.setattr("soota.providers.tikhub.client.load_tikhub_api_key", lambda: None)


def page(ids, cursor=None, *, title=None):
    data = {
        "videos": [{"video_id": ident, "title": f"Video {ident}"} for ident in ids],
        "continuation_token": cursor,
    }
    if title:
        data["channel"] = {"id": CHANNEL_ID, "name": title}
    return {"code": 200, "data": data}


def fail_request(*args, **kwargs):
    raise AssertionError("Provider request must not happen")


def read_items(result):
    return json.loads(Path(result.data["catalog"]).read_text())["items"]


def test_dry_run_is_offline_and_creates_no_job(settings) -> None:
    result = catalog(settings, "https://youtube.com/@example", dry_run=True, requester=fail_request)
    assert result.ok
    assert result.data["request_limit"] == 11
    assert result.data["estimated_cost_usd"] == 0.011
    assert not settings.data_dir.exists()
    assert not settings.cache_dir.exists()
    assert not settings.output_dir.exists()


@pytest.mark.parametrize(
    "kwargs",
    [{"max_items": 0}, {"max_pages": 0}, {"max_cost": -1}, {"max_cost": float("nan")}, {"max_cost": float("inf")}],
)
def test_invalid_limits_rejected_before_job_creation(settings, kwargs) -> None:
    with pytest.raises(InputError):
        catalog(settings, CHANNEL, requester=fail_request, **kwargs)
    assert not settings.data_dir.exists()


def test_cost_cap_is_optional_with_explicit_auto_policy(settings) -> None:
    result = catalog(settings, CHANNEL, requester=lambda *args: page(IDS[:1]))
    assert result.ok
    assert result.data["max_cost"] is None


def test_unsupported_playlist_has_no_fake_zero_cost_estimate(settings) -> None:
    with pytest.raises(SourceError):
        estimate_catalog(settings, "https://www.youtube.com/playlist?list=PLexample")


def test_page_remainder_resumes_without_request_or_rebilling(settings) -> None:
    calls = []

    def request(method, path, params):
        calls.append(path)
        return page(IDS[:3], title="Example")

    first = catalog(settings, CHANNEL, max_items=1, max_cost=0.001, requester=request)
    assert first.exit_code == 2
    assert first.data["item_count"] == 1
    assert first.data["pending_items"] == 2
    assert len(read_items(first)) == 1
    resumed = resume_catalog(settings, first.data["job_id"], max_items=3, requester=fail_request)
    assert resumed.ok
    assert resumed.data["requests_this_run"] == 0
    assert resumed.data["reserved_cost_usd"] == 0.001
    assert [item["source"]["id"] for item in read_items(resumed)] == IDS[:3]
    assert calls == [YOUTUBE_CHANNEL_VIDEOS]
    again = resume_catalog(settings, first.data["job_id"], requester=fail_request)
    assert again.ok and again.data["requests_this_run"] == 0


def test_budget_is_checked_between_pages_and_kept_across_resume(settings) -> None:
    cursors = []

    def request(method, path, params):
        cursor = params.get("continuation_token")
        cursors.append(cursor)
        return page([IDS[0]], "p2", title="Channel") if cursor is None else page([IDS[1]])

    first = catalog(settings, CHANNEL, max_cost=0.001, requester=request)
    assert first.exit_code == 2
    assert first.data["error"]["code"] == "auth_budget"
    assert cursors == [None]
    stopped = resume_catalog(settings, first.data["job_id"], requester=fail_request)
    assert stopped.data["requests_this_run"] == 0
    finished = resume_catalog(settings, first.data["job_id"], max_cost=0.002, requester=request)
    assert finished.ok
    assert cursors == [None, "p2"]
    assert finished.data["reserved_cost_usd"] == 0.002
    assert all(item["creator"] == "Channel" for item in read_items(finished))


def test_zero_budget_and_unknown_price_make_no_request(settings, monkeypatch) -> None:
    zero = catalog(settings, CHANNEL, max_cost=0, requester=fail_request)
    assert zero.exit_code == 4
    assert zero.data["request_count"] == 0

    def no_quote(*args, **kwargs):
        raise AuthBudgetError("price unavailable")

    monkeypatch.setattr("soota.providers.tikhub.client.TikHubProvider.endpoint_price", no_quote)
    unknown = catalog(settings, CHANNEL, provider="tikhub", max_cost=1, requester=fail_request)
    assert unknown.exit_code == 4
    assert unknown.data["request_count"] == 0


def test_duplicate_items_and_repeating_cursor_preserve_successes(settings) -> None:
    calls = []

    def request(method, path, params):
        cursor = params.get("continuation_token")
        calls.append(cursor)
        return page([IDS[0], IDS[0]], "p2") if cursor is None else page(IDS[:2], "p2")

    result = catalog(settings, CHANNEL, max_cost=0.1, requester=request)
    assert result.exit_code == 2
    assert result.data["error"]["code"] == "contract"
    assert [item["source"]["id"] for item in read_items(result)] == IDS[:2]
    assert calls == [None, "p2"]
    again = resume_catalog(settings, result.data["job_id"], requester=fail_request)
    assert again.data["requests_this_run"] == 0
    assert again.data["error"]["code"] == "contract"


def test_interrupt_preserves_page_and_resumes_at_next_cursor(settings) -> None:
    calls = []

    def request(method, path, params):
        calls.append(params.get("continuation_token"))
        if len(calls) == 2:
            raise KeyboardInterrupt
        return page([IDS[0]], "p2") if len(calls) == 1 else page([IDS[1]])

    first = catalog(settings, CHANNEL, max_cost=0.003, requester=request)
    assert first.data["stop_reason"] == "interrupted"
    assert first.data["item_count"] == 1
    assert first.data["reserved_cost_usd"] == 0.002  # Unknown request outcomes keep their reservation.
    resumed = resume_catalog(settings, first.data["job_id"], requester=request)
    assert resumed.ok
    assert calls == [None, "p2", "p2"]
    assert resumed.data["reserved_cost_usd"] == 0.003


def test_incremental_skips_seen_ids_but_not_new_items_after_pinned_video(settings) -> None:
    initial = catalog(settings, CHANNEL, max_cost=0.001, requester=lambda *a: page(IDS[:2]))
    assert initial.ok
    calls = []

    def request(method, path, params):
        cursor = params.get("continuation_token")
        calls.append(cursor)
        return page([IDS[0], IDS[2]], "p2") if cursor is None else page(IDS[:2], "p3")

    update = catalog(settings, CHANNEL, incremental=True, max_cost=0.002, requester=request)
    assert update.ok
    assert update.data["stop_reason"] == "caught_up"
    assert [item["source"]["id"] for item in read_items(update)] == [IDS[2]]
    assert calls == [None, "p2"]
    assert len(read_items(initial)) == 2  # Old snapshots are retained.


def test_page_limit_stops_duplicate_only_pages_and_can_be_extended(settings) -> None:
    calls = []

    def request(method, path, params):
        calls.append(params.get("continuation_token"))
        return page([IDS[0]], f"p{len(calls) + 1}")

    result = catalog(settings, CHANNEL, max_pages=2, max_cost=1, requester=request)
    assert result.data["stop_reason"] == "max_pages"
    assert result.data["item_count"] == 1
    assert len(calls) == 2
    resumed = resume_catalog(settings, result.data["job_id"], max_pages=3, requester=request)
    assert resumed.data["pages_fetched"] == 3
    assert len(calls) == 3


def test_handle_resolution_is_cached_and_counts_toward_budget(settings) -> None:
    calls = []

    def request(method, path, params):
        calls.append(path)
        if path == YOUTUBE_CHANNEL_ID:
            return {"data": {"channel_id": CHANNEL_ID}}
        return page([IDS[0]])

    first = catalog(settings, "https://youtube.com/@example", max_cost=0.001, requester=request)
    assert first.exit_code == 4
    assert calls == [YOUTUBE_CHANNEL_ID]
    second = catalog(settings, "https://youtube.com/@example", max_cost=0.001, requester=request)
    assert second.ok
    assert calls == [YOUTUBE_CHANNEL_ID, YOUTUBE_CHANNEL_VIDEOS]


def test_bilibili_resume_keeps_page_size_when_max_items_changes(settings) -> None:
    pages = []

    def request(method, path, params):
        assert path == BILIBILI_USER_VIDEOS
        pages.append((params["pn"], params["ps"]))
        bvid = "BV1EXAM00000" if params["pn"] == 1 else "BV1M1421t7hT"
        return {"data": {"archives": [{"bvid": bvid, "title": "Video"}], "page": {"total": 2}}}

    first = catalog(
        settings, "https://space.bilibili.com/123/upload/video", max_items=1, max_cost=0.002, requester=request
    )
    resumed = resume_catalog(settings, first.data["job_id"], max_items=2, requester=request)
    assert resumed.ok
    assert pages == [(1, 1), (2, 1)]


def test_output_failure_republishes_saved_items_without_paid_calls(settings, monkeypatch) -> None:
    from soota.render.catalog import write_catalog

    def cannot_write(*args):
        raise OSError("test failure")

    monkeypatch.setattr("soota.catalog.write_catalog", cannot_write)
    first = catalog(settings, CHANNEL, max_cost=0.001, requester=lambda *a: page([IDS[0]]))
    assert first.exit_code == 7
    assert "output" not in first.data
    monkeypatch.setattr("soota.catalog.write_catalog", write_catalog)
    resumed = resume_catalog(settings, first.data["job_id"], requester=fail_request)
    assert resumed.ok
    assert len(read_items(resumed)) == 1


def test_legacy_jobs_remain_readable_but_are_not_misrepresented_as_resumable(settings) -> None:
    store = JobStore(settings.jobs_dir)
    legacy = store.create({"status": "completed", "source": {"url": CHANNEL}})
    assert store.get(legacy["id"])["status"] == "completed"
    with pytest.raises(InputError, match="只支持目录"):
        resume_catalog(settings, legacy["id"], requester=fail_request)


def test_job_lock_prevents_parallel_resume(settings) -> None:
    first = catalog(settings, CHANNEL, max_cost=0.001, requester=lambda *a: page([IDS[0]]))
    store = JobStore(settings.jobs_dir)
    with store.lock(first.data["job_id"]), pytest.raises(SourceError, match="另一个进程"):
        resume_catalog(settings, first.data["job_id"], requester=fail_request)


def test_saved_job_is_valid_and_public_urls_have_no_incidental_query_secrets(settings) -> None:
    result = catalog(settings, CHANNEL + "?token=fixture-secret", max_cost=0.001, requester=lambda *a: page([IDS[0]]))
    record = JobStore(settings.jobs_dir).get(result.data["job_id"])
    job = CatalogJob.model_validate(record)
    assert job.source.url == CHANNEL
    assert "fixture-secret" not in json.dumps(record)
    with pytest.raises(InputError):
        resume_catalog(settings, "../../outside", requester=fail_request)


def test_opaque_cursor_survives_pending_current_and_visited_checkpoints(settings) -> None:
    token = "sk-opaque-pagination-token"
    calls = []

    def request(method, path, params):
        cursor = params.get("continuation_token")
        calls.append(cursor)
        return page(IDS[:2], token) if cursor is None else page([IDS[2]])

    first = catalog(settings, CHANNEL, max_items=1, max_cost=0.001, requester=request)
    store = JobStore(settings.jobs_dir)
    job_id = first.data["job_id"]
    assert store.get(job_id)["pending_page"]["next_cursor"] == token
    second = resume_catalog(settings, job_id, max_items=10, requester=fail_request)
    assert second.data["error"]["code"] == "auth_budget"
    assert store.get(job_id)["cursor"] == token
    finished = resume_catalog(settings, job_id, max_cost=0.002, requester=request)
    assert finished.ok
    assert calls == [None, token]
    assert store.get(job_id)["visited_cursors"] == [None, token]
    assert token not in Path(finished.data["catalog"]).read_text()
    assert token not in Path(finished.data["content"]).read_text()
