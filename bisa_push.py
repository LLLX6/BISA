"""Role-scoped Web Push subscriptions and a transactional delivery outbox.

This module is deliberately independent from the HTTP server.  Production
composition injects a real Web Push transport only after VAPID credentials are
available.  The default transport is unavailable and can never report a fake
delivery.

``install_push_schema`` installs an SQLite trigger on ``notifications``.  The
trigger creates one outbox job per active role binding in the *same database
transaction* as the notification.  Other databases can call
``enqueue_notification`` immediately after inserting the notification, using
the same transaction/connection.
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import os
import re
import secrets
import socket
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import urlsplit, urlunsplit

from bisa_domain import DomainError, connect as domain_connect

try:  # Optional at import time; readiness remains closed if unavailable.
    import requests
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    from py_vapid import Vapid02
    from pywebpush import WebPushException, webpush
except ImportError:  # pragma: no cover - exercised by production readiness.
    requests = None
    Encoding = PublicFormat = Vapid02 = WebPushException = webpush = None


PUSH_ENDPOINT_HOSTS = frozenset(
    {
        "fcm.googleapis.com",
        "updates.push.services.mozilla.com",
        "web.push.apple.com",
    }
)
PUSH_ENDPOINT_HOST_SUFFIXES = (".notify.windows.com",)
PUSH_DELIVERY_TIMEOUT_SECONDS = 10
PUSH_DEFAULT_TTL_SECONDS = 24 * 60 * 60
PUSH_MAX_TTL_SECONDS = 28 * 24 * 60 * 60
PUSH_DEFAULT_LEASE_SECONDS = 60
PUSH_MAX_ATTEMPTS = 8

_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.:-]{1,180}$")
_KEY = re.compile(r"^[A-Za-z0-9_-]+={0,2}$")

SHOPPER_ROLES = frozenset({"shopper"})
MERCHANT_ROLES = frozenset({"merchant_owner", "merchant_manager", "merchant_staff"})
SUPPLIER_ROLES = frozenset({"supplier_advertiser"})
ADMIN_ROLES = frozenset(
    {
        "support_admin",
        "catalog_moderator",
        "merchant_reviewer",
        "finance",
        "advertising_manager",
        "admin",
        "super_admin",
    }
)


@dataclass(frozen=True)
class PushSendResult:
    """Normalized result returned by a production transport."""

    accepted: bool
    permanent: bool = False
    deactivate_binding: bool = False
    provider_reference: str = ""
    error_code: str = ""


class PushTransport(Protocol):
    """Narrow transport boundary; implementations must not follow redirects."""

    configured: bool
    vapid_configured: bool
    public_key: str

    def send(
        self,
        *,
        subscription: Mapping[str, Any],
        payload: Mapping[str, str],
        ttl_seconds: int,
        timeout_seconds: int,
        allow_redirects: bool,
    ) -> PushSendResult: ...


class UnavailablePushTransport:
    """Honest default used when no reviewed VAPID adapter is installed."""

    configured = False
    vapid_configured = False
    public_key = ""

    def __init__(self, reason: str = "push_not_configured"):
        self.reason = str(reason or "push_not_configured")[:120]

    def send(self, **_: Any) -> PushSendResult:
        return PushSendResult(False, error_code=self.reason)


if requests:
    class _NoRedirectPushSession(requests.Session):
        """A transport that ignores proxy environment and never follows redirects."""

        def __init__(self):
            super().__init__()
            self.trust_env = False

        def post(self, url, *args, **kwargs):
            kwargs["allow_redirects"] = False
            return super().post(url, *args, **kwargs)
else:  # pragma: no cover
    _NoRedirectPushSession = None


class PyWebPushTransport:
    """Reviewed VAPID adapter for ``BisaPushService``.

    It accepts only a matched public/private VAPID pair and an HTTPS or
    ``mailto:`` subject.  HTTP redirects are rejected by the injected session.
    """

    def __init__(self, *, public_key: str, private_key: str, subject: str):
        self.public_key = str(public_key or "").strip().rstrip("=")
        self.subject = str(subject or "").strip()
        self._vapid = None
        self.reason = "push_not_configured"
        if not (webpush and Vapid02 and _NoRedirectPushSession):
            self.reason = "push_transport_unavailable"
            return
        if not self.public_key or not str(private_key or "").strip() or not self._valid_subject():
            self.reason = "push_vapid_configuration_invalid"
            return
        try:
            private_value = str(private_key).strip()
            vapid = (
                Vapid02.from_pem(private_value.encode("utf-8"))
                if "-----BEGIN" in private_value
                else Vapid02.from_string(private_value)
            )
            derived = base64.urlsafe_b64encode(
                vapid.public_key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
            ).decode("ascii").rstrip("=")
        except Exception:
            self.reason = "push_vapid_configuration_invalid"
            return
        if not secrets.compare_digest(derived, self.public_key):
            self.reason = "push_vapid_key_mismatch"
            return
        self._vapid = vapid
        self.reason = ""

    def _valid_subject(self) -> bool:
        if self.subject.startswith("mailto:"):
            return "@" in self.subject[7:] and len(self.subject) <= 320
        try:
            parsed = urlsplit(self.subject)
        except ValueError:
            return False
        return bool(
            parsed.scheme == "https" and parsed.hostname and not parsed.username
            and not parsed.password and not parsed.fragment and len(self.subject) <= 500
        )

    @property
    def configured(self) -> bool:
        return self._vapid is not None

    @property
    def vapid_configured(self) -> bool:
        return self.configured

    def send(
        self,
        *,
        subscription: Mapping[str, Any],
        payload: Mapping[str, str],
        ttl_seconds: int,
        timeout_seconds: int,
        allow_redirects: bool,
    ) -> PushSendResult:
        if not self.configured:
            return PushSendResult(False, error_code=self.reason or "push_not_configured")
        if allow_redirects:
            return PushSendResult(False, permanent=True, error_code="push_redirect_forbidden")
        session = _NoRedirectPushSession()
        try:
            response = webpush(
                subscription_info=dict(subscription),
                data=json.dumps(dict(payload), ensure_ascii=False, separators=(",", ":")),
                vapid_private_key=self._vapid,
                vapid_claims={"sub": self.subject},
                ttl=max(60, min(int(ttl_seconds), PUSH_MAX_TTL_SECONDS)),
                timeout=max(1, min(int(timeout_seconds), PUSH_DELIVERY_TIMEOUT_SECONDS)),
                requests_session=session,
            )
            status = int(getattr(response, "status_code", 0) or 0)
            if 200 <= status <= 202:
                reference = str(getattr(response, "headers", {}).get("Location") or f"http-{status}")
                return PushSendResult(True, provider_reference=reference[:180])
            return PushSendResult(False, error_code=f"push_http_{status or 'unknown'}")
        except Exception as exc:
            response = getattr(exc, "response", None)
            status = int(getattr(response, "status_code", 0) or 0)
            if status in {404, 410}:
                return PushSendResult(
                    False, permanent=True, deactivate_binding=True,
                    error_code="push_endpoint_expired",
                )
            if 300 <= status < 400:
                return PushSendResult(False, permanent=True, error_code="push_redirect_rejected")
            return PushSendResult(
                False,
                error_code=f"push_http_{status}" if status else "push_transport_exception",
            )
        finally:
            session.close()


def push_transport_from_environment() -> PushTransport:
    """Build the real adapter only from a complete, validated BISA namespace."""

    candidate = PyWebPushTransport(
        public_key=os.environ.get("BISA_VAPID_PUBLIC_KEY", ""),
        private_key=os.environ.get("BISA_VAPID_PRIVATE_KEY", ""),
        subject=os.environ.get("BISA_VAPID_SUBJECT", ""),
    )
    return candidate if candidate.configured else UnavailablePushTransport(candidate.reason)


@dataclass(frozen=True)
class ClaimedPush:
    id: str
    notification_id: str
    binding_id: str
    claim_token: str
    attempts: int
    expires_at: str


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def _parse_iso(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _identifier(value: Any, code: str) -> str:
    result = str(value or "").strip()
    if not _IDENTIFIER.fullmatch(result):
        raise DomainError(code, 422)
    return result


def _endpoint_host_allowed(hostname: str) -> bool:
    hostname = str(hostname or "").rstrip(".").lower()
    return hostname in PUSH_ENDPOINT_HOSTS or any(
        hostname.endswith(suffix) and hostname != suffix[1:]
        for suffix in PUSH_ENDPOINT_HOST_SUFFIXES
    )


def validate_push_endpoint(
    endpoint: Any,
    *,
    resolver: Callable[..., Any] | None = None,
    resolve_dns: bool = True,
) -> str:
    """Validate and canonicalize a browser-vendor endpoint.

    HTTPS, the vendor allowlist and public DNS answers are all mandatory.
    Redirects are never accepted by this module's transport contract.
    """

    raw = str(endpoint or "").strip()
    if not raw or len(raw) > 2048 or any(ord(ch) < 32 for ch in raw):
        raise DomainError("invalid_push_endpoint", 422)
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise DomainError("invalid_push_endpoint", 422) from exc
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
        or port not in (None, 443)
    ):
        raise DomainError("invalid_push_endpoint", 422)
    hostname = parsed.hostname.rstrip(".").lower()
    if not _endpoint_host_allowed(hostname):
        raise DomainError("push_endpoint_host_not_allowed", 422)
    if hostname in {"localhost", "localhost.localdomain"} or hostname.endswith(
        (".localhost", ".local", ".internal")
    ):
        raise DomainError("push_endpoint_not_public", 422)

    if resolve_dns:
        lookup = resolver or socket.getaddrinfo
        try:
            addresses = lookup(hostname, port or 443, type=socket.SOCK_STREAM)
        except (OSError, socket.gaierror) as exc:
            raise DomainError("push_endpoint_unresolvable", 422) from exc
        if not addresses:
            raise DomainError("push_endpoint_unresolvable", 422)
        for address in addresses:
            try:
                candidate = ipaddress.ip_address(str(address[4][0]).split("%", 1)[0])
            except (IndexError, ValueError, TypeError) as exc:
                raise DomainError("invalid_push_endpoint", 422) from exc
            if isinstance(candidate, ipaddress.IPv6Address) and candidate.ipv4_mapped:
                candidate = candidate.ipv4_mapped
            if not candidate.is_global or any(
                (
                    candidate.is_private,
                    candidate.is_loopback,
                    candidate.is_link_local,
                    candidate.is_reserved,
                    candidate.is_multicast,
                    candidate.is_unspecified,
                )
            ):
                raise DomainError("push_endpoint_not_public", 422)

    netloc = hostname if port in (None, 443) else f"{hostname}:{port}"
    return urlunsplit(("https", netloc, parsed.path or "/", parsed.query, ""))


def _validate_key(
    value: Any, *, name: str, minimum: int, maximum: int, decoded_length: int
) -> str:
    result = str(value or "").strip()
    if not minimum <= len(result) <= maximum or not _KEY.fullmatch(result):
        raise DomainError(f"invalid_push_{name}", 422)
    try:
        decoded = base64.urlsafe_b64decode(result + "=" * (-len(result) % 4))
    except (ValueError, TypeError) as exc:
        raise DomainError(f"invalid_push_{name}", 422) from exc
    if len(decoded) != decoded_length or (name == "p256dh" and decoded[:1] != b"\x04"):
        raise DomainError(f"invalid_push_{name}", 422)
    return result.rstrip("=")


def actor_push_scope(actor: Mapping[str, Any] | None) -> dict[str, str]:
    """Return the one notification audience represented by the active role."""

    if not actor:
        raise DomainError("authentication_required", 401)
    role = _identifier(actor.get("role"), "invalid_push_role")
    account_id = _identifier(actor.get("accountId"), "invalid_push_account")
    if role in SHOPPER_ROLES:
        audience_kind, audience_id = "account", account_id
    elif role in MERCHANT_ROLES:
        audience_kind = "merchant"
        audience_id = _identifier(actor.get("merchantId"), "merchant_scope_required")
    elif role in SUPPLIER_ROLES:
        # Supplier sessions use the existing merchantId session-scope column.
        audience_kind = "supplier"
        audience_id = _identifier(
            actor.get("supplierId") or actor.get("merchantId"), "supplier_scope_required"
        )
    elif role in ADMIN_ROLES:
        audience_kind, audience_id = "admin", account_id
    else:
        raise DomainError("push_role_not_supported", 403)
    return {
        "accountId": account_id,
        "role": role,
        "audienceKind": audience_kind,
        "audienceId": audience_id,
    }


_SCHEMA_STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS push_subscriptions(
    id TEXT PRIMARY KEY, endpoint_hash TEXT NOT NULL UNIQUE, endpoint TEXT NOT NULL,
    p256dh TEXT NOT NULL, auth TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS push_subscription_bindings(
    id TEXT PRIMARY KEY,
    subscription_id TEXT NOT NULL REFERENCES push_subscriptions(id) ON DELETE CASCADE,
    account_id TEXT NOT NULL, role TEXT NOT NULL,
    audience_kind TEXT NOT NULL, audience_id TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1, deactivated_reason TEXT NOT NULL DEFAULT '',
    last_success_at TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE(subscription_id,account_id,role,audience_kind,audience_id))""",
    """CREATE TABLE IF NOT EXISTS push_delivery_outbox(
    id TEXT PRIMARY KEY,
    notification_id TEXT NOT NULL REFERENCES notifications(id) ON DELETE CASCADE,
    binding_id TEXT NOT NULL REFERENCES push_subscription_bindings(id) ON DELETE CASCADE,
    target_kind TEXT NOT NULL, target_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
      CHECK(status IN('pending','processing','delivered','expired','dead','cancelled')),
    attempts INTEGER NOT NULL DEFAULT 0, available_at TEXT NOT NULL,
    expires_at TEXT NOT NULL, claim_token TEXT NOT NULL DEFAULT '',
    lease_until TEXT NOT NULL DEFAULT '', last_error TEXT NOT NULL DEFAULT '',
    delivered_at TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE(notification_id,binding_id))""",
    """CREATE INDEX IF NOT EXISTS idx_push_binding_audience
    ON push_subscription_bindings(audience_kind,audience_id,active)""",
    """CREATE INDEX IF NOT EXISTS idx_push_outbox_claim
    ON push_delivery_outbox(status,available_at,lease_until,expires_at)""",
)


def install_push_schema(con: sqlite3.Connection) -> None:
    """Install the isolated schema and transactional SQLite notification hook."""

    notification_columns = {
        str(row[1]) for row in con.execute("PRAGMA table_info(notifications)").fetchall()
    }
    required = {"id", "target_kind", "target_id", "created_at"}
    if not required.issubset(notification_columns):
        raise RuntimeError("push_notifications_table_required")
    for statement in _SCHEMA_STATEMENTS:
        con.execute(statement)
    expiry_expression = (
        "CASE WHEN NEW.expires_at<>'' THEN NEW.expires_at "
        "ELSE strftime('%Y-%m-%dT%H:%M:%f+00:00','now','+1 day') END"
        if "expires_at" in notification_columns
        else "strftime('%Y-%m-%dT%H:%M:%f+00:00','now','+1 day')"
    )
    con.execute("DROP TRIGGER IF EXISTS bisa_notification_push_outbox")
    con.execute(
        f"""CREATE TRIGGER bisa_notification_push_outbox
        AFTER INSERT ON notifications
        BEGIN
          INSERT OR IGNORE INTO push_delivery_outbox(
            id,notification_id,binding_id,target_kind,target_id,status,attempts,
            available_at,expires_at,claim_token,lease_until,last_error,delivered_at,
            created_at,updated_at)
          SELECT 'pushout_' || lower(hex(randomblob(16))), NEW.id, binding.id,
            NEW.target_kind, NEW.target_id, 'pending', 0, NEW.created_at,
            {expiry_expression}, '', '', '', '', NEW.created_at, NEW.created_at
          FROM push_subscription_bindings binding
          WHERE binding.active=1 AND binding.audience_kind=NEW.target_kind
            AND (binding.audience_id=NEW.target_id OR
                 (NEW.target_kind='admin' AND NEW.target_id='admin'));
        END"""
    )
    con.execute("DROP TRIGGER IF EXISTS bisa_notification_push_cancel")
    lifecycle_columns = {"acted_at", "dismissed_at"}.intersection(notification_columns)
    if lifecycle_columns:
        resolved = " OR ".join(f"NEW.{column}<>''" for column in sorted(lifecycle_columns))
        con.execute(
            f"""CREATE TRIGGER bisa_notification_push_cancel
            AFTER UPDATE ON notifications
            WHEN {resolved}
            BEGIN
              UPDATE push_delivery_outbox SET status='cancelled',claim_token='',
                lease_until='',last_error='notification_resolved',updated_at=CURRENT_TIMESTAMP
              WHERE notification_id=NEW.id AND status IN('pending','processing');
            END"""
        )


def enqueue_notification(
    con: sqlite3.Connection, notification_id: str, *, expires_at: str = ""
) -> int:
    """Explicit transactional hook for non-trigger composition.

    Call this on the same connection immediately after inserting a notification.
    The uniqueness constraint makes repeated calls idempotent.
    """

    notification_id = _identifier(notification_id, "invalid_notification_id")
    notification = con.execute(
        "SELECT id,target_kind,target_id,created_at FROM notifications WHERE id=?",
        (notification_id,),
    ).fetchone()
    if not notification:
        raise DomainError("notification_not_found", 404)
    row = dict(notification)
    expiry = str(expires_at or "").strip()
    if not expiry:
        columns = {str(item[1]) for item in con.execute("PRAGMA table_info(notifications)")}
        if "expires_at" in columns:
            expiry_row = con.execute(
                "SELECT expires_at FROM notifications WHERE id=?", (notification_id,)
            ).fetchone()
            expiry = str(expiry_row[0] or "")
    expiry = expiry or _iso(_utc_now() + timedelta(seconds=PUSH_DEFAULT_TTL_SECONDS))
    bindings = con.execute(
        """SELECT id FROM push_subscription_bindings
        WHERE active=1 AND audience_kind=?
          AND (audience_id=? OR (?='admin' AND ?='admin'))""",
        (row["target_kind"], row["target_id"], row["target_kind"], row["target_id"]),
    ).fetchall()
    inserted = 0
    for binding in bindings:
        result = con.execute(
            """INSERT OR IGNORE INTO push_delivery_outbox(
            id,notification_id,binding_id,target_kind,target_id,status,attempts,
            available_at,expires_at,claim_token,lease_until,last_error,delivered_at,
            created_at,updated_at) VALUES(?,?,?,?,?,'pending',0,?,?,?,?,?,?,?,?)""",
            (
                f"pushout_{uuid.uuid4().hex}", notification_id, binding["id"],
                row["target_kind"], row["target_id"], row["created_at"], expiry,
                "", "", "", "", row["created_at"], row["created_at"],
            ),
        )
        inserted += int(result.rowcount or 0)
    return inserted


class BisaPushService:
    """Database-backed role-scoped Web Push orchestration."""

    def __init__(
        self,
        *,
        connection_factory: Callable[..., Any] = domain_connect,
        transport: PushTransport | None = None,
        resolver: Callable[..., Any] | None = None,
        lease_seconds: int = PUSH_DEFAULT_LEASE_SECONDS,
        max_attempts: int = PUSH_MAX_ATTEMPTS,
    ):
        self._connect = connection_factory
        self.transport = transport or UnavailablePushTransport()
        self.resolver = resolver or socket.getaddrinfo
        self.lease_seconds = max(15, min(int(lease_seconds), 15 * 60))
        self.max_attempts = max(1, min(int(max_attempts), 20))

    def capability(self) -> dict[str, Any]:
        implementation = callable(getattr(self.transport, "send", None))
        configured = bool(getattr(self.transport, "configured", False)) and implementation
        vapid_configured = bool(getattr(self.transport, "vapid_configured", False))
        public_key = (
            str(getattr(self.transport, "public_key", "") or "")
            if configured and vapid_configured
            else ""
        )
        available = bool(configured and vapid_configured and public_key)
        return {
            "available": available,
            "configured": bool(configured and vapid_configured),
            "status": "ready" if available else "unavailable",
            "publicKey": public_key,
            "errorCode": "" if available else str(
                getattr(self.transport, "reason", "push_not_configured")
                or "push_not_configured"
            )[:120],
        }

    def status(
        self,
        actor: Mapping[str, Any],
        endpoint: Any | None = None,
        endpoint_hash: Any | None = None,
    ) -> dict[str, Any]:
        scope = actor_push_scope(actor)
        normalized_hash = str(endpoint_hash or "").strip().lower()
        if normalized_hash and not re.fullmatch(r"[0-9a-f]{64}", normalized_hash):
            raise DomainError("invalid_push_endpoint_hash", 422)
        if endpoint not in (None, ""):
            canonical = validate_push_endpoint(endpoint, resolve_dns=False)
            normalized_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        with self._connect() as con:
            params: list[Any] = [
                scope["accountId"], scope["role"], scope["audienceKind"], scope["audienceId"],
            ]
            endpoint_clause = ""
            if normalized_hash:
                endpoint_clause = (
                    " AND subscription_id IN(SELECT id FROM push_subscriptions "
                    "WHERE endpoint_hash=?)"
                )
                params.append(normalized_hash)
            active = bool(con.execute(
                """SELECT 1 FROM push_subscription_bindings WHERE active=1
                AND account_id=? AND role=? AND audience_kind=? AND audience_id=?"""
                + endpoint_clause + " LIMIT 1",
                params,
            ).fetchone())
        return {**self.capability(), "activeForCurrentRole": active, "role": scope["role"]}

    def subscribe(self, actor: Mapping[str, Any], subscription: Mapping[str, Any]) -> dict[str, Any]:
        scope = actor_push_scope(actor)
        if not isinstance(subscription, Mapping):
            raise DomainError("valid_push_subscription_required", 422)
        endpoint = validate_push_endpoint(
            subscription.get("endpoint"), resolver=self.resolver, resolve_dns=True
        )
        keys = subscription.get("keys")
        if not isinstance(keys, Mapping):
            raise DomainError("valid_push_keys_required", 422)
        p256dh = _validate_key(
            keys.get("p256dh"), name="p256dh", minimum=80, maximum=180,
            decoded_length=65,
        )
        auth = _validate_key(
            keys.get("auth"), name="auth", minimum=20, maximum=120,
            decoded_length=16,
        )
        endpoint_hash = hashlib.sha256(endpoint.encode("utf-8")).hexdigest()
        stamp = _iso(_utc_now())
        with self._connect(immediate=True) as con:
            existing = con.execute(
                "SELECT * FROM push_subscriptions WHERE endpoint_hash=?", (endpoint_hash,)
            ).fetchone()
            if existing:
                if existing["p256dh"] != p256dh or existing["auth"] != auth:
                    other = con.execute(
                        """SELECT 1 FROM push_subscription_bindings
                        WHERE subscription_id=? AND active=1 AND NOT(
                          account_id=? AND role=? AND audience_kind=? AND audience_id=?) LIMIT 1""",
                        (
                            existing["id"], scope["accountId"], scope["role"],
                            scope["audienceKind"], scope["audienceId"],
                        ),
                    ).fetchone()
                    if other:
                        raise DomainError("push_subscription_key_mismatch", 409)
                    con.execute(
                        "UPDATE push_subscriptions SET p256dh=?,auth=?,updated_at=? WHERE id=?",
                        (p256dh, auth, stamp, existing["id"]),
                    )
                subscription_id = existing["id"]
            else:
                subscription_id = f"pushsub_{uuid.uuid4().hex}"
                con.execute(
                    """INSERT INTO push_subscriptions(
                    id,endpoint_hash,endpoint,p256dh,auth,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?)""",
                    (subscription_id, endpoint_hash, endpoint, p256dh, auth, stamp, stamp),
                )
            foreign_account = con.execute(
                """SELECT 1 FROM push_subscription_bindings
                WHERE subscription_id=? AND active=1 AND account_id<>? LIMIT 1""",
                (subscription_id, scope["accountId"]),
            ).fetchone()
            if foreign_account:
                raise DomainError("push_subscription_already_bound", 409)
            binding = con.execute(
                """SELECT id FROM push_subscription_bindings
                WHERE subscription_id=? AND account_id=? AND role=?
                  AND audience_kind=? AND audience_id=?""",
                (
                    subscription_id, scope["accountId"], scope["role"],
                    scope["audienceKind"], scope["audienceId"],
                ),
            ).fetchone()
            if binding:
                binding_id = binding["id"]
                con.execute(
                    """UPDATE push_subscription_bindings SET active=1,
                    deactivated_reason='',updated_at=? WHERE id=?""",
                    (stamp, binding_id),
                )
            else:
                binding_id = f"pushbind_{uuid.uuid4().hex}"
                con.execute(
                    """INSERT INTO push_subscription_bindings(
                    id,subscription_id,account_id,role,audience_kind,audience_id,
                    active,deactivated_reason,last_success_at,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,1,'','',?,?)""",
                    (
                        binding_id, subscription_id, scope["accountId"], scope["role"],
                        scope["audienceKind"], scope["audienceId"], stamp, stamp,
                    ),
                )
        return {
            "subscriptionId": subscription_id,
            "bindingId": binding_id,
            "role": scope["role"],
            "scopeKind": scope["audienceKind"],
            "active": True,
            "capability": self.capability(),
        }

    def _deactivate(
        self,
        actor: Mapping[str, Any],
        *,
        endpoint: Any | None,
        reason: str,
    ) -> dict[str, Any]:
        scope = actor_push_scope(actor)
        endpoint_hash = ""
        if endpoint not in (None, ""):
            canonical = validate_push_endpoint(endpoint, resolve_dns=False)
            endpoint_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        stamp = _iso(_utc_now())
        with self._connect(immediate=True) as con:
            args: list[Any] = [
                scope["accountId"], scope["role"], scope["audienceKind"], scope["audienceId"],
            ]
            endpoint_clause = ""
            if endpoint_hash:
                endpoint_clause = " AND subscription_id IN(SELECT id FROM push_subscriptions WHERE endpoint_hash=?)"
                args.append(endpoint_hash)
            rows = con.execute(
                """SELECT id FROM push_subscription_bindings WHERE active=1
                AND account_id=? AND role=? AND audience_kind=? AND audience_id=?"""
                + endpoint_clause,
                args,
            ).fetchall()
            binding_ids = [row["id"] for row in rows]
            for binding_id in binding_ids:
                con.execute(
                    """UPDATE push_subscription_bindings SET active=0,
                    deactivated_reason=?,updated_at=? WHERE id=? AND active=1""",
                    (reason, stamp, binding_id),
                )
                con.execute(
                    """UPDATE push_delivery_outbox SET status='cancelled',claim_token='',
                    lease_until='',last_error=?,updated_at=?
                    WHERE binding_id=? AND status IN('pending','processing')""",
                    (reason, stamp, binding_id),
                )
        return {"deactivated": len(binding_ids), "role": scope["role"]}

    def unsubscribe(self, actor: Mapping[str, Any], endpoint: Any) -> dict[str, Any]:
        return self._deactivate(actor, endpoint=endpoint, reason="unsubscribed")

    def logout_scope(
        self, actor: Mapping[str, Any], endpoint: Any | None = None
    ) -> dict[str, Any]:
        """Deactivate only the current role scope; other roles/devices survive."""

        return self._deactivate(actor, endpoint=endpoint, reason="role_logout")

    def logout_account(self, account_id: Any) -> dict[str, Any]:
        account_id = _identifier(account_id, "invalid_push_account")
        stamp = _iso(_utc_now())
        with self._connect(immediate=True) as con:
            rows = con.execute(
                "SELECT id FROM push_subscription_bindings WHERE account_id=? AND active=1",
                (account_id,),
            ).fetchall()
            binding_ids = [row["id"] for row in rows]
            for binding_id in binding_ids:
                con.execute(
                    """UPDATE push_subscription_bindings SET active=0,
                    deactivated_reason='account_logout',updated_at=? WHERE id=?""",
                    (stamp, binding_id),
                )
                con.execute(
                    """UPDATE push_delivery_outbox SET status='cancelled',claim_token='',
                    lease_until='',last_error='account_logout',updated_at=?
                    WHERE binding_id=? AND status IN('pending','processing')""",
                    (stamp, binding_id),
                )
        return {"deactivated": len(binding_ids), "accountId": account_id}

    def claim_pending(
        self, *, limit: int = 20, now: datetime | None = None
    ) -> list[ClaimedPush]:
        now = (now or _utc_now()).astimezone(UTC)
        stamp = _iso(now)
        lease_until = _iso(now + timedelta(seconds=self.lease_seconds))
        claimed: list[ClaimedPush] = []
        with self._connect(immediate=True) as con:
            rows = con.execute(
                """SELECT * FROM push_delivery_outbox
                WHERE (status='pending' AND available_at<=?)
                   OR (status='processing' AND lease_until<>'' AND lease_until<=?)
                ORDER BY created_at,id LIMIT ?""",
                (stamp, stamp, max(1, min(int(limit), 100))),
            ).fetchall()
            for raw in rows:
                row = dict(raw)
                expires = _parse_iso(row["expires_at"])
                if expires and expires <= now:
                    con.execute(
                        """UPDATE push_delivery_outbox SET status='expired',
                        claim_token='',lease_until='',last_error='notification_expired',updated_at=?
                        WHERE id=? AND status IN('pending','processing')""",
                        (stamp, row["id"]),
                    )
                    continue
                if int(row["attempts"] or 0) >= self.max_attempts:
                    con.execute(
                        """UPDATE push_delivery_outbox SET status='dead',claim_token='',
                        lease_until='',last_error='delivery_attempts_exhausted',updated_at=?
                        WHERE id=? AND status IN('pending','processing')""",
                        (stamp, row["id"]),
                    )
                    continue
                token = secrets.token_urlsafe(30)
                result = con.execute(
                    """UPDATE push_delivery_outbox SET status='processing',attempts=attempts+1,
                    claim_token=?,lease_until=?,updated_at=? WHERE id=? AND
                    ((status='pending' AND available_at<=?) OR
                     (status='processing' AND lease_until<>'' AND lease_until<=?))""",
                    (token, lease_until, stamp, row["id"], stamp, stamp),
                )
                if result.rowcount == 1:
                    claimed.append(
                        ClaimedPush(
                            id=row["id"], notification_id=row["notification_id"],
                            binding_id=row["binding_id"], claim_token=token,
                            attempts=int(row["attempts"] or 0) + 1,
                            expires_at=row["expires_at"],
                        )
                    )
        return claimed

    def _claim_delivery_data(self, claim: ClaimedPush) -> dict[str, Any] | None:
        with self._connect() as con:
            row = con.execute(
                """SELECT o.id,o.status,o.claim_token,o.expires_at,b.active,b.id binding_id,
                s.endpoint,s.p256dh,s.auth
                FROM push_delivery_outbox o
                JOIN push_subscription_bindings b ON b.id=o.binding_id
                JOIN push_subscriptions s ON s.id=b.subscription_id
                WHERE o.id=? AND o.status='processing' AND o.claim_token=?""",
                (claim.id, claim.claim_token),
            ).fetchone()
            return dict(row) if row else None

    def complete_claim(
        self,
        claim: ClaimedPush,
        result: PushSendResult,
        *,
        now: datetime | None = None,
    ) -> str:
        """Finalize only the holder of the current claim token."""

        now = (now or _utc_now()).astimezone(UTC)
        stamp = _iso(now)
        valid_result = isinstance(result, PushSendResult)
        if not valid_result:
            result = PushSendResult(False, error_code="invalid_transport_result")
        if result.accepted and not str(result.provider_reference or "").strip():
            result = PushSendResult(False, error_code="missing_provider_reference")
        error = str(result.error_code or "delivery_failed")[:120]
        with self._connect(immediate=True) as con:
            row = con.execute(
                """SELECT attempts,binding_id FROM push_delivery_outbox
                WHERE id=? AND status='processing' AND claim_token=?""",
                (claim.id, claim.claim_token),
            ).fetchone()
            if not row:
                return "stale_claim"
            if result.accepted:
                changed = con.execute(
                    """UPDATE push_delivery_outbox SET status='delivered',delivered_at=?,
                    claim_token='',lease_until='',last_error='',updated_at=?
                    WHERE id=? AND status='processing' AND claim_token=?""",
                    (stamp, stamp, claim.id, claim.claim_token),
                )
                if changed.rowcount:
                    con.execute(
                        """UPDATE push_subscription_bindings SET last_success_at=?,updated_at=?
                        WHERE id=?""",
                        (stamp, stamp, row["binding_id"]),
                    )
                    return "delivered"
                return "stale_claim"
            attempts = int(row["attempts"] or 0)
            if result.permanent or attempts >= self.max_attempts:
                final_error = error if result.permanent else "delivery_attempts_exhausted"
                con.execute(
                    """UPDATE push_delivery_outbox SET status='dead',claim_token='',
                    lease_until='',last_error=?,updated_at=?
                    WHERE id=? AND status='processing' AND claim_token=?""",
                    (final_error, stamp, claim.id, claim.claim_token),
                )
                if result.deactivate_binding:
                    con.execute(
                        """UPDATE push_subscription_bindings SET active=0,
                        deactivated_reason=?,updated_at=? WHERE id=?""",
                        (final_error, stamp, row["binding_id"]),
                    )
                return "dead"
            delay = min(3600, 15 * (2 ** min(attempts, 8)))
            con.execute(
                """UPDATE push_delivery_outbox SET status='pending',available_at=?,
                claim_token='',lease_until='',last_error=?,updated_at=?
                WHERE id=? AND status='processing' AND claim_token=?""",
                (
                    _iso(now + timedelta(seconds=delay)), error, stamp,
                    claim.id, claim.claim_token,
                ),
            )
            return "retried"

    def process_claim(
        self, claim: ClaimedPush, *, now: datetime | None = None
    ) -> str:
        now = (now or _utc_now()).astimezone(UTC)
        data = self._claim_delivery_data(claim)
        if not data:
            return "stale_claim"
        if not bool(data["active"]):
            with self._connect(immediate=True) as con:
                changed = con.execute(
                    """UPDATE push_delivery_outbox SET status='cancelled',claim_token='',
                    lease_until='',last_error='subscription_inactive',updated_at=?
                    WHERE id=? AND status='processing' AND claim_token=?""",
                    (_iso(now), claim.id, claim.claim_token),
                )
            return "cancelled" if changed.rowcount else "stale_claim"
        try:
            endpoint = validate_push_endpoint(
                data["endpoint"], resolver=self.resolver, resolve_dns=True
            )
        except DomainError as exc:
            transient = exc.code == "push_endpoint_unresolvable"
            return self.complete_claim(
                claim,
                PushSendResult(
                    False,
                    permanent=not transient,
                    deactivate_binding=not transient,
                    error_code=exc.code,
                ),
                now=now,
            )
        expiry = _parse_iso(data["expires_at"])
        if expiry and expiry <= now:
            with self._connect(immediate=True) as con:
                changed = con.execute(
                    """UPDATE push_delivery_outbox SET status='expired',claim_token='',
                    lease_until='',last_error='notification_expired',updated_at=?
                    WHERE id=? AND status='processing' AND claim_token=?""",
                    (_iso(now), claim.id, claim.claim_token),
                )
            return "expired" if changed.rowcount else "stale_claim"
        remaining = int((expiry - now).total_seconds()) if expiry else PUSH_DEFAULT_TTL_SECONDS
        ttl = max(60, min(remaining, PUSH_MAX_TTL_SECONDS))
        payload = {"notificationId": claim.notification_id}
        try:
            result = self.transport.send(
                subscription={
                    "endpoint": endpoint,
                    "keys": {"p256dh": data["p256dh"], "auth": data["auth"]},
                },
                payload=payload,
                ttl_seconds=ttl,
                timeout_seconds=PUSH_DELIVERY_TIMEOUT_SECONDS,
                allow_redirects=False,
            )
        except Exception:
            result = PushSendResult(False, error_code="transport_exception")
        return self.complete_claim(claim, result, now=now)

    def run_once(
        self, *, limit: int = 20, now: datetime | None = None
    ) -> dict[str, Any]:
        capability = self.capability()
        if not capability["available"]:
            return {
                "status": "unavailable", "configured": capability["configured"],
                "claimed": 0, "delivered": 0, "retried": 0, "dead": 0,
                "expired": 0, "cancelled": 0,
                "errorCode": capability["errorCode"],
            }
        claims = self.claim_pending(limit=limit, now=now)
        counts = {
            "delivered": 0, "retried": 0, "dead": 0,
            "expired": 0, "cancelled": 0,
        }
        for claim in claims:
            outcome = self.process_claim(claim, now=now)
            if outcome in counts:
                counts[outcome] += 1
        return {
            "status": "processed", "configured": True, "claimed": len(claims),
            **counts, "errorCode": "",
        }
