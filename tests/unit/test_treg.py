"""treg 公共契约与费用恢复测试；所有响应、凭据和文件均为隔离的合成数据。"""

from __future__ import annotations

import json
import socket
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from decimal import Decimal
from threading import Barrier, Lock

import httpx
import pytest

from soota.billing import actual_cost
from soota.config import PaymentConfig
from soota.errors import AuthBudgetError, ContractError, SourceError
from soota.models import TranscriptSource, TranscriptStatus
from soota.providers.treg.catalog import CRAWL4AI, SCRAPECREATORS
from soota.providers.treg.client import TregClient
from soota.providers.treg.provider import TregProvider
from soota.routing import recognize
from soota.storage.task_budget import TaskBudget, task_budget_scope
from soota.transcripts.acquisition import acquire_captions
from soota.transcripts.conversion import convert_captions

TOKEN = "synthetic-treg-unit-test-token"
VIDEO_ID = "Example0001"
URL = f"https://www.youtube.com/watch?v={VIDEO_ID}"
PRICE = Decimal("0.00165")
BODY = {"video": URL, "language": "en"}


def caption_body(endpoint=CRAWL4AI, *, auto=False):
    if endpoint == CRAWL4AI:
        return {
            "recipe": "youtube-captions",
            "version": 1,
            "meta": {"partial": False, "blocked": [], "warnings": []},
            "rows": [
                {
                    "video_id": VIDEO_ID,
                    "url": URL,
                    "title": "Synthetic video",
                    "language": "en",
                    "is_auto_generated": auto,
                    "available_languages": [{"code": "en", "name": "English", "auto": auto}],
                    "segments": [
                        {"start_s": 1.36, "duration_s": 1.68, "text": "第一句。"},
                        {"start_s": 4, "duration_s": 0.5, "text": "第二句。"},
                    ],
                    "text": "第一句。 第二句。",
                }
            ],
        }
    return {
        "success": True,
        "credits_remaining": 999999,
        "credits_charged": 1,
        "videoId": VIDEO_ID,
        "type": "video",
        "url": URL,
        "transcript": [
            {"startMs": "160", "endMs": "1920", "startTimeText": "0:00", "text": "第一句。"},
            {"startMs": "1920", "endMs": "3919", "startTimeText": "0:01", "text": "第二句。"},
        ],
        "transcript_only_text": "第一句。 第二句。",
        "language": "English",
    }


class MockTreg:
    """目录样本独立于生产映射，防止错误映射与测试一起通过。"""

    def __init__(self, endpoint=CRAWL4AI):
        self.endpoint = endpoint
        crawl = endpoint == CRAWL4AI
        self.price = 0.00165 if crawl else 0.00188
        provider = "crawl4ai" if crawl else "scrapecreators"
        self.catalog = {
            "endpoint": {
                "id": endpoint,
                "provider": provider,
                "path": "/recipes/youtube-captions" if crawl else "/v1/youtube/video/transcript",
                "method": "POST" if crawl else "GET",
                "platform_eligible": True,
                "async": None,
                "cost": {
                    "type": "per_success" if crawl else "per_call",
                    "value": 1.65 if crawl else 1,
                    "currency": "credit",
                    "per": 1,
                    "unit": "call",
                    "usd": self.price,
                },
            },
            "provider": {"service": provider},
        }
        self.identity = {"org_id": 7, "org": "synthetic-team", "role": "member"}
        self.balance = {
            "org_id": 7,
            "balance_micro": 2_000_000,
            "balance_usd": 999,
            "holds": [{"call_id": "other-call", "amount_micro": 500_000}],
        }
        self.balance_status = 200
        self.host = "treg.invalid"
        self.token = TOKEN
        self.payload = caption_body(endpoint)
        self.headers = {"X-Treg-Call-Id": "synthetic-call", "X-Treg-Cost-Micro": str(round(self.price * 1_000_000))}
        self.call_handler = None
        self.requests = []
        self.lock = Lock()

    @property
    def calls(self):
        return [request for request in self.requests if request.url.path.startswith("/call/")]

    def __call__(self, request):
        with self.lock:
            self.requests.append(request)
        assert request.url.host == self.host
        path = request.url.path
        if path == "/catalog/endpoints/" + self.endpoint:
            assert request.method == "GET"
            assert "X-Treg-Token" not in request.headers
            assert "Authorization" not in request.headers
            # 允许构造非有限报价，测试客户端必须拒绝它。
            return httpx.Response(200, text=json.dumps(self.catalog), headers={"Content-Type": "application/json"})
        assert request.headers.get("X-Treg-Token") == self.token
        if path == "/auth/me":
            assert request.method == "GET"
            return httpx.Response(200, json=self.identity)
        if path == "/orgs/7/balance":
            assert request.method == "GET"
            return httpx.Response(self.balance_status, json=self.balance)
        if path == "/call/" + self.endpoint:
            if self.call_handler is not None:
                return self.call_handler(request)
            return httpx.Response(200, json=self.payload, headers=self.headers)
        pytest.fail(f"Unexpected mock endpoint: {request.method} {path}")


@pytest.fixture(autouse=True)
def isolated_treg(settings, monkeypatch):
    settings.treg.enabled = True
    settings.treg.base_url = "https://treg.invalid"
    monkeypatch.setenv("TREG_TOKEN", TOKEN)
    for name, value in (
        ("SOOTA_CONFIG_DIR", settings.config_dir),
        ("SOOTA_DATA_DIR", settings.data_dir),
        ("SOOTA_CACHE_DIR", settings.cache_dir),
    ):
        monkeypatch.setenv(name, str(value))

    def blocked(*args, **kwargs):
        raise AssertionError("treg tests must not use real sockets or read TikHub credentials")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", blocked)
    monkeypatch.setattr("soota.providers.tikhub.client.load_tikhub_api_key", blocked)
    monkeypatch.setattr("soota.secrets.load_tikhub_api_key", blocked)


@pytest.fixture
def make_client(settings):
    clients = []

    def create(api, cap=1, **kwargs):
        client = TregClient(settings, cap, transport=httpx.MockTransport(api), **kwargs)
        clients.append(client)
        return client

    yield create
    for client in clients:
        client.http.close()


def test_public_quote_without_key_never_dispatches_or_queries_wallet(make_client, monkeypatch):
    monkeypatch.delenv("TREG_TOKEN", raising=False)
    api = MockTreg()
    client = make_client(api)
    assert client.quote(CRAWL4AI)["estimated_cost_usd"] == float(PRICE)
    with pytest.raises(AuthBudgetError):
        client.run(CRAWL4AI, body=BODY)
    with pytest.raises(AuthBudgetError):
        client.wallet()
    assert [r.url.path for r in api.requests] == ["/catalog/endpoints/" + CRAWL4AI]


@pytest.mark.parametrize("endpoint", [CRAWL4AI, SCRAPECREATORS])
def test_quote_uses_live_usd_and_explicit_refresh(make_client, endpoint):
    api = MockTreg(endpoint)
    api.catalog["endpoint"]["cost"]["usd"] = 0.004321
    client = make_client(api)
    assert client.quote(endpoint)["estimated_cost_usd"] == 0.004321
    api.catalog["endpoint"]["cost"]["usd"] = 0.005678
    assert client.quote(endpoint)["estimated_cost_usd"] == 0.004321
    assert client.quote(endpoint, refresh=True)["estimated_cost_usd"] == 0.005678
    assert len(api.requests) == 2


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", SCRAPECREATORS),
        ("provider", "wrong-provider"),
        ("path", "/recipes/something-else"),
        ("method", "GET"),
        ("platform_eligible", False),
    ],
)
def test_quote_rejects_mismatched_catalog_identity(make_client, field, value):
    api = MockTreg()
    api.catalog["endpoint"][field] = value
    with pytest.raises(ContractError):
        make_client(api).run(CRAWL4AI, body=BODY)
    assert api.calls == []


@pytest.mark.parametrize(
    "patch",
    [
        {"type": "per_result"},
        {"unit": "row"},
        {"per": 1000},
        {"varies": True},
        {"table": [{"usd": 0.001}]},
        {"settle": "usage"},
        {"reported_charge": "credits_charged"},
        {"usd": None},
        {"usd": True},
        {"usd": "0.00165"},
        {"usd": 0},
        {"usd": -0.001},
        {"usd": float("nan")},
        {"usd": float("inf")},
    ],
)
def test_dynamic_or_invalid_prices_block_paid_dispatch(make_client, patch):
    api = MockTreg()
    api.catalog["endpoint"]["cost"].update(patch)
    with pytest.raises(ContractError):
        make_client(api).run(CRAWL4AI, body=BODY)
    assert api.calls == []


def test_wallet_resolves_org_and_uses_micro_balance_without_subtracting_holds(make_client):
    api = MockTreg()
    assert make_client(api).wallet()["balance"] == 2
    assert [r.url.path for r in api.requests] == ["/auth/me", "/orgs/7/balance"]
    assert api.calls == []


@pytest.mark.parametrize("org", [None, True, "7", 0, -1])
def test_wallet_rejects_invalid_identity_before_balance_lookup(make_client, org):
    api = MockTreg()
    api.identity["org_id"] = org
    with pytest.raises(ContractError):
        make_client(api).wallet()
    assert [r.url.path for r in api.requests] == ["/auth/me"]


@pytest.mark.parametrize(
    "patch",
    [
        {"org_id": 8},
        {"balance_micro": None},
        {"balance_micro": True},
        {"balance_micro": "2000000"},
        {"balance_micro": 1.5},
    ],
)
def test_wallet_rejects_wrong_org_and_invalid_integer_amounts(make_client, patch):
    api = MockTreg()
    api.balance.update(patch)
    with pytest.raises(ContractError):
        make_client(api).wallet()


def test_zero_budget_precedes_ask_and_wallet(settings, make_client, monkeypatch):
    settings.payment = PaymentConfig(mode="ask", min_balance_usd=1)
    monkeypatch.setattr("soota.billing.confirm_payment", lambda _: pytest.fail("zero budget must not prompt"))
    api = MockTreg()
    with pytest.raises(AuthBudgetError):
        make_client(api, cap=0).run(CRAWL4AI, body=BODY)
    assert not api.calls
    assert all(r.url.path.startswith("/catalog/") for r in api.requests)


def test_live_price_above_cap_prevents_dispatch(make_client):
    api = MockTreg()
    api.catalog["endpoint"]["cost"]["usd"] = 0.006
    with pytest.raises(AuthBudgetError):
        make_client(api, cap=0.005).run(CRAWL4AI, body=BODY)
    assert len(api.requests) == 1 and not api.calls


@pytest.mark.parametrize("approved", [False, True])
def test_ask_policy_authorizes_before_reserving_and_sending(settings, make_client, monkeypatch, approved):
    settings.payment = PaymentConfig(mode="ask")
    api = MockTreg()
    budget = TaskBudget(settings.data_dir / "ask-task", float(PRICE))
    prompts = []

    def confirm(details):
        assert not api.calls and budget.snapshot()["requests"] == []
        prompts.append(details)
        return approved

    monkeypatch.setattr("soota.billing.confirm_payment", confirm)
    client = make_client(api)
    with task_budget_scope(budget):
        if approved:
            client.run(CRAWL4AI, body=BODY)
        else:
            with pytest.raises(AuthBudgetError):
                client.run(CRAWL4AI, body=BODY)
    assert len(prompts) == 1 and prompts[0]["provider"] == "treg"
    assert prompts[0]["estimated_cost_usd"] == float(PRICE)
    assert len(api.calls) == int(approved)
    assert len(budget.snapshot()["requests"]) == int(approved)


@pytest.mark.parametrize("balance_micro,allowed", [(1_001_649, False), (1_001_650, False), (1_001_651, True)])
def test_balance_threshold_includes_current_request(settings, make_client, balance_micro, allowed):
    settings.payment = PaymentConfig(mode="auto", min_balance_usd=1)
    api = MockTreg()
    api.balance["balance_micro"] = balance_micro
    client = make_client(api)
    if allowed:
        client.run(CRAWL4AI, body=BODY)
    else:
        with pytest.raises(AuthBudgetError):
            client.run(CRAWL4AI, body=BODY)
    assert len(api.calls) == int(allowed)
    assert [r.url.path for r in api.requests if not r.url.path.startswith("/catalog/")][:2] == [
        "/auth/me",
        "/orgs/7/balance",
    ]


def test_unavailable_wallet_blocks_before_prompt_or_paid_call(settings, make_client, monkeypatch):
    settings.payment = PaymentConfig(mode="ask", min_balance_usd=1)
    api = MockTreg()
    api.balance_status = 503
    monkeypatch.setattr("soota.billing.confirm_payment", lambda _: pytest.fail("unknown balance must not prompt"))
    with pytest.raises(AuthBudgetError):
        make_client(api).run(CRAWL4AI, body=BODY)
    assert not api.calls


def test_budget_and_idempotency_receipt_exist_before_send_and_survive_resume(settings, make_client):
    api = MockTreg()
    budget = TaskBudget(settings.data_dir / "receipt-task", float(PRICE))
    client = make_client(api)

    def call(request):
        saved_budget = budget.snapshot()
        assert Decimal(saved_budget["reserved_cost_usd"]) == PRICE
        assert len(saved_budget["requests"]) == 1
        files = list(client.root.glob("*.json"))
        assert len(files) == 1
        receipt_text = files[0].read_text()
        saved = json.loads(receipt_text)
        assert TOKEN not in receipt_text
        assert saved["idempotency_key"] == request.headers["Idempotency-Key"]
        assert saved["state"] != "completed"
        assert Decimal(request.headers["X-Treg-Route-Max-Cost"]) == PRICE
        assert request.method == "POST" and json.loads(request.content) == BODY
        return httpx.Response(200, json=api.payload, headers=api.headers)

    api.call_handler = call
    with task_budget_scope(budget):
        first = client.run(CRAWL4AI, body=BODY)
    original_budget = deepcopy(budget.snapshot())
    resumed = make_client(api, cap=0)
    with task_budget_scope(TaskBudget(budget.folder, float(PRICE))):
        assert resumed.run(CRAWL4AI, body=BODY) == first
    assert len(api.calls) == 1
    assert budget.snapshot() == original_budget
    assert actual_cost(client.records) == float(PRICE)
    assert actual_cost(resumed.records) == 0
    assert resumed.records[-1].cache_hit
    receipt = json.loads(next(client.root.glob("*.json")).read_text())
    assert receipt["call_id"] == "synthetic-call"


def test_identical_input_in_distinct_tasks_is_distinct_billable_work(settings, make_client):
    api = MockTreg()
    for name in ("task-one", "task-two"):
        with task_budget_scope(TaskBudget(settings.data_dir / name, float(PRICE))):
            make_client(api).run(CRAWL4AI, body=BODY)
    assert len(api.calls) == 2
    assert len({r.headers["Idempotency-Key"] for r in api.calls}) == 2


@pytest.mark.parametrize("outcome", ["timeout", 202, 409, 410, 422, "upstream_failed", "invalid_json"])
def test_unknown_or_pending_receipt_never_resubmits_on_repeated_resume(settings, make_client, outcome):
    api = MockTreg()
    budget = TaskBudget(settings.data_dir / "pending-task", float(PRICE))

    def failed(request):
        if outcome == "timeout":
            raise httpx.ReadTimeout("synthetic lost response", request=request)
        if outcome == "invalid_json":
            return httpx.Response(200, text="{broken", headers=api.headers)
        if outcome == "upstream_failed":
            return httpx.Response(200, json={"success": False}, headers={**api.headers, "X-Treg-Upstream-Failed": "1"})
        return httpx.Response(outcome, json={"status": "pending", "job_id": "synthetic-job"}, headers=api.headers)

    api.call_handler = failed
    with task_budget_scope(budget), pytest.raises((SourceError, ContractError)):
        make_client(api).run(CRAWL4AI, body=BODY)
    before = deepcopy(budget.snapshot())
    for _ in range(3):
        with task_budget_scope(TaskBudget(budget.folder, float(PRICE))), pytest.raises(SourceError):
            make_client(api).run(CRAWL4AI, body=BODY)
    assert len(api.calls) == 1
    assert budget.snapshot() == before
    assert Decimal(before["reserved_cost_usd"]) == PRICE


def test_missing_charge_header_remains_unknown_not_free(make_client):
    api = MockTreg()
    api.headers.pop("X-Treg-Cost-Micro")
    client = make_client(api)
    client.run(CRAWL4AI, body=BODY)
    assert actual_cost(client.records) is None


def test_overcharged_receipt_stops_and_is_not_resubmitted(settings, make_client):
    api = MockTreg()
    api.headers["X-Treg-Cost-Micro"] = "2000"
    budget = TaskBudget(settings.data_dir / "overcharged-task", 1)
    with task_budget_scope(budget), pytest.raises(ContractError):
        make_client(api).run(CRAWL4AI, body=BODY)
    with task_budget_scope(budget), pytest.raises(SourceError):
        make_client(api).run(CRAWL4AI, body=BODY)
    assert len(api.calls) == 1 and len(budget.snapshot()["requests"]) == 1


def test_parallel_task_budget_limits_dispatch_across_independent_clients(settings, make_client):
    api = MockTreg()
    folder = settings.data_dir / "parallel-task"
    cap = PRICE * 3
    budget = TaskBudget(folder, float(cap))
    clients = [make_client(api) for _ in range(8)]
    start = Barrier(len(clients))

    def run(index):
        start.wait(timeout=10)
        # 独立 TaskBudget 对象共享同一磁盘账本，覆盖文件锁而非仅进程内 RLock。
        with task_budget_scope(TaskBudget(folder, float(cap))):
            try:
                clients[index].run(CRAWL4AI, body={**BODY, "video": f"Example{index:04d}"})
            except AuthBudgetError:
                return False
        return True

    with ThreadPoolExecutor(max_workers=len(clients)) as pool:
        outcomes = list(pool.map(run, range(len(clients))))
    assert outcomes.count(True) == 3 and len(api.calls) == 3
    saved = budget.snapshot()
    assert Decimal(saved["reserved_cost_usd"]) == cap
    assert len(saved["requests"]) == 3
    assert len({request.headers["Idempotency-Key"] for request in api.calls}) == 3


@pytest.mark.parametrize(
    "route,auto,kind",
    [
        ("crawl4ai", False, TranscriptSource.OFFICIAL),
        ("crawl4ai", True, TranscriptSource.AUTO),
        ("scrapecreators", None, TranscriptSource.UNKNOWN),
    ],
)
def test_caption_formats_convert_and_inspect_reuses_one_paid_response(settings, make_client, route, auto, kind):
    settings.treg.youtube_captions = route
    settings.default_language = "zh-CN"
    endpoint = CRAWL4AI if route == "crawl4ai" else SCRAPECREATORS
    api = MockTreg(endpoint)
    api.payload = caption_body(endpoint, auto=auto)
    provider = TregProvider(settings, client=make_client(api))
    item = provider.inspect(recognize(URL))
    acquired = provider.get_captions(item)
    assert not provider.cache.has(item.source, "captions")
    converted = convert_captions(acquired)
    result = acquire_captions(provider, item)
    assert result == converted and result.status == TranscriptStatus.READY
    expected = [(1360, 3040), (4000, 4500)] if route == "crawl4ai" else [(160, 1920), (1920, 3919)]
    assert [(s.start_ms, s.end_ms) for s in result.segments] == expected
    assert [s.text for s in result.segments] == ["第一句。", "第二句。"]
    assert result.source == kind
    if route == "crawl4ai":
        assert result.language == "en"
        assert json.loads(api.calls[0].content)["video"] == URL
    else:
        assert result.language not in {"zh-CN", "English"}
        assert api.calls[0].method == "GET" and api.calls[0].url.params["url"] == URL
    assert len(api.calls) == 1
    assert provider.cache.has(item.source, "captions")


@pytest.mark.parametrize("route", ["crawl4ai", "scrapecreators"])
@pytest.mark.parametrize("identity", [None, "WrongVideo1"])
def test_wrong_or_missing_video_identity_never_becomes_valid_caption(settings, make_client, route, identity):
    settings.treg.youtube_captions = route
    endpoint = CRAWL4AI if route == "crawl4ai" else SCRAPECREATORS
    api = MockTreg(endpoint)
    row = api.payload["rows"][0] if route == "crawl4ai" else api.payload
    key = "video_id" if route == "crawl4ai" else "videoId"
    if identity is None:
        row.pop(key)
    else:
        row[key] = identity
    provider = TregProvider(settings, client=make_client(api))
    source = recognize(URL)
    with pytest.raises(ContractError):
        acquire_captions(provider, provider.inspect(source))
    assert not provider.cache.has(source, "captions")


def test_scrapecreators_embedded_failure_is_not_valid_transcript(settings, make_client):
    settings.treg.youtube_captions = "scrapecreators"
    api = MockTreg(SCRAPECREATORS)
    api.payload["success"] = False
    provider = TregProvider(settings, client=make_client(api))
    source = recognize(URL)
    with pytest.raises((ContractError, SourceError)):
        acquire_captions(provider, provider.inspect(source))
    assert not provider.cache.has(source, "captions")


@pytest.mark.parametrize(
    "route,empty", [("crawl4ai", "rows"), ("crawl4ai", "segments"), ("scrapecreators", None), ("scrapecreators", [])]
)
def test_empty_caption_results_are_unknown_not_confirmed_missing(settings, make_client, route, empty):
    settings.treg.youtube_captions = route
    endpoint = CRAWL4AI if route == "crawl4ai" else SCRAPECREATORS
    api = MockTreg(endpoint)
    if route == "crawl4ai":
        if empty == "rows":
            api.payload["rows"] = []
            api.payload["meta"]["warnings"] = ["Requested language unavailable; English captions exist."]
        else:
            api.payload["rows"][0]["segments"] = []
            api.payload["rows"][0]["text"] = ""
    else:
        api.payload.update(transcript=empty, transcript_only_text=None, language=None)
    provider = TregProvider(settings, client=make_client(api))
    source = recognize(URL)
    result = acquire_captions(provider, provider.inspect(source))
    assert result.status == TranscriptStatus.UNKNOWN and not result.segments
    assert not provider.cache.has(source, "captions")


@pytest.mark.parametrize("route", ["crawl4ai", "scrapecreators"])
def test_invalid_timestamps_fail_conversion_before_cache(settings, make_client, route):
    settings.treg.youtube_captions = route
    endpoint = CRAWL4AI if route == "crawl4ai" else SCRAPECREATORS
    api = MockTreg(endpoint)
    if route == "crawl4ai":
        api.payload["rows"][0]["segments"][0]["duration_s"] = -1
    else:
        api.payload["transcript"][0]["endMs"] = "159"
    provider = TregProvider(settings, client=make_client(api))
    source = recognize(URL)
    with pytest.raises(ContractError):
        acquire_captions(provider, provider.inspect(source))
    assert not provider.cache.has(source, "captions")


@pytest.mark.parametrize("new_client", [False, True])
def test_unscoped_lost_response_cannot_be_reissued_as_new_work(make_client, new_client):
    api = MockTreg()

    def lost(request):
        raise httpx.ReadTimeout("synthetic response lost after upstream accepted", request=request)

    api.call_handler = lost
    original = make_client(api)
    with pytest.raises(SourceError):
        original.run(CRAWL4AI, body=BODY)
    # 普通 api/inspect 入口未传 run_scope，重建客户端仍不能忘记未决提交。
    retried = make_client(api) if new_client else original
    with pytest.raises(SourceError):
        retried.run(CRAWL4AI, body=BODY)
    assert len(api.calls) == 1, "A lost response was submitted again under a fresh idempotency key"


def test_overcharge_blocks_next_distinct_request_within_original_task_budget(settings, make_client):
    api = MockTreg()
    budget = TaskBudget(settings.data_dir / "overcharge-followup", float(PRICE * 2))
    api.headers["X-Treg-Cost-Micro"] = "2000"
    with task_budget_scope(budget), pytest.raises(ContractError):
        make_client(api).run(CRAWL4AI, body=BODY)
    # 已实付 0.002 后，再支出 0.00165 会超过累计上限 0.0033。
    api.headers["X-Treg-Cost-Micro"] = "1650"
    with task_budget_scope(TaskBudget(budget.folder, float(PRICE * 2))):
        with pytest.raises((AuthBudgetError, ContractError)):
            make_client(api).run(CRAWL4AI, body={**BODY, "video": "Example0002"})
    assert len(api.calls) == 1, "An underestimated settled charge must not leave spendable task budget"


@pytest.mark.parametrize("changed", ["credential", "base_url"])
def test_quotes_are_isolated_by_account_and_server(settings, make_client, monkeypatch, changed):
    original_api = MockTreg()
    original = make_client(original_api)
    assert original.quote(CRAWL4AI)["estimated_cost_usd"] == float(PRICE)
    second_api = MockTreg()
    second_api.catalog["endpoint"]["cost"]["usd"] = 0.009
    if changed == "credential":
        second_api.token = "synthetic-other-treg-token"
        monkeypatch.setenv("TREG_TOKEN", second_api.token)
    else:
        second_api.host = "other-treg.invalid"
        settings.treg.base_url = "https://other-treg.invalid"
    second = make_client(second_api)
    assert second.namespace != original.namespace
    assert second.quote(CRAWL4AI)["estimated_cost_usd"] == 0.009
    assert len(original_api.requests) == len(second_api.requests) == 1


def test_quotes_do_not_cross_caption_endpoint_identity(make_client):
    crawl_api, scrape_api = MockTreg(), MockTreg(SCRAPECREATORS)
    assert make_client(crawl_api).quote(CRAWL4AI)["estimated_cost_usd"] == 0.00165
    assert make_client(scrape_api).quote(SCRAPECREATORS)["estimated_cost_usd"] == 0.00188
    assert len(crawl_api.requests) == len(scrape_api.requests) == 1


def test_explicit_scope_reuses_alias_and_reordered_body_without_another_reservation(settings, make_client):
    api = MockTreg()
    client = make_client(api, run_scope="synthetic-operation")
    client.run(CRAWL4AI, body=BODY)
    resumed = make_client(api, cap=0, run_scope="synthetic-operation")
    assert resumed.run("/recipes/youtube-captions", body=dict(reversed(list(BODY.items())))) == api.payload
    assert len(api.calls) == 1 and actual_cost(resumed.records) == 0
    assert len(list(client.root.glob("*.json"))) == 1


@pytest.mark.parametrize("scope", ["task", "explicit"])
def test_pending_receipt_stays_blocked_after_server_idempotency_window(settings, make_client, monkeypatch, scope):
    now = [1000.0]
    monkeypatch.setattr("soota.providers.quotes.time.time", lambda: now[0])
    api = MockTreg()

    def lost(request):
        raise httpx.ReadTimeout("synthetic lost response", request=request)

    api.call_handler = lost
    kwargs = {"run_scope": "synthetic-operation"} if scope == "explicit" else {}
    budget = TaskBudget(settings.data_dir / "expired-pending", float(PRICE))
    with task_budget_scope(budget), pytest.raises(SourceError):
        make_client(api, **kwargs).run(CRAWL4AI, body=BODY)
    before = deepcopy(budget.snapshot())
    now[0] += 25 * 3600
    with task_budget_scope(TaskBudget(budget.folder, float(PRICE))), pytest.raises(SourceError):
        make_client(api, **kwargs).run(CRAWL4AI, body=BODY)
    assert len(api.calls) == 1 and budget.snapshot() == before


def test_parallel_identical_operation_only_reserves_and_calls_once(settings, make_client):
    api = MockTreg()
    folder = settings.data_dir / "same-operation"
    budget = TaskBudget(folder, float(PRICE))
    clients = [make_client(api) for _ in range(4)]
    start = Barrier(len(clients))

    def run(index):
        start.wait(timeout=10)
        with task_budget_scope(TaskBudget(folder, float(PRICE))):
            return clients[index].run(CRAWL4AI, body=BODY)

    with ThreadPoolExecutor(max_workers=len(clients)) as pool:
        results = list(pool.map(run, range(len(clients))))
    assert all(result == api.payload for result in results)
    assert len(api.calls) == 1 and len(budget.snapshot()["requests"]) == 1
    assert Decimal(budget.snapshot()["reserved_cost_usd"]) == PRICE
    assert sum(actual_cost(client.records) for client in clients) == float(PRICE)
