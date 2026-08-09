"""Supplier-advertiser application services for the independent BISA app.

Supplier campaigns are private drafts until an administrator approves them.
The supplier tenant is revalidated inside every transaction, and campaign
creative remains a private media object served only through an authorized
campaign route.  This module deliberately owns no HTTP routing so it can be
composed without weakening the central server boundary.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import bisa_config
from bisa_domain import DomainError, clean_text, connect, dumps, loads, new_id, normalize_phone, now_iso
from bisa_integrations import default_registry
from bisa_security import has_permission, require_permission


SUPPLIER_ROLE = "supplier_advertiser"
SUPPLIER_CAMPAIGN_EDITABLE_STATUSES = {"draft", "changes_requested", "rejected"}
SUPPLIER_CAMPAIGN_STATUSES = {
    "draft", "pending_review", "changes_requested", "approved", "rejected", "paused",
}
SUPPLIER_CONTACT_MODES = {"quote_request", "platform_contact", "whatsapp"}
SUPPLIER_CREATIVE_PURPOSES = {"supplier_campaign_creative", "supplier_campaign_image"}
SUPPLIER_CREATIVE_MIME_TYPES = {"image/jpeg", "image/png", "image/webp"}
MERCHANT_ROLES = {"merchant_owner", "merchant_manager", "merchant_staff"}
ADMIN_ROLES = {
    "support_admin", "catalog_moderator", "merchant_reviewer", "finance",
    "advertising_manager", "admin", "super_admin",
}

# Merge this map into ``bisa_application.API_CONTRACTS`` when the composition
# root is ready. Keeping it here makes the server integration explicit while
# avoiding edits to the shared HTTP/application files during parallel work.
SUPPLIER_API_CONTRACTS = {
    "GET /api/supplier/dashboard": {"method": "supplier_dashboard"},
    "GET /api/supplier/campaigns": {
        "method": "own_supplier_campaigns", "request": ["status", "cursor", "limit"],
    },
    "GET /api/supplier/campaigns/{campaignId}": {"method": "supplier_campaign_detail"},
    "POST /api/supplier/campaigns": {
        "method": "save_supplier_campaign",
        "request": [
            "idempotencyKey", "titleAr", "titleEn", "wholesaleDescriptionAr",
            "wholesaleDescriptionEn", "offerAr", "offerEn", "minimumOrderQuantity",
            "targetCategories", "targetWilayats", "targetAreas", "termsAr", "termsEn",
            "contactMode", "contactValue", "contactLabelAr", "contactLabelEn",
            "creativeMediaId", "startsAt", "endsAt",
        ],
    },
    "PUT /api/supplier/campaigns/{campaignId}": {
        "method": "save_supplier_campaign", "pathField": "id",
        "request": ["idempotencyKey", "expectedUpdatedAt", "campaign fields from POST"],
    },
    "POST /api/supplier/campaigns/{campaignId}/submit": {
        "method": "submit_supplier_campaign", "request": ["idempotencyKey", "expectedUpdatedAt"],
    },
    "GET /api/supplier/campaigns/{campaignId}/creative": {
        "method": "resolve_supplier_campaign_creative", "response": ["authorized binary", "ETag"],
    },
    "GET /api/supplier/leads": {
        "method": "supplier_leads", "request": ["campaignId", "cursor", "limit"],
    },
}


def _campaign_hash(value: Any) -> str:
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _bounded_int(value: Any, minimum: int, maximum: int, code: str) -> int:
    if isinstance(value, bool):
        raise DomainError(code, 422)
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise DomainError(code, 422) from exc
    if number < minimum or number > maximum:
        raise DomainError(code, 422, {"minimum": minimum, "maximum": maximum})
    return number


def _campaign_window(starts_at: Any, ends_at: Any) -> tuple[str, str]:
    normalized: list[str] = []
    parsed: list[datetime | None] = []
    for raw in (starts_at, ends_at):
        text = clean_text(raw, 40)
        if not text:
            normalized.append("")
            parsed.append(None)
            continue
        try:
            moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise DomainError("invalid_supplier_campaign_window", 422) from exc
        if moment.tzinfo is None:
            raise DomainError("supplier_campaign_timezone_required", 422)
        moment = moment.astimezone(UTC)
        normalized.append(moment.isoformat())
        parsed.append(moment)
    if parsed[0] and parsed[1] and parsed[1] <= parsed[0]:
        raise DomainError("invalid_supplier_campaign_window", 422)
    return normalized[0], normalized[1]


def _clean_id_list(value: Any, field: str, *, maximum: int = 100) -> list[str]:
    if value in (None, ""):
        return []
    if not isinstance(value, list) or len(value) > maximum:
        raise DomainError("invalid_supplier_campaign_targets", 422, {"field": field, "maximum": maximum})
    output: list[str] = []
    for item in value:
        identifier = clean_text(item, 90, True)
        if identifier not in output:
            output.append(identifier)
    return output


def _storage_candidate(storage_key: str) -> Path:
    root = Path(bisa_config.UPLOAD_DIR).resolve()
    candidate = (root / str(storage_key or "")).resolve()
    if root not in candidate.parents or not candidate.is_file() or candidate.is_symlink():
        raise DomainError("supplier_campaign_creative_not_found", 404)
    return candidate


class SupplierAdvertiserMixin:
    """Tenant-scoped campaign and lead services for approved suppliers."""

    def _require_supplier_actor(self, con, actor: dict | None, permission: str) -> dict:
        if not actor or actor.get("role") != SUPPLIER_ROLE or not actor.get("accountId"):
            raise DomainError("forbidden", 403)
        supplier_id = clean_text(actor.get("supplierId") or actor.get("merchantId"), 90)
        if not supplier_id:
            raise DomainError("session_not_authorized", 401)
        row = con.execute(
            """SELECT s.id,s.name_ar,s.name_en,s.logo_path,s.status,a.name account_name
               FROM suppliers s
               JOIN supplier_members sm ON sm.supplier_id=s.id
               JOIN accounts a ON a.id=sm.account_id
               JOIN account_roles ar ON ar.account_id=a.id
                 AND ar.role='supplier_advertiser' AND ar.merchant_id=s.id AND ar.active=1
               WHERE s.id=? AND s.status='approved' AND sm.account_id=?
                 AND sm.role='supplier_advertiser' AND sm.status='active' AND a.status='active'""",
            (supplier_id, actor["accountId"]),
        ).fetchone()
        if not row:
            raise DomainError("session_not_authorized", 401)
        normalized = {
            "accountId": actor["accountId"], "name": row["account_name"],
            "role": SUPPLIER_ROLE, "supplierId": row["id"], "merchantId": "",
        }
        require_permission(normalized, permission, con=con)
        normalized["supplier"] = {
            "id": row["id"], "nameAr": row["name_ar"], "nameEn": row["name_en"],
            "status": row["status"], "hasLogo": bool(row["logo_path"]),
        }
        return normalized

    @staticmethod
    def _validate_targets(con, payload: dict) -> None:
        target_sets = (
            ("targetCategories", "product_categories", "id", "active=1"),
            ("targetWilayats", "locations", "id", "kind='wilayah' AND active=1"),
            ("targetAreas", "locations", "id", "kind='area' AND active=1"),
        )
        for field, table, column, condition in target_sets:
            identifiers = payload[field]
            if not identifiers:
                continue
            marks = ",".join("?" for _ in identifiers)
            rows = con.execute(
                f"SELECT {column} id FROM {table} WHERE {column} IN ({marks}) AND {condition}",
                tuple(identifiers),
            ).fetchall()
            found = {row["id"] for row in rows}
            missing = [identifier for identifier in identifiers if identifier not in found]
            if missing:
                raise DomainError("supplier_campaign_target_not_found", 422, {"field": field, "ids": missing[:10]})
        if payload["targetAreas"] and payload["targetWilayats"]:
            marks = ",".join("?" for _ in payload["targetAreas"])
            parents = {
                row["parent_id"] for row in con.execute(
                    f"SELECT parent_id FROM locations WHERE id IN ({marks})", tuple(payload["targetAreas"])
                )
            }
            if not parents.issubset(set(payload["targetWilayats"])):
                raise DomainError("supplier_campaign_target_hierarchy_mismatch", 422)

    @staticmethod
    def _validate_creative(con, supplier_id: str, media_id: str, *, required: bool) -> None:
        if not media_id:
            if required:
                raise DomainError("supplier_campaign_creative_required", 422)
            return
        row = con.execute(
            """SELECT id FROM private_media_objects
               WHERE id=? AND owner_kind='supplier' AND owner_id=? AND status='active'
                 AND purpose IN ('supplier_campaign_creative','supplier_campaign_image')
                 AND mime_type IN ('image/jpeg','image/png','image/webp')""",
            (media_id, supplier_id),
        ).fetchone()
        if not row:
            # A not-found response avoids confirming another supplier's media ID.
            raise DomainError("supplier_campaign_creative_not_found", 404)

    def _normalize_supplier_campaign(self, source: dict) -> dict:
        if not isinstance(source, dict):
            raise DomainError("json_object_required", 422)
        starts_at, ends_at = _campaign_window(source.get("startsAt"), source.get("endsAt"))
        minimum_raw = source.get("minimumOrderQuantity")
        minimum = None if minimum_raw in (None, "") else _bounded_int(
            minimum_raw, 1, 1_000_000, "invalid_supplier_campaign_moq"
        )
        contact_mode = clean_text(source.get("contactMode"), 30)
        if contact_mode and contact_mode not in SUPPLIER_CONTACT_MODES:
            raise DomainError("invalid_supplier_campaign_contact", 422)
        contact_value = clean_text(source.get("contactValue"), 180)
        if contact_mode == "whatsapp" and contact_value:
            contact_value = normalize_phone(contact_value)
        elif contact_mode != "whatsapp":
            # Platform CTAs resolve the supplier from the authenticated campaign;
            # arbitrary links and addresses are intentionally not accepted.
            contact_value = ""
        payload = {
            "wholesaleDescriptionAr": clean_text(source.get("wholesaleDescriptionAr"), 1600),
            "wholesaleDescriptionEn": clean_text(source.get("wholesaleDescriptionEn"), 1600),
            "offerAr": clean_text(source.get("offerAr"), 600),
            "offerEn": clean_text(source.get("offerEn"), 600),
            "minimumOrderQuantity": minimum,
            "targetCategories": _clean_id_list(source.get("targetCategories"), "targetCategories"),
            "targetWilayats": _clean_id_list(source.get("targetWilayats"), "targetWilayats"),
            "targetAreas": _clean_id_list(source.get("targetAreas"), "targetAreas"),
            "termsAr": clean_text(source.get("termsAr"), 2500),
            "termsEn": clean_text(source.get("termsEn"), 2500),
            "contactMode": contact_mode,
            "contactValue": contact_value,
            "contactLabelAr": clean_text(source.get("contactLabelAr"), 80),
            "contactLabelEn": clean_text(source.get("contactLabelEn"), 80),
            "creativeMediaId": clean_text(source.get("creativeMediaId"), 90),
            "startsAt": starts_at,
            "endsAt": ends_at,
        }
        return {
            "titleAr": clean_text(source.get("titleAr"), 120),
            "titleEn": clean_text(source.get("titleEn"), 120),
            **payload,
        }

    @staticmethod
    def _campaign_missing_fields(campaign: dict) -> list[str]:
        required = (
            "titleAr", "titleEn", "wholesaleDescriptionAr", "wholesaleDescriptionEn",
            "offerAr", "offerEn", "minimumOrderQuantity", "termsAr", "termsEn",
            "contactMode", "creativeMediaId", "startsAt", "endsAt",
        )
        missing = [field for field in required if campaign.get(field) in (None, "", [])]
        if campaign.get("contactMode") == "whatsapp" and not campaign.get("contactValue"):
            missing.append("contactValue")
        return missing

    @classmethod
    def _campaign_response(cls, row) -> dict:
        stored = loads(row["payload"], {})
        if not isinstance(stored, dict):
            stored = {}
        payload_fields = (
            "wholesaleDescriptionAr", "wholesaleDescriptionEn", "offerAr", "offerEn",
            "minimumOrderQuantity", "targetCategories", "targetWilayats", "targetAreas",
            "termsAr", "termsEn", "contactMode", "contactValue", "contactLabelAr",
            "contactLabelEn", "creativeMediaId",
        )
        campaign = {
            "id": row["id"], "supplierId": row["supplier_id"],
            "titleAr": row["title_ar"], "titleEn": row["title_en"],
            **{field: stored.get(field) for field in payload_fields},
            "startsAt": row["starts_at"], "endsAt": row["ends_at"],
            "status": row["status"], "createdAt": row["created_at"], "updatedAt": row["updated_at"],
        }
        missing = cls._campaign_missing_fields(campaign)
        campaign["completion"] = {"readyForReview": not missing, "missingFields": missing}
        return campaign

    @staticmethod
    def _idempotency_replay(con, actor_id: str, operation: str, key: str, request_hash: str) -> dict | None:
        row = con.execute(
            """SELECT payload_hash,response_json FROM idempotency_records
               WHERE actor_id=? AND operation=? AND idempotency_key=?""",
            (actor_id, operation, key),
        ).fetchone()
        if not row:
            return None
        if row["payload_hash"] != request_hash:
            raise DomainError("idempotency_key_reused", 409)
        response = loads(row["response_json"], {})
        response["duplicate"] = True
        return response

    @staticmethod
    def _record_idempotency(con, actor_id: str, operation: str, key: str, request_hash: str, response: dict) -> None:
        con.execute(
            """INSERT INTO idempotency_records(
                actor_id,operation,idempotency_key,payload_hash,response_json,created_at)
               VALUES(?,?,?,?,?,?)""",
            (actor_id, operation, key, request_hash, dumps(response), now_iso()),
        )

    @staticmethod
    def _campaign_audit(con, actor_id: str, event: str, campaign_id: str, context: dict) -> None:
        con.execute(
            """INSERT INTO security_audit_events(
                id,event_kind,actor_id,subject_kind,subject_id,context_json,created_at)
               VALUES(?,?,?,?,?,?,?)""",
            (new_id("secevt"), event, actor_id, "supplier_campaign", campaign_id, dumps(context), now_iso()),
        )

    def supplier_dashboard(self, actor: dict) -> dict:
        with connect() as con:
            actor = self._require_supplier_actor(con, actor, "supplier_campaign.manage")
            counts = {status: 0 for status in SUPPLIER_CAMPAIGN_STATUSES}
            for row in con.execute(
                "SELECT status,COUNT(*) n FROM supplier_campaigns WHERE supplier_id=? GROUP BY status",
                (actor["supplierId"],),
            ):
                counts[row["status"]] = row["n"]
            lead_count = con.execute(
                """SELECT COUNT(*) n FROM supplier_leads l JOIN supplier_campaigns c ON c.id=l.campaign_id
                   WHERE c.supplier_id=?""",
                (actor["supplierId"],),
            ).fetchone()["n"]
            recent = [
                self._campaign_response(row) for row in con.execute(
                    "SELECT * FROM supplier_campaigns WHERE supplier_id=? ORDER BY updated_at DESC,id LIMIT 10",
                    (actor["supplierId"],),
                )
            ]
            registry = getattr(self, "integration_registry", None) or default_registry()
            integrations = registry.snapshot()
            return {
                "supplier": actor["supplier"],
                "capabilities": {
                    "campaignManage": True, "leadRead": has_permission(actor, "supplier_lead.read", con=con),
                    "whatsAppAvailable": integrations["whatsapp"]["available"],
                },
                "summary": {"campaigns": counts, "leads": lead_count},
                "recentCampaigns": recent,
            }

    def own_supplier_campaigns(self, actor: dict, filters: dict | None = None) -> dict:
        filters = filters or {}
        status = clean_text(filters.get("status"), 30)
        if status and status not in SUPPLIER_CAMPAIGN_STATUSES:
            raise DomainError("invalid_supplier_campaign_status", 422)
        cursor = _bounded_int(filters.get("cursor", 0), 0, 10_000_000, "invalid_cursor")
        limit = _bounded_int(filters.get("limit", 30), 1, 100, "invalid_limit")
        with connect() as con:
            actor = self._require_supplier_actor(con, actor, "supplier_campaign.manage")
            where = "supplier_id=?"
            params: list[Any] = [actor["supplierId"]]
            if status:
                where += " AND status=?"
                params.append(status)
            total = con.execute(f"SELECT COUNT(*) n FROM supplier_campaigns WHERE {where}", tuple(params)).fetchone()["n"]
            rows = con.execute(
                f"SELECT * FROM supplier_campaigns WHERE {where} ORDER BY updated_at DESC,id LIMIT ? OFFSET ?",
                (*params, limit, cursor),
            ).fetchall()
            return {
                "campaigns": [self._campaign_response(row) for row in rows],
                "pagination": {"cursor": cursor, "nextCursor": cursor + len(rows) if cursor + len(rows) < total else None, "total": total},
            }

    def supplier_campaign_detail(self, actor: dict, campaign_id: str) -> dict:
        campaign_id = clean_text(campaign_id, 90, True)
        with connect() as con:
            if actor and actor.get("role") == SUPPLIER_ROLE:
                actor = self._require_supplier_actor(con, actor, "supplier_campaign.manage")
                row = con.execute(
                    "SELECT * FROM supplier_campaigns WHERE id=? AND supplier_id=?",
                    (campaign_id, actor["supplierId"]),
                ).fetchone()
            elif actor and actor.get("role") in ADMIN_ROLES:
                actor = self._require_actor(con, actor, ADMIN_ROLES)
                require_permission(actor, "supplier_campaign.review", con=con)
                row = con.execute("SELECT * FROM supplier_campaigns WHERE id=?", (campaign_id,)).fetchone()
            else:
                raise DomainError("forbidden", 403)
            if not row:
                raise DomainError("supplier_campaign_not_found", 404)
            return {"campaign": self._campaign_response(row)}

    def save_supplier_campaign(self, actor: dict, payload: dict) -> dict:
        if not isinstance(payload, dict):
            raise DomainError("json_object_required", 422)
        campaign_id = clean_text(payload.get("id"), 90)
        idempotency_key = clean_text(payload.get("idempotencyKey"), 120, True)
        expected_updated_at = clean_text(payload.get("expectedUpdatedAt"), 40)
        with connect(immediate=True) as con:
            actor = self._require_supplier_actor(con, actor, "supplier_campaign.manage")
            existing_campaign = None
            if campaign_id:
                # Establish tenant ownership before validating referenced media
                # so foreign campaign IDs never become a media-scope oracle.
                existing_campaign = con.execute(
                    "SELECT * FROM supplier_campaigns WHERE id=? AND supplier_id=?",
                    (campaign_id, actor["supplierId"]),
                ).fetchone()
                if not existing_campaign:
                    raise DomainError("supplier_campaign_not_found", 404)
            normalized = self._normalize_supplier_campaign(payload)
            operation = f"supplier_campaign:{'update:' + campaign_id if campaign_id else 'create:' + actor['supplierId']}"
            request_hash = _campaign_hash({"campaignId": campaign_id, "expectedUpdatedAt": expected_updated_at, "campaign": normalized})
            replay = self._idempotency_replay(con, actor["accountId"], operation, idempotency_key, request_hash)
            if replay:
                return replay
            self._validate_targets(con, normalized)
            self._validate_creative(con, actor["supplierId"], normalized["creativeMediaId"], required=False)
            stamp = now_iso()
            event = "supplier_campaign_created"
            if campaign_id:
                row = existing_campaign
                if row["status"] not in SUPPLIER_CAMPAIGN_EDITABLE_STATUSES:
                    raise DomainError("supplier_campaign_not_editable", 409, {"status": row["status"]})
                if not expected_updated_at:
                    raise DomainError("supplier_campaign_version_required", 428)
                if row["updated_at"] != expected_updated_at:
                    raise DomainError("supplier_campaign_version_conflict", 409, {"currentUpdatedAt": row["updated_at"]})
                con.execute(
                    """UPDATE supplier_campaigns SET title_ar=?,title_en=?,payload=?,status='draft',
                       starts_at=?,ends_at=?,updated_at=? WHERE id=? AND supplier_id=?""",
                    (
                        normalized.pop("titleAr"), normalized.pop("titleEn"), dumps(normalized),
                        normalized["startsAt"], normalized["endsAt"], stamp, campaign_id, actor["supplierId"],
                    ),
                )
                event = "supplier_campaign_updated"
            else:
                campaign_id = new_id("scampaign")
                title_ar = normalized.pop("titleAr")
                title_en = normalized.pop("titleEn")
                con.execute(
                    """INSERT INTO supplier_campaigns(
                        id,supplier_id,title_ar,title_en,payload,status,starts_at,ends_at,created_at,updated_at)
                       VALUES(?,?,?,?,?,'draft',?,?,?,?)""",
                    (
                        campaign_id, actor["supplierId"], title_ar, title_en, dumps(normalized),
                        normalized["startsAt"], normalized["endsAt"], stamp, stamp,
                    ),
                )
            row = con.execute("SELECT * FROM supplier_campaigns WHERE id=?", (campaign_id,)).fetchone()
            campaign = self._campaign_response(row)
            result = {"campaign": campaign, "duplicate": False}
            self._campaign_audit(
                con, actor["accountId"], event, campaign_id,
                {"status": campaign["status"], "readyForReview": campaign["completion"]["readyForReview"]},
            )
            self._record_idempotency(con, actor["accountId"], operation, idempotency_key, request_hash, result)
            return result

    def submit_supplier_campaign(self, actor: dict, campaign_id: str, payload: dict) -> dict:
        campaign_id = clean_text(campaign_id, 90, True)
        payload = payload if isinstance(payload, dict) else {}
        idempotency_key = clean_text(payload.get("idempotencyKey"), 120, True)
        expected_updated_at = clean_text(payload.get("expectedUpdatedAt"), 40, True)
        operation = f"supplier_campaign:submit:{campaign_id}"
        request_hash = _campaign_hash({"campaignId": campaign_id, "expectedUpdatedAt": expected_updated_at})
        with connect(immediate=True) as con:
            actor = self._require_supplier_actor(con, actor, "supplier_campaign.manage")
            replay = self._idempotency_replay(con, actor["accountId"], operation, idempotency_key, request_hash)
            if replay:
                return replay
            row = con.execute(
                "SELECT * FROM supplier_campaigns WHERE id=? AND supplier_id=?",
                (campaign_id, actor["supplierId"]),
            ).fetchone()
            if not row:
                raise DomainError("supplier_campaign_not_found", 404)
            if row["status"] not in SUPPLIER_CAMPAIGN_EDITABLE_STATUSES:
                raise DomainError("supplier_campaign_not_submittable", 409, {"status": row["status"]})
            if row["updated_at"] != expected_updated_at:
                raise DomainError("supplier_campaign_version_conflict", 409, {"currentUpdatedAt": row["updated_at"]})
            campaign = self._campaign_response(row)
            missing = self._campaign_missing_fields(campaign)
            if missing:
                raise DomainError("supplier_campaign_incomplete", 422, {"missingFields": missing})
            self._validate_creative(con, actor["supplierId"], campaign["creativeMediaId"], required=True)
            if datetime.fromisoformat(campaign["endsAt"]) <= datetime.now(UTC):
                raise DomainError("supplier_campaign_window_expired", 409)
            if campaign["contactMode"] == "whatsapp":
                registry = getattr(self, "integration_registry", None) or default_registry()
                whatsapp = registry.snapshot()["whatsapp"]
                if not whatsapp["available"]:
                    raise DomainError("supplier_whatsapp_unavailable", 409)
            stamp = now_iso()
            con.execute(
                "UPDATE supplier_campaigns SET status='pending_review',updated_at=? WHERE id=? AND supplier_id=?",
                (stamp, campaign_id, actor["supplierId"]),
            )
            self._insert_notification(
                con,
                "admin",
                "admin",
                "حملة مورد للمراجعة",
                "Supplier campaign pending review",
                campaign["titleAr"],
                campaign["titleEn"],
                f"admin:supplier-campaign:{campaign_id}",
                True,
                f"supplier-campaign:{campaign_id}:pending:{stamp}",
                priority=85,
            )
            updated = self._campaign_response(con.execute("SELECT * FROM supplier_campaigns WHERE id=?", (campaign_id,)).fetchone())
            result = {"campaign": updated, "requiresAdminApproval": True, "duplicate": False}
            self._campaign_audit(
                con, actor["accountId"], "supplier_campaign_submitted", campaign_id,
                {"status": "pending_review"},
            )
            self._record_idempotency(con, actor["accountId"], operation, idempotency_key, request_hash, result)
            return result

    def supplier_leads(self, actor: dict, filters: dict | None = None) -> dict:
        filters = filters or {}
        campaign_id = clean_text(filters.get("campaignId"), 90)
        cursor = _bounded_int(filters.get("cursor", 0), 0, 10_000_000, "invalid_cursor")
        limit = _bounded_int(filters.get("limit", 50), 1, 100, "invalid_limit")
        with connect() as con:
            actor = self._require_supplier_actor(con, actor, "supplier_lead.read")
            where = "c.supplier_id=?"
            params: list[Any] = [actor["supplierId"]]
            if campaign_id:
                owned = con.execute(
                    "SELECT 1 FROM supplier_campaigns WHERE id=? AND supplier_id=?",
                    (campaign_id, actor["supplierId"]),
                ).fetchone()
                if not owned:
                    raise DomainError("supplier_campaign_not_found", 404)
                where += " AND c.id=?"
                params.append(campaign_id)
            total = con.execute(
                f"SELECT COUNT(*) n FROM supplier_leads l JOIN supplier_campaigns c ON c.id=l.campaign_id WHERE {where}",
                tuple(params),
            ).fetchone()["n"]
            rows = con.execute(
                f"""SELECT l.id,l.campaign_id,l.merchant_id,l.action_kind,l.note,l.created_at,
                       m.name_ar merchant_name_ar,m.name_en merchant_name_en,m.verified merchant_verified
                    FROM supplier_leads l JOIN supplier_campaigns c ON c.id=l.campaign_id
                    JOIN merchants m ON m.id=l.merchant_id
                    WHERE {where} ORDER BY l.created_at DESC,l.id LIMIT ? OFFSET ?""",
                (*params, limit, cursor),
            ).fetchall()
            leads = [{
                "id": row["id"], "campaignId": row["campaign_id"], "merchantId": row["merchant_id"],
                "action": row["action_kind"], "note": row["note"], "createdAt": row["created_at"],
                "merchant": {
                    "nameAr": row["merchant_name_ar"], "nameEn": row["merchant_name_en"],
                    "verified": bool(row["merchant_verified"]),
                },
            } for row in rows]
            return {
                "leads": leads,
                "pagination": {"cursor": cursor, "nextCursor": cursor + len(leads) if cursor + len(leads) < total else None, "total": total},
            }

    def resolve_supplier_campaign_creative(self, actor: dict, campaign_id: str) -> dict:
        """Resolve an approved B2B creative after checking the current viewer.

        The returned path is for the HTTP handler only and must never be emitted
        in a JSON response.  Authorization is re-evaluated on every request.
        """
        campaign_id = clean_text(campaign_id, 90, True)
        with connect() as con:
            row = con.execute(
                """SELECT c.*,s.status supplier_status FROM supplier_campaigns c
                   JOIN suppliers s ON s.id=c.supplier_id WHERE c.id=?""",
                (campaign_id,),
            ).fetchone()
            if not row:
                raise DomainError("supplier_campaign_not_found", 404)
            if actor and actor.get("role") == SUPPLIER_ROLE:
                supplier_actor = self._require_supplier_actor(con, actor, "supplier_campaign.manage")
                if row["supplier_id"] != supplier_actor["supplierId"]:
                    raise DomainError("supplier_campaign_not_found", 404)
            elif actor and actor.get("role") in MERCHANT_ROLES:
                merchant_actor = self._require_actor(con, actor, MERCHANT_ROLES)
                require_permission(merchant_actor, "supplier_hub.read", merchant_id=merchant_actor["merchantId"], con=con)
                plan = self._active_plan(con, merchant_actor["merchantId"])
                if not plan["entitlements"].get("supplierHub"):
                    raise DomainError("supplier_hub_not_in_plan", 403)
                if campaign_id not in {item["id"] for item in self._supplier_campaign_rows(con, merchant_actor)}:
                    raise DomainError("supplier_campaign_not_found", 404)
            elif actor and actor.get("role") in ADMIN_ROLES:
                admin_actor = self._require_actor(con, actor, ADMIN_ROLES)
                require_permission(admin_actor, "supplier_campaign.review", con=con)
            else:
                raise DomainError("forbidden", 403)
            campaign = self._campaign_response(row)
            media_id = campaign.get("creativeMediaId") or ""
            media = con.execute(
                """SELECT * FROM private_media_objects WHERE id=? AND owner_kind='supplier'
                   AND owner_id=? AND status='active' AND purpose IN ('supplier_campaign_creative','supplier_campaign_image')
                   AND mime_type IN ('image/jpeg','image/png','image/webp')""",
                (media_id, row["supplier_id"]),
            ).fetchone()
            if not media:
                raise DomainError("supplier_campaign_creative_not_found", 404)
            candidate = _storage_candidate(media["storage_key"])
            return {
                "path": candidate, "mimeType": media["mime_type"], "byteSize": media["byte_size"],
                "etag": media["sha256_hex"],
            }
