"""Wire-format readers. They perform no network requests or semantic rewriting."""

from __future__ import annotations

from typing import Any

from soota.errors import ContractError
from soota.transcripts.formats import structured, text, xml
from soota.transcripts.formats.common import Cue

FORMATS = frozenset(
    {
        "json3",
        "youtube-transcript",
        "crawl4ai-youtube",
        "scrapecreators-youtube",
        "bilibili-json",
        "opencli-youtube",
        "opencli-bilibili",
        "opencli-mcp",
        "whisper-json",
        "whisper-jsonl",
        "soota-segments",
        "srt",
        "vtt",
        "ttml",
        "dfxp",
        "srv1",
        "srv2",
        "srv3",
        "xml",
        "txt",
        "tsv",
    }
)


def read_cues(format: str, body: Any) -> list[Cue]:
    format = format.lower()
    if format not in FORMATS:
        raise ContractError(f"不支持的字幕格式: {format}", hint="必须指定已知字幕格式或 JSON 结构，不能仅标注 json。")
    if format.startswith("opencli-"):
        return structured.opencli(body, format)
    readers = {
        "json3": structured.json3,
        "youtube-transcript": structured.youtube_transcript,
        "crawl4ai-youtube": structured.crawl4ai_youtube,
        "scrapecreators-youtube": structured.scrapecreators_youtube,
        "bilibili-json": structured.bilibili,
        "whisper-json": structured.whisper,
        "soota-segments": structured.saved_segments,
    }
    if format in readers:
        return readers[format](body)
    if not isinstance(body, str):
        raise ContractError("该字幕格式需要文本正文。")
    if format in {"xml", "ttml", "dfxp", "srv1", "srv2", "srv3"}:
        return xml.parse(body, format)
    return {
        "srt": text.srt,
        "vtt": text.vtt,
        "txt": text.txt,
        "tsv": text.tsv,
        "whisper-jsonl": structured.whisper_jsonl,
    }[format](body)
