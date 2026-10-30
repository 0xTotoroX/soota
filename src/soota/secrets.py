from __future__ import annotations

import os
import re
import subprocess
from typing import Any

_BEARER_RE = re.compile(r"(?i)(bearer\s+)([A-Za-z0-9._\-+=/]{8,})")
_KEY_RE = re.compile(r"(?i)(api[_-]?key|authorization|token)([\"']?\s*[:=]\s*[\"']?)([^\s\"']{8,})")
_SIGNED_QUERY_RE = re.compile(
    r"(?i)([?&](?:sig(?:nature)?|expire|expires|token|skid|usig|oh|mm|ei|ipbits|ip)(?:=|%3D))([^&]+)"
)
_LONG_TOKEN_RE = re.compile(r"\b(?:sk-|th_|tikhub_)[A-Za-z0-9_\-]{12,}\b")
REDACTED = "[redacted]"
CREDENTIAL_FIELDS = frozenset(
    {
        "authorization",
        "x-treg-token",
        "treg_token",
        "api_key",
        "apikey",
        "cookie",
        "auth",
        "password",
        "session_token",
        "xsec_token",
        "passcode",
        "stoken",
    }
)
KEYCHAIN_SERVICE = "soota.tikhub"
KEYCHAIN_ACCOUNT = "api_key"


def redact_text(value: str) -> str:
    text = _BEARER_RE.sub(rf"\1{REDACTED}", value)
    text = _KEY_RE.sub(rf"\1\2{REDACTED}", text)
    text = _SIGNED_QUERY_RE.sub(rf"\1{REDACTED}", text)
    text = _LONG_TOKEN_RE.sub(REDACTED, text)
    return text


def redact_url(url: str) -> str:
    if "?" not in url:
        return url
    head, query = url.split("?", 1)
    return f"{head}?{REDACTED}" if query else url


def _looks_signed(url: str) -> bool:
    lowered = url.lower()
    return any(marker in lowered for marker in ("sig=", "signature=", "expire=", "expires=", "token=", "skid="))


def redact_any(value: Any) -> Any:
    if isinstance(value, str):
        # A native text field can contain several paragraphs and URLs. Mask
        # each transport URL without truncating everything after its query.
        value = re.sub(
            r"https?://[^\s<>\"')]+", lambda match: redact_url(match[0]) if _looks_signed(match[0]) else match[0], value
        )
        return redact_text(value)
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            key_l = str(key).lower()
            if key_l in CREDENTIAL_FIELDS:
                out[key] = REDACTED if isinstance(item, str) else redact_any(item)
            elif key_l in {"url", "play_url", "subtitle_url"} and isinstance(item, str) and _looks_signed(item):
                out[key] = redact_url(item)
            else:
                out[key] = redact_any(item)
        return out
    if isinstance(value, list):
        return [redact_any(item) for item in value]
    return value


def load_tikhub_api_key() -> str | None:
    env = os.environ.get("TIKHUB_API_KEY", "").strip()
    if env:
        return env
    return _load_keychain(KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT)


def api_key_present() -> bool:
    return bool(load_tikhub_api_key())


def _load_keychain(service: str, account: str) -> str | None:
    try:
        result = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-s",
                service,
                "-a",
                account,
                "-w",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    secret = result.stdout.strip()
    return secret or None
