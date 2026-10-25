from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Protocol

from soota.config import AsrConfig
from soota.errors import TranscriptError
from soota.models import CaptionPayload, TranscriptSegment
from soota.platform_support import utf8_environment


class AsrEngine(Protocol):
    name: str

    def transcribe(self, audio: Path, *, language: str | None) -> CaptionPayload | list[TranscriptSegment]: ...


class WhisperCliEngine:
    name = "whisper-cli"

    def __init__(self, config: AsrConfig) -> None:
        self.config = config

    def transcribe(self, audio: Path, *, language: str | None) -> CaptionPayload:
        binary = shutil.which(self.config.whisper_bin) or self.config.whisper_bin
        if not shutil.which(binary) and not Path(binary).exists():
            raise TranscriptError(
                "本机没有 whisper CLI。",
                hint="brew install openai-whisper，或把 asr.engine 设为 faster-whisper。大模型下载前会先停下来。",
            )
        work = audio.parent / "asr"
        work.mkdir(parents=True, exist_ok=True)
        cmd = [
            binary,
            str(audio),
            "--model",
            self.config.model,
            "--output_dir",
            str(work),
            "--output_format",
            "json",
            "--verbose",
            "False",
        ]
        if language:
            cmd.extend(["--language", _whisper_lang(language)])
        result = subprocess.run(
            cmd, check=False, capture_output=True, text=True, encoding="utf-8", env=utf8_environment()
        )
        if result.returncode != 0:
            raise TranscriptError(
                "whisper CLI 转写失败。",
                hint=result.stderr[-400:] if result.stderr else "检查模型是否已下载。默认使用 base，避免自动拉 turbo。",
            )
        json_path = work / f"{audio.stem}.json"
        if not json_path.is_file():
            matches = list(work.glob("*.json"))
            if not matches:
                raise TranscriptError("whisper 没有写出 JSON。")
            json_path = matches[0]
        payload = json.loads(json_path.read_text(encoding="utf-8"))
        return CaptionPayload(format="whisper-json", body=payload)


class FasterWhisperEngine:
    name = "faster-whisper"

    def __init__(self, config: AsrConfig) -> None:
        self.config = config

    def transcribe(self, audio: Path, *, language: str | None) -> CaptionPayload:
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise TranscriptError(
                "未安装 faster-whisper。",
                hint="uv sync --extra asr ，并确认模型已在本地，不要在未确认时下载大模型。",
            ) from exc
        model = WhisperModel(self.config.model, device=self.config.device)
        segments_iter, _info = model.transcribe(str(audio), language=_whisper_lang(language) if language else None)
        return CaptionPayload(
            format="whisper-json",
            body={"segments": [{"start": item.start, "end": item.end, "text": item.text} for item in segments_iter]},
        )


class MlxWhisperEngine:
    name = "mlx-whisper"

    def __init__(self, config: AsrConfig) -> None:
        self.config = config

    def transcribe(self, audio: Path, *, language: str | None) -> CaptionPayload:
        binary = shutil.which(self.config.mlx_whisper_bin)
        if not binary:
            raise TranscriptError("未找到 mlx_whisper CLI。", hint="安装 mlx-whisper，并配置 asr.mlx_whisper_bin。")
        model = self.config.mlx_model_path
        if not model:
            if self.config.model != "base":
                raise TranscriptError("MLX 非 base 模型需设置 asr.mlx_model_path。")
            model = "mlx-community/whisper-base-mlx"
        model = _mlx_model_path(model)
        with tempfile.TemporaryDirectory(prefix="soota-mlx-") as folder:
            cmd = [
                binary,
                str(audio),
                "--model",
                model,
                "--output-dir",
                folder,
                "--output-name",
                "transcript",
                "--output-format",
                "json",
                "--verbose",
                "False",
                "--task",
                "transcribe",
                "--fp16",
                "True",
                "--best-of",
                "None",
            ]
            if language:
                cmd.extend(["--language", _whisper_lang(language)])
            result = subprocess.run(
                cmd, check=False, capture_output=True, text=True, encoding="utf-8", env=utf8_environment()
            )
            output = Path(folder) / "transcript.json"
            if result.returncode or not output.is_file():
                raise TranscriptError(
                    "MLX Whisper 转写失败。", hint="检查模型目录与 CLI；可设置 asr.engine=whisper-cli 回退。"
                )
            try:
                payload = json.loads(output.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise TranscriptError("MLX Whisper 没有生成有效 JSON。") from exc
            return CaptionPayload(format="whisper-json", body=payload)


def _mlx_model_path(model: str) -> str:
    # MLX treats a nonexistent path as a Hugging Face repo ID. Keep explicit
    # local paths local, without exposing private paths in public diagnostics.
    path = Path(model).expanduser()
    if not (path.is_absolute() or model.startswith(("./", "../", "~")) or path.exists()):
        return model
    if not path.is_dir():
        raise TranscriptError(
            "MLX Whisper 本地模型目录缺失或不是目录。",
            hint="检查 asr.mlx_model_path，指向已有本地模型目录；未调用 MLX、下载或切换远程模型。",
            details={"reason": "local_model_missing", "dispatched": False},
        )
    if not (path / "config.json").is_file() or not any(
        (path / name).is_file() for name in ("weights.safetensors", "weights.npz")
    ):
        raise TranscriptError(
            "MLX Whisper 本地模型文件不完整。",
            hint="目录需要 config.json 和 weights.safetensors 或 weights.npz；未自动补下载。",
            details={"reason": "local_model_incomplete", "dispatched": False},
        )
    return str(path.resolve())


def resolve_engine(config: AsrConfig) -> AsrEngine:
    engine = config.engine
    if engine == "mlx-whisper":
        return MlxWhisperEngine(config)
    if engine == "faster-whisper":
        return FasterWhisperEngine(config)
    if engine == "whisper-cli":
        return WhisperCliEngine(config)
    if shutil.which("whisper") or Path(config.whisper_bin).exists():
        return WhisperCliEngine(config)
    try:
        import faster_whisper  # noqa: F401

        return FasterWhisperEngine(config)
    except ImportError:
        return WhisperCliEngine(config)


def _whisper_lang(language: str) -> str:
    lowered = language.lower()
    if lowered.startswith("zh"):
        return "zh"
    if lowered.startswith("en"):
        return "en"
    return lowered.split("-")[0]
