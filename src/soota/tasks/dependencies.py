"""采集目标的无副作用依赖判断。

[INPUT]: TaskSpec、InputSpec 和已知原生附件。
[OUTPUT]: acquisition_goals 返回媒体/正文依赖，task_stages 分出必需与可选阶段。
[POS]: 规划预览与任务执行共用，图片依赖正文时不能漏报正文获取。
[PROTOCOL]: 变更时更新此头部，然后检查 AGENTS.md
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

from soota.content.native import MEDIA_SUFFIXES
from soota.content.native_bindings import media_binding
from soota.errors import InputError
from soota.tasks.models import InputSpec, TaskSpec


def task_stages(spec: TaskSpec) -> list[TaskSpec]:
    if spec.required and spec.required != spec.wants:
        required = spec.model_copy(update={"outputs": [output for output in spec.outputs if output.required]})
        return [required, spec]
    return [spec]


def acquisition_goals(spec: TaskSpec, seed: InputSpec, files: list[Path] | None = None) -> tuple[set[str], set[str]]:
    native_images = seed.kind == "operation" and any(path.suffix.lower() in MEDIA_SUFFIXES for path in files or [])
    if seed.kind == "file":
        native_images = Path(seed.value).suffix.lower() in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".avif"}
    if seed.kind == "url":
        try:
            media_binding(seed.value)
            native_images = True
        except InputError:
            native_images = Path(urlsplit(seed.value).path).suffix.lower() in {".jpg", ".jpeg", ".png", ".gif", ".webp"}
    media = spec.wants & {"video", "audio"}
    text = spec.wants & {"context", "markdown", "transcript"}
    if "images" in spec.wants:
        (media if native_images else text).add("images")
    return media, text
