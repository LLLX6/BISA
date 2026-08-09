"""Merchant approval assets and post-onboarding branch launch lifecycle.

This module is deliberately additive.  It uses the existing opaque private
media registry, branch table, notifications, and append-only admin audit log;
it does not add a second source of business truth.  Compose the mixin before
``BisaApplication`` so the application-approval hook can publish reviewed
brand media after the existing approval transaction succeeds.

Public brand URLs contain only a high-entropy media identifier.  Every read
rechecks merchant and branch publication state.  Storage keys are resolved
only by :meth:`resolve_merchant_brand_asset_path` and must never be serialized
into an HTTP response.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote

import bisa_config
from bisa_config import coordinates_in_muscat
from bisa_domain import (
    DomainError,
    clean_text,
    connect,
    dumps,
    loads,
    new_id,
    now_iso,
)
from bisa_marketplace import ADMIN_ROLES, MERCHANT_ROLES, _bounded_int


BRAND_ASSET_KINDS = {"logo", "cover"}
BRANCH_DOCUMENT_KINDS = {"storefront", "branch_license", "other"}
BRANCH_REVIEW_STATES = {
    "draft", "pending_review", "changes_requested", "approved", "rejected", "paused",
}
BRANCH_MANAGER_ROLES = {"merchant_owner", "merchant_manager"}
IMAGE_MIME_TYPES = {"image/jpeg", "image/png", "image/webp"}
WEEK_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
OPAQUE_MEDIA_ID = re.compile(r"media_[A-Za-z0-9._:-]{8,170}\Z")


MERCHANT_LAUNCH_ROUTE_CONTRACTS = {
    "POST /api/merchant/branches/{branchId}/submit": {
        "method": "submit_branch_for_review",
        "request": ["documents", "documentExceptionReason", "hours"],
        "response": ["id", "status", "publicVisible", "revision"],
    },
    "PATCH /api/merchant/branches/{branchId}/hours": {
        "method": "update_branch_hours",
        "request": ["hours"],
        "response": ["id", "status", "publicVisible", "hours"],
    },
    "GET /api/merchant/branches/{branchId}/launch": {
        "method": "branch_launch_detail",
        "response": ["branch", "submission", "documents"],
    },
    "GET /api/admin/branches/{branchId}/launch": {
        "method": "branch_launch_detail",
        "response": ["branch", "submission", "documents"],
    },
    "POST /api/admin/branches/{branchId}/decision": {
        "method": "admin_branch_decision",
        "request": ["decision", "note"],
        "response": ["id", "status", "publicVisible", "duplicate"],
    },
    "GET /api/merchants/{merchantId}/assets/{kind}": {
        "method": "merchant_brand_asset_descriptor",
        "response": ["id", "merchantId", "kind", "url", "mimeType", "byteSize"],
    },
    "GET /api/merchant-assets/{assetId}": {
        "method": "resolve_merchant_brand_asset",
        "response": "stream-only; never serialize the returned filesystem path",
        "headers": {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    },
}

MERCHANT_LAUNCH_COMPOSITION = (
    "class BisaApplication(MerchantLaunchMixin, SupplierAdvertiserMixin, "
    "OperationsMixin, MarketplaceMixin, BisaService): ..."
)


@dataclass(frozen=True)
class ResolvedMerchantBrandAsset:
    """Internal-only stream contract; this object is never JSON serialized."""

    asset_id: str
    path: Path
    mime_type: str
    byte_size: int


def _time_minutes(value: Any) -> int:
    text = str(value or "")
    parts = text.split(":")
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        raise DomainError("invalid_opening_time", 422)
    hour, minute = map(int, parts)
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise DomainError("invalid_opening_time", 422)
    return hour * 60 + minute


def _opening_hours(value: Any) -> dict[str, Any]:
    """Normalize the only hours shape accepted by launch and update routes."""
    if not isinstance(value, dict) or not value:
        raise DomainError("opening_hours_required", 422)
    normalized: dict[str, Any] = {}
    has_open_day = False
    for day in WEEK_DAYS:
        raw = value.get(day, [])
        if raw == "24h":
            normalized[day] = "24h"
            has_open_day = True
            continue
        if raw in (None, "closed"):
            raw = []
        if isinstance(raw, dict):
            raw = [raw]
        if not isinstance(raw, list) or len(raw) > 4:
            raise DomainError("invalid_opening_hours", 422)
        slots = []
        seen = set()
        for item in raw:
            if not isinstance(item, dict):
                raise DomainError("invalid_opening_hours", 422)
            opening = clean_text(item.get("open"), 5, True)
            closing = clean_text(item.get("close"), 5, True)
            start, end = _time_minutes(opening), _time_minutes(closing)
            if start == end or (start, end) in seen:
                raise DomainError("invalid_opening_hours", 422)
            seen.add((start, end))
            slots.append({"open": opening, "close": closing})
        normalized[day] = slots
        has_open_day = has_open_day or bool(slots)
    if not has_open_day:
        raise DomainError("opening_day_required", 422)
    return normalized


def _safe_internal_path(storage_key: str) -> Path:
    """Resolve a DB storage key under uploads without trusting the DB value."""
    raw = str(storage_key or "").replace("\\", "/").strip("/")
    logical = PurePosixPath(raw)
    if (
        not raw
        or logical.is_absolute()
        or ".." in logical.parts
        or not logical.parts
        or logical.parts[0] != "private"
        or any(not re.fullmatch(r"[A-Za-z0-9._-]{1,180}", part) for part in logical.parts)
    ):
        raise DomainError("merchant_asset_not_found", 404)
    root = Path(bisa_config.UPLOAD_DIR).resolve()
    unresolved = root / logical.as_posix()
    # Check every existing component before resolving it.  ``Path.resolve``
    # follows links, so checking only the resolved leaf would miss an in-root
    # symlink even though uploads are required to be ordinary files.
    current = root
    for part in logical.parts:
        current = current / part
        if current.exists() and current.is_symlink():
            raise DomainError("merchant_asset_not_found", 404)
    candidate = unresolved.resolve()
    if root not in candidate.parents or not candidate.is_file():
        raise DomainError("merchant_asset_not_found", 404)
    return candidate


class MerchantLaunchMixin:
    """Add reviewed brand publication and typed branch launch operations."""

    # ---------- approved merchant brand assets ----------

    def admin_application_decision(self, actor, payload: dict) -> dict:
        """Preflight brand ownership, run the existing decision, then bind assets.

        The existing approval transaction remains the authority for plans,
        documents, and merchant status.  Asset binding is idempotent and is
        intentionally performed only after that transaction commits, so a
        failed application approval can never publish an applicant upload.
        """
        decision = clean_text(payload.get("decision"), 30, True)
        application_id = clean_text(payload.get("applicationId"), 90, True)
        if decision == "approve":
            self._preflight_application_brand_assets(actor, application_id)
        result = super().admin_application_decision(actor, payload)
        if result.get("status") == "approved":
            result = dict(result)
            result["brandAssets"] = self.bind_approved_application_brand_assets(
                actor, application_id,
            )
        return result

    def _preflight_application_brand_assets(self, actor, application_id: str) -> None:
        with connect() as con:
            self._require_admin_permission(con, actor, "merchant.review")
            application = con.execute(
                """SELECT a.*,m.id merchant_id FROM merchant_applications a
                   JOIN merchants m ON m.id=a.merchant_id WHERE a.id=?""",
                (application_id,),
            ).fetchone()
            if not application:
                raise DomainError("application_not_found", 404)
            snapshot = loads(application["payload"], {})
            branch_id = clean_text(snapshot.get("branchId"), 90, True)
            branch = con.execute(
                "SELECT * FROM store_branches WHERE id=? AND merchant_id=?",
                (branch_id, application["merchant_id"]),
            ).fetchone()
            if not branch:
                raise DomainError("branch_not_found", 404)
            self._validate_branch_location(con, branch)
            self._application_brand_rows(con, application)

    def _application_brand_rows(self, con, application) -> dict[str, Any]:
        snapshot = loads(application["payload"], {})
        brand = snapshot.get("brandMedia", {}) if isinstance(snapshot, dict) else {}
        if not isinstance(brand, dict):
            raise DomainError("merchant_brand_media_invalid", 409)
        result = {}
        for kind, field in (("logo", "logoMediaId"), ("cover", "coverMediaId")):
            media_id = clean_text(brand.get(field), 180)
            if not media_id:
                continue
            if not OPAQUE_MEDIA_ID.fullmatch(media_id):
                raise DomainError("merchant_brand_media_not_found", 404)
            row = con.execute(
                "SELECT * FROM private_media_objects WHERE id=? AND status='active'",
                (media_id,),
            ).fetchone()
            expected_purpose = f"public_merchant_{kind}"
            is_application_asset = bool(
                row
                and row["owner_kind"] == "merchant_application"
                and row["owner_id"] == application["id"]
            )
            is_bound_asset = bool(
                row
                and row["owner_kind"] == "merchant"
                and row["owner_id"] == application["merchant_id"]
                and row["purpose"] == expected_purpose
            )
            if not row or not (is_application_asset or is_bound_asset):
                raise DomainError("merchant_brand_media_not_found", 404)
            if row["mime_type"] not in IMAGE_MIME_TYPES:
                raise DomainError("merchant_brand_image_required", 422)
            if is_application_asset and con.execute(
                "SELECT 1 FROM merchant_documents WHERE media_id=? LIMIT 1", (media_id,),
            ).fetchone():
                raise DomainError("merchant_brand_media_reused_as_document", 409)
            result[kind] = row
        return result

    def bind_approved_application_brand_assets(self, actor, application_id: str) -> dict:
        application_id = clean_text(application_id, 90, True)
        stamp = now_iso()
        with connect(immediate=True) as con:
            actor = self._require_admin_permission(con, actor, "merchant.review")
            application = con.execute(
                """SELECT a.*,m.logo_path,m.cover_path,m.owner_account_id
                   FROM merchant_applications a JOIN merchants m ON m.id=a.merchant_id
                   WHERE a.id=?""",
                (application_id,),
            ).fetchone()
            if not application:
                raise DomainError("application_not_found", 404)
            if application["status"] != "approved":
                raise DomainError("merchant_application_not_approved", 409)
            assets = self._application_brand_rows(con, application)
            paths = {"logo": "", "cover": ""}
            for kind, row in assets.items():
                media_id = row["id"]
                purpose = f"public_merchant_{kind}"
                con.execute(
                    """UPDATE private_media_objects
                       SET owner_kind='merchant',owner_id=?,purpose=?,updated_at=?
                       WHERE id=? AND status='active'""",
                    (application["merchant_id"], purpose, stamp, media_id),
                )
                # Application-time grants must not survive the ownership
                # transfer. Public access is derived only from merchant and
                # branch publication state on every request.
                con.execute(
                    "DELETE FROM private_media_access_grants WHERE media_id=?", (media_id,),
                )
                paths[kind] = f"media:{media_id}"
            changed = (
                application["logo_path"] != paths["logo"]
                or application["cover_path"] != paths["cover"]
            )
            con.execute(
                """UPDATE merchants SET logo_path=?,cover_path=?,updated_at=? WHERE id=?""",
                (paths["logo"], paths["cover"], stamp, application["merchant_id"]),
            )
            descriptors = {
                kind: self._asset_descriptor(row, application["merchant_id"], kind)
                for kind, row in assets.items()
            }
            if changed:
                con.execute(
                    """INSERT INTO admin_audit_logs(
                        id,actor_id,action,target_kind,target_id,before_json,after_json,reason,created_at)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    (
                        new_id("audit"), actor["accountId"], "merchant_brand_assets_bound",
                        "merchant", application["merchant_id"],
                        dumps({
                            "logoAssetId": self._path_media_id(application["logo_path"]),
                            "coverAssetId": self._path_media_id(application["cover_path"]),
                        }),
                        dumps({
                            "logoAssetId": assets.get("logo")["id"] if assets.get("logo") else "",
                            "coverAssetId": assets.get("cover")["id"] if assets.get("cover") else "",
                        }),
                        "approved_application_brand_publication", stamp,
                    ),
                )
            return descriptors

    @staticmethod
    def _path_media_id(value: Any) -> str:
        text = str(value or "")
        return text[6:] if text.startswith("media:") else ""

    @staticmethod
    def _asset_descriptor(row, merchant_id: str, kind: str) -> dict:
        return {
            "id": row["id"],
            "merchantId": merchant_id,
            "kind": kind,
            "url": f"/api/merchant-assets/{quote(row['id'])}",
            "mimeType": row["mime_type"],
            "byteSize": int(row["byte_size"]),
        }

    def _brand_asset_access(self, con, actor, row) -> tuple[str, str]:
        purpose = str(row["purpose"] or "")
        kind = purpose.removeprefix("public_merchant_")
        if (
            row["owner_kind"] != "merchant"
            or kind not in BRAND_ASSET_KINDS
            or row["status"] != "active"
            or row["mime_type"] not in IMAGE_MIME_TYPES
        ):
            raise DomainError("merchant_asset_not_found", 404)
        merchant_id = row["owner_id"]
        public = con.execute(
            """SELECT 1 FROM merchants m JOIN store_branches b ON b.merchant_id=m.id
               WHERE m.id=? AND m.status='approved' AND m.active=1
                 AND b.status='approved' AND b.active=1 AND b.public_visible=1 LIMIT 1""",
            (merchant_id,),
        ).fetchone()
        if public:
            return merchant_id, kind
        if not actor:
            raise DomainError("merchant_asset_not_found", 404)
        role = str(actor.get("role") or "")
        if role in ADMIN_ROLES:
            self._require_admin_permission(con, actor, "merchant.read")
            return merchant_id, kind
        try:
            normalized = self._require_actor(con, actor, MERCHANT_ROLES)
        except DomainError as exc:
            if exc.status in {401, 403, 404}:
                raise DomainError("merchant_asset_not_found", 404) from exc
            raise
        if normalized["merchantId"] != merchant_id:
            raise DomainError("merchant_asset_not_found", 404)
        return merchant_id, kind

    def merchant_brand_asset_descriptor(
        self, actor, merchant_id: str, kind: str,
    ) -> dict:
        merchant_id = clean_text(merchant_id, 90, True)
        kind = clean_text(kind, 20, True)
        if kind not in BRAND_ASSET_KINDS:
            raise DomainError("merchant_asset_not_found", 404)
        field = "logo_path" if kind == "logo" else "cover_path"
        with connect() as con:
            merchant = con.execute(
                f"SELECT {field} asset_path FROM merchants WHERE id=?", (merchant_id,),
            ).fetchone()
            media_id = self._path_media_id(merchant["asset_path"] if merchant else "")
            row = con.execute(
                "SELECT * FROM private_media_objects WHERE id=?", (media_id,),
            ).fetchone() if media_id else None
            if not row:
                raise DomainError("merchant_asset_not_found", 404)
            owner_id, actual_kind = self._brand_asset_access(con, actor, row)
            if owner_id != merchant_id or actual_kind != kind:
                raise DomainError("merchant_asset_not_found", 404)
            return self._asset_descriptor(row, merchant_id, kind)

    def resolve_merchant_brand_asset(self, actor, asset_id: str) -> ResolvedMerchantBrandAsset:
        asset_id = clean_text(asset_id, 180, True)
        if not OPAQUE_MEDIA_ID.fullmatch(asset_id):
            raise DomainError("merchant_asset_not_found", 404)
        with connect() as con:
            row = con.execute(
                "SELECT * FROM private_media_objects WHERE id=?", (asset_id,),
            ).fetchone()
            if not row:
                raise DomainError("merchant_asset_not_found", 404)
            self._brand_asset_access(con, actor, row)
            candidate = _safe_internal_path(row["storage_key"])
            if candidate.stat().st_size != int(row["byte_size"]):
                raise DomainError("merchant_asset_not_found", 404)
            digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
            if not hmac.compare_digest(digest, str(row["sha256_hex"] or "")):
                raise DomainError("merchant_asset_not_found", 404)
            return ResolvedMerchantBrandAsset(
                asset_id=row["id"], path=candidate,
                mime_type=row["mime_type"], byte_size=int(row["byte_size"]),
            )

    def resolve_merchant_brand_asset_path(self, actor, asset_id: str) -> Path:
        """Compatibility helper for callers that already supply safe headers."""
        return self.resolve_merchant_brand_asset(actor, asset_id).path

    # ---------- typed branch launch lifecycle ----------

    def _branch_for_merchant(self, con, actor, branch_id: str):
        actor = self._require_actor(con, actor, BRANCH_MANAGER_ROLES)
        row = con.execute(
            "SELECT * FROM store_branches WHERE id=? AND merchant_id=?",
            (branch_id, actor["merchantId"]),
        ).fetchone()
        if not row:
            raise DomainError("branch_not_found", 404)
        return actor, row

    @staticmethod
    def _validate_branch_location(con, branch) -> None:
        hierarchy = con.execute(
            """SELECT 1 FROM locations a JOIN locations w ON w.id=a.parent_id
               WHERE a.id=? AND a.kind='area' AND a.active=1
                 AND w.id=? AND w.kind='wilayat' AND w.active=1""",
            (branch["area_id"], branch["wilayah_id"]),
        ).fetchone()
        if not hierarchy or not clean_text(branch["address_text"], 300):
            raise DomainError("invalid_branch_location", 422)
        if branch["latitude"] is None or branch["longitude"] is None:
            raise DomainError("branch_map_pin_required", 422)
        try:
            latitude, longitude = float(branch["latitude"]), float(branch["longitude"])
        except (TypeError, ValueError) as exc:
            raise DomainError("invalid_branch_location", 422) from exc
        if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
            raise DomainError("invalid_branch_location", 422)
        if not coordinates_in_muscat(latitude, longitude):
            raise DomainError("branch_map_pin_outside_muscat", 422)

    def _plan_branch_capacity(self, con, merchant_id: str) -> tuple[int, int]:
        plan = self._active_plan(con, merchant_id)
        maximum = _bounded_int(
            plan["entitlements"].get("branches", 0), 0, 10_000, "invalid_plan_limit",
        )
        used = con.execute(
            "SELECT COUNT(*) n FROM store_branches WHERE merchant_id=? AND active=1",
            (merchant_id,),
        ).fetchone()["n"]
        if used > maximum:
            raise DomainError("plan_branch_limit", 409, {"limit": maximum, "used": used})
        return used, maximum

    def _branch_documents(
        self, con, merchant_id: str, branch_id: str, documents: Any,
    ) -> list[dict]:
        if documents is None:
            documents = []
        if not isinstance(documents, list) or len(documents) > 10:
            raise DomainError("invalid_branch_documents", 422)
        normalized = []
        seen = set()
        for item in documents:
            if not isinstance(item, dict):
                raise DomainError("invalid_branch_document", 422)
            kind = clean_text(item.get("kind"), 40, True)
            media_id = clean_text(item.get("mediaId"), 180, True)
            if kind not in BRANCH_DOCUMENT_KINDS or kind in seen:
                raise DomainError("invalid_branch_document", 422)
            if not OPAQUE_MEDIA_ID.fullmatch(media_id):
                raise DomainError("branch_document_not_found", 404)
            row = con.execute(
                """SELECT id,mime_type,byte_size FROM private_media_objects
                   WHERE id=? AND owner_kind='merchant' AND owner_id=?
                     AND purpose=? AND status='active'""",
                (media_id, merchant_id, f"branch:{branch_id}:{kind}"),
            ).fetchone()
            if not row:
                raise DomainError("branch_document_not_found", 404)
            seen.add(kind)
            normalized.append({
                "kind": kind,
                "mediaId": row["id"],
                "mimeType": row["mime_type"],
                "byteSize": int(row["byte_size"]),
            })
        return normalized

    def submit_branch_for_review(self, actor, branch_id: str, payload: dict) -> dict:
        branch_id = clean_text(branch_id, 90, True)
        if not isinstance(payload, dict):
            raise DomainError("branch_submission_object_required", 422)
        exception_reason = clean_text(payload.get("documentExceptionReason"), 500)
        stamp = now_iso()
        with connect(immediate=True) as con:
            actor, branch = self._branch_for_merchant(con, actor, branch_id)
            if branch["status"] not in {"draft", "changes_requested"}:
                raise DomainError("branch_submission_locked", 409)
            self._validate_branch_location(con, branch)
            hours_value = payload.get("hours")
            hours = _opening_hours(
                hours_value if hours_value is not None else loads(branch["hours_json"], {}),
            )
            documents = self._branch_documents(
                con, actor["merchantId"], branch_id, payload.get("documents"),
            )
            if not any(item["kind"] == "storefront" for item in documents):
                if len(exception_reason) < 20:
                    raise DomainError("branch_documents_or_reason_required", 422)
            used, maximum = self._plan_branch_capacity(con, actor["merchantId"])
            revision = con.execute(
                """SELECT COUNT(*) n FROM admin_audit_logs
                   WHERE target_kind='store_branch' AND target_id=?
                     AND action='branch_submitted_for_review'""",
                (branch_id,),
            ).fetchone()["n"] + 1
            submission = {
                "revision": revision,
                "documents": documents,
                "documentExceptionReason": exception_reason,
                "hours": hours,
                "location": {
                    "wilayahId": branch["wilayah_id"], "areaId": branch["area_id"],
                    "address": branch["address_text"],
                    "latitude": branch["latitude"], "longitude": branch["longitude"],
                },
            }
            con.execute(
                """UPDATE store_branches
                   SET hours_json=?,status='pending_review',active=1,public_visible=0,updated_at=?
                   WHERE id=? AND merchant_id=?""",
                (dumps(hours), stamp, branch_id, actor["merchantId"]),
            )
            con.execute(
                """UPDATE notifications SET acted_at=?
                   WHERE target_kind='admin' AND route=? AND acted_at=''""",
                (stamp, f"admin:branch-review:{branch_id}"),
            )
            self._insert_notification(
                con, "admin", "admin", "فرع جديد للمراجعة", "New branch for review",
                f"{branch['name_ar']} — مراجعة الإطلاق",
                f"{branch['name_en']} — launch review",
                f"admin:branch-review:{branch_id}", True,
                f"branch-review:{branch_id}:submitted:v{revision}", priority=90,
            )
            con.execute(
                """INSERT INTO admin_audit_logs(
                    id,actor_id,action,target_kind,target_id,before_json,after_json,reason,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    new_id("audit"), actor["accountId"], "branch_submitted_for_review",
                    "store_branch", branch_id, dumps({"status": branch["status"]}),
                    dumps(submission), exception_reason, stamp,
                ),
            )
            return {
                "id": branch_id, "status": "pending_review", "publicVisible": False,
                "revision": revision, "planUsage": {"branches": used, "limit": maximum},
            }

    def _latest_branch_submission(self, con, branch_id: str) -> dict:
        row = con.execute(
            """SELECT after_json FROM admin_audit_logs
               WHERE target_kind='store_branch' AND target_id=?
                 AND action='branch_submitted_for_review'
               ORDER BY created_at DESC,id DESC LIMIT 1""",
            (branch_id,),
        ).fetchone()
        submission = loads(row["after_json"], {}) if row else {}
        if not isinstance(submission, dict) or not submission:
            raise DomainError("branch_submission_not_found", 409)
        return submission

    def _revalidate_branch_submission(self, con, branch, submission: dict) -> None:
        self._validate_branch_location(con, branch)
        _opening_hours(loads(branch["hours_json"], {}))
        documents = submission.get("documents", [])
        payload_documents = [
            {"kind": item.get("kind"), "mediaId": item.get("mediaId")}
            for item in documents if isinstance(item, dict)
        ]
        current = self._branch_documents(
            con, branch["merchant_id"], branch["id"], payload_documents,
        )
        reason = clean_text(submission.get("documentExceptionReason"), 500)
        if not any(item["kind"] == "storefront" for item in current) and len(reason) < 20:
            raise DomainError("branch_documents_or_reason_required", 409)

    def admin_branch_decision(self, actor, branch_id: str, payload: dict) -> dict:
        branch_id = clean_text(branch_id, 90, True)
        decision = clean_text(payload.get("decision"), 30, True)
        note = clean_text(payload.get("note"), 500)
        target = {
            "approve": "approved", "reject": "rejected",
            "changes_requested": "changes_requested", "pause": "paused",
        }.get(decision)
        if not target:
            raise DomainError("invalid_branch_decision", 422)
        if decision in {"reject", "changes_requested", "pause"} and len(note) < 5:
            raise DomainError("branch_decision_note_required", 422)
        stamp = now_iso()
        with connect(immediate=True) as con:
            actor = self._require_admin_permission(con, actor, "merchant.review")
            branch = con.execute(
                """SELECT b.*,m.owner_account_id,m.status merchant_status,m.active merchant_active
                   FROM store_branches b JOIN merchants m ON m.id=b.merchant_id
                   WHERE b.id=?""",
                (branch_id,),
            ).fetchone()
            if not branch:
                raise DomainError("branch_not_found", 404)
            if branch["status"] == target:
                return {
                    "id": branch_id, "status": target,
                    "publicVisible": bool(branch["public_visible"]), "duplicate": True,
                }
            allowed = {
                "approve": {"pending_review", "paused"},
                "reject": {"pending_review"},
                "changes_requested": {"pending_review"},
                "pause": {"approved"},
            }
            if branch["status"] not in allowed[decision]:
                raise DomainError("invalid_branch_transition", 409, {
                    "from": branch["status"], "decision": decision,
                })
            submission = self._latest_branch_submission(con, branch_id)
            if decision == "approve":
                if branch["merchant_status"] != "approved" or not branch["merchant_active"]:
                    raise DomainError("merchant_not_active", 409)
                self._revalidate_branch_submission(con, branch, submission)
                used, maximum = self._plan_branch_capacity(con, branch["merchant_id"])
                has_storefront = any(
                    item.get("kind") == "storefront"
                    for item in submission.get("documents", []) if isinstance(item, dict)
                )
                if not has_storefront and len(note) < 10:
                    raise DomainError("branch_document_exception_approval_reason_required", 422)
                public_visible, active = 1, 1
            elif decision == "reject":
                used = maximum = 0
                public_visible, active = 0, 0
            else:
                used = maximum = 0
                public_visible, active = 0, 1
            con.execute(
                """UPDATE store_branches SET status=?,active=?,public_visible=?,updated_at=?
                   WHERE id=?""",
                (target, active, public_visible, stamp, branch_id),
            )
            con.execute(
                """UPDATE notifications SET acted_at=?
                   WHERE target_kind='admin' AND route=? AND requires_action=1 AND acted_at=''""",
                (stamp, f"admin:branch-review:{branch_id}"),
            )
            revision = con.execute(
                """SELECT COUNT(*) n FROM admin_audit_logs
                   WHERE target_kind='store_branch' AND target_id=?""",
                (branch_id,),
            ).fetchone()["n"] + 1
            messages = {
                "approved": ("تم اعتماد الفرع", "Branch approved", "أصبح الفرع جاهزاً للظهور", "The branch is ready for publication"),
                "rejected": ("تعذر اعتماد الفرع", "Branch rejected", note, note),
                "changes_requested": ("الفرع يحتاج تحديثاً", "Branch needs changes", note, note),
                "paused": ("تم إيقاف ظهور الفرع", "Branch publication paused", note, note),
            }
            title_ar, title_en, body_ar, body_en = messages[target]
            self._insert_notification(
                con, "account", branch["owner_account_id"], title_ar, title_en,
                body_ar, body_en, f"merchant:branch:{branch_id}", False,
                f"branch-review:{branch_id}:{target}:v{revision}", priority=70,
            )
            after = {"status": target, "publicVisible": bool(public_visible)}
            if decision == "approve":
                after["planUsage"] = {"branches": used, "limit": maximum}
            con.execute(
                """INSERT INTO admin_audit_logs(
                    id,actor_id,action,target_kind,target_id,before_json,after_json,reason,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    new_id("audit"), actor["accountId"], f"branch_{target}",
                    "store_branch", branch_id, dumps({"status": branch["status"]}),
                    dumps(after), note, stamp,
                ),
            )
            return {
                "id": branch_id, "status": target,
                "publicVisible": bool(public_visible), "duplicate": False,
            }

    def update_branch_hours(self, actor, branch_id: str, payload: dict) -> dict:
        branch_id = clean_text(branch_id, 90, True)
        hours = _opening_hours(payload.get("hours") if isinstance(payload, dict) else None)
        stamp = now_iso()
        with connect(immediate=True) as con:
            actor, branch = self._branch_for_merchant(con, actor, branch_id)
            if branch["status"] == "pending_review":
                raise DomainError("branch_under_review", 409)
            if branch["status"] == "rejected" or not branch["active"]:
                raise DomainError("branch_not_active", 409)
            before = loads(branch["hours_json"], {})
            con.execute(
                "UPDATE store_branches SET hours_json=?,updated_at=? WHERE id=? AND merchant_id=?",
                (dumps(hours), stamp, branch_id, actor["merchantId"]),
            )
            con.execute(
                """INSERT INTO admin_audit_logs(
                    id,actor_id,action,target_kind,target_id,before_json,after_json,reason,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    new_id("audit"), actor["accountId"], "branch_hours_updated",
                    "store_branch", branch_id, dumps({"hours": before}),
                    dumps({"hours": hours}), "", stamp,
                ),
            )
            return {
                "id": branch_id, "status": branch["status"],
                "publicVisible": bool(branch["public_visible"]), "hours": hours,
            }

    def branch_launch_detail(self, actor, branch_id: str) -> dict:
        branch_id = clean_text(branch_id, 90, True)
        with connect() as con:
            role = str((actor or {}).get("role") or "")
            if role in ADMIN_ROLES:
                self._require_admin_permission(con, actor, "merchant.read")
                branch = con.execute(
                    "SELECT * FROM store_branches WHERE id=?", (branch_id,),
                ).fetchone()
            else:
                _, branch = self._branch_for_merchant(con, actor, branch_id)
            if not branch:
                raise DomainError("branch_not_found", 404)
            submission = self._latest_branch_submission(con, branch_id)
            # The review surface receives only safe descriptors. The private
            # media route still performs its own authorization before streaming.
            return {
                "branch": {
                    "id": branch["id"], "merchantId": branch["merchant_id"],
                    "nameAr": branch["name_ar"], "nameEn": branch["name_en"],
                    "status": branch["status"], "active": bool(branch["active"]),
                    "publicVisible": bool(branch["public_visible"]),
                    "wilayahId": branch["wilayah_id"], "areaId": branch["area_id"],
                    "address": branch["address_text"],
                    "latitude": branch["latitude"], "longitude": branch["longitude"],
                    "hours": loads(branch["hours_json"], {}),
                },
                "submission": {
                    "revision": submission.get("revision"),
                    "documentExceptionReason": submission.get("documentExceptionReason", ""),
                },
                "documents": submission.get("documents", []),
            }
