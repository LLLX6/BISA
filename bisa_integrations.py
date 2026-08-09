"""Honest, dependency-injected external adapter boundary for BISA.

No adapter in this repository claims delivery or payment success. A production
connector must be injected after its credentials, webhook verification, retry
policy, and contract tests are approved.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import asdict, dataclass
from typing import Any, Mapping, Protocol

from bisa_security import iso


ADAPTER_NAMES = {"whatsapp", "payment", "email", "push"}


@dataclass(frozen=True)
class AdapterResult:
    ok: bool
    adapter: str
    status: str
    configured: bool
    provider_reference: str = ""
    error_code: str = ""

    def public(self) -> dict[str, Any]:
        return asdict(self)


class ExternalAdapter(Protocol):
    name: str
    configured: bool

    def status_result(self) -> AdapterResult:
        """Return local capability state without making an external request."""

    def perform(self, action_kind: str, request: Mapping[str, Any]) -> AdapterResult:
        """Execute one provider action and return a truthful normalized result."""


class UnavailableAdapter:
    def __init__(self, name: str, *, configured: bool = False, reason: str = "adapter_not_configured"):
        if name not in ADAPTER_NAMES:
            raise ValueError("unknown_adapter")
        self.name = name
        self.configured = bool(configured)
        self.reason = reason

    def status_result(self) -> AdapterResult:
        return AdapterResult(
            ok=False,
            adapter=self.name,
            status="unavailable",
            configured=self.configured,
            error_code=self.reason,
        )

    def perform(self, action_kind: str, request: Mapping[str, Any]) -> AdapterResult:
        del action_kind, request
        return self.status_result()


class WhatsAppAdapter(Protocol):
    name: str
    configured: bool

    def perform(self, action_kind: str, request: Mapping[str, Any]) -> AdapterResult: ...


class PaymentAdapter(Protocol):
    name: str
    configured: bool

    def perform(self, action_kind: str, request: Mapping[str, Any]) -> AdapterResult: ...


class EmailAdapter(Protocol):
    name: str
    configured: bool

    def perform(self, action_kind: str, request: Mapping[str, Any]) -> AdapterResult: ...


class PushAdapter(Protocol):
    name: str
    configured: bool

    def perform(self, action_kind: str, request: Mapping[str, Any]) -> AdapterResult: ...


REQUIRED_ENV = {
    "whatsapp": ("BISA_WHATSAPP_PHONE_NUMBER_ID", "BISA_WHATSAPP_ACCESS_TOKEN"),
    "payment": ("BISA_PAYMENT_GATEWAY", "BISA_PAYMENT_WEBHOOK_SECRET"),
    "email": ("BISA_SMTP_HOST", "BISA_SMTP_USER", "BISA_SMTP_PASSWORD", "BISA_SMTP_FROM_EMAIL"),
    "push": ("BISA_VAPID_PUBLIC_KEY", "BISA_VAPID_PRIVATE_KEY", "BISA_VAPID_SUBJECT"),
}


def unavailable_from_environment(name: str, environment: Mapping[str, str] | None = None) -> UnavailableAdapter:
    if name not in ADAPTER_NAMES:
        raise ValueError("unknown_adapter")
    values = environment if environment is not None else os.environ
    required = REQUIRED_ENV[name]
    placeholders = {"unconfigured", "disabled", "none", "null", "false"}
    present = [
        bool(str(values.get(key, "")).strip())
        and str(values.get(key, "")).strip().lower() not in placeholders
        for key in required
    ]
    if not any(present):
        reason = "adapter_not_configured"
        configured = False
    elif not all(present):
        reason = "adapter_configuration_incomplete"
        configured = False
    else:
        # Credentials alone are not an implementation and never mean success.
        reason = "adapter_implementation_required"
        configured = True
    return UnavailableAdapter(name, configured=configured, reason=reason)


class AdapterRegistry:
    def __init__(self, adapters: Mapping[str, ExternalAdapter] | None = None, *, environment: Mapping[str, str] | None = None):
        supplied = dict(adapters or {})
        unknown = set(supplied) - ADAPTER_NAMES
        if unknown:
            raise ValueError("unknown_adapter")
        self._adapters: dict[str, ExternalAdapter] = {
            name: supplied.get(name) or unavailable_from_environment(name, environment)
            for name in sorted(ADAPTER_NAMES)
        }

    def get(self, name: str) -> ExternalAdapter:
        if name not in self._adapters:
            raise ValueError("unknown_adapter")
        return self._adapters[name]

    def snapshot(self) -> dict[str, dict[str, Any]]:
        result = {}
        for name, adapter in self._adapters.items():
            status_method = getattr(adapter, "status_result", None)
            probe = status_method() if callable(status_method) else AdapterResult(
                ok=False, adapter=name, status="unavailable",
                configured=bool(getattr(adapter, "configured", False)),
                error_code="adapter_status_unavailable",
            )
            result[name] = {
                "available": bool(probe.ok),
                "configured": bool(probe.configured),
                "status": probe.status,
                "errorCode": probe.error_code,
            }
        return result


def execute_external_action(
    con,
    adapter: ExternalAdapter,
    *,
    action_kind: str,
    target_kind: str,
    target_id: str,
    request: Mapping[str, Any],
) -> AdapterResult:
    """Execute and audit only delivery metadata; request content is never persisted."""
    adapter_name = str(getattr(adapter, "name", ""))
    if adapter_name not in ADAPTER_NAMES:
        raise ValueError("unknown_adapter")
    action_kind = str(action_kind or "")[:80]
    target_kind = str(target_kind or "")[:40]
    target_id = str(target_id or "")[:180]
    if not action_kind or not target_kind or not target_id:
        raise ValueError("external_action_target_required")
    try:
        result = adapter.perform(action_kind, request)
    except Exception:
        result = AdapterResult(
            ok=False, adapter=adapter_name, status="failed",
            configured=bool(getattr(adapter, "configured", False)),
            error_code="adapter_execution_failed",
        )
    valid_type = isinstance(result, AdapterResult)
    success_status = valid_type and result.status in {"accepted", "succeeded", "pending"}
    invalid_success = (
        not valid_type
        or bool(result.ok) != success_status
        or (result.ok and not result.provider_reference)
    )
    if not valid_type or (
        result.adapter != adapter_name
        or result.status not in {"unavailable", "accepted", "succeeded", "failed", "pending"}
        or invalid_success
    ):
        result = AdapterResult(
            ok=False, adapter=adapter_name, status="failed",
            configured=bool(getattr(adapter, "configured", False)),
            error_code="adapter_result_invalid",
        )
    stamp = iso()
    con.execute(
        """INSERT INTO external_action_attempts(
        id,adapter,action_kind,target_kind,target_id,status,provider_reference,error_code,created_at,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (
            f"external_{uuid.uuid4().hex}", adapter_name, action_kind, target_kind, target_id,
            result.status, str(result.provider_reference or "")[:180], str(result.error_code or "")[:120],
            stamp, stamp,
        ),
    )
    return result


def default_registry(environment: Mapping[str, str] | None = None) -> AdapterRegistry:
    return AdapterRegistry(environment=environment)
