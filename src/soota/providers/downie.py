from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlencode

from soota.config import SootaSettings
from soota.errors import ContractError, SourceError, TranscriptError
from soota.models import AudioAsset, MediaItem, RequestRecord, SourceRef
from soota.providers.opencli_runtime import RuntimeUnavailable
from soota.providers.process import run_process
from soota.storage.cache import CacheStore
from soota.storage.locking import lock_file


class DownloadPending(TranscriptError):
    code = "download_pending"
    exit_code = 2


class DownieProvider:
    """Opt-in macOS media acquisition with a post-download completion receipt."""

    name = "downie"

    def __init__(self, settings: SootaSettings) -> None:
        self.settings = settings
        self.cache = CacheStore(settings.cache_dir / "downie")
        self.records: list[RequestRecord] = []

    def get_audio(self, item: MediaItem) -> AudioAsset:
        dest = self.cache.audio_path(item.source)
        with (dest.parent / ".lock").open("a") as handle:
            try:
                lock_file(handle, blocking=False)
            except BlockingIOError as exc:
                raise DownloadPending("同一 Downie 任务仍在处理中，未重复提交。") from exc
            if dest.is_file() and dest.stat().st_size:
                self.records.append(RequestRecord(endpoint="downie:cache:audio", billed=False, cache_hit=True))
                return AudioAsset(path=dest, provider=self.name)
            self._preflight()
            folder = self._submit_or_resume(item)
            media = self._wait_for_receipt(folder)
            self._verify_audio(media)
            self._write_audio(media, dest)
            self.records.append(RequestRecord(endpoint="downie:audio", billed=False, estimated_cost_usd=0))
            self.cache.put_json(item.source, "download", {"folder": str(folder), "status": "completed"})
            return AudioAsset(path=dest, provider=self.name)

    def _preflight(self) -> None:
        if sys.platform != "darwin":
            raise RuntimeUnavailable("Downie 仅能在安装了该应用的 macOS 上调用。")
        if not shutil.which("open") or not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            raise RuntimeUnavailable("Downie 音频接入需要 macOS open、ffmpeg 和 ffprobe。")

    def _submit_or_resume(self, item: MediaItem) -> Path:
        existing = self.cache.get_json(item.source, "download")
        if existing:
            folder = Path(existing["folder"])
            if not (folder / "request.json").is_file():
                raise ContractError("Downie 任务记录缺失；未自动重提，请检查原任务。")
            try:
                source = SourceRef.model_validate_json((folder / "request.json").read_text(encoding="utf-8"))
                if source.cache_identity != item.source.cache_identity or not folder.name.startswith("soota-downie-"):
                    raise ValueError("source mismatch")
            except (ValueError, OSError) as exc:
                raise ContractError("Downie 任务与本次来源不匹配；未自动重提。") from exc
            self.records.append(RequestRecord(endpoint="downie:resume", billed=False, estimated_cost_usd=0))
            return folder
        root = self.settings.output_dir / ".downloads"
        root.mkdir(parents=True, exist_ok=True)
        folder = Path(tempfile.mkdtemp(prefix="soota-downie-", dir=root))
        (folder / "request.json").write_text(item.source.model_dump_json(indent=2), encoding="utf-8")
        # Persist intent before dispatch. An uncertain outcome must never cause a duplicate submission.
        self.cache.put_json(item.source, "download", {"folder": str(folder), "status": "submitted"})
        url = "downie://XUOpenURL?" + urlencode({"url": item.source.canonical_url, "destination": str(folder)})
        self.records.append(RequestRecord(endpoint="downie:submit", billed=False, estimated_cost_usd=0))
        result = run_process(["open", "-g", "-a", "Downie 4", url], timeout=15, max_bytes=65536)
        if result.returncode:
            raise SourceError("Downie 任务提交失败或结果不明；未自动重提，请检查应用和任务记录。")
        return folder

    def _wait_for_receipt(self, folder: Path) -> Path:
        deadline = time.monotonic() + self.settings.audio.timeout_seconds
        receipt = folder / "complete.path"
        while time.monotonic() < deadline:
            if receipt.is_file():
                if receipt.stat().st_size > 16384:
                    raise ContractError("Downie 完成标记过大。")
                media = Path(receipt.read_text(encoding="utf-8")).resolve()
                if media.parent != folder.resolve() or not media.is_file() or not media.stat().st_size:
                    raise ContractError("Downie 完成标记不指向本任务内的有效文件。")
                return media
            time.sleep(0.5)
        raise DownloadPending(
            "尚未收到 Downie 下载完成标记，未开始转写，也未重复提交。",
            hint="检查 Downie 队列及自定义完成脚本；重新运行会继续等待原任务。",
            details={"task_dir": str(folder)},
        )

    def _verify_audio(self, media: Path) -> None:
        probe = run_process(
            ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "json", str(media)],
            timeout=30,
            max_bytes=65536,
        )
        try:
            streams = json.loads(probe.stdout)["streams"]
            has_audio = any(stream.get("codec_type") == "audio" for stream in streams)
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise TranscriptError("Downie 下载文件无法识别为媒体。") from exc
        if probe.returncode or not has_audio:
            raise TranscriptError("Downie 下载文件没有可用音轨。")

    def _write_audio(self, media: Path, dest: Path) -> None:
        temp = dest.with_name("audio.partial.m4a")
        try:
            result = run_process(
                ["ffmpeg", "-nostdin", "-y", "-v", "error", "-i", str(media), "-vn", "-c:a", "aac", str(temp)],
                timeout=self.settings.audio.timeout_seconds,
                max_bytes=65536,
            )
            if result.returncode or not temp.is_file() or not temp.stat().st_size:
                raise TranscriptError("Downie 文件已下载，但音轨提取失败。")
            temp.replace(dest)
        finally:
            temp.unlink(missing_ok=True)
