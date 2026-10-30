"""treg 同步读取、报价和持久回执。

[INPUT]: 受控目录、HTTP、共享报价缓存、PaymentGate、任务预算。
[OUTPUT]: TregClient、load_key。
[POS]: 不执行自动供应商切换；未知提交保留回执并停止重复收费。
[PROTOCOL]: 变更时更新此头部，然后检查 AGENTS.md
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import uuid
from collections.abc import Callable
from typing import Any

import httpx

from soota.billing import PaymentGate, selected_provider
from soota.config import SootaSettings
from soota.errors import AuthBudgetError, ContractError, SourceError
from soota.models import RequestRecord
from soota.providers.quotes import cached_quote, namespace
from soota.providers.treg.catalog import ENDPOINTS, resolve
from soota.storage.atomic import atomic_write
from soota.storage.locking import lock_file
from soota.storage.task_budget import active_task_identity, block_active_task, reserve_active_task


def load_key(settings: SootaSettings) -> str:
    value = os.environ.get("TREG_TOKEN", "").strip()
    path = settings.config_dir / "credentials" / "treg.key"
    return value or (path.read_text(encoding="utf-8").strip() if path.is_file() else "")


class TregClient:
    def __init__(
        self,
        settings: SootaSettings,
        max_cost: float | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
        before_request: Callable[[RequestRecord], None] | None = None,
        run_scope: str | None = None,
    ) -> None:
        selected_provider(settings, "treg")
        self.settings = settings
        self.key = load_key(settings)
        self.namespace = namespace(settings.treg.base_url, self.key)
        self.gate = PaymentGate(settings, max_cost, provider="treg")
        self.before_request = before_request
        self.run_scope = run_scope
        self.records: list[RequestRecord] = []
        self.http = httpx.Client(
            base_url=settings.treg.base_url,
            timeout=settings.treg.timeout_seconds,
            transport=transport,
            follow_redirects=False,
        )
        self.root = settings.data_dir / "treg-runs" / self.namespace
        self.blocked = False

    def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            return self.http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise SourceError("treg 网络请求未完成；已发送请求的回执会保留，不自动重复提交。") from exc

    def quote(self, endpoint: str, *, refresh: bool = False) -> dict[str, Any]:
        endpoint = resolve(endpoint)

        def fetch() -> dict[str, Any]:
            response = self.request("GET", "/catalog/endpoints/" + endpoint)
            if response.is_error:
                raise SourceError(f"treg 报价查询失败（HTTP {response.status_code}）。")
            data = self._json(response).get("endpoint")
            path, method, _ = ENDPOINTS[endpoint]
            if (
                not isinstance(data, dict)
                or data.get("id") != endpoint
                or data.get("path") != path
                or data.get("method") != method
                or data.get("provider") != endpoint.split(".", 1)[0]
                or data.get("platform_eligible") is not True
            ):
                raise ContractError("treg 目录身份、方法或托管可用性与已适配契约不符。")
            cost = data.get("cost")
            if (
                not isinstance(cost, dict)
                or cost.get("type") not in {"per_call", "per_success"}
                or cost.get("unit") != "call"
                or cost.get("per", 1) != 1
                or any(cost.get(k) for k in ("varies", "table", "settle", "reported_charge"))
            ):
                raise ContractError("treg 只接受已核实的固定单次报价。")
            price = cost.get("usd")
            if isinstance(price, bool) or not isinstance(price, (int, float)) or not math.isfinite(price) or price <= 0:
                raise ContractError("treg 缺少有效美元单价。")
            return {"estimated_cost_usd": price, "endpoint": endpoint, "price": cost}

        return cached_quote(self.settings, "treg", self.namespace, endpoint, fetch, refresh=refresh)

    @staticmethod
    def _json(response: httpx.Response) -> dict[str, Any]:
        try:
            data = response.json()
        except ValueError as exc:
            raise ContractError("treg 响应不是有效 JSON。") from exc
        if not isinstance(data, dict):
            raise ContractError("treg 响应必须是对象。")
        return data

    def wallet(self) -> dict[str, float]:
        if not self.key:
            raise AuthBudgetError("未配置 treg Token，无法查询余额。")
        headers = {"X-Treg-Token": self.key}
        identity = self.request("GET", "/auth/me", headers=headers)
        if identity.status_code != 200:
            raise AuthBudgetError("无法确认 treg 当前组织。")
        org = self._json(identity).get("org_id")
        if type(org) is not int or org <= 0:
            raise ContractError("treg 身份响应缺少有效 org_id。")
        response = self.request("GET", f"/orgs/{org}/balance", headers=headers)
        if response.status_code != 200:
            raise AuthBudgetError("无法读取 treg 组织余额。")
        data = self._json(response)
        balance = data.get("balance_micro")
        if data.get("org_id") != org or type(balance) is not int:
            raise ContractError("treg 余额响应的组织或金额无效。")
        # Available balance already excludes holds; do not subtract them twice.
        return {"balance": balance / 1_000_000}

    def run(self, endpoint: str, *, params: dict[str, Any] | None = None, body: Any = None) -> dict[str, Any]:
        endpoint = resolve(endpoint)
        if not self.key:
            raise AuthBudgetError("未配置 treg Token。", hint="设置 TREG_TOKEN 或运行 soota providers auth treg。")
        scope = self.run_scope or active_task_identity()
        digest = hashlib.sha256(json.dumps([scope, endpoint, params, body], sort_keys=True).encode()).hexdigest()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.root / (digest + ".json")
        with (self.root / (digest + ".lock")).open("a") as lock:
            lock_file(lock)
            saved = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
            if saved.get("state") == "completed" and scope is None:
                saved = {}  # New standalone reads may refresh data; uncertain outcomes never reset.
            if saved:
                if saved.get("state") != "completed":
                    raise SourceError(
                        "上次 treg 请求未确认成功，已阻止重复扣费。", hint="根据回执中的 call_id 核查上游结果。"
                    )
                record = RequestRecord.model_validate(saved["record"])
                record.billed, record.cache_hit = False, True
                self.records.append(record)
                return dict(saved["response"])
            if self.blocked:
                raise AuthBudgetError("treg 费用需要核对，已停止后续收费请求。")
            if self.gate.cap == 0:
                raise AuthBudgetError("--max-cost 0 禁止发送计费接口请求。")
            cost = self.quote(endpoint)["estimated_cost_usd"]
            record = RequestRecord(endpoint="treg:" + endpoint, billed=True, estimated_cost_usd=cost)

            def authorize(r: RequestRecord) -> None:
                self.gate.authorize(r, self.wallet)
                if self.before_request is not None:
                    self.before_request(r)

            reserve_active_task(record, authorize)
            self.records.append(record)
            saved = {
                "state": "submitting",
                "idempotency_key": uuid.uuid4().hex,
                "endpoint": endpoint,
                "params": params,
                "body": body,
                "record": record.model_dump(mode="json"),
            }
            atomic_write(path, json.dumps(saved))
            headers = {
                "X-Treg-Token": self.key,
                "Idempotency-Key": saved["idempotency_key"],
                "X-Treg-Route-Max-Cost": str(cost),
                "X-Treg-Route-Waterfall": "0",
            }
            response = self.request(
                ENDPOINTS[endpoint][1], "/call/" + endpoint, params=params, json=body, headers=headers
            )
            call_id = response.headers.get("X-Treg-Call-Id")
            saved["call_id"] = call_id
            record.note = f"call_id:{call_id};endpoint:{endpoint}"
            charged = response.headers.get("X-Treg-Cost-Micro")
            if charged is not None and charged.isascii() and charged.isdigit() and response.status_code != 202:
                record.actual_cost_usd = int(charged) / 1_000_000
            saved["record"] = record.model_dump(mode="json")
            atomic_write(path, json.dumps(saved))
            if record.actual_cost_usd is not None and record.actual_cost_usd > cost:
                self.blocked = True
                block_active_task("treg_actual_cost_exceeds_quote")
                raise ContractError("treg 实付超过预留报价，保留回执并停止后续请求。")
            if response.status_code in {401, 402, 403}:
                raise AuthBudgetError(f"treg 认证、权限或预算拒绝（HTTP {response.status_code}）。")
            if response.status_code != 200 or response.headers.get("X-Treg-Upstream-Failed", "").lower() in {
                "1",
                "true",
            }:
                raise SourceError(f"treg 未返回已完成的读取结果（HTTP {response.status_code}）；不会重新提交。")
            data = self._json(response)
            saved.update(state="completed", response=data)
            atomic_write(path, json.dumps(saved, ensure_ascii=False))
            return data
