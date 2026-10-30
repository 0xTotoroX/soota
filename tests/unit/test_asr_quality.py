import json
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest

from soota.errors import TranscriptError
from soota.models import CaptionPayload, TranscriptSource
from soota.transcripts.conversion import parse_payload
from soota.transcripts.quality import audio_duration_ms, parse_asr_payload, quality_flags


def payload(rows):
    return CaptionPayload(format="whisper-json", body={"segments": rows})


def segment(start=0, end=1, text="a real spoken sentence"):
    return {"start": start, "end": end, "text": text}


def test_actual_wav_boundary_rejects_without_clipping_and_preserves_each_attempt(tmp_path):
    audio = tmp_path / "audio.wav"
    with wave.open(str(audio), "wb") as stream:
        stream.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        stream.writeframes(b"\x00\x00" * 16000)
    original = payload([segment(end=23)])
    assert audio_duration_ms(audio) == 1000
    diagnostics = []
    for _ in range(2):
        with pytest.raises(TranscriptError) as caught:
            parse_asr_payload(original, audio, language="en")
        assert caught.value.details["flags"] == ["segment_past_audio_eof"]
        diagnostics.append(Path(caught.value.details["diagnostic"]))
    assert diagnostics[0] != diagnostics[1]
    for path in diagnostics:
        saved = json.loads(path.read_text())
        assert saved["payload"] == original.model_dump()
        assert saved["audio_duration_ms"] == 1000
    assert original.body["segments"][0]["end"] == 23


def test_audio_track_boundary_is_not_video_container_duration(monkeypatch):
    monkeypatch.setattr(
        "soota.transcripts.quality.inspect_media",
        lambda *a, **k: (
            "mp4",
            [
                SimpleNamespace(type="video", duration_seconds=36.398),
                SimpleNamespace(type="audio", duration_seconds=34.667),
            ],
            36.398,
        ),
    )
    assert audio_duration_ms(Path("synthetic.mp4")) == 34667
    assert quality_flags(parse_payload(payload([segment(end=35)])), 34667) == ["segment_past_audio_eof"]


def test_unverifiable_audio_duration_is_not_assumed_from_video(monkeypatch):
    monkeypatch.setattr(
        "soota.transcripts.quality.inspect_media",
        lambda *a, **k: (
            "mp4",
            [SimpleNamespace(type="video", duration_seconds=36), SimpleNamespace(type="audio", duration_seconds=None)],
            36,
        ),
    )
    with pytest.raises(TranscriptError, match="音轨时长"):
        audio_duration_ms(Path("synthetic.mp4"))


@pytest.mark.parametrize(
    "rows, expected",
    [
        ([segment(i, i + 1, "same phrase") for i in range(22)], "high_repetition"),
        ([segment(text="repeated word " * 20)], "repeated_text_loop"),
        ([segment(1, 2), segment(0, 1)], "invalid_segment_timeline"),
    ],
)
def test_repetition_and_reversed_order_are_visible_rejections(tmp_path, monkeypatch, rows, expected):
    monkeypatch.setattr("soota.transcripts.quality.audio_duration_ms", lambda _: 60000)
    with pytest.raises(TranscriptError) as caught:
        parse_asr_payload(payload(rows), tmp_path / "audio.wav", language=None)
    assert expected in caught.value.details["flags"]
    assert json.loads(Path(caught.value.details["diagnostic"]).read_text())["payload"]["body"] == {"segments": rows}


def test_normal_speech_and_frame_rounding_are_preserved(tmp_path, monkeypatch):
    monkeypatch.setattr("soota.transcripts.quality.audio_duration_ms", lambda _: 1000)
    result = parse_asr_payload(payload([segment(end=1.02)]), tmp_path / "audio.wav", language="en")
    assert result[0].end_ms == 1020
    assert result[0].source == TranscriptSource.ASR
    assert not (tmp_path / "asr").exists()


def test_scattered_repeated_short_responses_are_not_a_loop():
    rows = [segment(i, i + 1, "yes" if i % 3 == 0 else f"unique detail {i}") for i in range(18)]
    assert quality_flags(parse_payload(payload(rows)), 18000) == []


def test_brief_repeated_thanks_at_end_of_long_speech_are_not_rejected():
    rows = [segment(i, i + 1, f"distinct point {i}") for i in range(100)]
    rows.extend(segment(i, i + 1, "Thank you") for i in range(100, 107))
    assert quality_flags(parse_payload(payload(rows)), 107000) == []


def test_empty_recognition_never_claims_no_speech(tmp_path):
    with pytest.raises(TranscriptError, match="不能据此确认没有语音") as caught:
        parse_asr_payload(payload([]), tmp_path / "audio.wav", language=None)
    assert caught.value.details == {"reason": "asr_no_text"}


def test_resolver_rejection_never_enters_ready_cache(settings, tmp_path, monkeypatch):
    from soota.models import AudioAsset, MediaItem, TranscriptMode
    from soota.routing import recognize
    from soota.storage.cache import CacheStore
    from soota.transcripts.resolver import TranscriptResolver

    audio = tmp_path / "input.wav"
    audio.write_bytes(b"routing-test-placeholder")
    monkeypatch.setattr("soota.transcripts.resolver.ensure_audio", lambda path, **kw: path)
    monkeypatch.setattr("soota.transcripts.quality.audio_duration_ms", lambda _: 1000)

    class Engine:
        name = "synthetic"

        def transcribe(self, *a, **kw):
            return payload([segment(end=23)])

    provider = SimpleNamespace(name="synthetic", records=[], get_audio=lambda _: AudioAsset(path=audio))
    resolver = TranscriptResolver(None, CacheStore(settings.cache_dir), settings, asr=Engine(), audio_provider=provider)
    item = MediaItem(source=recognize("https://www.youtube.com/watch?v=dQw4w9WgXcQ"))
    for _ in range(2):
        with pytest.raises(TranscriptError, match="质量检查未通过"):
            resolver.resolve(item, TranscriptMode.ASR)
    assert len(list((tmp_path / "asr").glob("quality-rejected-*.json"))) == 2
    assert not list(settings.cache_dir.rglob("transcript.*.json"))


def test_local_task_cannot_accept_out_of_bounds_transcript(settings, tmp_path, monkeypatch):
    from soota.media import MediaService
    from soota.tasks.acquire import collect_text
    from soota.tasks.models import InputSpec, OutputSpec, TaskSpec

    audio = tmp_path / "input.wav"
    audio.write_bytes(b"routing-test-placeholder")
    asset = SimpleNamespace(path=audio)
    monkeypatch.setattr(MediaService, "from_file", lambda *a, **kw: asset)
    monkeypatch.setattr(MediaService, "extract_audio", lambda *a, **kw: asset)
    monkeypatch.setattr("soota.transcripts.quality.audio_duration_ms", lambda _: 1000)
    monkeypatch.setattr(
        "soota.tasks.acquire.resolve_engine",
        lambda _: SimpleNamespace(name="synthetic", transcribe=lambda *a, **kw: payload([segment(end=23)])),
    )
    seed = InputSpec(kind="file", value=str(audio))
    spec = TaskSpec(inputs=[seed], outputs=[OutputSpec(kind="transcript")], output=tmp_path / "out")
    with pytest.raises(TranscriptError, match="质量检查未通过"):
        collect_text(settings, spec, seed, tmp_path / "work")
