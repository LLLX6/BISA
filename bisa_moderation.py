"""Authorized, state-bound moderation review for BISA products and merchant ads.

This module is deliberately independent of the HTTP server and composition
root.  Compose :class:`ModerationReviewMixin` before ``MarketplaceMixin`` to
make the existing generic admin endpoint require a fresh review receipt before
it can approve or reject a product or merchant advertisement.

Review JSON never includes storage keys or filesystem paths.  Media bytes are
resolved by a separate, permission-checked method that also binds access to the
review receipt and the exact product/campaign state the reviewer opened.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any

import bisa_config
from bisa_domain import DomainError, clean_text, connect, dumps, loads, new_id, now_iso, omr


REVIEW_RESOURCES = {"product": "catalog.moderate", "ad": "ad.manage"}
REVIEW_RECEIPT_TTL_SECONDS = 300
REVIEW_TOKEN_BYTES = 32


# This schema belongs in the next additive migration when the mixin is wired
# into the production composition root.  Runtime creation keeps the module
# independently testable and is idempotent, but a migration is preferred at
# deployment time.
MODERATION_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS moderation_review_receipts(
 receipt_hash TEXT PRIMARY KEY,
 target_kind TEXT NOT NULL CHECK(target_kind IN('product','ad')),
 target_id TEXT NOT NULL,
 reviewer_account_id TEXT NOT NULL,
 snapshot_hash TEXT NOT NULL,
 issued_at TEXT NOT NULL,
 expires_at TEXT NOT NULL,
 consumed_at TEXT NOT NULL DEFAULT '',
 decision TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_moderation_receipt_target
ON moderation_review_receipts(target_kind,target_id,reviewer_account_id,expires_at);
"""


MODERATION_API_CONTRACTS = {
    "GET /api/admin/moderation/{resource}/{id}": {
        "method": "moderation_review_detail",
        "resources": ["product", "ad"],
        "response": ["resource", "item", "reviewStateVersion", "reviewReceipt", "reviewExpiresAt"],
    },
    "GET /api/admin/moderation/{resource}/{id}/media/{mediaId}": {
        "method": "resolve_moderation_media",
        "request": ["reviewReceipt"],
        "response": ["binary", "Content-Type", "Content-Length", "ETag", "Cache-Control"],
        "note": "The method result is an internal binary descriptor and must never be JSON serialized.",
    },
    "POST /api/admin/resources/{resource}/{action}": {
        "method": "admin_action",
        "resources": ["product", "ad"],
        "request": ["id", "reviewReceipt", "reason"],
        "note": "Compose ModerationReviewMixin first; approve/reject require a fresh one-time receipt.",
    },
}


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _token_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _as_utc(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _safe_identifier(value: Any, *, required: bool = True) -> str:
    identifier = clean_text(value, 90, required)
    if any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-" for character in identifier):
        raise DomainError("invalid_identifier", 422)
    return identifier


def _safe_storage_candidate(storage_key: str) -> Path:
    """Resolve a private object without allowing absolute/traversal paths."""
    raw = str(storage_key or "").replace("\\", "/").strip("/")
    relative = PurePosixPath(raw)
    if (
        not raw
        or relative.is_absolute()
        or ".." in relative.parts
        or not relative.parts
        or relative.parts[0] != "private"
    ):
        raise DomainError("moderation_media_not_found", 404)
    root = Path(bisa_config.UPLOAD_DIR).resolve()
    candidate = (root / Path(*relative.parts)).resolve()
    if root not in candidate.parents or not candidate.is_file() or candidate.is_symlink():
        raise DomainError("moderation_media_not_found", 404)
    return candidate


class ModerationReviewMixin:
    """Typed review details, private media resolution, and receipt-bound decisions."""

    moderation_review_ttl_seconds = REVIEW_RECEIPT_TTL_SECONDS

    @staticmethod
    def _ensure_moderation_schema(con) -> None:
        # ``executescript`` implicitly commits in sqlite3 and would break the
        # state-read/receipt-write transaction.  Keep both DDL operations on
        # the caller's existing BEGIN IMMEDIATE transaction.
        con.execute(
            """CREATE TABLE IF NOT EXISTS moderation_review_receipts(
                 receipt_hash TEXT PRIMARY KEY,
                 target_kind TEXT NOT NULL CHECK(target_kind IN('product','ad')),
                 target_id TEXT NOT NULL,
                 reviewer_account_id TEXT NOT NULL,
                 snapshot_hash TEXT NOT NULL,
                 issued_at TEXT NOT NULL,
                 expires_at TEXT NOT NULL,
                 consumed_at TEXT NOT NULL DEFAULT '',
                 decision TEXT NOT NULL DEFAULT ''
               )"""
        )
        con.execute(
            """CREATE INDEX IF NOT EXISTS idx_moderation_receipt_target
               ON moderation_review_receipts(
                 target_kind,target_id,reviewer_account_id,expires_at
               )"""
        )

    @staticmethod
    def _permission_for_review(resource: str) -> str:
        permission = REVIEW_RESOURCES.get(resource)
        if not permission:
            raise DomainError("moderation_resource_not_supported", 404)
        return permission

    def _product_review_snapshot(self, con, target_id: str) -> dict:
        row = con.execute(
            """SELECT p.*,c.name_ar category_name_ar,c.name_en category_name_en,
                      c.regulated_rules,c.active category_active,
                      m.name_ar merchant_name_ar,m.name_en merchant_name_en,
                      m.status merchant_status,m.verified merchant_verified,m.active merchant_active
               FROM products p
               JOIN product_categories c ON c.id=p.category_id
               JOIN merchants m ON m.id=p.merchant_id
               WHERE p.id=?""",
            (target_id,),
        ).fetchone()
        if not row:
            raise DomainError("product_not_found", 404)

        inventory = [
            {
                "branchId": branch["branch_id"],
                "branchNameAr": branch["branch_name_ar"],
                "branchNameEn": branch["branch_name_en"],
                "wilayahId": branch["wilayah_id"],
                "areaId": branch["area_id"],
                "branchStatus": branch["branch_status"],
                "branchActive": bool(branch["branch_active"]),
                "publicVisible": bool(branch["public_visible"]),
                "stockMode": branch["stock_mode"],
                "quantity": branch["quantity"],
                "availability": branch["availability"],
                "inventoryActive": bool(branch["inventory_active"]),
                "freshnessStatus": branch["freshness_status"],
                "lastStockVerifiedAt": branch["last_stock_verified_at"],
                "updatedAt": branch["inventory_updated_at"],
            }
            for branch in con.execute(
                """SELECT i.branch_id,b.name_ar branch_name_ar,b.name_en branch_name_en,
                          b.wilayah_id,b.area_id,b.status branch_status,b.active branch_active,
                          b.public_visible,i.stock_mode,i.quantity,i.availability,
                          i.active inventory_active,i.freshness_status,
                          i.last_stock_verified_at,i.updated_at inventory_updated_at
                   FROM product_branch_inventory i
                   JOIN store_branches b ON b.id=i.branch_id AND b.merchant_id=?
                   WHERE i.product_id=? ORDER BY b.id""",
                (row["merchant_id"], target_id),
            )
        ]

        total_media = con.execute(
            "SELECT COUNT(*) n FROM product_media WHERE product_id=? AND status='active'",
            (target_id,),
        ).fetchone()["n"]
        media_rows = con.execute(
            """SELECT pm.id,pm.mime_type,pm.width,pm.height,pm.sort_order,pm.created_at,
                      mo.purpose,mo.byte_size,mo.sha256_hex,mo.original_name,mo.updated_at
               FROM product_media pm
               JOIN private_media_objects mo ON mo.id=pm.id
               WHERE pm.product_id=? AND pm.status='active' AND mo.status='active'
                 AND mo.owner_kind='merchant' AND mo.owner_id=?
                 AND mo.purpose='product_image' AND mo.mime_type LIKE 'image/%'
               ORDER BY pm.sort_order,pm.id""",
            (target_id, row["merchant_id"]),
        ).fetchall()
        media = [
            {
                "id": item["id"],
                "mimeType": item["mime_type"],
                "byteSize": item["byte_size"],
                "width": item["width"],
                "height": item["height"],
                "sortOrder": item["sort_order"],
                "purpose": item["purpose"],
                "originalName": item["original_name"],
                "contentUrl": f"/api/admin/moderation/product/{target_id}/media/{item['id']}",
                "createdAt": item["created_at"],
                # The digest and object update time bind the receipt to the exact bytes
                # without exposing either field in a filesystem-oriented form.
                "contentDigest": item["sha256_hex"],
                "objectUpdatedAt": item["updated_at"],
            }
            for item in media_rows
        ]
        broken_media = int(total_media) - len(media)
        item = {
            "id": row["id"],
            "merchant": {
                "id": row["merchant_id"],
                "nameAr": row["merchant_name_ar"],
                "nameEn": row["merchant_name_en"],
                "status": row["merchant_status"],
                "verified": bool(row["merchant_verified"]),
                "active": bool(row["merchant_active"]),
            },
            "category": {
                "id": row["category_id"],
                "nameAr": row["category_name_ar"],
                "nameEn": row["category_name_en"],
                "active": bool(row["category_active"]),
                "regulatedRules": loads(row["regulated_rules"], {}),
            },
            "nameAr": row["name_ar"],
            "nameEn": row["name_en"],
            "descriptionAr": row["description_ar"],
            "descriptionEn": row["description_en"],
            "price": omr(row["price_baisa"]),
            "priceBaisa": row["price_baisa"],
            "unit": row["unit_text"],
            "barcode": row["barcode"],
            "metadata": loads(row["metadata_json"], {}),
            "tags": loads(row["tags_json"], []),
            "status": row["status"],
            "moderationStatus": row["moderation_status"],
            "active": bool(row["active"]),
            "archivedAt": row["archived_at"],
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
            "inventory": inventory,
            "media": media,
            "mediaIntegrity": {
                "valid": broken_media == 0,
                "activeAssociations": int(total_media),
                "reviewableAssociations": len(media),
                "brokenAssociations": broken_media,
            },
        }
        return item

    @staticmethod
    def _ad_landing(con, row) -> dict:
        owner_id = row["owner_id"]
        kind = row["landing_kind"]
        landing_id = row["landing_id"]
        if kind == "store":
            landing = con.execute(
                """SELECT id,name_ar,name_en,wilayah_id,area_id,status,active,public_visible,updated_at
                   FROM store_branches WHERE id=? AND merchant_id=?""",
                (landing_id, owner_id),
            ).fetchone()
            detail = dict(landing) if landing else None
            eligible = bool(
                landing and landing["status"] == "approved"
                and landing["active"] and landing["public_visible"]
            )
        elif kind == "product":
            landing = con.execute(
                """SELECT id,name_ar,name_en,price_baisa,status,moderation_status,active,
                          archived_at,updated_at
                   FROM products WHERE id=? AND merchant_id=?""",
                (landing_id, owner_id),
            ).fetchone()
            detail = dict(landing) if landing else None
            eligible = bool(
                landing and landing["status"] == "approved"
                and landing["moderation_status"] == "approved"
                and landing["active"] and not landing["archived_at"]
            )
        elif kind == "bundle":
            landing = con.execute(
                """SELECT id,branch_id,title_ar,title_en,description,selling_price_baisa,
                          status,moderation_status,starts_at,ends_at,updated_at
                   FROM bundles WHERE id=? AND merchant_id=?""",
                (landing_id, owner_id),
            ).fetchone()
            detail = dict(landing) if landing else None
            eligible = bool(
                landing and landing["status"] == "approved"
                and landing["moderation_status"] == "approved"
            )
        else:
            detail, eligible = None, False
        if detail:
            normalized = {}
            for key, value in detail.items():
                normalized_key = {
                    "name_ar": "nameAr", "name_en": "nameEn",
                    "title_ar": "titleAr", "title_en": "titleEn",
                    "price_baisa": "priceBaisa", "selling_price_baisa": "priceBaisa",
                    "moderation_status": "moderationStatus", "public_visible": "publicVisible",
                    "archived_at": "archivedAt", "starts_at": "startsAt",
                    "ends_at": "endsAt", "updated_at": "updatedAt",
                    "branch_id": "branchId", "wilayah_id": "wilayahId", "area_id": "areaId",
                }.get(key, key)
                normalized[normalized_key] = bool(value) if key in {"active", "public_visible"} else value
            detail = normalized
        return {"kind": kind, "id": landing_id, "eligible": eligible, "detail": detail}

    def _ad_review_snapshot(self, con, target_id: str) -> dict:
        row = con.execute(
            """SELECT a.*,m.name_ar merchant_name_ar,m.name_en merchant_name_en,
                      m.status merchant_status,m.verified merchant_verified,m.active merchant_active
               FROM ad_campaigns a
               JOIN merchants m ON m.id=a.owner_id AND a.owner_kind='merchant'
               WHERE a.id=?""",
            (target_id,),
        ).fetchone()
        if not row:
            # Supplier campaigns have a dedicated review workflow and must not
            # be discoverable through the merchant-ad moderation endpoint.
            raise DomainError("ad_not_found", 404)
        raw_target = loads(row["target_json"], {})
        if not isinstance(raw_target, dict):
            raw_target = {}
        target = {
            "titleAr": clean_text(raw_target.get("titleAr"), 120),
            "titleEn": clean_text(raw_target.get("titleEn"), 120),
            "creativeMediaId": clean_text(raw_target.get("creativeMediaId"), 90),
            "paymentStatus": clean_text(raw_target.get("paymentStatus"), 40) or "not_started",
            "wilayatIds": [clean_text(value, 90) for value in raw_target.get("wilayatIds", [])[:20]],
            "areaIds": [clean_text(value, 90) for value in raw_target.get("areaIds", [])[:100]],
            "categoryIds": [clean_text(value, 90) for value in raw_target.get("categoryIds", [])[:30]],
            "language": clean_text(raw_target.get("language"), 10) or "all",
        }
        creative = None
        creative_id = target["creativeMediaId"]
        if creative_id:
            media = con.execute(
                """SELECT id,mime_type,byte_size,sha256_hex,original_name,purpose,updated_at
                   FROM private_media_objects
                   WHERE id=? AND owner_kind='merchant' AND owner_id=?
                     AND purpose='ad_creative' AND status='active' AND mime_type LIKE 'image/%'""",
                (creative_id, row["owner_id"]),
            ).fetchone()
            if media:
                creative = {
                    "id": media["id"], "mimeType": media["mime_type"],
                    "byteSize": media["byte_size"], "contentDigest": media["sha256_hex"],
                    "originalName": media["original_name"], "purpose": media["purpose"],
                    "objectUpdatedAt": media["updated_at"],
                    "contentUrl": f"/api/admin/moderation/ad/{target_id}/media/{media['id']}",
                }
        landing = self._ad_landing(con, row)
        commercial_status = target["paymentStatus"]
        commercial_eligible = commercial_status in {
            "paid", "included_credit", "admin_waived",
        }
        return {
            "id": row["id"],
            "ownerKind": "merchant",
            "merchant": {
                "id": row["owner_id"], "nameAr": row["merchant_name_ar"],
                "nameEn": row["merchant_name_en"], "status": row["merchant_status"],
                "verified": bool(row["merchant_verified"]), "active": bool(row["merchant_active"]),
            },
            "placement": row["placement"],
            "labelAr": row["label_ar"],
            "labelEn": row["label_en"],
            "status": row["status"],
            "startsAt": row["starts_at"],
            "endsAt": row["ends_at"],
            "frequencyCap": row["frequency_cap"],
            "target": target,
            "commercial": {
                "paymentStatus": commercial_status,
                "eligibleForApproval": commercial_eligible,
            },
            "landing": landing,
            "creative": creative,
            "creativeIntegrity": {"valid": not creative_id or creative is not None},
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
        }

    def _review_snapshot(self, con, resource: str, target_id: str) -> dict:
        if resource == "product":
            return self._product_review_snapshot(con, target_id)
        if resource == "ad":
            return self._ad_review_snapshot(con, target_id)
        raise DomainError("moderation_resource_not_supported", 404)

    def _issue_review_receipt(
        self, con, actor: dict, resource: str, target_id: str, snapshot_hash: str
    ) -> tuple[str, str]:
        token = secrets.token_urlsafe(REVIEW_TOKEN_BYTES)
        issued = datetime.now(UTC)
        ttl = max(60, min(int(self.moderation_review_ttl_seconds), 900))
        expires = issued + timedelta(seconds=ttl)
        con.execute(
            """INSERT INTO moderation_review_receipts(
                   receipt_hash,target_kind,target_id,reviewer_account_id,snapshot_hash,
                   issued_at,expires_at,consumed_at,decision)
               VALUES(?,?,?,?,?,?,?,'','')""",
            (
                _token_hash(token), resource, target_id, actor["accountId"], snapshot_hash,
                issued.isoformat(), expires.isoformat(),
            ),
        )
        return token, expires.isoformat()

    def moderation_review_detail(self, actor: dict, resource: str, target_id: str) -> dict:
        resource = clean_text(resource, 20, True)
        target_id = _safe_identifier(target_id)
        permission = self._permission_for_review(resource)
        with connect(immediate=True) as con:
            self._ensure_moderation_schema(con)
            actor = self._require_admin_permission(con, actor, permission)
            item = self._review_snapshot(con, resource, target_id)
            snapshot_hash = _canonical_hash(item)
            receipt, expires_at = self._issue_review_receipt(
                con, actor, resource, target_id, snapshot_hash
            )
            return {
                "resource": resource,
                "item": item,
                "reviewStateVersion": snapshot_hash,
                "reviewReceipt": receipt,
                "reviewExpiresAt": expires_at,
            }

    def _validated_receipt(
        self,
        con,
        actor: dict,
        resource: str,
        target_id: str,
        token: Any,
        current_snapshot: dict,
    ):
        raw = str(token or "").strip()
        if not raw:
            raise DomainError("moderation_review_required", 409)
        receipt = con.execute(
            """SELECT * FROM moderation_review_receipts
               WHERE receipt_hash=? AND target_kind=? AND target_id=? AND reviewer_account_id=?""",
            (_token_hash(raw), resource, target_id, actor["accountId"]),
        ).fetchone()
        if not receipt:
            raise DomainError("moderation_review_receipt_not_found", 404)
        if receipt["consumed_at"]:
            raise DomainError("moderation_review_receipt_consumed", 409)
        expires_at = _as_utc(receipt["expires_at"])
        if not expires_at or expires_at <= datetime.now(UTC):
            raise DomainError("moderation_review_receipt_expired", 409)
        if not secrets.compare_digest(receipt["snapshot_hash"], _canonical_hash(current_snapshot)):
            raise DomainError("moderation_review_stale", 409)
        return receipt

    def _media_object_for_review(
        self, con, resource: str, target_id: str, media_id: str, snapshot: dict
    ):
        if resource == "product":
            if media_id not in {item["id"] for item in snapshot["media"]}:
                return None
            merchant_id = snapshot["merchant"]["id"]
            return con.execute(
                """SELECT mo.id,mo.storage_key,mo.mime_type,mo.byte_size,mo.sha256_hex
                   FROM product_media pm
                   JOIN products p ON p.id=pm.product_id
                   JOIN private_media_objects mo ON mo.id=pm.id
                   WHERE pm.product_id=? AND pm.id=? AND pm.status='active'
                     AND mo.status='active' AND mo.owner_kind='merchant' AND mo.owner_id=?
                     AND mo.owner_id=p.merchant_id AND mo.purpose='product_image'
                     AND mo.mime_type LIKE 'image/%'""",
                (target_id, media_id, merchant_id),
            ).fetchone()
        creative = snapshot.get("creative")
        if not creative or creative.get("id") != media_id:
            return None
        return con.execute(
            """SELECT id,storage_key,mime_type,byte_size,sha256_hex
               FROM private_media_objects
               WHERE id=? AND owner_kind='merchant' AND owner_id=?
                 AND purpose='ad_creative' AND status='active' AND mime_type LIKE 'image/%'""",
            (media_id, snapshot["merchant"]["id"]),
        ).fetchone()

    def resolve_moderation_media(
        self,
        actor: dict,
        resource: str,
        target_id: str,
        media_id: str,
        review_receipt: str,
    ) -> dict:
        """Return an internal binary descriptor; the HTTP layer must stream it."""
        resource = clean_text(resource, 20, True)
        target_id = _safe_identifier(target_id)
        media_id = _safe_identifier(media_id)
        permission = self._permission_for_review(resource)
        with connect(immediate=True) as con:
            self._ensure_moderation_schema(con)
            actor = self._require_admin_permission(con, actor, permission)
            snapshot = self._review_snapshot(con, resource, target_id)
            self._validated_receipt(
                con, actor, resource, target_id, review_receipt, snapshot
            )
            media = self._media_object_for_review(con, resource, target_id, media_id, snapshot)
            if not media:
                raise DomainError("moderation_media_not_found", 404)
            candidate = _safe_storage_candidate(media["storage_key"])
            if candidate.stat().st_size != int(media["byte_size"]):
                raise DomainError("moderation_media_not_found", 404)
            return {
                "path": candidate,
                "mimeType": media["mime_type"],
                "byteSize": int(media["byte_size"]),
                "etag": media["sha256_hex"],
                "cacheControl": "private, no-store",
            }

    def moderation_decision(
        self, actor: dict, resource: str, action: str, payload: dict
    ) -> dict:
        resource = clean_text(resource, 20, True)
        action = clean_text(action, 20, True)
        target_id = _safe_identifier(payload.get("id"))
        reason = clean_text(payload.get("reason"), 500)
        permission = self._permission_for_review(resource)
        stamp = now_iso()
        with connect(immediate=True) as con:
            self._ensure_moderation_schema(con)
            actor = self._require_admin_permission(con, actor, permission)
            before = self._review_snapshot(con, resource, target_id)

            receipt = None
            if action in {"approve", "reject"}:
                receipt = self._validated_receipt(
                    con, actor, resource, target_id,
                    payload.get("reviewReceipt"), before,
                )
            if action != "approve" and not reason:
                raise DomainError("admin_decision_reason_required", 422)

            if resource == "product":
                if action in {"approve", "reject"} and not (
                    before["status"] == "pending_review"
                    and before["moderationStatus"] == "pending"
                ):
                    raise DomainError("product_moderation_stage_not_allowed", 409)
                if action == "suspend" and not (
                    before["status"] == "approved"
                    and before["moderationStatus"] == "approved"
                ):
                    raise DomainError("product_moderation_stage_not_allowed", 409)
                if action not in {"approve", "reject", "suspend"}:
                    raise DomainError("invalid_admin_action", 422)
                if action == "approve" and not before["mediaIntegrity"]["valid"]:
                    raise DomainError("moderation_media_integrity_failed", 409)
                status, moderation_status, active = {
                    "approve": ("approved", "approved", 1),
                    "reject": ("rejected", "rejected", 0),
                    "suspend": ("suspended", "suspended", 0),
                }[action]
                con.execute(
                    """UPDATE products SET status=?,moderation_status=?,active=?,updated_at=?
                       WHERE id=?""",
                    (status, moderation_status, active, stamp, target_id),
                )
                after = {
                    "status": status,
                    "moderationStatus": moderation_status,
                    "active": bool(active),
                }
            else:
                if action in {"approve", "reject"} and before["status"] not in {
                    "draft", "pending_review"
                }:
                    raise DomainError("ad_moderation_stage_not_allowed", 409)
                if action == "pause" and before["status"] != "approved":
                    raise DomainError("ad_moderation_stage_not_allowed", 409)
                if action not in {"approve", "reject", "pause"}:
                    raise DomainError("invalid_admin_action", 422)
                if action == "approve":
                    if not before["commercial"]["eligibleForApproval"]:
                        raise DomainError("ad_payment_or_credit_required", 409)
                    if not before["creativeIntegrity"]["valid"]:
                        raise DomainError("moderation_media_integrity_failed", 409)
                    if not before["landing"]["eligible"]:
                        raise DomainError("campaign_landing_unavailable", 409)
                status = {"approve": "approved", "reject": "rejected", "pause": "paused"}[action]
                con.execute(
                    "UPDATE ad_campaigns SET status=?,updated_at=? WHERE id=?",
                    (status, stamp, target_id),
                )
                after = {"status": status}

            if receipt:
                updated = con.execute(
                    """UPDATE moderation_review_receipts
                       SET consumed_at=?,decision=?
                       WHERE receipt_hash=? AND consumed_at=''""",
                    (stamp, action, receipt["receipt_hash"]),
                ).rowcount
                if updated != 1:
                    raise DomainError("moderation_review_receipt_consumed", 409)
            con.execute(
                """INSERT INTO admin_audit_logs(
                       id,actor_id,action,target_kind,target_id,before_json,after_json,reason,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    new_id("audit"), actor["accountId"], f"{resource}.{action}",
                    resource, target_id,
                    dumps({
                        "status": before["status"],
                        **({"moderationStatus": before["moderationStatus"]} if resource == "product" else {}),
                        "reviewStateVersion": _canonical_hash(before),
                    }),
                    dumps(after), reason, stamp,
                ),
            )
            decision = {"id": target_id, "resource": resource, "action": action, **after}
            # Keep the historically flat response while honoring the generic
            # admin_action contract used by the rest of the control plane.
            return {**decision, "result": dict(after)}

    def admin_action(self, actor, resource: str, action: str, payload: dict) -> dict:
        """Protect generic product/ad decisions while preserving other resources."""
        if str(resource or "").strip() in REVIEW_RESOURCES:
            return self.moderation_decision(actor, resource, action, payload)
        return super().admin_action(actor, resource, action, payload)
