"""
[INPUT]: Task inputs, provider policy and artifact goals.
[OUTPUT]: Raw source text, transcript or media collection results.
[POS]: Shared task acquisition; local ASR passes timeline/repetition acceptance guards.
[PROTOCOL]: 变更时更新此头部，然后检查 AGENTS.md
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, TypedDict
from urllib.parse import unquote, urlsplit

from pydantic import ValidationError

from soota.artifacts.bundle import content_from_markdown, load_bundle, load_transcript
from soota.artifacts.models import ContentDocument
from soota.billing import PaymentGate, provider_enabled, selected_provider
from soota.config import SootaSettings
from soota.content.native import capture_content, capture_media, capture_operation, convert_content
from soota.content.native_bindings import media_binding
from soota.content.native_types import NativeOperation
from soota.errors import InputError, SourceError
from soota.media import MediaAsset, MediaDescriptor, MediaKind, MediaService
from soota.models import (
    AudioAsset,
    CaptionPayload,
    ContentBundleManifest,
    MediaItem,
    ProcessingManifest,
    SourceRef,
    TranscriptDocument,
    TranscriptMode,
    TranscriptSource,
    TranscriptStatus,
)
from soota.providers import opencli_commands
from soota.providers.downie import DownieProvider
from soota.providers.mcp_client import StdioMCPClient, require_enabled
from soota.providers.monid.provider import MonidProvider
from soota.providers.opencli_runtime import RuntimeUnavailable
from soota.providers.priority import ordered_names
from soota.providers.tikhub.client import TikHubProvider
from soota.providers.tikhub.normalize import pick_audio_url
from soota.providers.tikhub.registry import load_registry
from soota.providers.treg.provider import TregProvider
from soota.secrets import redact_any
from soota.storage.cache import CacheStore
from soota.tasks.inputs import source_for, subtitle_format
from soota.tasks.models import InputSpec, TaskSpec
from soota.transcripts.asr import resolve_engine
from soota.transcripts.captions import parse_transcript_markdown
from soota.transcripts.conversion import parse_payload
from soota.transcripts.quality import parse_asr_payload


class MediaOptions(TypedDict):
    min_height: int | None
    allow_silent: bool


@dataclass
class Collection:
    item: MediaItem
    document: TranscriptDocument | None = None
    original_document: TranscriptDocument | None = None
    content: ContentDocument | None = None
    processing: ProcessingManifest | None = None
    data: Any = None
    diagnostics: dict[str, Any] = field(default_factory=dict)
    attachments: list[Path] = field(default_factory=list)
    raw_body: str | None = None
    reference_base: str | None = None


@dataclass
class MediaBatch:
    assets: list[MediaAsset] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def safe_item(item: MediaItem) -> MediaItem:
    source = item.source.model_copy(
        update={"url": redact_any(item.source.url), "canonical_url": redact_any(item.source.canonical_url)}
    )
    return item.model_copy(update={"source": source, "provenance": redact_any(item.provenance)})


def public_text(text: str) -> str:
    # Redact URL tokens individually, never treat a whole article as one URL.
    return re.sub(r'https?://[^\s<>"\)]+', lambda match: str(redact_any(match[0])), text)


def native_operation(settings: SootaSettings, spec: TaskSpec, seed: InputSpec, work: Path) -> NativeOperation:
    operation = seed.operation or ""
    if operation.startswith(("tikhub.", "monid.", "treg.")):
        if spec.provider not in {"auto", "tikhub", "monid", "treg"}:
            raise InputError("原生接口与显式 Provider 不一致。")
        from soota.api import _validate

        endpoint = load_registry().resolve(operation.split(".", 1)[1])
        if not endpoint.callable or endpoint.deprecated:
            raise InputError("只允许当前已审查的读取接口。")
        _validate(seed.params, endpoint.query_schema, "查询参数")
        chosen = spec.provider
        if operation.startswith(("monid.", "treg.")):
            requested = operation.split(".", 1)[0]
            if chosen not in {"auto", requested}:
                raise InputError("原生操作与显式 Provider 不一致。")
            chosen = requested
        if chosen == "auto":
            source = SourceRef(platform=endpoint.path.split("/")[3], kind="video", id="api", url="", canonical_url="")
            from soota.storage.task_budget import remaining_active_budget

            choices = ordered_names(
                settings,
                source,
                "api",
                ["tikhub", "monid", "treg"],
                endpoint=endpoint.path,
                max_cost=remaining_active_budget(spec.max_cost),
            )
            if not choices:
                raise SourceError("没有可用的原生接口来源。")
            chosen = choices[0]
        selected_provider(settings, chosen)
        client = (
            (TregProvider if chosen == "treg" else MonidProvider)(settings, spec.max_cost, purpose="api")
            if chosen in {"monid", "treg"}
            else TikHubProvider(
                cache=CacheStore(settings.cache_dir),
                runtime_python=settings.tikhub.runtime_python,
                base_url=settings.tikhub.base_url,
                timeout=settings.tikhub.timeout_seconds,
                payment_gate=PaymentGate(settings, spec.max_cost),
                max_retries=0,
            )
        )
        client.endpoint_price(endpoint)
        raw = client.query(endpoint, seed.params)
        return NativeOperation(raw=raw, provider=chosen, operation=operation, workdir=work)
    if operation.startswith("opencli-mcp."):
        if spec.provider not in {"auto", "opencli-mcp"}:
            raise InputError("原生接口与显式 Provider 不一致。")
        selected_provider(settings, "opencli-mcp")
        require_enabled(settings.opencli_mcp)
        parts = operation.removeprefix("opencli-mcp.").split(".")
        if len(parts) != 2:
            raise InputError("MCP 操作使用 opencli-mcp.site.command。")
        raw = StdioMCPClient(settings.opencli_mcp).call_site_readonly(parts[0], parts[1], seed.params)
        return NativeOperation(raw=raw, provider="opencli-mcp", operation=operation, workdir=work)
    if spec.provider not in {"auto", "opencli"}:
        raise InputError("原生 OpenCLI 操作与显式 Provider 不一致。")
    return capture_operation(settings, operation, seed.params, work, max_cost=spec.max_cost)


def _collect_text(
    settings: SootaSettings,
    spec: TaskSpec,
    seed: InputSpec,
    work: Path,
    *,
    media: MediaAsset | None = None,
    operation: NativeOperation | None = None,
) -> Collection:
    source = source_for(seed)
    item = MediaItem(source=source, title=Path(seed.value).name if seed.kind == "file" else None)
    if seed.kind == "bundle":
        bundle = load_bundle(Path(seed.value))
        item = MediaItem(
            source=bundle.source, title=bundle.metadata.get("title"), provenance=bundle.metadata.get("provenance", {})
        )
        document = load_transcript(Path(seed.value), bundle)
        eligible = document is not None and bool(document.segments)
        if (
            document is not None
            and eligible
            and spec.transcript_mode == TranscriptMode.ASR
            and document.source != TranscriptSource.ASR
        ):
            eligible = False
        if (
            document is not None
            and eligible
            and spec.transcript_mode == TranscriptMode.CAPTIONS_ONLY
            and document.source == TranscriptSource.ASR
        ):
            eligible = False
        needs_transcript = "transcript" in spec.wants or spec.transcript_mode == TranscriptMode.ASR
        if (needs_transcript and not eligible) or (not eligible and not (bundle.content and bundle.content.has_text)):
            origin = backing_input(bundle.source.url)
            if spec.provider == "local" and origin.kind != "file":
                raise SourceError("旧包缺少所需正文，local 模式不联网补取。")
            collected = _collect_text(settings, spec, origin, work, media=media)
            collected.item.source = bundle.source
            return collected
        return Collection(
            item,
            document=document,
            content=bundle.content,
            processing=bundle.processing,
            raw_body=bundle.content.body_md if bundle.content else None,
            reference_base=str(Path(seed.value)),
        )
    if (
        seed.kind == "url"
        and _direct_url(seed.value)
        and Path(urlsplit(seed.value).path).suffix.lower() not in {".png", ".jpg", ".jpeg", ".gif", ".webp"}
    ):
        if spec.transcript_mode in {TranscriptMode.CAPTIONS_ONLY, TranscriptMode.NONE}:
            raise SourceError("直接媒体地址没有附带原字幕；当前模式不允许转写。")
        service = MediaService(settings, work / "media", max_bytes=spec.max_bytes)
        if media is None:
            if spec.provider in {"auto", "http"}:
                selected_provider(settings, "http")
                media = service.from_descriptor(MediaDescriptor(url=seed.value, source_provider="http"), kind="audio")
            elif spec.provider == "yt-dlp":
                selected_provider(settings, "yt-dlp")
                media = service.from_page(seed.value, kind="audio")
            else:
                raise SourceError("指定 Provider 未接入该直接媒体地址的转写获取。")
        collected = _collect_text(settings, spec, InputSpec(kind="file", value=str(media.path)), work, media=media)
        collected.item.source = source
        return collected
    if seed.kind == "file":
        path = Path(seed.value)
        format = subtitle_format(path, spec.input_format)
        if format:
            segments = parse_payload(
                CaptionPayload(format=format, body=path.read_text(encoding="utf-8-sig")),
                language=None,
                source=TranscriptSource.UNKNOWN,
            )
            if not segments:
                raise SourceError("本地字幕没有可用正文。")
            item.provenance = {"provider": "local", "caption_format": format}
            return Collection(
                item,
                document=TranscriptDocument(
                    status=TranscriptStatus.READY,
                    segments=segments,
                    language=None,
                    source=TranscriptSource.UNKNOWN,
                ),
            )
        if path.suffix.lower() in {".md", ".markdown", ".txt"}:
            body = path.read_text(encoding="utf-8-sig")
            if not body.strip():
                raise SourceError("本地正文为空。")
            item.provenance = {"provider": "local"}
            return Collection(
                item,
                content=content_from_markdown(source, public_text(body), path.stem, "full"),
                raw_body=body,
                reference_base=str(path.parent),
            )
        service = MediaService(settings, work / "media", max_bytes=spec.max_bytes)
        if media is None and path.suffix.lower() == ".json":
            payload = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(payload, dict) and isinstance(payload.get("media"), dict):
                media = service.from_descriptor(checked_descriptor(settings, spec, payload["media"]), kind="audio")
        asset = media or service.from_file(path)
        audio = service.extract_audio(asset)
        if spec.transcript_mode in {TranscriptMode.CAPTIONS_ONLY, TranscriptMode.NONE}:
            raise SourceError("本地媒体没有原字幕输入；当前字幕模式不允许语音转写。")
        engine = resolve_engine(settings.asr)
        raw = engine.transcribe(audio.path, language=spec.language)
        if isinstance(raw, list):
            raw = CaptionPayload(format="soota-segments", body=[s.model_dump() for s in raw])
        segments = parse_asr_payload(raw, audio.path, language=spec.language)
        item.provenance = {"provider": "local", "asr_engine": engine.name, "caption_format": raw.format}
        return Collection(
            item,
            document=TranscriptDocument(
                status=TranscriptStatus.READY, segments=segments, language=spec.language, source=TranscriptSource.ASR
            ),
        )
    if operation is not None:
        native = convert_content(operation, seed.value or operation.operation)
    elif source.platform in {"youtube", "bilibili"} and source.kind in {"video", "shortlink"}:
        from soota.pipeline import ingest

        audio_asset = None
        if media is not None:
            audio_asset = AudioAsset(path=media.path, provider=media.source_provider)
        caption_settings = settings
        if spec.strict_language and spec.provider == "auto":
            # These adapters cannot verify the requested language. Rank the remaining known-language sources.
            caption_settings = settings.model_copy(deep=True)
            caption_settings.routing.disabled_providers.update({"opencli", "opencli-mcp"})
        result = ingest(
            caption_settings,
            seed.value,
            transcript=spec.transcript_mode,
            provider=spec.provider,
            fallback=spec.fallback,
            audio_provider=spec.audio_provider,
            output=work / "source",
            max_cost=spec.max_cost,
            prefer_youtube=spec.prefer_youtube,
            exclude_promotions=spec.exclude_promotions,
            exclude_ranges=spec.exclude_ranges,
            record_job=False,
            audio_asset=audio_asset,
        )
        manifest = ContentBundleManifest.model_validate_json(Path(result.data["manifest"]).read_text(encoding="utf-8"))
        if manifest.transcript.status != TranscriptStatus.READY:
            raise SourceError(
                "尚未取得可用字幕正文。",
                details={"transcript_status": manifest.transcript.status.value, "result": result.data},
            )
        document = parse_transcript_markdown(
            Path(result.data["transcript"]).read_text(encoding="utf-8"), manifest.transcript
        )
        item = MediaItem(
            source=source,
            title=manifest.metadata.get("title"),
            creator=manifest.metadata.get("creator"),
            duration_seconds=manifest.metadata.get("duration_seconds"),
            provenance=manifest.metadata.get("provenance", {"provider": result.data.get("provider", spec.provider)}),
        )
        if manifest.source.cache_identity != source.cache_identity:
            item.provenance["transcript_source_url"] = manifest.source.canonical_url
            item.provenance["transcript_source_identity"] = manifest.source.cache_identity
        original_path = Path(result.data["output"]) / "transcript.original.md"
        original_document = (
            parse_transcript_markdown(original_path.read_text(encoding="utf-8"), manifest.transcript)
            if original_path.is_file()
            else None
        )
        return Collection(
            item,
            document=document,
            original_document=original_document,
            processing=manifest.processing,
            diagnostics=result.data,
        )
    else:
        native = capture_content(
            settings, seed.value, work / "content", provider=spec.provider, allow_credits=spec.max_cost != 0
        )
    item = safe_item(
        item.model_copy(
            update={
                "title": native.title,
                "provenance": {
                    "provider": native.provider,
                    "operation": native.operation,
                    "content_scope": native.scope,
                },
            }
        )
    )
    record: dict[str, Any] = {}
    if isinstance(native.raw, list):
        if native.raw and isinstance(native.raw[0], dict) and "field" not in native.raw[0]:
            record = native.raw[0]
        else:
            record = {row["field"]: row.get("value") for row in native.raw if isinstance(row, dict) and "field" in row}
    creator = record.get("author")
    if isinstance(creator, str):
        item.creator = creator
    published = record.get("publish_time")
    if isinstance(published, str):
        try:
            item.published_at = datetime.fromisoformat(published.replace("Z", "+00:00"))
        except ValueError:
            item.provenance["published_label"] = published
    base = str(Path(record["saved"]).parent) if isinstance(record.get("saved"), str) else native.source_url
    content = content_from_markdown(item.source, public_text(native.body), native.title, "unknown")
    return Collection(
        item,
        content=content,
        data=native.raw,
        attachments=native.attachments,
        raw_body=native.body,
        reference_base=base,
        processing=ProcessingManifest(provider=native.provider),
    )


def collect_text(
    settings: SootaSettings,
    spec: TaskSpec,
    seed: InputSpec,
    work: Path,
    *,
    media: MediaAsset | None = None,
    operation: NativeOperation | None = None,
) -> Collection:
    result = _collect_text(settings, spec, seed, work, media=media, operation=operation)
    if (
        result.document is not None
        and (spec.exclude_ranges or spec.exclude_promotions)
        and ("promotion_filter" not in result.item.provenance or seed.kind == "bundle")
    ):
        from soota.transcripts.promotions import filter_promotions, parse_ranges

        if seed.kind == "bundle" and (Path(seed.value) / "transcript.original.md").is_file():
            from soota.artifacts.bundle import file_sha256

            original_path = Path(seed.value) / "transcript.original.md"
            bundle = load_bundle(Path(seed.value))
            known = next((a for a in bundle.artifacts if a.path == "transcript.original.md"), None)
            if known is not None and file_sha256(original_path) != known.sha256:
                raise InputError("过滤前原文已被修改，未覆盖或自动继续过滤。")
            result.document = parse_transcript_markdown(original_path.read_text(encoding="utf-8"), bundle.transcript)
        result.original_document = result.document
        result.document, report, warnings = filter_promotions(
            result.item,
            result.document,
            automatic=bool(spec.exclude_promotions),
            ranges=parse_ranges(spec.exclude_ranges),
        )
        result.item.provenance["promotion_filter"] = report
        if result.processing is not None:
            result.processing.warnings.extend(warnings)
    return result


def _direct_url(value: str) -> bool:
    return Path(urlsplit(value).path).suffix.lower() in {
        ".mp4",
        ".webm",
        ".mkv",
        ".mov",
        ".m4a",
        ".mp3",
        ".wav",
        ".ogg",
        ".flac",
        ".m3u8",
        ".mpd",
        ".jpg",
        ".jpeg",
        ".png",
        ".webp",
        ".gif",
    }


def collect_media(
    settings: SootaSettings, spec: TaskSpec, seed: InputSpec, work: Path, *, operation: NativeOperation | None = None
) -> MediaBatch:
    service = MediaService(settings, work / "media", max_bytes=spec.max_bytes)
    kind: MediaKind = "video" if "video" in spec.wants else "audio" if "audio" in spec.wants else "image"
    options: MediaOptions = {
        "min_height": spec.min_height if kind == "video" else None,
        "allow_silent": spec.allow_silent,
    }
    if seed.kind == "bundle":
        bundle = load_bundle(Path(seed.value))
        if spec.provider == "local":
            raise SourceError("内容包缺少所需媒体，local 模式不联网补取。")
        seed = backing_input(bundle.source.url)
    if seed.kind == "file":
        path = Path(seed.value)
        if path.suffix.lower() == ".json":
            payload = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(payload, dict) and isinstance(payload.get("media"), dict):
                return MediaBatch(
                    [
                        service.from_descriptor(
                            checked_descriptor(settings, spec, payload["media"]), kind=kind, **options
                        )
                    ]
                )
        try:
            return MediaBatch([service.from_file(path, kind=kind, **options)])
        except SourceError:
            if {"video", "audio"} <= spec.wants:
                return MediaBatch(
                    [service.from_file(path, kind="audio")], errors=[{"message": "本地输入不能满足视频目标。"}]
                )
            raise
    if seed.kind == "operation":
        if operation is None:
            raise SourceError("原生操作尚未成功，不能用包元数据代替媒体结果。")
        return _native_media_files(service, spec, operation.files, operation.provider, operation.warnings)
    if spec.provider == "http" or (
        spec.provider == "auto" and _direct_url(seed.value) and provider_enabled(settings, "http")
    ):
        selected_provider(settings, "http")
        return MediaBatch(
            [service.from_descriptor(MediaDescriptor(url=seed.value, source_provider="http"), kind=kind, **options)]
        )
    if spec.provider == "downie":
        selected_provider(settings, "downie")
        provider = DownieProvider(settings)
        item = MediaItem(source=source_for(seed))
        if kind == "audio":
            asset = provider.get_audio(item)
            return MediaBatch([service.from_file(asset.path, kind="audio", source_provider="downie")])
        provider._preflight()
        folder = provider._submit_or_resume(item)
        media = provider._wait_for_receipt(folder)
        return MediaBatch([service.from_file(media, kind=kind, source_provider="downie", **options)])
    media_provider = spec.provider
    if media_provider == "auto" and kind == "audio":
        choices = ordered_names(
            settings, source_for(seed), "audio", ["yt-dlp", "tikhub", "monid", "treg"], max_cost=spec.max_cost
        )
        if not choices:
            raise SourceError("没有可用音频来源。")
        media_provider = choices[0]
    if media_provider in {"auto", "opencli"} and provider_enabled(settings, "opencli"):
        try:
            operation_name, _ = media_binding(seed.value)
        except InputError:
            if spec.provider == "opencli":
                raise
        else:
            try:
                found = any(
                    row["command"] == operation_name and row["access"] == "read"
                    for row in opencli_commands.commands(settings)
                )
                if found:
                    result = capture_media(settings, seed.value, work / "native-media", provider="opencli")
                    return _native_media_files(service, spec, result.files, "opencli", result.warnings)
                if spec.provider == "opencli":
                    raise SourceError("当前 OpenCLI 没有该媒体能力。")
            except RuntimeUnavailable:
                if spec.provider != "auto":
                    raise
    if media_provider in {"tikhub", "monid", "treg"}:
        selected_provider(settings, media_provider)
        if kind != "audio":
            raise SourceError("TikHub 标准视频产物尚无已验证分轨契约；可显式提供原生媒体描述。")
        client = (
            (TregProvider if media_provider == "treg" else MonidProvider)(settings, spec.max_cost, purpose="audio")
            if media_provider in {"monid", "treg"}
            else TikHubProvider(
                cache=CacheStore(settings.cache_dir),
                runtime_python=settings.tikhub.runtime_python,
                base_url=settings.tikhub.base_url,
                timeout=settings.tikhub.timeout_seconds,
                preferred_language=spec.language,
                max_retries=0,
                payment_gate=PaymentGate(settings, spec.max_cost),
            )
        )
        audio_source = source_for(seed)
        item = client.inspect(audio_source) if audio_source.platform == "bilibili" else MediaItem(source=audio_source)
        payload = client._audio_payload(item)
        descriptor = MediaDescriptor(url=pick_audio_url(payload), source_provider=media_provider)
        return MediaBatch([service.from_descriptor(descriptor, kind="audio", expected_duration=item.duration_seconds)])
    if spec.provider not in {"auto", "yt-dlp"}:
        raise SourceError("指定 Provider 不具备该媒体产物能力。")
    if kind == "image":
        raise SourceError("图片需要原生媒体结果、正文图片地址或本地图片文件。")
    selected_provider(settings, "yt-dlp")
    return MediaBatch([service.from_page(seed.value, kind=kind, source_provider="yt-dlp", **options)])


def _native_media_files(
    service: MediaService, spec: TaskSpec, files: list[Path], provider: str, warnings: list[str]
) -> MediaBatch:
    result = MediaBatch(warnings=warnings)
    candidates = [path for path in files if path.suffix.lower() not in {".md", ".json", ".txt"}]
    if not candidates:
        raise SourceError("原生操作没有返回可验证的媒体文件。")
    for index, path in enumerate(candidates):
        try:
            asset = service.from_file(path, source_provider=provider, allow_silent=spec.allow_silent)
            if asset.kind == "video" and spec.min_height is not None:
                if (
                    max((stream.height or 0 for stream in asset.streams if stream.type == "video"), default=0)
                    < spec.min_height
                ):
                    raise SourceError("视频未达到所需最低高度。")
            result.assets.append(asset)
        except (SourceError, OSError) as exc:
            result.errors.append(
                {"index": index, "message": "媒体未通过验证。", "code": getattr(exc, "code", "storage")}
            )
    if any("部分失败" in warning for warning in warnings):
        result.errors.append({"message": "上游只交付了部分媒体。"})
    return result


def backing_input(url: str) -> InputSpec:
    parsed = urlsplit(url)
    if parsed.scheme == "file":
        return InputSpec(kind="file", value=unquote(parsed.path))
    if "[redacted]" in url:
        raise InputError("旧包没有可重用的完整访问链接，请提供原始链接。")
    return InputSpec(kind="url", value=url)


def checked_descriptor(settings: SootaSettings, spec: TaskSpec, payload: Any) -> MediaDescriptor:
    try:
        descriptor = MediaDescriptor.model_validate(payload)
    except ValidationError as exc:
        raise InputError("媒体描述无效：应提供本地路径、单个URL或分轨地址其中一种。") from exc
    if descriptor.path is None:
        if spec.provider == "local":
            raise InputError("local 模式不能获取远程媒体描述。")
        selected_provider(settings, "http")
    return descriptor
