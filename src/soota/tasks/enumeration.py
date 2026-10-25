from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

from soota.billing import selected_provider
from soota.catalog import catalog, resume_catalog
from soota.config import SootaSettings
from soota.errors import ContractError, SourceError
from soota.providers.process import run_process
from soota.secrets import redact_any
from soota.storage.atomic import atomic_write
from soota.tasks.inputs import parse_inputs, source_for
from soota.tasks.models import InputSpec, TaskJob, TaskSpec
from soota.tasks.state import save_job, task_folder


def _playlist(settings: SootaSettings, value: str, limit: int) -> tuple[list[InputSpec], dict[str, Any]]:
    selected_provider(settings, "yt-dlp")
    binary = shutil.which(settings.local.binary)
    if binary is None:
        raise SourceError("播放列表枚举需要 yt-dlp。")
    args = [
        binary,
        "--ignore-config",
        "--flat-playlist",
        "--dump-single-json",
        "--skip-download",
        "--playlist-end",
        str(limit),
        "--retries",
        "0",
        "--extractor-retries",
        "0",
        "--socket-timeout",
        "20",
    ]
    if settings.local.cookies_browser:
        args.extend(["--cookies-from-browser", settings.local.cookies_browser])
    result = run_process(
        [*args, "--", value], timeout=settings.local.timeout_seconds, max_bytes=settings.local.max_output_bytes
    )
    if result.returncode:
        raise SourceError("播放列表枚举失败，未自动换源或收费重试。")
    try:
        data = json.loads(result.stdout)
        entries = data["entries"]
        if not isinstance(entries, list):
            raise ValueError
        urls = []
        for row in entries:
            if not isinstance(row, dict):
                raise ValueError
            url = row.get("webpage_url") or row.get("url")
            if not isinstance(url, str):
                raise ValueError
            if re.fullmatch(r"[A-Za-z0-9_-]{11}", url):
                url = "https://www.youtube.com/watch?v=" + url
            if not url.startswith(("https://", "http://")):
                raise ValueError
            urls.append(InputSpec(kind="url", value=url))
        total = data.get("playlist_count")
        complete = isinstance(total, int) and total <= len(urls)
        return urls, {
            "provider": "yt-dlp",
            "discovered": len(urls),
            "total": total,
            "completeness": "complete" if complete else "unknown",
            "stop_reason": "end" if complete else "max_items_or_unknown",
            "limit": limit,
        }
    except (ValueError, KeyError, TypeError) as exc:
        raise ContractError("播放列表不是已支持的条目结构。") from exc


def expand_inputs(settings: SootaSettings, spec: TaskSpec, job: TaskJob) -> list[InputSpec]:
    expanded = []
    folder = task_folder(settings, job.id)
    for index, seed in enumerate(spec.inputs):
        key = str(index)
        source = source_for(seed)
        collection = seed.kind == "collection" or source.kind in {"channel", "user", "playlist"}
        if seed.kind == "file" and Path(seed.value).name == "catalog.json":
            expanded.extend(parse_inputs(None, Path(seed.value)))
            continue
        if seed.kind == "operation" and seed.expand:
            from soota.content.native import capture_operation

            cached = folder / f"native-enumeration-{index}.json"
            if cached.is_file():
                urls = json.loads(cached.read_text(encoding="utf-8"))["urls"]
            else:
                if spec.provider not in {"auto", "opencli"}:
                    raise SourceError("该搜索展开契约当前使用 OpenCLI，未切换显式来源。")
                native_result = capture_operation(settings, seed.operation or "", seed.params, folder / "enumeration")
                if native_result.entries is None:
                    raise ContractError("原生查询尚无可靠的列表转换契约；可先请求 data 查看结果。")
                urls = [entry.url for entry in native_result.entries]
                atomic_write(cached, json.dumps({"urls": urls}, ensure_ascii=False))
                job.enumeration[key] = {
                    "provider": native_result.provider,
                    "completeness": native_result.completeness,
                    "discovered": len(urls),
                    "limit": spec.max_items,
                }
                save_job(settings, job)
            expanded.extend(InputSpec(kind="url", value=url) for url in urls)
            continue
        if not collection:
            expanded.append(seed)
            continue
        previous = job.enumeration.get(key, {})
        if source.kind == "playlist":
            if spec.provider not in {"auto", "yt-dlp"}:
                raise SourceError("该播放列表标准枚举当前使用 yt-dlp，未切换显式来源。")
            cache = folder / f"playlist-{index}.json"
            if cache.is_file() and previous.get("limit", 0) >= spec.max_items:
                payload = json.loads(cache.read_text(encoding="utf-8"))
                items, info = [InputSpec.model_validate(i) for i in payload["inputs"]], previous
            else:
                items, info = _playlist(settings, seed.value, spec.max_items)
                atomic_write(cache, json.dumps({"inputs": [i.model_dump() for i in items]}, ensure_ascii=False))
            job.enumeration[key] = info
            save_job(settings, job)
            expanded.extend(items)
            continue
        if previous.get("job_id") and previous.get("resumable"):
            result = resume_catalog(
                settings, previous["job_id"], max_items=spec.max_items, max_pages=spec.max_pages, max_cost=spec.max_cost
            )
        elif previous.get("catalog"):
            result = None
        else:
            result = catalog(
                settings,
                seed.value,
                provider=spec.provider,
                max_items=spec.max_items,
                max_pages=spec.max_pages,
                max_cost=spec.max_cost,
                output=folder / "enumeration",
            )
        if result is not None:
            job.enumeration[key] = redact_any(result.data)
            save_job(settings, job)
            previous = result.data
        catalog_path = previous.get("catalog")
        if not catalog_path or not Path(catalog_path).is_file():
            raise SourceError("目录尚未取得条目，已保留枚举检查点。", details={"enumeration": previous})
        if spec.wants == {"data"}:
            expanded.append(InputSpec(kind="file", value=catalog_path))
        else:
            expanded.extend(parse_inputs(None, Path(catalog_path)))
    return expanded
