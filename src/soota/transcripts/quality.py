"""ASR acceptance guards; never repair model text or truncate its timeline."""

from __future__ import annotations

import json
import math
import re
import tempfile
from itertools import groupby
from pathlib import Path

from soota.errors import TranscriptError
from soota.media.probe import inspect_media
from soota.models import CaptionPayload, TranscriptSegment, TranscriptSource
from soota.transcripts.conversion import parse_payload

ASR_QUALITY_VERSION = 1


def audio_duration_ms(audio: Path) -> int:
    _, streams, duration = inspect_media(audio, timeout=60)
    tracks = [stream for stream in streams if stream.type == "audio"]
    # A video container's duration can exceed the actual audio track.
    seconds = tracks[0].duration_seconds if tracks else None
    if seconds is None and tracks and not any(stream.type == "video" for stream in streams):
        seconds = duration
    if seconds is None or not math.isfinite(seconds) or seconds <= 0:
        raise TranscriptError("无法核验转写音轨时长。", details={"reason": "asr_duration_unknown"})
    return math.ceil(seconds * 1000)


def quality_flags(segments: list[TranscriptSegment], duration_ms: int) -> list[str]:
    flags = []
    previous_start = -1
    for segment in segments:
        start, end = segment.start_ms, segment.end_ms
        if start is None or end is None or start < 0 or end < start or start < previous_start:
            flags.append("invalid_segment_timeline")
            break
        # Whisper uses a 20 ms time grid. Preserve that small rounding excess;
        # never clamp an end time, drop words, or scale the source duration.
        if start > duration_ms or end > duration_ms + 20:
            flags.append("segment_past_audio_eof")
            break
        previous_start = start
    texts = [re.sub(r"\W+", "", segment.text.casefold()) for segment in segments]
    longest_run = max((sum(1 for _ in group) for _, group in groupby(texts)), default=0)
    if len(texts) >= 5 and (1 - len(set(texts)) / len(texts) >= 0.6 or longest_run >= 10):
        flags.append("high_repetition")
    # A decoder can also loop inside a single segment.
    if any(re.search(r"(.{2,40}?)\1{7,}", text) for text in texts):
        flags.append("repeated_text_loop")
    return flags


def parse_asr_payload(payload: CaptionPayload, audio: Path, *, language: str | None) -> list[TranscriptSegment]:
    segments = parse_payload(payload, language=language, source=TranscriptSource.ASR)
    if not segments:
        raise TranscriptError(
            "本地转写没有识别到文本；不能据此确认没有语音。",
            details={"reason": "asr_no_text"},
        )
    duration = audio_duration_ms(audio)
    flags = quality_flags(segments, duration)
    if flags:
        folder = audio.parent / "asr"
        folder.mkdir(parents=True, exist_ok=True)
        # Unique evidence file: subsequent attempts cannot overwrite a rejected
        # result. It is diagnostic evidence, never a ready transcript/cache.
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", prefix="quality-rejected-", suffix=".json", dir=folder, delete=False
        ) as handle:
            json.dump(
                {
                    "quality_version": ASR_QUALITY_VERSION,
                    "flags": flags,
                    "audio_duration_ms": duration,
                    "payload": payload.model_dump(mode="json"),
                },
                handle,
                ensure_ascii=False,
                indent=2,
            )
            diagnostic = handle.name
        raise TranscriptError(
            "转写质量检查未通过，需复核原音轨。",
            hint="异常输出已单独保留；未裁剪时间轴、删除重复文字或写为可用转写。",
            details={"reason": "asr_quality_rejected", "flags": flags, "diagnostic": diagnostic},
        )
    return segments
