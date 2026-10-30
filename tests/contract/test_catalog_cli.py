import json
from pathlib import Path

import httpx
import pytest
import tikhub
from typer.testing import CliRunner

from soota.cli import app
from soota.providers.tikhub.catalog import BILIBILI_USER_VIDEOS, YOUTUBE_CHANNEL_ID, YOUTUBE_CHANNEL_VIDEOS
from soota.storage.jobs import JobStore
from tests.conftest import load_fixture

runner = CliRunner()
CHANNEL_ID = "UCAAAAAAAAAAAAAAAAAAAAAA"
CHANNEL = f"https://www.youtube.com/channel/{CHANNEL_ID}"


@pytest.fixture(autouse=True)
def isolated_cli(settings, monkeypatch):
    settings.config_dir.mkdir(parents=True)
    (settings.config_dir / "payment-policy.json").write_text(settings.payment.model_dump_json())
    monkeypatch.setenv("SOOTA_CONFIG_DIR", str(settings.config_dir))
    monkeypatch.setenv("SOOTA_CACHE_DIR", str(settings.cache_dir))
    monkeypatch.setenv("SOOTA_DATA_DIR", str(settings.data_dir))
    monkeypatch.setenv("SOOTA_OUTPUT_DIR", str(settings.output_dir))
    monkeypatch.setattr("soota.providers.tikhub.client.load_tikhub_api_key", lambda: "fixture-key")


def test_catalog_dry_run_and_input_errors_never_construct_sdk(settings, monkeypatch) -> None:
    def forbidden(**kwargs):
        pytest.fail("Offline or invalid commands must not construct an SDK client")

    monkeypatch.setattr(tikhub, "TikHub", forbidden)
    result = runner.invoke(app, ["catalog", "https://youtube.com/@example", "--dry-run", "--json"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["data"]["estimated_cost_usd"] == 0.011
    estimate = runner.invoke(app, ["estimate", CHANNEL, "--mode", "catalog", "--max-pages", "2", "--json"])
    assert estimate.exit_code == 0
    assert json.loads(estimate.stdout)["data"]["estimated_cost_usd"] == 0.002
    unsupported = runner.invoke(
        app, ["catalog", "https://www.youtube.com/playlist?list=PLexample", "--dry-run", "--json"]
    )
    assert unsupported.exit_code == 5
    assert json.loads(unsupported.stdout)["code"] == "source"
    assert not settings.data_dir.exists()
    assert not settings.output_dir.exists()


def test_youtube_cli_budget_resume_and_incremental_through_real_sdk(
    settings, sdk_transport, cached_tikhub_prices
) -> None:
    cached_tikhub_prices("fixture-key")
    requests = []

    def respond(request):
        requests.append((request.url.path, dict(request.url.params)))
        if request.url.path == YOUTUBE_CHANNEL_ID:
            return httpx.Response(200, json={"code": 200, "data": {"channel_id": CHANNEL_ID}})
        assert request.url.path == YOUTUBE_CHANNEL_VIDEOS
        assert request.url.params["channel_id"] == CHANNEL_ID
        assert request.url.params["need_format"] == "true"
        assert request.url.params["language_code"] == "zh"
        fixture = load_fixture("youtube_catalog.json")
        if "continuation_token" in request.url.params:
            assert request.url.params["continuation_token"] == "fixture-page-2"
            fixture["data"].pop("channel")
            fixture["data"]["videos"] = [{"video_id": "Example0003", "title": "Third video"}]
            fixture["data"]["continuation_token"] = None
        return httpx.Response(200, json=fixture)

    sdk_transport(respond)
    first = runner.invoke(app, ["catalog", "https://youtube.com/@example", "--max-cost", "0.002", "--json"])
    assert first.exit_code == 2
    data = json.loads(first.stdout)["data"]
    assert data["item_count"] == 2
    assert data["request_count"] == 2  # Alias plus first page; second page is blocked before HTTP.
    assert len(requests) == 2
    job_id = data["job_id"]
    resumed = runner.invoke(app, ["jobs", "resume", job_id, "--max-cost", "0.003", "--json"])
    assert resumed.exit_code == 0
    data = json.loads(resumed.stdout)["data"]
    assert data["item_count"] == 3
    assert data["reserved_cost_usd"] == 0.003
    assert data["requests_this_run"] == 1
    assert len(requests) == 3
    artifact = json.loads(Path(data["catalog"]).read_text())
    assert len(artifact["items"]) == 3
    assert "Third video" in Path(data["content"]).read_text()
    assert "fixture-secret" not in json.dumps(artifact)
    listed = runner.invoke(app, ["jobs", "list", "--json"])
    listing = json.loads(listed.stdout)["data"]["jobs"][0]
    assert listing["id"] == job_id
    assert "pending_page" not in listing and "items" not in listing
    human = runner.invoke(app, ["jobs", "list"])
    assert job_id in human.stdout
    update = runner.invoke(
        app, ["catalog", "https://youtube.com/@example", "--incremental", "--max-cost", "0.001", "--json"]
    )
    assert update.exit_code == 0
    assert json.loads(update.stdout)["data"]["stop_reason"] == "caught_up"
    assert json.loads(update.stdout)["data"]["item_count"] == 0
    assert len(requests) == 4  # Cached alias; one page detects no new videos.


def test_bilibili_cli_page_limit_and_resume_keep_page_shape(sdk_transport, cached_tikhub_prices) -> None:
    cached_tikhub_prices("fixture-key")
    requests = []

    def respond(request):
        assert request.url.path == BILIBILI_USER_VIDEOS
        params = dict(request.url.params)
        requests.append(params)
        fixture = load_fixture("bilibili_catalog.json")
        if params["pn"] == "2":
            fixture["data"]["archives"][0]["bvid"] = "BV1M1421t7hT"
        return httpx.Response(200, json=fixture)

    sdk_transport(respond)
    first = runner.invoke(
        app,
        [
            "catalog",
            "https://space.bilibili.com/178360345/upload/video",
            "--max-items",
            "1",
            "--max-cost",
            "0.002",
            "--json",
        ],
    )
    assert first.exit_code == 2
    job_id = json.loads(first.stdout)["data"]["job_id"]
    resumed = runner.invoke(app, ["jobs", "resume", job_id, "--max-items", "2", "--json"])
    assert resumed.exit_code == 0
    assert requests == [{"uid": "178360345", "pn": "1", "ps": "1"}, {"uid": "178360345", "pn": "2", "ps": "1"}]


def test_sdk_does_not_retry_paid_server_errors(settings, sdk_transport, cached_tikhub_prices) -> None:
    cached_tikhub_prices("fixture-key")
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(503, json={"message": "Fixture service unavailable"})

    sdk_transport(respond)
    failed = runner.invoke(app, ["catalog", CHANNEL, "--max-cost", "0.1", "--json"])
    assert failed.exit_code == 5
    data = json.loads(failed.stdout)["data"]
    assert len(requests) == 1
    assert data["reserved_cost_usd"] == 0.001
    saved = JobStore(settings.jobs_dir).get(data["job_id"])
    assert saved["status"] == "failed"
    assert saved["cursor"] is None


def test_storage_creation_error_is_single_json_document(settings) -> None:
    settings.data_dir.write_text("A file obstructs the job directory")
    result = runner.invoke(app, ["catalog", CHANNEL, "--max-cost", "0.001", "--json"])
    assert result.exit_code == 7
    assert json.loads(result.stdout)["code"] == "storage"


def test_missing_key_does_not_reserve_request_budget(settings, monkeypatch) -> None:
    monkeypatch.setattr("soota.providers.tikhub.client.load_tikhub_api_key", lambda: None)
    result = runner.invoke(app, ["catalog", CHANNEL, "--provider", "tikhub", "--max-cost", "0.001", "--json"])
    assert result.exit_code == 4
    data = json.loads(result.stdout)["data"]
    assert data["request_count"] == 0
    assert data["reserved_cost_usd"] == 0
    assert not settings.ledger_path.exists()
