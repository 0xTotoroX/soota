from pathlib import Path

import pytest

from soota.models import (
    AnalysisStatus,
    ContentBundleManifest,
    MediaItem,
    TranscriptDocument,
    TranscriptManifest,
    TranscriptSegment,
    TranscriptSource,
    TranscriptStatus,
)
from soota.pipeline import _document_from_transcript_md
from soota.render.bundle import write_bundle
from soota.routing import recognize


def source_bundle(tmp_path, *, text="先说这个\n这个", start=250):
    item = MediaItem(source=recognize("https://www.youtube.com/watch?v=Example0001"), title="Example")
    segments = [
        TranscriptSegment(
            segment_id="seg-000001", start_ms=start, end_ms=1500, text=text, source=TranscriptSource.AUTO
        ),
        TranscriptSegment(
            segment_id="seg-000002", start_ms=1600, end_ms=2250, text="先说这个", source=TranscriptSource.AUTO
        ),
    ]
    document = TranscriptDocument(
        status=TranscriptStatus.READY, source=TranscriptSource.AUTO, language="zh", segments=segments
    )
    manifest = ContentBundleManifest(source=item.source, transcript=TranscriptManifest(status=document.status))
    folder = write_bundle(tmp_path, item, document, manifest, None)
    return folder, item, document, manifest


def test_default_bundle_is_context_only_and_keeps_cue_text(tmp_path):
    folder, _, _, manifest = source_bundle(tmp_path)
    text = (folder / "context.md").read_text()
    assert "[seg-000001] 先说这个\n这个\n[seg-000002] 先说这个" in text
    assert "## 摘要" not in text and "尚未生成分析" not in text
    assert not (folder / "content.md").exists()
    assert manifest.analysis.status == AnalysisStatus.NOT_REQUESTED and manifest.analysis.path is None
    assert set(manifest.files) == {"context.md", "transcript.md", "manifest.json"}
    transcript = (folder / "transcript.md").read_text()
    assert "00:00:00.250-00:00:01.500" in transcript
    restored = _document_from_transcript_md(folder / "transcript.md", manifest)
    assert restored.segments[0].start_ms == 250 and restored.segments[0].end_ms == 1500
    assert restored.segments[0].text == "先说这个\n这个"


@pytest.mark.parametrize("provider", ["opencli", "tikhub"])
def test_ingest_returns_context_for_both_paths_without_article(settings, monkeypatch, provider):
    from soota.models import CaptionResult, TranscriptMode
    from soota.pipeline import ingest
    from soota.providers.opencli import OpenCLIProvider
    from tests.unit.test_resolver import _youtube_requester

    _, _sample, document, _ = source_bundle(settings.output_dir / "fixture")
    monkeypatch.setattr(
        OpenCLIProvider, "inspect", lambda self, source: MediaItem(source=source, provenance={"provider": "opencli"})
    )
    monkeypatch.setattr(
        OpenCLIProvider,
        "get_captions",
        lambda *a: CaptionResult(status=TranscriptStatus.READY, segments=document.segments),
    )
    result = ingest(
        settings,
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        provider=provider,
        transcript=TranscriptMode.CAPTIONS_ONLY,
        requester=_youtube_requester(),
        max_cost=0.1,
    )
    assert result.ok
    assert result.data["context"] == result.data["content"]
    assert Path(result.data["context"]).name == "context.md" and Path(result.data["context"]).is_file()
    assert not (Path(result.data["output"]) / "content.md").exists()
    assert result.data["analysis"] == {"status": "not_requested", "path": None}
