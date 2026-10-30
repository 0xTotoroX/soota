from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import tikhub

from soota.config import AsrConfig, AutoUpdateConfig, PaymentConfig, SootaSettings, TikHubConfig

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def block_external_http(monkeypatch):
    original = httpx.HTTPTransport.handle_request

    def guarded(transport, request):
        if request.url.host not in {"localhost", "127.0.0.1", "::1"}:
            pytest.fail("Tests must use MockTransport; external HTTP could spend the user's balance.")
        return original(transport, request)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", guarded)


@pytest.fixture(autouse=True)
def isolate_credentials(monkeypatch):
    # Tests use synthetic credentials; even read-only quotes must not open the user's Keychain.
    for name in ("TIKHUB_API_KEY", "MONID_API_KEY", "TREG_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("soota.secrets._load_keychain", lambda *args: None)


@pytest.fixture
def cached_tikhub_prices(settings):
    """Existing acquisition tests start with a fresh, explicit cached price table."""
    from soota.providers.quotes import cached_quote, namespace
    from soota.providers.tikhub.client import load_price_snapshot

    def seed(credential=""):
        for path, price in load_price_snapshot()["endpoints"].items():
            cached_quote(
                settings,
                "tikhub",
                namespace(settings.tikhub.base_url, credential),
                path,
                lambda price=price: {"estimated_cost_usd": price},
            )

    seed()
    return seed


@pytest.fixture(autouse=True)
def block_browser_commands(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Tests must stub OpenCLI; never use the user's browser.")

    monkeypatch.setattr("soota.providers.opencli.OpenCLIProvider._run", blocked)
    monkeypatch.setattr("soota.providers.ytdlp.run_process", blocked)
    monkeypatch.setattr("soota.providers.downie.run_process", blocked)
    monkeypatch.setenv("SOOTA_AUTO_UPDATE", "false")
    monkeypatch.setenv("OPENCLI_PROFILE", "test-context")
    monkeypatch.setattr("soota.providers.opencli_context.profile_config", lambda: {})


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def settings(tmp_path: Path) -> SootaSettings:
    return SootaSettings(
        output_dir=tmp_path / "out",
        default_language="zh",
        config_dir=tmp_path / "config",
        cache_dir=tmp_path / "cache",
        data_dir=tmp_path / "data",
        tikhub=TikHubConfig(),
        updates=AutoUpdateConfig(enabled=False),
        payment=PaymentConfig(mode="auto"),
        asr=AsrConfig(engine="whisper-cli", model="base"),
    )


@pytest.fixture
def sdk_transport(monkeypatch):
    """Keep the real SDK's serialization, errors and retries; replace only its HTTP transport."""
    original = tikhub.TikHub
    clients = []

    def install(handler):
        def create(**kwargs):
            client = original(**kwargs, transport=httpx.MockTransport(handler))
            clients.append(client)
            return client

        monkeypatch.setattr(tikhub, "TikHub", create)

    yield install
    for client in clients:
        client.close()
