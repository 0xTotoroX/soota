"""
[INPUT]: User config, provider overrides and environment values.
[OUTPUT]: Validated settings, quote TTL and optional Monid/treg/Weir providers.
[POS]: Shared configuration models and loading; credentials remain external.
[PROTOCOL]: 变更时更新此头部，然后检查 AGENTS.md
"""

from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path
from typing import Any, Literal

import platformdirs
from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, ValidationError, field_validator

from soota.errors import InputError

APP_NAME = "Soota"
APP_AUTHOR = "soota"


class AsrConfig(BaseModel):
    engine: str = "auto"
    model: str = "base"
    device: str = "cpu"
    whisper_bin: str = "whisper"
    mlx_whisper_bin: str = "mlx_whisper"
    mlx_model_path: str | None = None


class AutoUpdateConfig(BaseModel):
    enabled: bool = True
    interval_seconds: int = Field(default=0, ge=0)


class TikHubConfig(BaseModel):
    runtime_python: str | None = Field(default=None, exclude=True)
    base_url: str = "https://api.tikhub.io"
    timeout_seconds: float = 60.0


class MonidConfig(BaseModel):
    enabled: bool = False
    base_url: str = "https://api.monid.ai"
    timeout_seconds: float = Field(default=60, gt=0)
    poll_seconds: float = Field(default=2, ge=0)
    max_polls: int = Field(default=30, ge=1)


class TregConfig(BaseModel):
    enabled: bool = False
    base_url: str = "https://treg.to"
    timeout_seconds: float = Field(default=90, gt=0)
    youtube_captions: Literal["crawl4ai", "scrapecreators"] = "crawl4ai"


class WeirConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    free_only: Literal[True] = True
    monthly_credits: int | None = Field(default=None, ge=0, strict=True)
    task_credits: int | None = Field(default=None, ge=0, strict=True)
    timeout_seconds: float = Field(default=60, gt=0)
    cache_seconds: int = Field(default=300, ge=0)


class PriorityRule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["cost", "manual"] = "cost"
    order: list[Literal["opencli-mcp", "opencli", "yt-dlp", "tikhub", "monid", "treg"]] = Field(default_factory=list)

    @field_validator("order")
    @classmethod
    def unique_order(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("来源顺序不能重复")
        return value


class OpenCLIConfig(BaseModel):
    binary: str | None = None
    profile: str | None = None
    timeout_seconds: float = Field(default=90, gt=0)
    max_output_bytes: int = Field(default=8_388_608, ge=1024)
    metadata_cache_seconds: int = Field(default=300, ge=0)


class OpenCLIMCPConfig(BaseModel):
    runtime_binary: str | None = Field(default=None, exclude=True)
    enabled: bool = False
    binary: str | None = None
    timeout_seconds: float = Field(default=180, gt=0)
    max_output_bytes: int = Field(default=8_388_608, ge=1024)
    metadata_cache_seconds: int = Field(default=300, ge=0)


class ContentSelectionConfig(BaseModel):
    prefer_youtube: bool = False


class LocalProviderConfig(BaseModel):
    binary: str = Field(default="yt-dlp", exclude=True)
    cookies_browser: str = Field(default="chrome", exclude=True)
    timeout_seconds: float = Field(default=90, gt=0)
    max_output_bytes: int = Field(default=8_388_608, ge=1024)
    cache_seconds: int = Field(default=300, ge=0)


class AudioConfig(BaseModel):
    provider: Literal["auto", "yt-dlp", "downie", "tikhub", "monid", "treg"] = "yt-dlp"
    timeout_seconds: float = Field(default=900, gt=0)


class RoutingConfig(BaseModel):
    price_ttl_seconds: int = Field(default=86400, gt=0)
    disabled_providers: set[
        Literal["opencli-mcp", "opencli", "yt-dlp", "tikhub", "monid", "treg", "weir", "downie", "http"]
    ] = Field(default_factory=set)
    default_provider: Literal["auto", "opencli-mcp", "opencli", "yt-dlp", "tikhub", "monid", "treg"] = "auto"
    priorities: dict[str, dict[str, PriorityRule]] = Field(default_factory=dict)

    @field_validator("default_provider", mode="before")
    @classmethod
    def legacy_provider(cls, value: Any) -> Any:
        return "yt-dlp" if value == "soota" else value


class PaymentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["ask", "auto"] = "ask"
    min_balance_usd: FiniteFloat | None = Field(default=None, ge=0)


class SootaSettings(BaseModel):
    output_dir: Path = Field(default_factory=lambda: Path.home() / "Documents" / "Soota")
    default_language: str = "zh"
    config_dir: Path
    cache_dir: Path
    data_dir: Path
    tikhub: TikHubConfig = Field(default_factory=TikHubConfig)
    monid: MonidConfig = Field(default_factory=MonidConfig)
    treg: TregConfig = Field(default_factory=TregConfig)
    weir: WeirConfig = Field(default_factory=WeirConfig)
    opencli: OpenCLIConfig = Field(default_factory=OpenCLIConfig)
    opencli_mcp: OpenCLIMCPConfig = Field(default_factory=OpenCLIMCPConfig)
    local: LocalProviderConfig = Field(default_factory=LocalProviderConfig)
    audio: AudioConfig = Field(default_factory=AudioConfig)
    routing: RoutingConfig = Field(default_factory=RoutingConfig)
    content_selection: ContentSelectionConfig = Field(default_factory=ContentSelectionConfig)
    payment: PaymentConfig = Field(default_factory=PaymentConfig)
    asr: AsrConfig = Field(default_factory=AsrConfig)
    updates: AutoUpdateConfig = Field(default_factory=AutoUpdateConfig)

    @property
    def config_path(self) -> Path:
        return self.config_dir / "config.toml"

    @property
    def jobs_dir(self) -> Path:
        return self.data_dir / "jobs"

    @property
    def ledger_path(self) -> Path:
        return self.data_dir / "ledger.jsonl"


def _env_path(name: str) -> Path | None:
    raw = os.environ.get(name, "").strip()
    return Path(raw).expanduser() if raw else None


def default_dirs() -> tuple[Path, Path, Path]:
    # Keep configuration in ~/.config/soota; cache and jobs use platform app directories.
    config_dir = _env_path("SOOTA_CONFIG_DIR") or Path.home() / ".config" / "soota"
    cache_dir = _env_path("SOOTA_CACHE_DIR") or Path(platformdirs.user_cache_dir(APP_NAME, APP_AUTHOR))
    data_dir = _env_path("SOOTA_DATA_DIR") or Path(platformdirs.user_data_dir(APP_NAME, APP_AUTHOR))
    return config_dir, cache_dir, data_dir


def load_settings(*, ignore_payment: bool = False) -> SootaSettings:
    config_dir, cache_dir, data_dir = default_dirs()
    path = config_dir / "config.toml"
    data: dict[str, Any] = {}
    if path.is_file():
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    soota = data.get("soota", data)
    payment = dict((data.get("tikhub") or {}).get("billing") or {})
    payment.pop("default_provider", None)
    policy_path = config_dir / "payment-policy.json"
    try:
        if ignore_payment:
            payment = {}
        elif policy_path.is_file():
            saved = json.loads(policy_path.read_text(encoding="utf-8"))
            if not isinstance(saved, dict):
                raise ValueError("expected policy object")
            # Old policy set coupled auto billing to a TikHub-first default.
            # Routing is now configured independently in [routing].
            saved.pop("default_provider", None)
            payment.update(saved)
        payment_config = PaymentConfig.model_validate(payment)
    except (ValueError, TypeError, ValidationError) as exc:
        raise InputError("费用策略文件无效，未执行任何请求。", hint="使用 soota policy set --mode ask 恢复。") from exc
    try:
        updates_config = AutoUpdateConfig.model_validate(
            {
                **(data.get("updates") or {}),
                **({"enabled": os.environ["SOOTA_AUTO_UPDATE"]} if "SOOTA_AUTO_UPDATE" in os.environ else {}),
            }
        )
        selection_config = ContentSelectionConfig.model_validate(
            {
                **(data.get("content_selection") or {}),
                **(
                    {"prefer_youtube": os.environ["SOOTA_PREFER_YOUTUBE"]}
                    if "SOOTA_PREFER_YOUTUBE" in os.environ
                    else {}
                ),
            }
        )
        routing_data = dict(data.get("routing") or {})
        provider_path = config_dir / "providers.json"
        provider_data = json.loads(provider_path.read_text(encoding="utf-8")) if provider_path.is_file() else {}
        if not isinstance(provider_data, dict):
            raise ValueError("providers.json 必须为对象")
        routing_data.update(provider_data.get("routing", {}))
        routing_config = RoutingConfig.model_validate(routing_data)
        monid_config = MonidConfig.model_validate({**(data.get("monid") or {}), **provider_data.get("monid", {})})
        treg_config = TregConfig.model_validate({**(data.get("treg") or {}), **provider_data.get("treg", {})})
        weir_config = WeirConfig.model_validate({**(data.get("weir") or {}), **provider_data.get("weir", {})})
        audio_config = AudioConfig.model_validate(data.get("audio") or {})
        mcp_config = OpenCLIMCPConfig.model_validate(
            {
                **(data.get("opencli_mcp") or {}),
                **(
                    {"binary": os.environ["SOOTA_OPENCLI_MCP_BINARY"]}
                    if os.environ.get("SOOTA_OPENCLI_MCP_BINARY")
                    else {}
                ),
            }
        )
    except (TypeError, ValueError) as exc:
        raise InputError("来源或 opencli-mcp 配置无效，未执行任何请求。") from exc
    output = _env_path("SOOTA_OUTPUT_DIR")
    settings = SootaSettings(
        output_dir=output or Path(soota.get("output_dir", Path.home() / "Documents" / "Soota")).expanduser(),
        default_language=str(soota.get("default_language", "zh")),
        config_dir=config_dir,
        cache_dir=cache_dir,
        data_dir=data_dir,
        payment=payment_config,
        updates=updates_config,
        routing=routing_config,
        monid=monid_config,
        treg=treg_config,
        weir=weir_config,
        audio=audio_config,
        content_selection=selection_config,
        tikhub=TikHubConfig(
            base_url=str((data.get("tikhub") or {}).get("base_url", "https://api.tikhub.io")),
            timeout_seconds=float((data.get("tikhub") or {}).get("timeout_seconds", 60.0)),
        ),
        opencli=OpenCLIConfig.model_validate(
            {
                **(data.get("opencli") or {}),
                **({"binary": os.environ["SOOTA_OPENCLI_BINARY"]} if os.environ.get("SOOTA_OPENCLI_BINARY") else {}),
                **({"profile": os.environ["OPENCLI_PROFILE"]} if os.environ.get("OPENCLI_PROFILE") else {}),
            }
        ),
        opencli_mcp=mcp_config,
        local=LocalProviderConfig(
            binary=os.environ.get("SOOTA_YTDLP_BINARY", "yt-dlp"),
            cookies_browser=os.environ.get("SOOTA_YTDLP_COOKIES_BROWSER", "chrome"),
        ),
        asr=AsrConfig(
            engine=str((data.get("asr") or {}).get("engine", "auto")),
            model=str((data.get("asr") or {}).get("model", "base")),
            device=str((data.get("asr") or {}).get("device", "cpu")),
            whisper_bin=str((data.get("asr") or {}).get("whisper_bin", "whisper")),
            mlx_whisper_bin=str((data.get("asr") or {}).get("mlx_whisper_bin", "mlx_whisper")),
            mlx_model_path=(data.get("asr") or {}).get("mlx_model_path"),
        ),
    )
    from soota.providers.managed import bind_runtimes

    bind_runtimes(settings)
    return settings


def ensure_dirs(settings: SootaSettings) -> None:
    for path in (settings.config_dir, settings.cache_dir, settings.data_dir, settings.jobs_dir, settings.output_dir):
        path.mkdir(parents=True, exist_ok=True)
