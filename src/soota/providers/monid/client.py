"""
[INPUT]: Monid HTTP contracts, PaymentGate and shared task reservations
[OUTPUT]: MonidClient, quote_cost and credential loading
[POS]: Monid transport and durable run receipts
[PROTOCOL]: 变更时更新此头部，然后检查 AGENTS.md
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import time
from collections.abc import Callable
from typing import Any

import httpx

from soota.billing import PaymentGate, selected_provider
from soota.config import SootaSettings
from soota.errors import AuthBudgetError, ContractError, SourceError
from soota.models import RequestRecord
from soota.storage.atomic import atomic_write
from soota.storage.locking import lock_file
from soota.storage.task_budget import active_task_identity, reserve_active_task

APIFY_CAPTIONS = "/starvibe/youtube-video-transcript"


def load_key(settings: SootaSettings) -> str:
    key = os.environ.get("MONID_API_KEY", "").strip()
    path = settings.config_dir / "credentials" / "monid.key"
    return key or (path.read_text(encoding="utf-8").strip() if path.is_file() else "")


def usd(value: Any) -> float:
    if not isinstance(value, dict) or value.get("currency") != "USD":
        raise ContractError("Monid 价格必须明确为 USD。")
    amount = value.get("value")
    if isinstance(amount, bool) or not isinstance(amount, (int, float)) or not math.isfinite(amount) or amount < 0:
        raise ContractError("Monid 返回无效金额。")
    return float(amount)


def quote_cost(detail: dict[str, Any]) -> float:
    price = detail.get("price", {})
    if not isinstance(price, dict) or price.get("type") not in {"PER_CALL", "PER_RESULT"}:
        raise AuthBudgetError("Monid 该计费单位尚未接入，未发送收费请求。")
    cost = usd(price.get("amount"))
    flat = price.get("flatFee")
    if flat is not None:
        cost += usd(flat if isinstance(flat, dict) else {"value": flat, "currency": "USD"})
    if not math.isfinite(cost) or cost < 0:
        raise AuthBudgetError("Monid 报价无效。")
    return cost


class MonidClient:
    def __init__(
        self,
        settings: SootaSettings,
        max_cost: float | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
        before_request: Callable[[RequestRecord], None] | None = None,
        run_scope: str | None = None,
    ) -> None:
        selected_provider(settings, "monid")
        self.settings = settings
        self.key = load_key(settings)
        self.records: list[RequestRecord] = []
        self.gate = PaymentGate(settings, max_cost, provider="monid")
        self.before_request = before_request
        self.run_scope = run_scope
        self.http = httpx.Client(
            base_url=settings.monid.base_url,
            timeout=settings.monid.timeout_seconds,
            transport=transport,
            follow_redirects=False,
        )
        namespace = hashlib.sha256((settings.monid.base_url + self.key).encode()).hexdigest()[:16]
        self.root = settings.data_dir / "monid-runs" / namespace

    def request(self, method: str, path: str, body: Any = None) -> dict[str, Any]:
        if not self.key:
            raise AuthBudgetError("未配置 Monid Key。", hint="设置 MONID_API_KEY 或运行 soota providers auth monid。")
        try:
            response = self.http.request(method, path, json=body, headers={"Authorization": "Bearer " + self.key})
        except httpx.HTTPError as exc:
            raise SourceError("Monid 网络请求未完成；已提交任务不会自动重新收费提交。") from exc
        if response.status_code in {401, 402, 403}:
            raise AuthBudgetError("Monid 认证、权限或余额不足。")
        if response.is_error:
            raise SourceError(f"Monid 返回 HTTP {response.status_code}。")
        try:
            data = response.json()
        except ValueError as exc:
            raise ContractError("Monid 响应不是有效 JSON。") from exc
        if not isinstance(data, dict):
            raise ContractError("Monid 响应必须是对象。")
        return data

    def inspect(self, provider: str, endpoint: str) -> dict[str, Any]:
        return self.request("POST", "/v1/inspect", {"provider": provider, "endpoint": endpoint})

    def wallet(self) -> dict[str, float]:
        data = self.request("GET", "/v1/wallet/balance")
        return {"balance": max(0, usd(data.get("balance")) - usd(data.get("held", {"value": 0, "currency": "USD"})))}

    def run(self, provider: str, endpoint: str, inputs: dict[str, Any], *, reuse: bool = False) -> dict[str, Any]:
        # Only reviewed single-result adapters use PER_RESULT; arbitrary batch sizes cannot bypass the cap.
        identity = {"provider": provider, "endpoint": endpoint, "input": inputs}
        scope = self.run_scope or active_task_identity()
        reuse = reuse or scope is not None
        digest = hashlib.sha256(json.dumps({**identity, "scope": scope}, sort_keys=True).encode()).hexdigest()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.root / (digest + ".json")
        with (self.root / (digest + ".lock")).open("a") as lock:
            lock_file(lock)
            saved = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
            if saved.get("response", {}).get("status") in {"COMPLETED", "FAILED"} and not reuse:
                saved = {}
            if not saved:
                detail = self.inspect(provider, endpoint)
                if detail.get("price", {}).get("type") == "PER_RESULT" and not (
                    provider == "apify"
                    and endpoint == APIFY_CAPTIONS
                    and isinstance(inputs.get("body"), dict)
                    and isinstance(inputs["body"].get("youtube_url"), str)
                    and not inputs.get("body", {}).get("channel_url")
                ):
                    raise AuthBudgetError("按结果计费仅开放已验证的单视频字幕请求。")
                cost = quote_cost(detail)
                record = RequestRecord(endpoint=f"monid:{provider}:{endpoint}", billed=True, estimated_cost_usd=cost)

                def authorize(r: RequestRecord) -> None:
                    self.gate.authorize(r, self.wallet)
                    if self.before_request is not None:
                        self.before_request(r)

                reserve_active_task(record, authorize)
                self.records.append(record)
                saved = {"state": "submitting", "record": record.model_dump(mode="json")}
                atomic_write(path, json.dumps(saved))
                # A lost POST response leaves submitting on disk. Never blindly resubmit it.
                response = self.request("POST", "/v1/run", identity)
                saved.update(state="accepted", response=response)
                atomic_write(path, json.dumps(saved))
            else:
                record = RequestRecord.model_validate(saved["record"])
                record.billed = False
                record.cache_hit = True
                record.note = "resume-original-run;reservation-already-persisted"
                self.records.append(record)
            if saved["state"] == "submitting":
                raise SourceError(
                    "上次 Monid 提交结果未知，已阻止重复扣费。", hint="检查 Monid 运行记录后再处理该任务。"
                )
            response = saved["response"]
            for _ in range(self.settings.monid.max_polls):
                if response.get("status") in {"COMPLETED", "FAILED"}:
                    break
                run_id = response.get("runId")
                if not isinstance(run_id, str) or not run_id.isalnum():
                    raise ContractError("Monid 缺少有效 runId。")
                time.sleep(self.settings.monid.poll_seconds)
                response = self.request("GET", "/v1/runs/" + run_id)
                saved["response"] = response
                atomic_write(path, json.dumps(saved))
            cost_value = response.get("cost")
            if cost_value is not None:
                record.actual_cost_usd = usd(cost_value)
            record.note = f"runId:{response.get('runId')};supplier:{provider};endpoint:{endpoint}"
            saved["record"] = record.model_dump(mode="json")
            atomic_write(path, json.dumps(saved))
            if record.actual_cost_usd is not None and record.actual_cost_usd > (record.estimated_cost_usd or 0):
                raise ContractError("Monid 实付超过报价，已保留费用回执并停止后续请求。")
            if response.get("status") not in {"COMPLETED", "FAILED"}:
                raise SourceError("Monid 任务仍在运行；续跑会查询原 runId，不会重新提交。")
            upstream_response = response.get("providerResponse")
            status = upstream_response.get("httpStatus") if isinstance(upstream_response, dict) else None
            if response.get("status") == "FAILED" or not isinstance(status, int) or not 200 <= status < 300:
                raise SourceError(
                    "Monid 上游请求失败。", details={"runId": response.get("runId"), "http_status": status}
                )
            return response
