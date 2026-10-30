from __future__ import annotations

import json
import re
from typing import Any

from soota.errors import ContractError
from soota.transcripts.formats.common import Cue, milliseconds


def decode(body: Any) -> Any:
    return json.loads(body) if isinstance(body, str) else body


def objects(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise ContractError("字幕片段必须是对象数组。")
    return value


def text_field(row: dict[str, Any], key: str) -> str:
    text = row.get(key)
    if not isinstance(text, str):
        raise ContractError(f"字幕正文缺少字符串字段 {key}。")
    return text


def json3(body: Any) -> list[Cue]:
    data = decode(body)
    if not isinstance(data, dict) or "events" not in data:
        raise ContractError("JSON3 字幕缺少 events。")
    cues = []
    for event in objects(data["events"]):
        # Window/style events carry no text. Append events remain separate:
        # rolling captions are evidence, not a request to deduplicate speech.
        if "segs" not in event:
            continue
        text = "".join(text_field(part, "utf8") for part in objects(event["segs"]))
        if not text.strip():
            continue
        start = milliseconds(event["tStartMs"])
        end = start + milliseconds(event["dDurationMs"]) if "dDurationMs" in event else None
        cues.append(Cue(start, end, text))
    if not cues:
        raise ContractError("JSON3 字幕没有可用文本片段。")
    return cues


def bilibili(body: Any) -> list[Cue]:
    data = decode(body)
    if not isinstance(data, dict) or "body" not in data:
        raise ContractError("Bilibili 字幕缺少 body；弹幕不是字幕。")
    return [
        Cue(milliseconds(row["from"], 1000), milliseconds(row["to"], 1000), text_field(row, "content"))
        for row in objects(data["body"])
    ]


def opencli(body: Any, format: str) -> list[Cue]:
    fields = ("start", "end", "text") if format == "opencli-youtube" else ("from", "to", "content")
    if format == "opencli-mcp":
        fields = ("from_s", "to_s", "content")
    cues = []
    for row in objects(decode(body)):
        times = [row[key] for key in fields[:2]]
        if format != "opencli-mcp":
            if any(not isinstance(t, str) or not re.fullmatch(r"\d+(?:\.\d+)?s", t) for t in times):
                raise ContractError("OpenCLI 字幕时间戳契约不匹配。")
            times = [t[:-1] for t in times]
        cues.append(Cue(milliseconds(times[0], 1000), milliseconds(times[1], 1000), text_field(row, fields[2])))
    return cues


def whisper(body: Any) -> list[Cue]:
    data = decode(body)
    if not isinstance(data, dict) or "segments" not in data:
        raise ContractError("Whisper 字幕缺少 segments。")
    return [
        Cue(milliseconds(row["start"], 1000), milliseconds(row["end"], 1000), text_field(row, "text"))
        for row in objects(data["segments"])
    ]


def whisper_jsonl(body: str) -> list[Cue]:
    # Whisper writes one segment (second-based start/end) per line.
    return whisper({"segments": [json.loads(line) for line in body.splitlines() if line.strip()]})


def saved_segments(body: Any) -> list[Cue]:
    return [
        Cue(
            milliseconds(row["start_ms"]) if row.get("start_ms") is not None else None,
            milliseconds(row["end_ms"]) if row.get("end_ms") is not None else None,
            text_field(row, "text"),
        )
        for row in objects(decode(body))
    ]


def youtube_transcript(body: Any) -> list[Cue]:
    cues = []
    for row in objects(decode(body)):
        start = milliseconds(row["start"], 1000)
        end = start + milliseconds(row["duration"], 1000) if row.get("duration") is not None else None
        cues.append(Cue(start, end, text_field(row, "text")))
    return cues


def crawl4ai_youtube(body: Any) -> list[Cue]:
    return youtube_transcript(
        [
            {"start": row["start_s"], "duration": row["duration_s"], "text": text_field(row, "text")}
            for row in objects(decode(body))
        ]
    )


def scrapecreators_youtube(body: Any) -> list[Cue]:
    return [
        Cue(milliseconds(row["startMs"]), milliseconds(row["endMs"]), text_field(row, "text"))
        for row in objects(decode(body))
    ]
