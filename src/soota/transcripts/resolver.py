from __future__ import annotations

import hashlib
import json
from pathlib import Path

from soota.config import SootaSettings
from soota.errors import TranscriptError
from soota.models import (
    CaptionPayload,
    CaptionResult,
    MediaItem,
    RequestRecord,
    TranscriptDocument,
    TranscriptMode,
    TranscriptSource,
    TranscriptStatus,
)
from soota.providers.base import AudioProvider, CaptionProvider
from soota.providers.ytdlp import YtDlpProvider
from soota.storage.cache import CacheStore
from soota.transcripts.acquisition import acquire_captions
from soota.transcripts.asr import AsrEngine, resolve_engine
from soota.transcripts.download import ensure_audio
from soota.transcripts.quality import ASR_QUALITY_VERSION, parse_asr_payload


def resumes_audio(data: dict[str, object], mode: TranscriptMode, audio_name: str) -> bool:
    return (
        mode == TranscriptMode.AUTO
        and audio_name == "downie"
        and data.get("status") == TranscriptStatus.PENDING
        and data.get("checkpoint") == "confirmed_absence_audio_v1"
        and data.get("audio_provider") == audio_name
    )


class TranscriptResolver:
    def __init__(
        self,
        provider: CaptionProvider | None,
        cache: CacheStore,
        settings: SootaSettings,
        asr: AsrEngine | None = None,
        audio_provider: AudioProvider | None = None,
    ) -> None:
        self.provider = provider
        self.cache = cache
        self.settings = settings
        self.asr = asr
        self.audio_provider = audio_provider or YtDlpProvider(settings)

    def resolve(self, item: MediaItem, mode: TranscriptMode) -> TranscriptDocument:
        cached = self.cache.get_json(item.source, "transcript")
        if cached and resumes_audio(cached, mode, self.audio_provider.name):
            document = self.run_asr(item)
            self.cache.put_json(item.source, "transcript", document.model_dump(mode="json"), redact=False)
            return document
        if (
            cached
            and cached.get("status") == TranscriptStatus.READY
            and cached.get("segments")
            and cached.get("source") != TranscriptSource.ASR
            and mode not in {TranscriptMode.NONE, TranscriptMode.ASR}
        ):
            document = TranscriptDocument.model_validate(cached)
            document.cache_hit = True
            return document

        if mode is TranscriptMode.NONE:
            document = TranscriptDocument(status=TranscriptStatus.NOT_REQUESTED)
            return document

        captions: CaptionResult | None = None
        if mode in {TranscriptMode.AUTO, TranscriptMode.CAPTIONS_ONLY}:
            assert self.provider is not None
            captions = acquire_captions(self.provider, item)
            if captions.status is TranscriptStatus.READY and captions.segments:
                document = TranscriptDocument(
                    status=TranscriptStatus.READY,
                    source=captions.source,
                    language=captions.language,
                    segments=captions.segments,
                )
                self.cache.put_json(item.source, "transcript", document.model_dump(mode="json"), redact=False)
                return document
            if mode is TranscriptMode.CAPTIONS_ONLY or captions.status != TranscriptStatus.MISSING:
                return TranscriptDocument(status=captions.status, language=captions.language)
            if self.audio_provider.name == "downie":
                # Resume this unfinished transcription without buying the same caption probe again.
                self.cache.put_json(
                    item.source,
                    "transcript",
                    {
                        "status": TranscriptStatus.PENDING,
                        "checkpoint": "confirmed_absence_audio_v1",
                        "audio_provider": self.audio_provider.name,
                    },
                )

        if mode is TranscriptMode.ASR or mode is TranscriptMode.AUTO:
            document = self.run_asr(item)
            self.cache.put_json(item.source, "transcript", document.model_dump(mode="json"), redact=False)
            return document

        raise TranscriptError("未知字幕模式。")

    def run_asr(self, item: MediaItem) -> TranscriptDocument:
        engine = self.asr or resolve_engine(self.settings.asr)
        signature = json.dumps(
            [
                ASR_QUALITY_VERSION,
                self.settings.asr.model_dump(),
                engine.name,
                self.audio_provider.name,
                item.language or self.settings.default_language,
            ],
            sort_keys=True,
        )
        cache = CacheStore(self.settings.cache_dir / "asr" / hashlib.sha256(signature.encode()).hexdigest()[:16])
        cached = cache.get_json(item.source, "transcript")
        if cached and cached.get("status") == TranscriptStatus.READY and cached.get("segments"):
            self.audio_provider.records.append(RequestRecord(endpoint="local:cache:asr", billed=False, cache_hit=True))
            document = TranscriptDocument.model_validate(cached)
            document.cache_hit = True
            item.provenance["audio_provider"] = cached.get("audio_provider", "unknown")
            return document
        audio = self.audio_provider.get_audio(item)
        item.provenance["audio_provider"] = audio.provider or self.audio_provider.name
        media = ensure_audio(Path(audio.path), settings=self.settings)
        language = item.language or self.settings.default_language
        payload = engine.transcribe(media, language=language)
        if isinstance(payload, list):
            # Compatibility for injected ASR engines implementing the older API.
            payload = CaptionPayload(format="soota-segments", body=[s.model_dump() for s in payload])
        segments = parse_asr_payload(payload, media, language=language)
        item.provenance["caption_format"] = payload.format
        self.audio_provider.records.append(RequestRecord(endpoint=f"local:asr/{engine.name}", billed=False))
        document = TranscriptDocument(
            status=TranscriptStatus.READY,
            source=TranscriptSource.ASR,
            language=item.language or self.settings.default_language,
            segments=segments,
        )
        cache.put_json(
            item.source,
            "transcript",
            {**document.model_dump(mode="json"), "audio_provider": item.provenance["audio_provider"]},
            redact=False,
        )
        return document
