import json

import httpx
import pytest
import tikhub
from typer.testing import CliRunner

from soota.cli import app
from soota.providers.tikhub.client import ENDPOINT_INFO
from soota.providers.tikhub.registry import PLATFORM_NAMES, load_registry
from tests.conftest import load_fixture

runner = CliRunner()
BILI_URL = "https://www.bilibili.com/video/BV1M1421t7hT"


@pytest.fixture(autouse=True)
def isolated_cli(settings, monkeypatch):
    settings.config_dir.mkdir(parents=True)
    (settings.config_dir / "payment-policy.json").write_text(settings.payment.model_dump_json())
    monkeypatch.setenv("SOOTA_CONFIG_DIR", str(settings.config_dir))
    monkeypatch.setenv("SOOTA_CACHE_DIR", str(settings.cache_dir))
    monkeypatch.setenv("SOOTA_DATA_DIR", str(settings.data_dir))
    monkeypatch.setenv("SOOTA_OUTPUT_DIR", str(settings.output_dir))
    monkeypatch.setenv("TIKHUB_API_KEY", "fixture-key")
    monkeypatch.setattr("soota.providers.tikhub.client.load_tikhub_api_key", lambda: "fixture-key")


def forbid_sdk(monkeypatch):
    def forbidden(**kwargs):
        pytest.fail("This command must not create an HTTP client")

    monkeypatch.setattr(tikhub, "TikHub", forbidden)


def test_platform_inventory_and_endpoint_search_are_offline(settings, monkeypatch) -> None:
    forbid_sdk(monkeypatch)
    listed = runner.invoke(app, ["platforms", "--json"])
    assert listed.exit_code == 0
    platforms = {row["platform"]: row for row in json.loads(listed.stdout)["data"]["platforms"]}
    assert set(platforms) == set(PLATFORM_NAMES)
    assert platforms["wechat_mp"]["normalized"] == {}
    assert platforms["bilibili"]["normalized"]["user"]["catalog"]
    assert not any(row["live_verified"] for row in platforms.values())
    matched = runner.invoke(app, ["api", "list", "--platform", "instagram", "--task", "comments", "--json"])
    assert matched.exit_code == 0
    rows = json.loads(matched.stdout)["data"]["endpoints"]
    assert rows and all(row["preferred"] for row in rows)
    assert any(row["path"].endswith("/v3/get_post_comments") for row in rows)
    assert not settings.cache_dir.exists() and not settings.data_dir.exists()


def test_old_endpoint_name_uses_latest_parameter_contract_without_network(settings, monkeypatch) -> None:
    forbid_sdk(monkeypatch)
    command = ["api", "call", "/api/v1/bilibili/web/fetch_one_video", "--dry-run", "--json"]
    valid = runner.invoke(app, [*command, "--param", f"url={BILI_URL}"])
    assert valid.exit_code == 0
    assert json.loads(valid.stdout)["data"]["request"]["path"].endswith("fetch_one_video_v3")
    invalid = runner.invoke(app, [*command, "--param", "bv_id=BV1M1421t7hT"])
    assert invalid.exit_code == 3
    assert json.loads(invalid.stdout)["code"] == "input"
    assert not settings.cache_dir.exists()


@pytest.mark.parametrize(
    "params",
    [
        ["--param", "pn=not-an-int"],
        ["--param", "ps=101"],
        ["--param", "unknown=value"],
        ["--param", "pn=1", "--param", "pn=2"],
        ["--param", "malformed"],
    ],
)
def test_query_validation_blocks_bad_values_before_sdk(monkeypatch, params) -> None:
    forbid_sdk(monkeypatch)
    result = runner.invoke(
        app,
        [
            "api",
            "call",
            "bilibili.web.fetch_user_post_videos",
            "--param",
            "uid=00123",
            *params,
            "--max-cost",
            "0.1",
            "--json",
        ],
    )
    assert result.exit_code == 3
    assert json.loads(result.stdout)["code"] == "input"


def test_query_types_preserve_id_precision_and_coerce_flags() -> None:
    result = runner.invoke(
        app,
        [
            "api",
            "call",
            "youtube.web.get_video_info",
            "--param",
            "video_id=00000000000",
            "--param",
            "need_format=false",
            "--dry-run",
            "--json",
        ],
    )
    assert result.exit_code == 0
    assert json.loads(result.stdout)["data"]["request"]["params"] == {"video_id": "00000000000", "need_format": False}


@pytest.mark.parametrize(
    "selector,params,body",
    [
        ("bilibili.web.fetch_one_video", {"url": BILI_URL}, None),
        ("wechat_mp.fetch_article_detail_h5", {}, {"url": "https://mp.weixin.qq.com/s/fixture", "raw": False}),
        ("tiktok.app.fetch_multi_video", {}, ["7339393672959757570", "7339393672959757571"]),
        ("douyin.index.fetch_keyword_valid_date", {"keyword_list": "美食,旅游"}, None),
    ],
)
def test_real_sdk_get_post_object_post_array_and_post_query(
    settings, sdk_transport, tmp_path, selector, params, body
) -> None:
    endpoint = load_registry().resolve(selector)
    requests = []

    def respond(request):
        requests.append(request)
        assert request.headers["Authorization"] == "Bearer fixture-key"
        if request.url.path == ENDPOINT_INFO:
            assert request.url.params["endpoint"] == endpoint.path
            return httpx.Response(
                200, json={"code": 200, "data": {"endpoint_uri": endpoint.path, "endpoint_cost": 0.001}}
            )
        assert request.url.path == endpoint.path
        assert request.method == endpoint.method
        assert dict(request.url.params) == params
        if body is not None:
            assert json.loads(request.content) == body
        else:
            assert request.content == b""
        return httpx.Response(200, json={"code": 200, "data": {"text": "正文", "id": "9223372036854775807"}})

    sdk_transport(respond)
    params_path = tmp_path / "params.json"
    params_path.write_text(json.dumps(params))
    command = ["api", "call", selector, "--params-file", str(params_path), "--max-cost", "0.01", "--json"]
    if body is not None:
        body_path = tmp_path / "body.json"
        body_path.write_text(json.dumps(body))
        command += ["--body", str(body_path)]
    result = runner.invoke(app, command)
    assert result.exit_code == 0, result.stdout
    data = json.loads(result.stdout)["data"]
    assert data["response"]["data"]["id"] == "9223372036854775807"
    assert data["reserved_cost_usd"] == 0.001 and data["actual_cost_usd"] is None
    assert len(requests) == 2
    assert endpoint.path in settings.ledger_path.read_text()


def test_post_schema_validation_and_secret_redaction(monkeypatch, tmp_path) -> None:
    forbid_sdk(monkeypatch)
    endpoint = next(
        e
        for e in load_registry().preferred()
        if e.session_required
        and e.body_schema is not None
        and "cookie" in e.body_schema.get("properties", {})
        and e.callable
    )
    body = tmp_path / "body.json"
    body.write_text(json.dumps({"cookie": "private-cookie-value", "unexpected": "private-secret-value"}))
    invalid = runner.invoke(app, ["api", "call", endpoint.key, "--body", str(body), "--dry-run", "--json"])
    assert invalid.exit_code == 3
    assert "private-cookie-value" not in invalid.stdout
    assert "private-secret-value" not in invalid.stdout


@pytest.mark.parametrize("price", [None, -1, 10])
def test_unknown_negative_or_excessive_price_stops_target_call(settings, sdk_transport, price) -> None:
    requests = []
    endpoint = load_registry().resolve("bilibili.web.fetch_one_video")

    def respond(request):
        requests.append(request)
        assert request.url.path == ENDPOINT_INFO
        return httpx.Response(200, json={"data": {"endpoint_uri": endpoint.path, "endpoint_cost": price}})

    sdk_transport(respond)
    result = runner.invoke(
        app, ["api", "call", endpoint.key, "--param", f"url={BILI_URL}", "--max-cost", "0.001", "--json"]
    )
    assert result.exit_code == 4
    assert json.loads(result.stdout)["data"]["reserved_cost_usd"] == 0
    assert len(requests) == 1
    assert not settings.ledger_path.exists()


def test_http_failure_keeps_reservation_and_never_retries(settings, sdk_transport) -> None:
    endpoint = load_registry().resolve("bilibili.web.fetch_one_video")
    requests = []

    def respond(request):
        requests.append(request)
        if request.url.path == ENDPOINT_INFO:
            return httpx.Response(200, json={"data": {"endpoint_uri": endpoint.path, "endpoint_cost": 0.001}})
        return httpx.Response(503, json={"message": "unavailable"})

    sdk_transport(respond)
    result = runner.invoke(
        app, ["api", "call", endpoint.key, "--param", f"url={BILI_URL}", "--max-cost", "0.1", "--json"]
    )
    assert result.exit_code == 5
    assert len(requests) == 2
    assert json.loads(result.stdout)["data"]["reserved_cost_usd"] == 0.001
    assert settings.ledger_path.exists()


@pytest.mark.parametrize("url", [BILI_URL, "https://www.bilibili.com/video/av123456", "https://b23.tv/fixture"])
def test_normalized_bilibili_inspect_always_uses_v3(sdk_transport, url, cached_tikhub_prices) -> None:
    cached_tikhub_prices("fixture-key")
    requests = []

    def respond(request):
        requests.append(request)
        assert request.url.path == "/api/v1/bilibili/web/fetch_one_video_v3"
        assert request.url.params["url"] == url
        return httpx.Response(200, json=load_fixture("bilibili_info.json"))

    sdk_transport(respond)
    result = runner.invoke(app, ["inspect", url, "--provider", "tikhub", "--max-cost", "0.01", "--json"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["data"]["metadata"]["title"]
    assert len(requests) == 1


def test_api_show_preserves_cookie_field_schema() -> None:
    result = runner.invoke(app, ["api", "show", "douyin.web.fetch_user_collection_videos", "--json"])
    assert result.exit_code == 0
    endpoint = json.loads(result.stdout)["data"]["endpoint"]
    assert endpoint["body_schema"]["properties"]["cookie"]["type"] == "string"
    assert endpoint["session_required"]
    assert endpoint["docs_url"].startswith("https://api.tikhub.io/#/")


def test_export_redacts_echoed_credentials_and_preserves_old_output(settings, sdk_transport, tmp_path) -> None:
    endpoint = load_registry().resolve("douyin.web.fetch_user_collection_videos")
    body_path = tmp_path / "body.json"
    body_path.write_text(json.dumps({"cookie": "private-cookie-value"}))
    output = tmp_path / "response.json"
    requests = []

    def respond(request):
        requests.append(request)
        if request.url.path == ENDPOINT_INFO:
            return httpx.Response(200, json={"data": {"endpoint_uri": endpoint.path, "endpoint_cost": 0.001}})
        assert json.loads(request.content)["cookie"] == "private-cookie-value"
        return httpx.Response(200, json={"data": {"message": "echo private-cookie-value", "auth": "fixture-key"}})

    sdk_transport(respond)
    command = [
        "api",
        "call",
        endpoint.key,
        "--body",
        str(body_path),
        "--max-cost",
        "0.01",
        "--output",
        str(output),
        "--json",
    ]
    result = runner.invoke(app, command)
    assert result.exit_code == 0
    assert "private-cookie-value" not in result.stdout
    assert "private-cookie-value" not in output.read_text()
    assert "fixture-key" not in output.read_text()
    assert "private-cookie-value" not in settings.ledger_path.read_text()
    preserved = output.read_bytes()
    again = runner.invoke(app, command)
    assert again.exit_code == 3
    assert len(requests) == 2
    assert output.read_bytes() == preserved


def test_unsupported_helpers_never_construct_sdk(monkeypatch) -> None:
    forbid_sdk(monkeypatch)
    helper = runner.invoke(app, ["api", "call", "/api/v1/tiktok/web/device_register", "--dry-run", "--json"])
    assert helper.exit_code == 5
    external = runner.invoke(app, ["api", "call", "https://example.com/collect", "--dry-run", "--json"])
    assert external.exit_code == 5


def test_scalar_cookie_body_keeps_wire_value_but_redacts_echo(sdk_transport, tmp_path) -> None:
    endpoint = load_registry().resolve("tiktok.ads.get_location_list")
    path = tmp_path / "cookie.json"
    path.write_text(json.dumps("private-scalar-cookie"))

    def respond(request):
        if request.url.path == ENDPOINT_INFO:
            return httpx.Response(200, json={"data": {"endpoint_uri": endpoint.path, "endpoint_cost": 0.001}})
        assert json.loads(request.content) == "private-scalar-cookie"
        return httpx.Response(200, json={"data": {"message": "echo private-scalar-cookie"}})

    sdk_transport(respond)
    result = runner.invoke(app, ["api", "call", endpoint.key, "--body", str(path), "--max-cost", "0.001", "--json"])
    assert result.exit_code == 0
    assert "private-scalar-cookie" not in result.stdout
