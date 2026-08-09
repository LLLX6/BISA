"""Independent authentication, authorization, and private-media controls for BISA.

The module deliberately does not import ``bisa_domain``. HTTP and domain code
can adopt it without creating a circular dependency. All tokens are returned
only once and only hashes are persisted.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from http.cookies import CookieError, SimpleCookie
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping
from urllib.parse import quote

import bisa_config


MERCHANT_ROLES = {"merchant_owner", "merchant_manager", "merchant_staff"}
ADMIN_ROLES = {
    "support_admin", "catalog_moderator", "merchant_reviewer", "finance",
    "advertising_manager", "admin", "super_admin",
}
REFRESH_COOKIE_NAMES = {
    "shopper": "bisa_shopper_refresh",
    "supplier_advertiser": "bisa_supplier_refresh",
    "support_admin": "bisa_admin_refresh",
    "catalog_moderator": "bisa_admin_refresh",
    "merchant_reviewer": "bisa_admin_refresh",
    "finance": "bisa_admin_refresh",
    "advertising_manager": "bisa_admin_refresh",
    "admin": "bisa_admin_refresh",
    "super_admin": "bisa_admin_refresh",
}
PRIVATE_OWNER_KINDS = {"account", "merchant", "merchant_application", "supplier", "support_case"}
PRIVATE_MIME_TYPES = {"image/jpeg", "image/png", "image/webp", "application/pdf"}
IDENTIFIER = re.compile(r"[A-Za-z0-9._:-]{1,180}\Z")
SHA256_HEX = re.compile(r"[a-f0-9]{64}\Z")


class SecurityError(Exception):
    def __init__(self, code: str, status: int = 400, detail: dict[str, Any] | None = None):
        super().__init__(code)
        self.code = code
        self.status = status
        self.detail = detail or {}


def utcnow() -> datetime:
    return datetime.now(UTC)


def as_utc(value: datetime | None = None) -> datetime:
    current = value or utcnow()
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return current.astimezone(UTC)


def iso(value: datetime | None = None) -> str:
    return as_utc(value).isoformat()


def parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _bounded_env(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, min(maximum, value))


@contextmanager
def security_connection(*, immediate: bool = False):
    bisa_config.ensure_runtime_directories()
    con = sqlite3.connect(bisa_config.DB_PATH, timeout=15, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def _token_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def refresh_cookie_name(role: str) -> str:
    if role in MERCHANT_ROLES:
        return "bisa_merchant_refresh"
    name = REFRESH_COOKIE_NAMES.get(str(role or ""))
    if not name:
        raise SecurityError("invalid_role", 422)
    return name


def refresh_cookie_header(
    role: str,
    refresh_token: str,
    *,
    secure: bool | None = None,
    max_age_seconds: int | None = None,
) -> str:
    """Build a role-scoped refresh cookie without exposing it to JavaScript."""
    if not isinstance(refresh_token, str) or not 30 <= len(refresh_token) <= 512:
        raise SecurityError("refresh_session_required", 401)
    name = refresh_cookie_name(role)
    use_secure = bisa_config.ENVIRONMENT == "production" if secure is None else bool(secure)
    if max_age_seconds is None:
        max_age_seconds = _bounded_env("BISA_REFRESH_TOKEN_DAYS", 30, 1, 90) * 86_400
    max_age = max(60, min(90 * 86_400, int(max_age_seconds)))
    cookie = SimpleCookie()
    cookie[name] = refresh_token
    morsel = cookie[name]
    morsel["path"] = "/api/auth"
    morsel["httponly"] = True
    morsel["samesite"] = "Strict"
    morsel["max-age"] = str(max_age)
    if use_secure:
        morsel["secure"] = True
    return cookie.output(header="").strip()


def clear_refresh_cookie_header(role: str, *, secure: bool | None = None) -> str:
    """Expire only the selected role cookie; other account contexts survive."""
    name = refresh_cookie_name(role)
    use_secure = bisa_config.ENVIRONMENT == "production" if secure is None else bool(secure)
    cookie = SimpleCookie()
    cookie[name] = ""
    morsel = cookie[name]
    morsel["path"] = "/api/auth"
    morsel["httponly"] = True
    morsel["samesite"] = "Strict"
    morsel["max-age"] = "0"
    morsel["expires"] = "Thu, 01 Jan 1970 00:00:00 GMT"
    if use_secure:
        morsel["secure"] = True
    return cookie.output(header="").strip()


def refresh_token_from_cookie(cookie_header: str, role: str) -> str:
    """Read exactly one role cookie from a bounded HTTP Cookie header."""
    if not isinstance(cookie_header, str) or not cookie_header or len(cookie_header) > 8192:
        raise SecurityError("refresh_session_required", 401)
    cookie = SimpleCookie()
    try:
        cookie.load(cookie_header)
    except CookieError as exc:
        raise SecurityError("refresh_session_required", 401) from exc
    morsel = cookie.get(refresh_cookie_name(role))
    value = morsel.value if morsel else ""
    if not 30 <= len(value) <= 512:
        raise SecurityError("refresh_session_required", 401)
    return value


def session_http_exchange(
    session: dict[str, Any], *, secure: bool | None = None,
) -> tuple[dict[str, Any], str]:
    """Split an issued session into a JSON-safe body and a Set-Cookie value."""
    if not isinstance(session, dict) or not isinstance(session.get("account"), dict):
        raise SecurityError("invalid_session_exchange", 500)
    refresh_token = str(session.get("refreshToken") or "")
    role = str(session["account"].get("role") or "")
    payload = {key: value for key, value in session.items() if key != "refreshToken"}
    return payload, refresh_cookie_header(role, refresh_token, secure=secure)


def security_production_readiness(
    environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Return secret-free production readiness checks for the HTTP bootstrap."""
    values = environment if environment is not None else os.environ
    target = str(values.get("BISA_ENV", bisa_config.ENVIRONMENT) or "development").strip().lower()
    pepper = str(values.get("BISA_AUTH_PEPPER", "") or "")
    signing_key = str(values.get("BISA_MEDIA_SIGNING_KEY", "") or "")
    errors: list[str] = []
    if target == "production":
        if len(pepper) < 32:
            errors.append("BISA_AUTH_PEPPER must contain at least 32 characters")
        if len(signing_key) < 32:
            errors.append("BISA_MEDIA_SIGNING_KEY must contain at least 32 characters")
        if len(pepper) >= 32 and len(signing_key) >= 32 and hmac.compare_digest(pepper, signing_key):
            errors.append("BISA authentication and media signing keys must be different")
    return {
        "ready": not errors,
        "errors": errors,
        "checks": {
            "environment": target,
            "authPepperConfigured": len(pepper) >= 32,
            "mediaSigningConfigured": len(signing_key) >= 32,
            "keysSeparated": bool(pepper and signing_key and pepper != signing_key),
        },
    }


def _identifier(value: Any, *, required: bool = True) -> str:
    result = str(value or "").strip()
    if (required and not result) or (result and not IDENTIFIER.fullmatch(result)):
        raise SecurityError("invalid_identifier", 422)
    return result


def _audit(
    con: sqlite3.Connection,
    event_kind: str,
    *,
    actor_id: str = "",
    subject_kind: str = "",
    subject_id: str = "",
    context: dict[str, Any] | None = None,
) -> None:
    safe_context = json.dumps(context or {}, ensure_ascii=False, separators=(",", ":"))[:2000]
    con.execute(
        """INSERT INTO security_audit_events(
        id,event_kind,actor_id,subject_kind,subject_id,context_json,created_at)
        VALUES(?,?,?,?,?,?,?)""",
        (
            f"secaudit_{uuid.uuid4().hex}", str(event_kind)[:80], str(actor_id)[:180],
            str(subject_kind)[:40], str(subject_id)[:180], safe_context, iso(),
        ),
    )


def _auth_pepper() -> bytes:
    value = os.environ.get("BISA_AUTH_PEPPER", "")
    if len(value) >= 32:
        return value.encode("utf-8")
    if bisa_config.ENVIRONMENT == "production":
        raise SecurityError("auth_pepper_unavailable", 503)
    return b"bisa-development-login-bucket-pepper"


def _scope_hash(scope_kind: str, value: Any) -> str:
    normalized = "".join(ch for ch in str(value or "").strip().lower() if ch.isalnum() or ch in ".:_-")[:240]
    if not normalized:
        normalized = "unknown"
    return hmac.new(_auth_pepper(), f"{scope_kind}:{normalized}".encode(), hashlib.sha256).hexdigest()


def _login_scopes(subject: Any, source_id: Any = "") -> list[tuple[str, str]]:
    scopes = [("account", _scope_hash("account", subject))]
    if str(source_id or "").strip():
        scopes.append(("source", _scope_hash("source", source_id)))
    return scopes


def ensure_login_allowed(subject: Any, *, source_id: Any = "", now: datetime | None = None) -> None:
    current = as_utc(now)
    with security_connection() as con:
        for scope_kind, scope_hash in _login_scopes(subject, source_id):
            row = con.execute(
                "SELECT locked_until FROM auth_login_buckets WHERE scope_kind=? AND scope_hash=?",
                (scope_kind, scope_hash),
            ).fetchone()
            locked_until = parse_time(row["locked_until"]) if row else None
            if locked_until and locked_until > current:
                retry_after = max(1, int((locked_until - current).total_seconds()))
                raise SecurityError("login_temporarily_locked", 429, {"retryAfter": retry_after})


def record_login_failure(subject: Any, *, source_id: Any = "", now: datetime | None = None) -> dict[str, Any]:
    current = as_utc(now)
    window_seconds = _bounded_env("BISA_LOGIN_WINDOW_SECONDS", 900, 60, 86_400)
    lock_seconds = _bounded_env("BISA_LOGIN_LOCK_SECONDS", 900, 60, 86_400)
    maximum = _bounded_env("BISA_LOGIN_MAX_ATTEMPTS", 5, 2, 20)
    source_maximum = _bounded_env("BISA_LOGIN_SOURCE_MAX_ATTEMPTS", 50, maximum, 500)
    highest_retry = 0
    with security_connection(immediate=True) as con:
        for scope_kind, scope_hash in _login_scopes(subject, source_id):
            row = con.execute(
                "SELECT * FROM auth_login_buckets WHERE scope_kind=? AND scope_hash=?",
                (scope_kind, scope_hash),
            ).fetchone()
            started = parse_time(row["window_started_at"]) if row else None
            if not row or not started or current - started >= timedelta(seconds=window_seconds):
                attempts = 1
                started = current
            else:
                attempts = int(row["failed_attempts"] or 0) + 1
            threshold = maximum if scope_kind == "account" else source_maximum
            locked_until = current + timedelta(seconds=lock_seconds) if attempts >= threshold else None
            con.execute(
                """INSERT INTO auth_login_buckets(
                scope_kind,scope_hash,failed_attempts,window_started_at,last_failed_at,locked_until,updated_at)
                VALUES(?,?,?,?,?,?,?) ON CONFLICT(scope_kind,scope_hash) DO UPDATE SET
                failed_attempts=excluded.failed_attempts,window_started_at=excluded.window_started_at,
                last_failed_at=excluded.last_failed_at,locked_until=excluded.locked_until,updated_at=excluded.updated_at""",
                (scope_kind, scope_hash, attempts, iso(started), iso(current), iso(locked_until) if locked_until else "", iso(current)),
            )
            if locked_until:
                highest_retry = max(highest_retry, lock_seconds)
        _audit(con, "login_failed", subject_kind="login_bucket", context={"locked": bool(highest_retry)})
    return {"locked": bool(highest_retry), "retryAfter": highest_retry}


def clear_login_failures(subject: Any, *, source_id: Any = "", clear_source: bool = False) -> None:
    scopes = _login_scopes(subject, source_id)
    if not clear_source:
        scopes = scopes[:1]
    with security_connection(immediate=True) as con:
        for scope_kind, scope_hash in scopes:
            con.execute(
                "DELETE FROM auth_login_buckets WHERE scope_kind=? AND scope_hash=?",
                (scope_kind, scope_hash),
            )


def guarded_credentials(
    subject: Any,
    verifier: Callable[[], dict[str, Any] | None],
    *,
    source_id: Any = "",
) -> dict[str, Any]:
    """Run a credential verifier behind persisted lockout without storing the subject."""
    ensure_login_allowed(subject, source_id=source_id)
    try:
        actor = verifier()
    except Exception as exc:
        if getattr(exc, "code", "") in {"invalid_login", "invalid_pin", "authentication_failed"}:
            record_login_failure(subject, source_id=source_id)
        raise
    if not actor:
        record_login_failure(subject, source_id=source_id)
        raise SecurityError("invalid_login", 403)
    clear_login_failures(subject)
    return actor


def _authorized_actor(
    con: sqlite3.Connection,
    account_id: str,
    role: str,
    merchant_id: str,
) -> dict[str, Any] | None:
    row = con.execute(
        """SELECT a.id,a.name,a.status,r.role,r.merchant_id
        FROM accounts a JOIN account_roles r ON r.account_id=a.id
        WHERE a.id=? AND a.status='active' AND r.role=? AND r.merchant_id=? AND r.active=1""",
        (account_id, role, merchant_id),
    ).fetchone()
    if not row:
        return None
    if role in MERCHANT_ROLES:
        merchant = con.execute(
            "SELECT id,owner_account_id FROM merchants WHERE id=? AND status='approved' AND active=1",
            (merchant_id,),
        ).fetchone()
        if not merchant:
            return None
        if role == "merchant_owner":
            if merchant["owner_account_id"] != account_id:
                return None
        else:
            membership = con.execute(
                """SELECT 1 FROM merchant_members
                WHERE merchant_id=? AND account_id=? AND role=? AND status='active'""",
                (merchant_id, account_id, role),
            ).fetchone()
            if not membership:
                return None
    actor = {
        "accountId": row["id"], "name": row["name"], "role": row["role"],
        "merchantId": row["merchant_id"],
    }
    if role == "supplier_advertiser":
        supplier = con.execute(
            """SELECT s.id FROM suppliers s JOIN supplier_members sm ON sm.supplier_id=s.id
            WHERE s.id=? AND s.status='approved' AND sm.account_id=?
              AND sm.role='supplier_advertiser' AND sm.status='active'""",
            (merchant_id, account_id),
        ).fetchone()
        if not supplier:
            return None
        actor["supplierId"] = merchant_id
        actor["merchantId"] = ""
    return actor


def _issue_session(
    con: sqlite3.Connection,
    account_id: str,
    role: str,
    merchant_id: str,
    device_id: str,
    *,
    family_id: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    current = as_utc(now)
    actor = _authorized_actor(con, account_id, role, merchant_id)
    if not actor:
        raise SecurityError("session_role_unavailable", 403)
    access_minutes = _bounded_env("BISA_ACCESS_TOKEN_MINUTES", 20, 5, 120)
    refresh_days = _bounded_env("BISA_REFRESH_TOKEN_DAYS", 30, 1, 90)
    access_token = secrets.token_urlsafe(32)
    refresh_token = secrets.token_urlsafe(48)
    session_id = f"session_{uuid.uuid4().hex}"
    family_id = family_id or session_id
    access_expiry = current + timedelta(minutes=access_minutes)
    refresh_expiry = current + timedelta(days=refresh_days)
    con.execute(
        """INSERT INTO sessions(
        token_hash,account_id,active_role,merchant_id,expires_at,revoked_at,created_at,
        session_id,session_family_id,refresh_hash,access_expires_at,refresh_expires_at,
        device_id,last_used_at,rotated_at,replaced_by,revoked_reason)
        VALUES(?,?,?,?,?,'',?,?,?,?,?,?,?,?,'','','')""",
        (
            _token_hash(access_token), account_id, role, merchant_id, iso(access_expiry), iso(current),
            session_id, family_id, _token_hash(refresh_token), iso(access_expiry), iso(refresh_expiry),
            str(device_id or "")[:120], iso(current),
        ),
    )
    _audit(con, "session_issued", actor_id=account_id, subject_kind="session", subject_id=session_id, context={"role": role})
    return {
        "token": access_token, "refreshToken": refresh_token, "sessionId": session_id,
        "accessExpiresAt": iso(access_expiry), "refreshExpiresAt": iso(refresh_expiry),
        "account": actor,
    }


def issue_session(account_id: str, role: str, merchant_id: str = "", device_id: str = "") -> dict[str, Any]:
    with security_connection(immediate=True) as con:
        return _issue_session(
            con, _identifier(account_id), _identifier(role),
            _identifier(merchant_id, required=False), str(device_id or "")[:120],
        )


def authenticate_access(access_token: str, *, touch: bool = True, now: datetime | None = None) -> dict[str, Any] | None:
    if not isinstance(access_token, str) or not 20 <= len(access_token) <= 512:
        return None
    current = as_utc(now)
    with security_connection(immediate=touch) as con:
        row = con.execute(
            "SELECT * FROM sessions WHERE token_hash=? AND revoked_at=''",
            (_token_hash(access_token),),
        ).fetchone()
        if not row:
            return None
        access_expiry = parse_time(row["access_expires_at"] or row["expires_at"])
        refresh_expiry = parse_time(row["refresh_expires_at"] or row["expires_at"])
        if not access_expiry or access_expiry <= current or not refresh_expiry or refresh_expiry <= current:
            if touch:
                con.execute(
                    "UPDATE sessions SET revoked_at=?,revoked_reason='expired' WHERE token_hash=? AND revoked_at=''",
                    (iso(current), row["token_hash"]),
                )
            return None
        actor = _authorized_actor(con, row["account_id"], row["active_role"], row["merchant_id"])
        if not actor:
            if touch:
                con.execute(
                    "UPDATE sessions SET revoked_at=?,revoked_reason='role_or_account_disabled' WHERE token_hash=? AND revoked_at=''",
                    (iso(current), row["token_hash"]),
                )
            return None
        if touch:
            con.execute("UPDATE sessions SET last_used_at=? WHERE token_hash=?", (iso(current), row["token_hash"]))
        actor.update({
            "sessionId": row["session_id"], "deviceId": row["device_id"],
            "accessExpiresAt": iso(access_expiry),
        })
        return actor


def rotate_refresh_token(refresh_token: str, *, device_id: str = "", now: datetime | None = None) -> dict[str, Any]:
    if not isinstance(refresh_token, str) or not 30 <= len(refresh_token) <= 512:
        raise SecurityError("refresh_session_required", 401)
    current = as_utc(now)
    refresh_hash = _token_hash(refresh_token)
    pending_error: SecurityError | None = None
    replacement: dict[str, Any] | None = None
    with security_connection(immediate=True) as con:
        row = con.execute("SELECT * FROM sessions WHERE refresh_hash=?", (refresh_hash,)).fetchone()
        if not row:
            raise SecurityError("refresh_session_invalid", 401)
        if row["revoked_at"]:
            if row["session_family_id"]:
                con.execute(
                    """UPDATE sessions SET revoked_at=?,revoked_reason='refresh_reuse'
                    WHERE session_family_id=? AND revoked_at=''""",
                    (iso(current), row["session_family_id"]),
                )
            _audit(con, "refresh_reuse_detected", actor_id=row["account_id"], subject_kind="session", subject_id=row["session_id"])
            pending_error = SecurityError("refresh_token_reused", 401)
        refresh_expiry = parse_time(row["refresh_expires_at"] or row["expires_at"])
        if pending_error is None and (not refresh_expiry or refresh_expiry <= current):
            con.execute(
                "UPDATE sessions SET revoked_at=?,revoked_reason='refresh_expired' WHERE refresh_hash=?",
                (iso(current), refresh_hash),
            )
            pending_error = SecurityError("refresh_session_expired", 401)
        stored_device = str(row["device_id"] or "")
        if pending_error is None and stored_device and stored_device != str(device_id or "")[:120]:
            _audit(con, "refresh_device_mismatch", actor_id=row["account_id"], subject_kind="session", subject_id=row["session_id"])
            pending_error = SecurityError("refresh_device_mismatch", 403)
        if pending_error is None and not _authorized_actor(con, row["account_id"], row["active_role"], row["merchant_id"]):
            con.execute(
                "UPDATE sessions SET revoked_at=?,revoked_reason='role_or_account_disabled' WHERE refresh_hash=?",
                (iso(current), refresh_hash),
            )
            pending_error = SecurityError("session_role_unavailable", 403)
        if pending_error is None:
            replacement = _issue_session(
                con, row["account_id"], row["active_role"], row["merchant_id"], stored_device,
                family_id=row["session_family_id"] or row["session_id"], now=current,
            )
            con.execute(
                """UPDATE sessions SET revoked_at=?,revoked_reason='rotated',rotated_at=?,replaced_by=?
                WHERE refresh_hash=? AND revoked_at=''""",
                (iso(current), iso(current), replacement["sessionId"], refresh_hash),
            )
    if pending_error is not None:
        raise pending_error
    if replacement is None:
        raise SecurityError("refresh_session_invalid", 401)
    return replacement


def logout_session(access_token: str, *, reason: str = "logout") -> bool:
    if not isinstance(access_token, str) or not access_token:
        return False
    with security_connection(immediate=True) as con:
        row = con.execute(
            "SELECT account_id,session_id FROM sessions WHERE token_hash=? AND revoked_at=''",
            (_token_hash(access_token),),
        ).fetchone()
        if not row:
            return False
        changed = con.execute(
            "UPDATE sessions SET revoked_at=?,revoked_reason=? WHERE token_hash=? AND revoked_at=''",
            (iso(), str(reason or "logout")[:80], _token_hash(access_token)),
        ).rowcount
        if changed:
            _audit(con, "session_revoked", actor_id=row["account_id"], subject_kind="session", subject_id=row["session_id"])
        return bool(changed)


def revoke_account_sessions(
    account_id: str,
    *,
    role: str = "",
    merchant_id: str = "",
    device_id: str = "",
    reason: str = "administrative_revoke",
) -> int:
    clauses = ["account_id=?", "revoked_at=''"]
    params: list[Any] = [_identifier(account_id)]
    for column, value in (("active_role", role), ("merchant_id", merchant_id), ("device_id", device_id)):
        if value:
            clauses.append(f"{column}=?")
            params.append(str(value)[:180])
    with security_connection(immediate=True) as con:
        changed = con.execute(
            f"UPDATE sessions SET revoked_at=?,revoked_reason=? WHERE {' AND '.join(clauses)}",
            [iso(), str(reason)[:80], *params],
        ).rowcount
        _audit(con, "account_sessions_revoked", actor_id=account_id, subject_kind="account", subject_id=account_id, context={"count": changed})
        return int(changed or 0)


def list_account_sessions(actor: dict[str, Any]) -> list[dict[str, Any]]:
    account_id = _identifier(actor.get("accountId"))
    with security_connection() as con:
        rows = con.execute(
            """SELECT session_id,active_role,merchant_id,device_id,access_expires_at,
            refresh_expires_at,last_used_at,created_at,revoked_at,revoked_reason
            FROM sessions WHERE account_id=? ORDER BY created_at DESC LIMIT 100""",
            (account_id,),
        ).fetchall()
        return [dict(row) for row in rows]


def resolve_permissions(actor: dict[str, Any], *, con: sqlite3.Connection | None = None) -> set[str]:
    if not actor:
        return set()
    account_id = _identifier(actor.get("accountId"))
    role = _identifier(actor.get("role"))

    def load(connection: sqlite3.Connection) -> set[str]:
        permissions = {
            row["permission"] for row in connection.execute(
                "SELECT permission FROM role_permissions WHERE role=?", (role,)
            )
        }
        for row in connection.execute(
            "SELECT permission,allowed FROM account_permission_overrides WHERE account_id=?",
            (account_id,),
        ):
            if row["allowed"]:
                permissions.add(row["permission"])
            else:
                permissions.discard(row["permission"])
        return permissions

    if con is not None:
        return load(con)
    with security_connection() as owned:
        return load(owned)


def has_permission(actor: dict[str, Any], permission: str, *, con: sqlite3.Connection | None = None) -> bool:
    permissions = resolve_permissions(actor, con=con)
    return "*" in permissions or permission in permissions


def require_permission(
    actor: dict[str, Any],
    permission: str,
    *,
    merchant_id: str = "",
    con: sqlite3.Connection | None = None,
) -> None:
    if not has_permission(actor, permission, con=con):
        raise SecurityError("forbidden", 403)
    if merchant_id and actor.get("merchantId") != merchant_id:
        if not (has_permission(actor, "merchant.manage", con=con) or has_permission(actor, "*", con=con)):
            raise SecurityError("forbidden", 403)


def _safe_storage_key(value: Any) -> str:
    raw = str(value or "").replace("\\", "/").strip("/")
    path = PurePosixPath(raw)
    if not raw or path.is_absolute() or ".." in path.parts or path.parts[0] != "private":
        raise SecurityError("private_storage_key_required", 422)
    if any(not re.fullmatch(r"[A-Za-z0-9._-]{1,180}", part) for part in path.parts):
        raise SecurityError("invalid_private_storage_key", 422)
    return path.as_posix()


def _storage_candidate(storage_key: str) -> Path:
    root = Path(bisa_config.UPLOAD_DIR).resolve()
    candidate = (root / _safe_storage_key(storage_key)).resolve()
    if root not in candidate.parents:
        raise SecurityError("private_media_path_invalid", 500)
    return candidate


def validate_private_blob(mime_type: str, blob: bytes) -> None:
    maximum = _bounded_env("BISA_PRIVATE_MEDIA_MAX_BYTES", 10 * 1024 * 1024, 1024, 25 * 1024 * 1024)
    if mime_type not in PRIVATE_MIME_TYPES or not blob or len(blob) > maximum:
        raise SecurityError("private_media_invalid", 422)
    signatures = {
        "image/jpeg": blob.startswith(b"\xff\xd8\xff"),
        "image/png": blob.startswith(b"\x89PNG\r\n\x1a\n"),
        "image/webp": len(blob) >= 12 and blob[:4] == b"RIFF" and blob[8:12] == b"WEBP",
        "application/pdf": blob.startswith(b"%PDF-"),
    }
    if not signatures.get(mime_type, False):
        raise SecurityError("private_media_signature_mismatch", 422)


def _owner_access(con: sqlite3.Connection, actor: dict[str, Any], owner_kind: str, owner_id: str) -> bool:
    if owner_kind == "account":
        return actor.get("accountId") == owner_id
    if owner_kind == "merchant":
        return actor.get("merchantId") == owner_id and actor.get("role") in {"merchant_owner", "merchant_manager"}
    if owner_kind == "merchant_application":
        row = con.execute(
            """SELECT m.owner_account_id,m.id merchant_id FROM merchant_applications a
            JOIN merchants m ON m.id=a.merchant_id WHERE a.id=?""",
            (owner_id,),
        ).fetchone()
        return bool(row and (
            actor.get("accountId") == row["owner_account_id"]
            or (
                actor.get("merchantId") == row["merchant_id"]
                and actor.get("role") in {"merchant_owner", "merchant_manager"}
            )
        ))
    if owner_kind == "supplier":
        row = con.execute(
            """SELECT 1 FROM suppliers s JOIN supplier_members sm ON sm.supplier_id=s.id
            WHERE s.id=? AND s.status='approved' AND sm.account_id=?
              AND sm.role='supplier_advertiser' AND sm.status='active'""",
            (owner_id, actor.get("accountId", "")),
        ).fetchone()
        return bool(row and actor.get("role") == "supplier_advertiser")
    if owner_kind == "support_case":
        row = con.execute(
            "SELECT opened_by,assigned_to FROM support_cases WHERE id=?", (owner_id,)
        ).fetchone()
        return bool(row and actor.get("accountId") in {row["opened_by"], row["assigned_to"]})
    return False


def _media_access(con: sqlite3.Connection, actor: dict[str, Any], row: sqlite3.Row, *, now: datetime | None = None) -> bool:
    if not actor or row["status"] != "active":
        return False
    if _owner_access(con, actor, row["owner_kind"], row["owner_id"]):
        return True
    if has_permission(actor, "private_media.read", con=con):
        return True
    grantees = [("account", actor.get("accountId", ""))]
    if actor.get("merchantId"):
        grantees.append(("merchant", actor["merchantId"]))
    if actor.get("supplierId"):
        grantees.append(("supplier", actor["supplierId"]))
    current = as_utc(now)
    for kind, grantee_id in grantees:
        grant = con.execute(
            """SELECT expires_at FROM private_media_access_grants
            WHERE media_id=? AND grantee_kind=? AND grantee_id=? AND permission='read'""",
            (row["id"], kind, grantee_id),
        ).fetchone()
        expiry = parse_time(grant["expires_at"]) if grant else None
        if grant and (not grant["expires_at"] or (expiry and expiry > current)):
            return True
    return False


def _can_manage_foreign_media(con: sqlite3.Connection, actor: dict[str, Any]) -> bool:
    """Only an explicitly permitted administrative role may cross owner scope."""
    return bool(
        actor
        and actor.get("role") in ADMIN_ROLES
        and has_permission(actor, "private_media.manage", con=con)
    )


def register_private_media(
    actor: dict[str, Any],
    *,
    owner_kind: str,
    owner_id: str,
    purpose: str,
    storage_key: str,
    mime_type: str,
    byte_size: int,
    sha256_hex: str,
    original_name: str = "",
) -> dict[str, Any]:
    owner_kind = str(owner_kind or "")
    if owner_kind not in PRIVATE_OWNER_KINDS:
        raise SecurityError("invalid_private_media_owner", 422)
    owner_id = _identifier(owner_id)
    storage_key = _safe_storage_key(storage_key)
    if mime_type not in PRIVATE_MIME_TYPES or not 0 <= int(byte_size) <= _bounded_env("BISA_PRIVATE_MEDIA_MAX_BYTES", 10 * 1024 * 1024, 1024, 25 * 1024 * 1024):
        raise SecurityError("private_media_invalid", 422)
    sha256_hex = str(sha256_hex or "").lower()
    if not SHA256_HEX.fullmatch(sha256_hex):
        raise SecurityError("private_media_hash_required", 422)
    candidate = _storage_candidate(storage_key)
    if not candidate.is_file() or candidate.is_symlink():
        raise SecurityError("private_media_file_required", 422)
    if candidate.stat().st_size != int(byte_size):
        raise SecurityError("private_media_size_mismatch", 422)
    digest = hashlib.sha256()
    with candidate.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    if not hmac.compare_digest(digest.hexdigest(), sha256_hex):
        raise SecurityError("private_media_hash_mismatch", 422)
    validate_private_blob(mime_type, candidate.read_bytes())
    with security_connection(immediate=True) as con:
        if not _owner_access(con, actor, owner_kind, owner_id) and not _can_manage_foreign_media(con, actor):
            raise SecurityError("forbidden", 403)
        media_id = f"media_{uuid.uuid4().hex}"
        stamp = iso()
        con.execute(
            """INSERT INTO private_media_objects(
            id,owner_kind,owner_id,purpose,storage_key,mime_type,byte_size,sha256_hex,
            original_name,status,created_by,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,'active',?,?,?)""",
            (
                media_id, owner_kind, owner_id, str(purpose or "document")[:80], storage_key,
                mime_type, int(byte_size), sha256_hex, str(original_name or "")[:180],
                actor.get("accountId", ""), stamp, stamp,
            ),
        )
        _audit(con, "private_media_registered", actor_id=actor.get("accountId", ""), subject_kind="private_media", subject_id=media_id, context={"purpose": str(purpose)[:80]})
        return _public_media(con.execute("SELECT * FROM private_media_objects WHERE id=?", (media_id,)).fetchone())


def _public_media(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"], "ownerKind": row["owner_kind"], "ownerId": row["owner_id"],
        "purpose": row["purpose"], "mimeType": row["mime_type"], "byteSize": row["byte_size"],
        "originalName": row["original_name"], "status": row["status"], "createdAt": row["created_at"],
    }


def private_media_metadata(actor: dict[str, Any], media_id: str) -> dict[str, Any]:
    media_id = _identifier(media_id)
    with security_connection() as con:
        row = con.execute("SELECT * FROM private_media_objects WHERE id=?", (media_id,)).fetchone()
        if not row or not _media_access(con, actor, row):
            raise SecurityError("private_media_not_found", 404)
        return _public_media(row)


def grant_private_media(
    actor: dict[str, Any],
    media_id: str,
    *,
    grantee_kind: str,
    grantee_id: str,
    expires_at: str = "",
) -> None:
    if grantee_kind not in {"account", "merchant", "supplier"}:
        raise SecurityError("invalid_media_grantee", 422)
    with security_connection(immediate=True) as con:
        row = con.execute("SELECT * FROM private_media_objects WHERE id=?", (_identifier(media_id),)).fetchone()
        if not row:
            raise SecurityError("private_media_not_found", 404)
        if not _owner_access(con, actor, row["owner_kind"], row["owner_id"]):
            if not _can_manage_foreign_media(con, actor):
                raise SecurityError("private_media_not_found", 404)
        parsed_expiry = parse_time(expires_at) if expires_at else None
        if expires_at and (not parsed_expiry or parsed_expiry <= utcnow()):
            raise SecurityError("invalid_media_grant_expiry", 422)
        con.execute(
            """INSERT INTO private_media_access_grants(
            media_id,grantee_kind,grantee_id,permission,expires_at,granted_by,created_at)
            VALUES(?,?,?,'read',?,?,?) ON CONFLICT(media_id,grantee_kind,grantee_id,permission)
            DO UPDATE SET expires_at=excluded.expires_at,granted_by=excluded.granted_by,created_at=excluded.created_at""",
            (row["id"], grantee_kind, _identifier(grantee_id), iso(parsed_expiry) if parsed_expiry else "", actor.get("accountId", ""), iso()),
        )


def _media_signing_key() -> bytes:
    value = os.environ.get("BISA_MEDIA_SIGNING_KEY", "")
    if len(value) < 32:
        raise SecurityError("media_signing_unavailable", 503)
    return value.encode("utf-8")


def _media_signature(media_id: str, actor: dict[str, Any], expires: int) -> str:
    payload = "|".join((
        media_id, str(actor.get("accountId", "")), str(actor.get("role", "")),
        str(actor.get("merchantId", "")), str(actor.get("supplierId", "")), str(expires),
    ))
    return hmac.new(_media_signing_key(), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def signed_private_media_route(actor: dict[str, Any], media_id: str, *, ttl_seconds: int = 300) -> dict[str, Any]:
    metadata = private_media_metadata(actor, media_id)
    ttl = max(30, min(_bounded_env("BISA_MEDIA_URL_MAX_TTL_SECONDS", 900, 30, 3600), int(ttl_seconds)))
    expires = int(utcnow().timestamp()) + ttl
    signature = _media_signature(metadata["id"], actor, expires)
    route = f"/api/private-media/{quote(metadata['id'])}?exp={expires}&sig={signature}"
    return {"id": metadata["id"], "route": route, "expiresAt": iso(datetime.fromtimestamp(expires, UTC))}


def verify_private_media_signature(
    actor: dict[str, Any], media_id: str, expires: Any, signature: str,
) -> dict[str, Any]:
    try:
        expires_int = int(expires)
    except (TypeError, ValueError) as exc:
        raise SecurityError("private_media_link_invalid", 403) from exc
    now_epoch = int(utcnow().timestamp())
    maximum = _bounded_env("BISA_MEDIA_URL_MAX_TTL_SECONDS", 900, 30, 3600)
    if expires_int < now_epoch or expires_int > now_epoch + maximum:
        raise SecurityError("private_media_link_expired", 403)
    expected = _media_signature(_identifier(media_id), actor, expires_int)
    if not hmac.compare_digest(expected, str(signature or "")):
        raise SecurityError("private_media_link_invalid", 403)
    return private_media_metadata(actor, media_id)


def resolve_private_media_path(
    actor: dict[str, Any], media_id: str, expires: Any, signature: str,
) -> Path:
    verify_private_media_signature(actor, media_id, expires, signature)
    with security_connection() as con:
        row = con.execute("SELECT * FROM private_media_objects WHERE id=?", (media_id,)).fetchone()
        # Recheck authorization and status in the same read that resolves the
        # opaque storage key; a grant can be revoked after a link was signed.
        if not row or not _media_access(con, actor, row):
            raise SecurityError("private_media_not_found", 404)
        candidate = _storage_candidate(row["storage_key"])
        if not candidate.is_file() or candidate.is_symlink():
            raise SecurityError("private_media_not_found", 404)
        return candidate
