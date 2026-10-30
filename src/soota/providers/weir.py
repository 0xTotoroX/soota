"""Weir's read-only HTTP acquisition and free-credit guard.

[INPUT]: Ordered private keys, key-bound gift confirmations, usage and shared credit ledger.
[OUTPUT]: Raw single-post/Article/Reddit responses with confirmed credit settlement.
[POS]: Acquires provider data; content.weir owns source-text conversion.
[PROTOCOL]: 变更时更新此头部，然后检查 AGENTS.md
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import httpx

from soota.billing import selected_provider
from soota.config import SootaSettings
from soota.errors import AuthBudgetError, ContractError, SourceError
from soota.providers.weir_keys import WeirKey, gift_credits, load_keys, next_start_index
from soota.storage.credit_budget import CreditBudget
from soota.storage.task_budget import active_task_identity

BASE_URL = "https://api.weirapp.io"


def load_key(settings: SootaSettings) -> str | None:
    keys = load_keys(settings)
    return keys[0].key.get_secret_value() if keys else None


def _integer(data: dict[str, Any], name: str) -> int:
    value = data.get(name)
    if type(value) is not int or value < 0:
        raise ContractError("Weir 余额响应缺少有效整数，未发送数据请求。")
    return value


def free_period(data: dict[str, Any], needed: int, *, confirmed_gift_credits: int = 0) -> str:
    grant = data.get("freeGrant")
    if not isinstance(grant, dict) or data.get("spendingPaused") is not False:
        raise AuthBudgetError("Weir 没有可确认的免费额度，或账号暂停消费。")
    remaining = _integer(grant, "remaining")
    granted = _integer(grant, "granted")
    balance, reserved, available = (_integer(data, k) for k in ("balance", "reserved", "available"))
    # A confirmed gift may coexist, but never increases spendable monthly credits.
    if balance < remaining or data.get("tier") != "free" or balance - remaining > confirmed_gift_credits:
        raise AuthBudgetError("免费模式拒绝非 free 等级或未经确认的额外余额；赠送额度须由用户明确确认。")
    if remaining > granted or available != balance - reserved:
        raise ContractError("Weir 免费余额状态不一致，未发送数据请求。")
    if remaining - reserved < needed:
        raise AuthBudgetError("Weir 免费余额不足以覆盖本次最大扣费，未发送。")
    try:
        reset = datetime.fromisoformat(grant["resetsAt"].replace("Z", "+00:00"))
        if reset.tzinfo is None or reset <= datetime.now(UTC):
            raise ValueError("expired")
    except (KeyError, TypeError, AttributeError, ValueError) as exc:
        raise ContractError("Weir 免费额度重置时间无效，未发送数据请求。") from exc
    return reset.astimezone(UTC).isoformat()


class WeirClient:
    def __init__(self, settings: SootaSettings, *, credentials: list[WeirKey] | None = None) -> None:
        selected_provider(settings, "weir")
        self.settings = settings
        self.credentials = load_keys(settings) if credentials is None else credentials
        if not self.credentials:
            raise AuthBudgetError("缺少 Weir API Key；设置 WEIR_API_KEY 或使用 soota providers auth weir。")
        self.index = next_start_index(settings, len(self.credentials))
        self.budget = CreditBudget(settings.data_dir / "billing" / "weir")
        task = active_task_identity() or uuid4().hex
        self.task = hashlib.sha256(task.encode()).hexdigest()

    @property
    def credential(self) -> WeirKey:
        return self.credentials[self.index]

    def _request(self, client: httpx.Client, path: str) -> httpx.Response:
        try:
            return client.get(path)
        except httpx.HTTPError as exc:
            # A local timeout does not prove the server did not finish/charge.
            raise SourceError("Weir 请求未取得明确响应；已发送请求的预留保留，不自动重试。") from exc

    @staticmethod
    def _data(response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
            if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
                raise ValueError("invalid data")
            return dict(payload["data"])
        except ValueError as exc:
            raise ContractError("Weir 响应不符合 data 对象契约。") from exc

    def fetch(self, path: str, *, max_credits: int) -> dict[str, Any]:
        # Serializes usage -> reservation -> send -> settlement for local callers.
        with self.budget.locked() as budget:
            while self.index < len(self.credentials):
                credential = self.credential
                with httpx.Client(
                    base_url=BASE_URL,
                    headers={"Authorization": f"Bearer {credential.key.get_secret_value()}"},
                    timeout=self.settings.weir.timeout_seconds,
                    follow_redirects=False,
                ) as client:
                    usage = self._request(client, "/v1/usage")
                    if usage.status_code == 401:
                        self.index += 1
                        continue
                    if usage.status_code != 200:
                        raise AuthBudgetError(
                            "无法核实 Weir 免费余额，未发送数据请求。", details={"status": usage.status_code}
                        )
                    period = free_period(self._data(usage), max_credits,
                                         confirmed_gift_credits=gift_credits(self.settings, credential))
                    reservation = budget.reserve(
                        period=period,
                        task=self.task,
                        credits=max_credits,
                        monthly_cap=self.settings.weir.monthly_credits,
                        task_cap=self.settings.weir.task_credits,
                        key_name=credential.name,
                    )
                    response = self._request(client, path)
                    if response.status_code >= 400:
                        budget.settle(reservation, 0)
                        if response.status_code == 401:
                            self.index += 1
                            continue
                        error = AuthBudgetError if response.status_code in {402, 403, 429} else SourceError
                        raise error(
                            "Weir 拒绝或未能完成请求，不自动重试或切换付费来源。",
                            details={"status": response.status_code},
                        )
                    if response.status_code != 200:
                        raise ContractError("Weir 返回未知状态；保留预留，不自动重试。")
                    charged = response.headers.get("x-credits-charged", "")
                    if not charged.isascii() or not charged.isdigit():
                        raise ContractError("Weir 缺少有效扣费回执，保留最大预留。")
                    budget.settle(reservation, int(charged))
                    self.complete()
                    return self._data(response)
            raise AuthBudgetError("所有已配置 Weir Key 均认证失败；请更新凭据。", details={"status": 401})

    def complete(self) -> None:
        """Persist the next successful position for other local processes."""
        if len(self.credentials) > 1:
            next_start_index(self.settings, len(self.credentials), advance_from=self.index)
