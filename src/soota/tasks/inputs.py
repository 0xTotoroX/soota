from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from pydantic import ValidationError

from soota.errors import InputError, SourceError
from soota.models import SourceRef
from soota.routing import recognize
from soota.tasks.models import InputSpec, OutputSpec

URL = re.compile(r"https?://[^\s<>\"\u3000]+")
SUBTITLE_FORMATS = {
    ".srt": "srt",
    ".vtt": "vtt",
    ".ttml": "ttml",
    ".dfxp": "dfxp",
    ".xml": "xml",
    ".srv1": "srv1",
    ".srv2": "srv2",
    ".srv3": "srv3",
    ".json3": "json3",
    ".tsv": "tsv",
    ".jsonl": "whisper-jsonl",
}


def parse_outputs(value: str | None, optional: str | None = None) -> list[OutputSpec]:
    names = [name.strip() for name in (value or "context").split(",") if name.strip()]
    extras = [name.strip() for name in (optional or "").split(",") if name.strip()]
    if set(names + extras) - {"video", "audio", "transcript", "context", "markdown", "images", "data"}:
        raise InputError("Soota 只采集原文；分析、笔记、文章和交付请使用 lorelab content。")
    try:
        return [OutputSpec(kind=name, required=name not in extras) for name in dict.fromkeys([*names, *extras])]  # type: ignore[arg-type]
    except ValidationError as exc:
        raise InputError("产物支持 video/audio/transcript/context/markdown/images/data。") from exc


def parse_inputs(value: str | None, inputs_file: Path | None) -> list[InputSpec]:
    items: list[InputSpec] = []
    if inputs_file is not None:
        path = inputs_file.expanduser().resolve()
        if not path.is_file():
            raise InputError("输入清单不存在。")
        text = path.read_text(encoding="utf-8-sig")
        if path.suffix.lower() == ".json":
            try:
                payload = json.loads(text)
                if isinstance(payload, dict):
                    payload = payload.get("inputs", payload.get("items"))
                if not isinstance(payload, list):
                    raise ValueError("not a list")
                for row in payload:
                    if isinstance(row, str):
                        item = InputSpec(value=row)
                    elif isinstance(row, dict) and "source" in row:
                        item = InputSpec(kind="url", value=row["source"].get("url") or row["source"]["canonical_url"])
                    else:
                        item = InputSpec.model_validate(row)
                    items.extend(expand_text(item, path.parent))
            except (ValueError, KeyError, TypeError, AttributeError) as exc:
                raise InputError("JSON 清单必须是 inputs/items 数组，条目为链接、路径或 InputSpec。") from exc
        else:
            for line in text.splitlines():
                if line.strip() and not line.lstrip().startswith("#"):
                    items.extend(expand_text(InputSpec(value=line.strip()), path.parent))
    if value is not None:
        items.extend(expand_text(InputSpec(value=value), Path.cwd()))
    if not items:
        raise InputError("请提供链接、本地文件或 --inputs-file。")
    return items


def expand_text(item: InputSpec, base: Path) -> list[InputSpec]:
    if item.kind == "operation":
        return [item]
    value = item.value.strip()
    if not value.startswith(("http://", "https://")):
        candidate = Path(value).expanduser()
        candidate = candidate if candidate.is_absolute() else base / candidate
        try:
            if candidate.exists():
                if candidate.is_dir() or candidate.name == "manifest.json":
                    from soota.artifacts.bundle import load_bundle

                    folder = candidate if candidate.is_dir() else candidate.parent
                    if not (folder / "manifest.json").is_file():
                        raise InputError("目录不是 Soota 内容包；请提供具体文件或输入清单。")
                    load_bundle(folder)
                    return [item.model_copy(update={"value": str(folder.resolve()), "kind": "bundle"})]
                return [item.model_copy(update={"value": str(candidate.resolve()), "kind": "file"})]
        except OSError:
            pass
    urls = URL.findall(value)
    if urls:
        return [
            item.model_copy(
                update={"kind": "url" if item.kind == "auto" else item.kind, "value": url.rstrip("。，、；！】}）")}
            )
            for url in urls
        ]
    if item.kind in {"url", "collection"}:
        raise InputError("输入不是有效 HTTP(S) 链接。")
    raise InputError("本地输入不存在，分享文本中也没有可识别的链接。")


def source_for(item: InputSpec) -> SourceRef:
    if item.kind == "operation":
        identity = hashlib.sha256(json.dumps([item.operation, item.params], sort_keys=True).encode()).hexdigest()[:24]
        return SourceRef(
            platform="native", kind="query", id=identity, url=item.operation or "", canonical_url=item.operation or ""
        )
    if item.kind in {"file", "bundle"}:
        path = Path(item.value).resolve()
        identity = hashlib.sha256(str(path).encode()).hexdigest()[:24]
        return SourceRef(platform="local", kind=item.kind, id=identity, url=path.as_uri(), canonical_url=path.as_uri())
    try:
        return recognize(item.value)
    except SourceError:
        parts = urlsplit(item.value)
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
            raise InputError("链接必须使用 HTTP(S)，且不能在 URL 中包含用户名或密码。") from None
        canonical = urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", parts.query, ""))
        ident = hashlib.sha256(canonical.encode()).hexdigest()[:24]
        return SourceRef(
            platform=parts.hostname.lower(), kind="page", id=ident, url=item.value, canonical_url=canonical
        )


def file_fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def subtitle_format(path: Path, explicit: str | None = None) -> str | None:
    if explicit:
        return explicit
    suffix = path.suffix.lower()
    if suffix in SUBTITLE_FORMATS:
        return SUBTITLE_FORMATS[suffix]
    if suffix != ".json":
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except ValueError as exc:
        raise InputError("JSON 输入无效。") from exc
    if isinstance(data, dict):
        for field, format in (("events", "json3"), ("body", "bilibili-json"), ("segments", "whisper-json")):
            if field in data:
                return format
    return None
