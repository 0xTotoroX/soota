"""[INPUT]: 采集和处理层的确定失败。
[OUTPUT]: 稳定错误码、退出码及结构化证据。
[POS]: 共享错误边界；删除与环境验证分别编码。
[PROTOCOL]: 变更时更新此头部，然后检查 AGENTS.md
"""

from __future__ import annotations


class SootaError(Exception):
    """Typed CLI/application error with a stable machine code and exit status."""

    code = "internal"
    exit_code = 1

    def __init__(self, message: str, *, hint: str | None = None, details: dict[str, object] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.details = details or {}

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "ok": False,
            "status": "error",
            "code": self.code,
            "exit_code": self.exit_code,
            "message": self.message,
        }
        if self.hint:
            payload["hint"] = self.hint
        if self.details:
            payload["details"] = self.details
        return payload


class InputError(SootaError):
    code = "input"
    exit_code = 3


class AuthBudgetError(SootaError):
    code = "auth_budget"
    exit_code = 4


class SourceError(SootaError):
    code = "source"
    exit_code = 5


class RemoteDeleted(SourceError):
    code = "remote_deleted"


class VerificationRequired(SourceError):
    code = "verification_required"


class TranscriptError(SootaError):
    code = "transcript"
    exit_code = 6


class StorageError(SootaError):
    code = "storage"
    exit_code = 7


class SourcePreserved(SootaError):
    code = "source_preserved"
    exit_code = 2


class ContractError(SootaError):
    code = "contract"
    exit_code = 8


class CaptionUnavailable(SourceError):
    """Known acquisition failure eligible for another free caption provider."""


class CaptionsAbsent(SourceError):
    """The current source explicitly reports an empty caption track list."""
