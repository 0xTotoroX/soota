"""Exercise the public task and configuration paths with synthetic HTTP."""

import json
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from soota.cli import app
from soota.config import load_settings
from soota.models import TranscriptMode
from soota.providers.priority import ordered_names
from soota.providers.treg.client import TregClient
from soota.routing import recognize
from soota.tasks.entry import ingest
from tests.unit.test_treg import TOKEN, URL, MockTreg


@pytest.mark.parametrize("provider", ["treg", "auto"])
def test_unified_task_uses_treg_and_reuses_converted_captions(settings, monkeypatch, provider):
    settings.treg.enabled = True
    settings.treg.base_url = "https://treg.invalid"
    settings.routing.disabled_providers.update({"opencli", "yt-dlp", "tikhub"})
    monkeypatch.setenv("TREG_TOKEN", TOKEN)
    api = MockTreg()
    clients = []

    def client(st, cap=None, **kwargs):
        created = TregClient(st, cap, transport=httpx.MockTransport(api), **kwargs)
        clients.append(created)
        return created

    monkeypatch.setattr("soota.providers.treg.provider.TregClient", client)
    monkeypatch.setattr("soota.providers.priority.TregClient", client)
    try:
        result = ingest(
            settings, URL, want="context", provider=provider, transcript=TranscriptMode.CAPTIONS_ONLY, max_cost=0.002
        )
        assert result.ok, result.model_dump()
        assert "第一句。" in Path(result.data["context"]).read_text()
        assert json.loads(Path(result.data["manifest"]).read_text())["metadata"]["provenance"]["provider"] == "treg"
        second = ingest(
            settings, URL, want="context", provider="treg", transcript=TranscriptMode.CAPTIONS_ONLY, max_cost=0
        )
        assert second.ok, second.model_dump()
        assert len(api.calls) == 1
        assert len([r for r in api.requests if r.url.path.startswith("/catalog/")]) == 1
    finally:
        for instance in clients:
            instance.http.close()


def test_cli_auth_enable_and_cost_priority_keep_free_sources_first(settings, monkeypatch, tmp_path):
    monkeypatch.setenv("SOOTA_CONFIG_DIR", str(settings.config_dir))
    key = tmp_path / "synthetic-key"
    key.write_text(TOKEN)
    cli = CliRunner()
    for args in (
        ["providers", "auth", "treg", "--key-file", str(key)],
        ["providers", "enable", "treg"],
        ["providers", "show", "--json"],
    ):
        result = cli.invoke(app, args)
        assert result.exit_code == 0, result.stdout
        assert TOKEN not in result.stdout
    current = load_settings()
    assert current.treg.enabled
    assert ordered_names(current, recognize(URL), "captions", ["treg", "yt-dlp", "opencli"]) == [
        "opencli",
        "yt-dlp",
        "treg",
    ]
    preview = cli.invoke(app, ["providers", "priority", "show", "youtube", "captions", "--json"])
    assert preview.exit_code == 0
    rows = json.loads(preview.stdout)["data"]["candidates"]
    assert next(r for r in rows if r["provider"] == "treg")["estimated_cost_usd"] == 0.00165
