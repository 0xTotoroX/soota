from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context

import pytest

from soota.errors import SourceError
from soota.providers.quotes import cached_quote, quote_scope


def test_ttl_refresh_and_cache_reuse_across_workers(settings, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("soota.providers.quotes.time.time", lambda: now[0])
    settings.routing.price_ttl_seconds = 30
    calls = []

    def fetch():
        calls.append(1)
        return {"estimated_cost_usd": len(calls) / 1000}

    def get():
        return cached_quote(settings, "example", "account-a", "/read", fetch)

    with quote_scope():
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(copy_context().run, get) for _ in range(16)]
            assert all(f.result()["estimated_cost_usd"] == 0.001 for f in futures)
        now[0] += 31
        assert get()["estimated_cost_usd"] == 0.002
    with quote_scope():
        assert get()["estimated_cost_usd"] == 0.002
    assert len(calls) == 2


def test_expired_refresh_failure_does_not_reuse_old_price(settings, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("soota.providers.quotes.time.time", lambda: now[0])
    settings.routing.price_ttl_seconds = 10
    calls = []

    def fetch():
        calls.append(1)
        if len(calls) > 1:
            raise SourceError("offline")
        return {"estimated_cost_usd": 0.001}

    with quote_scope():
        cached_quote(settings, "example", "a", "/read", fetch)
        now[0] += 11
        for _ in range(3):
            with pytest.raises(SourceError):
                cached_quote(settings, "example", "a", "/read", fetch)
    assert len(calls) == 2


def test_namespace_and_explicit_refresh(settings):
    calls = []

    def fetch():
        calls.append(1)
        return {"estimated_cost_usd": len(calls)}

    with quote_scope():
        assert cached_quote(settings, "p", "a", "/read", fetch)["estimated_cost_usd"] == 1
        assert cached_quote(settings, "p", "b", "/read", fetch)["estimated_cost_usd"] == 2
    with quote_scope():
        for _ in range(2):
            assert cached_quote(settings, "p", "a", "/read", fetch, refresh=True)["estimated_cost_usd"] == 3
    assert len(calls) == 3


def test_execution_reuses_quote_then_expired_increase_blocks_dispatch(settings, monkeypatch):
    from soota.api import call_endpoint
    from soota.providers.tikhub.client import ENDPOINT_INFO

    now = [1000.0]
    monkeypatch.setattr("soota.providers.quotes.time.time", lambda: now[0])
    monkeypatch.setattr("soota.billing.time.sleep", lambda delay: None)
    settings.routing.price_ttl_seconds = 10
    prices, reads = [], []

    def request(method, path, params):
        if path == ENDPOINT_INFO:
            prices.append(path)
            return {"data": {"endpoint_uri": params["endpoint"], "endpoint_cost": 0.001 if len(prices) == 1 else 0.01}}
        reads.append(path)
        return {"data": {"id": "synthetic"}}

    def run():
        return call_endpoint(
            settings,
            "bilibili.web.fetch_one_video",
            params=["url=https://bilibili.com/video/BV1M1421t7hT"],
            max_cost=0.005,
            requester=request,
        )

    with quote_scope():
        assert run().ok
        assert run().ok
        assert len(prices) == 1 and len(reads) == 2
        now[0] += 11
        assert run().code == "auth_budget"
        assert len(prices) == 2 and len(reads) == 2


def test_automatic_sort_refreshes_and_excludes_failed_expired_quote(settings, monkeypatch):
    from soota.providers.priority import ordered_names
    from soota.routing import recognize

    now = [1000.0]
    monkeypatch.setattr("soota.providers.quotes.time.time", lambda: now[0])
    monkeypatch.setattr("soota.billing.time.sleep", lambda delay: None)
    settings.routing.price_ttl_seconds = 10
    requests = []

    def request(method, path, params):
        requests.append(path)
        if len(requests) > 1:
            raise SourceError("metadata unavailable")
        return {"data": {"endpoint_uri": params["endpoint"], "endpoint_cost": 0.001}}

    source = recognize("https://www.youtube.com/watch?v=Example0001")
    with quote_scope(tikhub_requester=request):
        assert ordered_names(settings, source, "inspect", ["tikhub"]) == ["tikhub"]
        now[0] += 11
        assert ordered_names(settings, source, "inspect", ["tikhub"]) == []
        assert ordered_names(settings, source, "inspect", ["tikhub"]) == []
    assert len(requests) == 2
