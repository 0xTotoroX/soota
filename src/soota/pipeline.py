from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from soota.billing import PaymentGate, actual_cost, provider_enabled, selected_provider
from soota.config import SootaSettings, ensure_dirs
from soota.counterpart import youtube_counterpart
from soota.errors import AuthBudgetError, CaptionUnavailable, InputError, SootaError, SourceError
from soota.models import (
    AnalysisManifest,
    AudioAsset,
    CommandResult,
    ContentBundleManifest,
    MediaItem,
    ProcessingManifest,
    RequestRecord,
    TranscriptDocument,
    TranscriptManifest,
    TranscriptMode,
    TranscriptSource,
    TranscriptStatus,
)
from soota.providers.audio import audio_provider as make_audio_provider
from soota.providers.audio import conditional_audio_estimate, selected_audio_provider
from soota.providers.mcp_client import StdioMCPClient
from soota.providers.monid.provider import MonidProvider
from soota.providers.opencli import OpenCLIProvider
from soota.providers.opencli import capabilities_for as opencli_capabilities
from soota.providers.opencli_mcp import OpenCLIMCPProvider
from soota.providers.opencli_mcp import capabilities_for as mcp_capabilities
from soota.providers.opencli_runtime import OpenCLIRuntime, RuntimeUnavailable
from soota.providers.priority import ordered_names, quote_rows
from soota.providers.router import RequestBudget, inspect_item, remaining_budget, validate_policy
from soota.providers.tikhub.capability import capabilities_for
from soota.providers.tikhub.client import TikHubProvider
from soota.providers.ytdlp import YtDlpProvider
from soota.providers.ytdlp import capabilities_for as ytdlp_capabilities
from soota.render.bundle import write_bundle
from soota.render.markdown import render_transcript
from soota.routing import recognize
from soota.secrets import api_key_present, load_tikhub_api_key
from soota.storage.atomic import atomic_write
from soota.storage.cache import CacheStore
from soota.storage.jobs import JobStore
from soota.storage.ledger import CostLedger
from soota.transcripts.acquisition import acquire_captions
from soota.transcripts.asr import AsrEngine, resolve_engine
from soota.transcripts.promotions import filter_promotions, parse_ranges
from soota.transcripts.resolver import TranscriptResolver, resumes_audio

INGESTABLE_KINDS = {"video", "shortlink"}


def _provider(
    settings: SootaSettings,
    cache: CacheStore,
    requester: Callable[..., Any] | None = None,
    max_cost: float | None = None,
) -> TikHubProvider:
    selected_provider(settings, "tikhub")
    return TikHubProvider(
        runtime_python=settings.tikhub.runtime_python,
        cache=cache,
        api_key=load_tikhub_api_key(),
        base_url=settings.tikhub.base_url,
        timeout=settings.tikhub.timeout_seconds,
        requester=requester,
        preferred_language=settings.default_language,
        before_request=RequestBudget(max_cost).reserve if max_cost is not None else None,
        max_retries=0,
        payment_gate=PaymentGate(settings, max_cost),
    )


def doctor(settings: SootaSettings) -> CommandResult:
    ensure_dirs(settings)
    from soota import TIKHUB_SDK_VERSION, __version__

    tikhub_ok = False
    tikhub_ver = None
    try:
        import tikhub

        tikhub_ok = True
        tikhub_ver = getattr(tikhub, "__version__", None) or TIKHUB_SDK_VERSION
    except ImportError:
        tikhub_ok = False
    bundled_version = tikhub_ver
    if settings.tikhub.runtime_python:
        from soota.providers.managed import TikHubRuntime

        try:
            tikhub_ver = TikHubRuntime(settings).probe(settings.tikhub.runtime_python)
            tikhub_ok = True
        except SootaError:
            tikhub_ver = None
            tikhub_ok = False
    asr_engine = resolve_engine(settings.asr)
    data = {
        "soota_version": __version__,
        "opencli_mcp": {
            "enabled": settings.opencli_mcp.enabled,
            "installed": bool(
                shutil.which(settings.opencli_mcp.binary or settings.opencli_mcp.runtime_binary or "opencli-mcp")
            ),
        },
        "opencli": OpenCLIRuntime(settings).status(),
        "yt-dlp": {
            "yt_dlp_installed": bool(shutil.which(settings.local.binary)),
            "capabilities": [
                "youtube.video",
                "youtube.captions",
                "youtube.audio",
                "bilibili.video",
                "bilibili.captions",
                "bilibili.audio",
            ],
        },
        "tikhub_sdk_required": TIKHUB_SDK_VERSION,
        "tikhub_sdk_bundled": bundled_version,
        "tikhub_sdk_managed": bool(settings.tikhub.runtime_python),
        "automatic_updates": settings.updates.model_dump(),
        "tikhub_sdk_installed": tikhub_ver,
        "tikhub_import_ok": tikhub_ok,
        "api_key_present": api_key_present(),
        "ffmpeg": bool(shutil.which("ffmpeg")),
        "whisper_cli": bool(shutil.which(settings.asr.whisper_bin)),
        "mlx_whisper_cli": bool(shutil.which(settings.asr.mlx_whisper_bin)),
        "asr_engine": asr_engine.name,
        "audio_provider": settings.audio.provider,
        "asr_model": settings.asr.model,
        "config_path": str(settings.config_path),
        "cache_dir": str(settings.cache_dir),
        "data_dir": str(settings.data_dir),
        "output_dir": str(settings.output_dir),
    }
    warnings: list[str] = []
    if not data["api_key_present"]:
        warnings.append("未检测到 TikHub API Key；OpenCLI 能力独立可用，TikHub 路径需要密钥。")
    if not data["ffmpeg"]:
        warnings.append("未找到 ffmpeg，无字幕音频抽取可能失败。")
    if not data["whisper_cli"] and asr_engine.name == "whisper-cli":
        warnings.append("未找到 whisper CLI；无字幕路径在运行 ASR 前会失败。")
    if not data["mlx_whisper_cli"] and asr_engine.name == "mlx-whisper":
        warnings.append("未找到 mlx_whisper CLI；检查安装或 asr.mlx_whisper_bin。")
    return CommandResult(
        ok=True,
        status="success",
        code="ok",
        exit_code=0,
        message="doctor 完成。",
        data=data,
        warnings=warnings,
    )


def inspect(
    settings: SootaSettings,
    url: str,
    *,
    fetch: bool = True,
    requester: Callable[..., Any] | None = None,
    provider: str | None = None,
    fallback: str | None = None,
    max_cost: float | None = None,
) -> CommandResult:
    provider = selected_provider(settings, provider)
    validate_policy(provider, fallback, max_cost)
    source = recognize(url)
    capabilities = {
        **({"opencli-mcp": mcp_capabilities(source).model_dump()} if settings.opencli_mcp.enabled else {}),
        "opencli": opencli_capabilities(source).model_dump(),
        "yt-dlp": ytdlp_capabilities(source).model_dump(),
        "tikhub": capabilities_for(source).model_dump(),
    }
    data: dict[str, Any] = {"source": source.model_dump(), "capabilities": capabilities, "metadata": None}
    if not fetch or source.kind not in INGESTABLE_KINDS:
        return CommandResult(ok=True, status="success", code="ok", exit_code=0, message="已识别来源。", data=data)
    item, records, warnings = inspect_item(
        settings, source, provider=provider, fallback=fallback, max_cost=max_cost, requester=requester
    )
    data.update(
        source=item.source.model_dump(),
        metadata=item.model_dump(mode="json", exclude={"source"}),
        provider=item.provenance.get("provider"),
        requests=[r.model_dump() for r in records],
    )
    CostLedger(settings.ledger_path).append(item.source.id, records)
    return CommandResult(
        ok=True, status="success", code="ok", exit_code=0, message="inspect 完成。", data=data, warnings=warnings
    )


def estimate(
    settings: SootaSettings,
    url: str,
    *,
    mode: str = "ingest",
    provider: str | None = None,
    transcript: TranscriptMode = TranscriptMode.AUTO,
    audio_provider: str | None = None,
    max_items: int = 100,
    max_pages: int = 10,
    requester: Callable[..., Any] | None = None,
) -> CommandResult:
    provider = selected_provider(settings, provider)
    audio_name = selected_audio_provider(settings, audio_provider)
    if mode == "catalog":
        from soota.catalog import estimate_catalog

        return estimate_catalog(settings, url, max_items=max_items, max_pages=max_pages, provider=provider)
    if mode != "ingest":
        raise InputError("--mode 只支持 ingest 或 catalog。")
    source = recognize(url)
    validate_policy(provider, None, None)
    ability = "inspect" if transcript in {TranscriptMode.NONE, TranscriptMode.ASR} else "captions"
    available = [
        n
        for n, cap in (
            ("opencli-mcp", mcp_capabilities(source)),
            ("opencli", opencli_capabilities(source)),
            ("yt-dlp", ytdlp_capabilities(source)),
        )
        if getattr(cap, ability)
    ]
    if capabilities_for(source).inspect:
        available += ["tikhub", "monid"]
    rank = ordered_names(settings, source, ability, available) if provider == "auto" else [provider]
    if not rank:
        raise SourceError("没有可用来源。")
    if rank[0] == "monid":
        row = quote_rows(settings, source, ability, ["monid"])[0]
        audio_quote = (
            conditional_audio_estimate(source, settings, audio_name)
            if (transcript in {TranscriptMode.AUTO, TranscriptMode.ASR} and audio_name in {"tikhub", "monid", "auto"})
            else None
        )
        if audio_quote:
            audio_cost = audio_quote["estimated_cost_usd"]
            row["estimated_cost_usd"] = (
                round(row["estimated_cost_usd"] + audio_cost, 9)
                if row["estimated_cost_usd"] is not None and isinstance(audio_cost, (int, float))
                else None
            )
        return CommandResult(
            ok=True,
            status="success",
            code="ok",
            exit_code=0,
            message="Monid 报价预估；执行前重新核实报价，无字幕结果也可能收费。",
            data={
                **row,
                "source": source.model_dump(),
                "routing": rank,
                "audio_provider": audio_name,
                "conditional_audio": audio_quote,
            },
        )
    if provider == "auto":
        provider = rank[0]
    mcp = (provider == "opencli-mcp" or provider == "auto" and StdioMCPClient.available(settings.opencli_mcp)) and (
        mcp_capabilities(source).inspect
    )
    opencli = provider in {"auto", "opencli"} and opencli_capabilities(source).inspect
    local = provider in {"auto", "yt-dlp"} and ytdlp_capabilities(source).inspect
    if (
        provider != "tikhub"
        and (transcript != TranscriptMode.ASR or audio_name != "tikhub")
        and (mcp or opencli or local)
    ):
        free_name = "opencli-mcp" if mcp else "opencli" if opencli else "yt-dlp"
        conditional_audio = (
            conditional_audio_estimate(source, settings, audio_name)
            if transcript in {TranscriptMode.AUTO, TranscriptMode.ASR} and audio_name in {"tikhub", "monid", "auto"}
            else None
        )
        return CommandResult(
            ok=True,
            status="success",
            code="ok",
            exit_code=0,
            message="采集预估完成；未联网。条件音频费用仅在确认无字幕后适用。",
            data={
                "provider": free_name,
                "audio_provider": audio_name,
                "estimated_cost_usd": conditional_audio["estimated_cost_usd"] if conditional_audio else 0,
                "caption_estimated_cost_usd": 0,
                "conditional_audio": conditional_audio,
                "request_count": 1 if transcript == TranscriptMode.NONE else 2,
                "source": source.model_dump(),
                "endpoints": [f"{free_name}:{source.platform}/video"]
                + (
                    []
                    if transcript == TranscriptMode.NONE
                    else [
                        f"{audio_name}:{source.platform}/audio"
                        if transcript == TranscriptMode.ASR
                        else f"{free_name}:{source.platform}/"
                        + ("subtitle" if source.platform == "bilibili" else "transcript")
                    ]
                ),
            },
        )
    if provider in {"opencli-mcp", "opencli", "yt-dlp"} and not (
        transcript == TranscriptMode.ASR and audio_name == "tikhub"
    ):
        raise SourceError(f"{provider} 尚未接入该来源或音频下载；可显式选择 TikHub。")
    cache = CacheStore(settings.cache_dir)
    client = _provider(settings, cache, requester=requester)
    need_captions = transcript in {TranscriptMode.AUTO, TranscriptMode.CAPTIONS_ONLY}
    need_audio = transcript in {TranscriptMode.AUTO, TranscriptMode.ASR} and audio_name == "tikhub"
    saved_transcript = cache.get_json(source, "transcript") or {}
    if resumes_audio(saved_transcript, transcript, audio_name):
        need_captions = False
    if (
        saved_transcript.get("status") == TranscriptStatus.READY
        and saved_transcript.get("segments")
        and saved_transcript.get("source") != TranscriptSource.ASR
        and transcript != TranscriptMode.ASR
    ):
        endpoints: list[str] = []
    else:
        endpoints = client.estimate_requests(source, need_captions=need_captions, need_audio=need_audio)
        if cache.has(source, "inspect"):
            endpoints = [
                e
                for e in endpoints
                if e
                not in {
                    "/api/v1/youtube/web_v2/get_video_info_v2",
                    "/api/v1/bilibili/web/fetch_one_video",
                    "/api/v1/bilibili/web/fetch_one_video_v3",
                }
            ]
        if (cache.get_json(source, "captions") or {}).get("status") == TranscriptStatus.READY:
            endpoints = [e for e in endpoints if "caption" not in e and "subtitle" not in e]
        if cache.has(source, "audio") or cache.audio_path(source).is_file():
            endpoints = [e for e in endpoints if "stream" not in e and "playurl" not in e]
    cost, basis = client.estimate_cost(endpoints)
    other_audio = (
        conditional_audio_estimate(source, settings, audio_name)
        if (transcript in {TranscriptMode.AUTO, TranscriptMode.ASR} and audio_name in {"monid", "auto"})
        else None
    )
    if other_audio:
        amount = other_audio["estimated_cost_usd"]
        cost = round(cost + amount, 9) if cost is not None and isinstance(amount, (float, int)) else None
    warnings: list[str] = []
    wallet_view: dict[str, Any] | None = None
    try:
        wallet = client.get_wallet() if endpoints else None
    except Exception:
        warnings.append("无法读取 TikHub 余额，估价未与钱包核对。")
        wallet = None
    if wallet is not None:
        wallet_view = client.assess_wallet(endpoints, wallet)
        if wallet_view.get("sufficient") is False:
            paid_need = wallet_view.get("paid_required_usd")
            warnings.append(
                f"TikHub 余额不足：预计需要付费余额 {paid_need} USD，"
                f"当前付费余额 {wallet_view.get('balance')}、免费额度 {wallet_view.get('free_credit')}。"
                "免费额度不能用于不接受它的接口。"
            )
    short = bool(wallet_view and wallet_view.get("sufficient") is False)
    return CommandResult(
        ok=True,
        status="success",
        code="ok",
        exit_code=0,
        message="估价完成。",
        hint="到 https://user.tikhub.io/dashboard/add-credit 充值后再 ingest。" if short else None,
        data={
            "mode": mode,
            "source": source.model_dump(),
            "endpoints": endpoints,
            "request_count": len(endpoints),
            "estimated_cost_usd": cost,
            "price_basis": basis,
            "conditional_audio": other_audio,
            "cache_hit": not endpoints,
            "wallet": wallet_view,
        },
        warnings=warnings,
    )


def ingest(
    settings: SootaSettings,
    url: str,
    *,
    transcript: TranscriptMode = TranscriptMode.AUTO,
    audio_provider: str | None = None,
    provider: str | None = None,
    fallback: str | None = None,
    output: Path | None = None,
    max_cost: float | None = None,
    dry_run: bool = False,
    force: bool = False,
    requester: Callable[..., Any] | None = None,
    asr: AsrEngine | None = None,
    prefer_youtube: bool | None = None,
    exclude_promotions: bool | None = None,
    exclude_ranges: list[str] | None = None,
    record_job: bool = True,
    audio_asset: AudioAsset | None = None,
) -> CommandResult:
    parse_ranges(exclude_ranges or [])
    provider = selected_provider(settings, provider)
    audio_name = selected_audio_provider(settings, audio_provider)
    validate_policy(provider, fallback, max_cost)
    if provider in {"tikhub", "monid"} or transcript == TranscriptMode.ASR:
        if prefer_youtube or exclude_promotions or exclude_ranges:
            raise InputError("跨平台字幕选择和推广过滤当前用于 auto/opencli 字幕路径；请先以 captions-only 获取。")
        # An explicit provider/ASR choice takes priority over a saved free-caption preference.
        prefer_youtube = exclude_promotions = False
    else:
        prefer_youtube = settings.content_selection.prefer_youtube if prefer_youtube is None else prefer_youtube
        exclude_promotions = prefer_youtube if exclude_promotions is None else exclude_promotions
    if provider == "auto" and fallback is None and max_cost != 0 and provider_enabled(settings, "tikhub"):
        fallback = "tikhub"
    if provider != "tikhub":
        if dry_run:
            result = estimate(
                settings, url, transcript=transcript, provider=provider, requester=requester, audio_provider=audio_name
            )
            result.data["prefer_youtube"] = prefer_youtube
            result.data["exclude_promotions"] = exclude_promotions
            if prefer_youtube:
                result.warnings.append("跨平台模式会额外进行有限的频道与视频查询；dry-run 不执行搜索。")
            return result
        return ingest_free(
            settings,
            url,
            provider=provider,
            fallback=fallback,
            max_cost=max_cost,
            output=output,
            force=force,
            requester=requester,
            transcript=transcript,
            asr=asr,
            audio_provider=audio_name,
            prefer_youtube=prefer_youtube,
            exclude_promotions=exclude_promotions,
            exclude_ranges=exclude_ranges or [],
            record_job=record_job,
            audio_asset=audio_asset,
        )
    ensure_dirs(settings)
    source = recognize(url)
    if source.kind not in INGESTABLE_KINDS:
        raise SourceError("ingest 只摄取单条视频。", hint="频道目录请使用 catalog URL --dry-run。")
    caps = capabilities_for(source)
    if not caps.inspect:
        raise SourceError("该来源不支持摄取。")
    guessed = estimate(
        settings,
        url,
        mode="ingest",
        transcript=transcript,
        provider="tikhub",
        requester=requester,
        audio_provider=audio_name,
    )
    estimated = guessed.data.get("estimated_cost_usd")
    if max_cost is not None:
        over = estimated is None or float(estimated) > max_cost
        if over:
            raise AuthBudgetError(
                f"预计费用 {estimated} USD 超过 --max-cost {max_cost}，或价格未知。",
                hint="提高上限、先 estimate，或等缓存命中后再跑。未知单价时硬限制拒绝执行。",
            )
    wallet_view = guessed.data.get("wallet") if isinstance(guessed.data, dict) else None
    if isinstance(wallet_view, dict) and wallet_view.get("sufficient") is False and not dry_run:
        raise AuthBudgetError(
            (
                f"TikHub 付费余额不足：预计需要付费余额 {wallet_view.get('paid_required_usd')} USD，"
                f"当前 {wallet_view.get('balance')}（免费额度 {wallet_view.get('free_credit')} "
                "不能用于这些接口）。"
            ),
            hint="到 https://user.tikhub.io/dashboard/add-credit 充值后再 ingest。不要重试会 402 的计费接口。",
            details={"wallet": wallet_view, "estimated_cost_usd": estimated},
        )
    if dry_run:
        return guessed.model_copy(update={"message": "dry-run：未发起付费请求。"})

    output_root = (output or settings.output_dir).expanduser()
    cache = CacheStore(settings.cache_dir)
    client = _provider(settings, cache, requester=requester, max_cost=max_cost)
    downloader = None
    try:
        item = client.inspect(source)
        downloader = make_audio_provider(
            settings,
            audio_name,
            lambda: client,
            asset=audio_asset,
            max_cost=remaining_budget(max_cost, client.records),
            remaining_cost=lambda: remaining_budget(max_cost, client.records),
        )
        resolver = TranscriptResolver(client, cache, settings, asr=asr, audio_provider=downloader)
        document = resolver.resolve(item, transcript)
    except Exception:
        if downloader is not None and downloader is not client:
            client.records.extend(downloader.records)
        if client.records:
            CostLedger(settings.ledger_path).append(source.id, client.records)
        raise
    if downloader is not None and downloader is not client:
        client.records.extend(downloader.records)
    billed = [r for r in client.records if r.billed and not r.cache_hit]
    cost = sum(r.estimated_cost_usd or 0.0 for r in billed)
    manifest = ContentBundleManifest(
        source=item.source,
        transcript=TranscriptManifest(
            status=document.status,
            source=document.source,
            language=document.language,
            segments=len(document.segments),
        ),
        analysis=AnalysisManifest(),
        processing=ProcessingManifest(
            provider="tikhub",
            cache_hit=all(r.cache_hit or not r.billed for r in client.records) and bool(client.records),
            estimated_cost_usd=cost,
            actual_cost_usd=None,
            request_count=len(billed),
            price_basis=f"snapshot:{client.snapshot.get('as_of')}",
            requests=client.records,
        ),
    )
    manifest.metadata_from_item(item)
    manifest.metadata["audio_provider"] = item.provenance.get("audio_provider")
    CostLedger(settings.ledger_path).append(item.source.id, client.records)
    folder = write_bundle(output_root, item, document, manifest, None, force=force)
    if record_job:
        JobStore(settings.jobs_dir).create(
            {
                "status": "completed"
                if document.status in {TranscriptStatus.READY, TranscriptStatus.NOT_REQUESTED}
                else "partial",
                "source": item.source.model_dump(),
                "output": str(folder),
                "transcript_status": document.status,
            }
        )
    status: Literal["success", "partial"] = (
        "success" if document.status in {TranscriptStatus.READY, TranscriptStatus.NOT_REQUESTED} else "partial"
    )
    code = "ok" if status == "success" else "transcript_" + document.status.value
    return CommandResult(
        ok=status == "success",
        status=status,
        code=code,
        exit_code=0 if status == "success" else 2,
        message="摄取完成。" if status == "success" else f"已保存元数据，字幕状态为 {document.status.value}。",
        warnings=manifest.processing.warnings,
        data={
            "output": str(folder),
            "manifest": str(folder / "manifest.json"),
            "transcript": str(folder / "transcript.md"),
            "context": str(folder / "context.md"),
            "content": str(folder / "context.md"),
            "analysis": manifest.analysis.model_dump(mode="json"),
            "source": item.source.model_dump(),
            "transcript_status": document.status,
            "transcript_source": document.source,
            "audio_provider": item.provenance.get("audio_provider"),
            "audio_provider_requested": audio_name,
            "cache_hit": manifest.processing.cache_hit,
            "request_count": manifest.processing.request_count,
            "estimated_cost_usd": manifest.processing.estimated_cost_usd,
            "actual_cost_usd": manifest.processing.actual_cost_usd,
            "requests": [r.model_dump() for r in client.records],
        },
    )


def ingest_free(
    settings: SootaSettings,
    url: str,
    *,
    provider: str,
    fallback: str | None,
    max_cost: float | None,
    output: Path | None,
    force: bool,
    requester: Callable[..., Any] | None,
    transcript: TranscriptMode,
    asr: AsrEngine | None,
    audio_provider: str = "yt-dlp",
    prefer_youtube: bool = False,
    exclude_promotions: bool = False,
    exclude_ranges: list[str] | None = None,
    record_job: bool = True,
    audio_asset: AudioAsset | None = None,
) -> CommandResult:
    source = recognize(url)
    records: list[RequestRecord] = []
    warnings: list[str] = []
    document = TranscriptDocument(status=TranscriptStatus.UNKNOWN)
    item: MediaItem | None = None
    missing_reason: str | None = None
    attempted: list[str] = []
    confirmed_absent = False
    if transcript in {TranscriptMode.NONE, TranscriptMode.ASR}:
        item, records, warnings = inspect_item(
            settings,
            source,
            provider=provider,
            fallback=fallback,
            max_cost=max_cost,
            requester=requester,
            caption_metadata=False,
        )
        attempted.append(str(item.provenance.get("provider", provider)))
        if transcript == TranscriptMode.NONE:
            document = TranscriptDocument(status=TranscriptStatus.NOT_REQUESTED)
    else:
        candidates: list[tuple[str, Any]] = []
        if (
            provider_enabled(settings, "opencli-mcp")
            and provider in {"auto", "opencli-mcp"}
            and mcp_capabilities(source).captions
        ):
            candidates.append(("opencli-mcp", OpenCLIMCPProvider))
        if (
            provider_enabled(settings, "opencli")
            and provider in {"auto", "opencli"}
            and opencli_capabilities(source).captions
        ):
            candidates.append(("opencli", OpenCLIProvider))
        if (
            provider_enabled(settings, "yt-dlp")
            and provider in {"auto", "yt-dlp"}
            and ytdlp_capabilities(source).captions
        ):
            candidates.append(("yt-dlp", YtDlpProvider))
        if provider in {"auto", "monid"} and capabilities_for(source).captions:
            if provider_enabled(settings, "monid") and (max_cost != 0 or provider == "monid"):
                candidates.append(("monid", lambda st: MonidProvider(st, remaining_budget(max_cost, records))))
            if provider == "auto" and fallback == "tikhub" and max_cost != 0:
                candidates.append(
                    (
                        "tikhub",
                        lambda st: _provider(
                            st,
                            CacheStore(st.cache_dir),
                            requester=requester,
                            max_cost=remaining_budget(max_cost, records),
                        ),
                    )
                )
        if provider == "auto":
            rank = ordered_names(settings, source, "captions", [n for n, _ in candidates], max_cost=max_cost)
            candidates = sorted([(n, f) for n, f in candidates if n in rank], key=lambda c: rank.index(c[0]))
        for name, factory in candidates:
            if (
                provider == "auto"
                and name in {"tikhub", "monid"}
                and not ordered_names(
                    settings, source, "captions", [name], max_cost=remaining_budget(max_cost, records)
                )
            ):
                warnings.append(f"剩余预算不足以覆盖 {name} 的完整字幕请求链，已跳过。")
                continue
            attempted.append(name)
            client: Any = None
            counterpart_fallback: YtDlpProvider | None = None
            try:
                client = factory(settings)
                candidate_item = client.inspect(source)
                candidate_item.provenance.setdefault("provider", name)
                item = candidate_item
                captions = None
                if prefer_youtube and name == "opencli" and source.platform == "bilibili":
                    assert isinstance(client, OpenCLIProvider)
                    counterpart_fallback = YtDlpProvider(settings) if provider == "auto" else None
                    candidate_item, captions, notes = youtube_counterpart(client, candidate_item, counterpart_fallback)
                    item = candidate_item
                    warnings.extend(notes)
                captions = captions or acquire_captions(client, candidate_item)
                document = TranscriptDocument(
                    status=captions.status,
                    source=captions.source,
                    language=captions.language,
                    segments=captions.segments,
                )
                if captions.status == TranscriptStatus.READY:
                    missing_reason = None
                    if captions.language is None or captions.source is None or captions.source.value == "unknown":
                        warnings.append(f"{name} 未标明实际字幕语言及人工/自动轨道类型，已保留为未知。")
                    break
                confirmed_absent = captions.status == TranscriptStatus.MISSING
                item.provenance["caption_status"] = captions.status.value
                item.provenance["caption_reason"] = captions.reason
                missing_reason = f"{name} 已确认没有字幕轨道。" if confirmed_absent else f"{name} 未能确定字幕可用性。"
                if confirmed_absent or captions.status == TranscriptStatus.PENDING:
                    break
            except CaptionUnavailable:
                if provider != "auto":
                    raise
                missing_reason = f"{name} 字幕获取失败，尝试下一允许的来源。"
                warnings.append(missing_reason)
            except RuntimeUnavailable as exc:
                if provider != "auto" and fallback != "tikhub":
                    raise
                missing_reason = f"{name} 不可用：{exc.message}"
                warnings.append(missing_reason)
            except Exception:
                CostLedger(settings.ledger_path).append(
                    source.id, [*records, *(client.records if client is not None else [])]
                )
                raise
            finally:
                if client is not None:
                    records.extend(client.records)
                if counterpart_fallback is not None and counterpart_fallback.records:
                    records.extend(counterpart_fallback.records)
                    attempted.append("yt-dlp")
        if not candidates:
            missing_reason = "所选来源尚未接入该视频字幕或费用超过预算。"
    if transcript == TranscriptMode.ASR or confirmed_absent and transcript == TranscriptMode.AUTO:
        assert item is not None
        downloader = make_audio_provider(
            settings,
            audio_provider,
            lambda: _provider(
                settings,
                CacheStore(settings.cache_dir),
                requester=requester,
                max_cost=remaining_budget(max_cost, records),
            ),
            asset=audio_asset,
            max_cost=remaining_budget(max_cost, records),
        )
        try:
            resolver = TranscriptResolver(
                None, CacheStore(settings.cache_dir), settings, asr=asr, audio_provider=downloader
            )
            document = resolver.run_asr(item)
        except Exception:
            CostLedger(settings.ledger_path).append(item.source.id, [*records, *downloader.records])
            raise
        records.extend(downloader.records)
        item.provenance["audio_provider_requested"] = audio_provider
        item.provenance["asr_engine"] = (asr or resolve_engine(settings.asr)).name
        missing_reason = None
    if missing_reason and not confirmed_absent:
        if (
            fallback == "tikhub"
            and "tikhub" not in attempted
            and "monid" not in attempted
            and document.status != TranscriptStatus.PENDING
        ):
            if prefer_youtube or exclude_promotions or exclude_ranges:
                raise SourceError("免费字幕流程未完成；跨平台或过滤任务未自动转入付费路径。")
            result = ingest(
                settings,
                url,
                provider="tikhub",
                transcript=transcript,
                output=output,
                max_cost=max_cost,
                force=force,
                requester=requester,
                asr=asr,
                audio_provider=audio_provider,
                record_job=record_job,
                audio_asset=audio_asset,
            )
            result.warnings.insert(0, missing_reason + " 已按费用策略使用 TikHub 兜底。")
            result.data["routing"] = {
                "attempted": [*attempted, "tikhub"],
                "free_requests": [r.model_dump() for r in records],
            }
            CostLedger(settings.ledger_path).append(source.id, records)
            return result
        if item is None:
            raise SourceError(missing_reason, hint="检查免费来源，或显式选择 TikHub；不会自动产生付费请求。")
        warnings.append(missing_reason + " 已保留元数据；未执行本地转写。")
    elif confirmed_absent and transcript == TranscriptMode.CAPTIONS_ONLY:
        warnings.append("已确认没有字幕轨道；captions-only 不下载音频、不转写，也不查询下一字幕来源。")
    assert item is not None
    original_document = None
    filter_report = None
    if document.status == TranscriptStatus.READY and (exclude_promotions or exclude_ranges):
        original_document = document
        document, filter_report, notes = filter_promotions(
            item, document, automatic=exclude_promotions, ranges=parse_ranges(exclude_ranges or [])
        )
        warnings.extend(notes)
        item.provenance["promotion_filter"] = filter_report
    ready = document.status == TranscriptStatus.READY or transcript == TranscriptMode.NONE
    manifest = ContentBundleManifest(
        source=item.source,
        transcript=TranscriptManifest(
            status=document.status, source=document.source, language=document.language, segments=len(document.segments)
        ),
        processing=ProcessingManifest(
            provider=str(item.provenance["provider"]),
            requests=records,
            cache_hit=bool(records) and all(r.cache_hit for r in records),
            estimated_cost_usd=sum(r.estimated_cost_usd or 0 for r in records if r.billed and not r.cache_hit),
            actual_cost_usd=actual_cost(records),
            request_count=len([r for r in records if not r.cache_hit]),
            warnings=warnings,
        ),
    )
    manifest.metadata_from_item(item)
    manifest.metadata["provenance"] = item.provenance
    output_root = (output or settings.output_dir).expanduser()
    CostLedger(settings.ledger_path).append(item.source.id, records)
    previous_manifest = output_root / item.source.platform / item.source.id / "manifest.json"
    if previous_manifest.is_file() and not force:
        previous = ContentBundleManifest.model_validate_json(previous_manifest.read_text(encoding="utf-8"))
        old_filter = previous.metadata.get("provenance", {}).get("promotion_filter")
        if old_filter != filter_report:
            raise InputError("推广过滤结果变化；请使用新输出目录，或 --force 重建内容包，避免保留不匹配的旧总结。")
    folder = write_bundle(output_root, item, document, manifest, None, force=force)
    if original_document is not None:
        atomic_write(folder / "transcript.original.md", render_transcript(item, original_document))
        manifest.files["transcript.original.md"] = "transcript.original.md"
        atomic_write(folder / "manifest.json", manifest.model_dump_json(indent=2) + "\n")
    if record_job:
        JobStore(settings.jobs_dir).create(
            {
                "status": "completed" if ready else "partial",
                "source": item.source.model_dump(),
                "output": str(folder),
                "provider": item.provenance["provider"],
                "transcript_status": document.status if transcript != TranscriptMode.NONE else "not_requested",
            }
        )
    return CommandResult(
        ok=ready,
        status="success" if ready else "partial",
        code="ok" if ready else "transcript_" + document.status.value,
        exit_code=0 if ready else 2,
        message="内容包已保存。" if ready else f"已保存元数据，字幕状态为 {document.status.value}。",
        warnings=manifest.processing.warnings,
        data={
            "output": str(folder),
            "manifest": str(folder / "manifest.json"),
            "context": str(folder / "context.md"),
            "content": str(folder / "context.md"),
            "analysis": manifest.analysis.model_dump(mode="json"),
            "transcript": str(folder / "transcript.md"),
            "provider": item.provenance["provider"],
            "routing": {"attempted": attempted},
            "selection": item.provenance.get("selection"),
            "youtube_check": item.provenance.get("youtube_check"),
            "promotion_filter": filter_report,
            "transcript_status": document.status,
            "transcript_source": document.source,
            "audio_provider": item.provenance.get("audio_provider"),
            "audio_provider_requested": audio_provider,
            "asr_engine": item.provenance.get("asr_engine"),
            "estimated_cost_usd": manifest.processing.estimated_cost_usd,
            "actual_cost_usd": manifest.processing.actual_cost_usd,
            "cache_hit": manifest.processing.cache_hit,
            "request_count": manifest.processing.request_count,
            "requests": [r.model_dump() for r in records],
        },
    )


def render(settings: SootaSettings, *, bundle: Path, analysis: Path, force: bool = False) -> CommandResult:
    """Compatibility entry; semantic rendering is owned by LoreLab."""
    from soota.workflow_compat import invoke

    return invoke("render", settings, bundle=bundle, analysis=analysis, force=force)


def _document_from_transcript_md(path: Path, manifest: ContentBundleManifest) -> TranscriptDocument:
    from soota.transcripts.captions import parse_transcript_markdown

    return parse_transcript_markdown(path.read_text(encoding="utf-8"), manifest.transcript)


def list_jobs(settings: SootaSettings) -> CommandResult:
    jobs = JobStore(settings.jobs_dir).list_jobs()
    jobs = [
        {
            **{
                key: job[key]
                for key in ("id", "kind", "status", "source", "created_at", "updated_at", "stop_reason")
                if key in job
            },
            "item_count": len(job.get("items", [])),
            "pages_fetched": job.get("pages_fetched", 0),
        }
        if job.get("kind") == "catalog"
        else job
        for job in jobs
    ]
    return CommandResult(
        ok=True,
        status="success",
        code="ok",
        exit_code=0,
        message=f"{len(jobs)} 个任务。",
        data={"jobs": jobs},
    )
