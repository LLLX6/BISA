"""Security-hardened marketplace application services for BISA.

This module deliberately sits above :mod:`bisa_domain`.  It preserves the
small foundation's database and response shapes while revalidating tenant,
publication, pricing, inventory and authorization invariants inside the same
transaction as every mutation.  ``BisaApplication`` composes this mixin with
the foundation service so the HTTP layer can switch without a flag day.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

import bisa_config
from bisa_config import (
    MUSCAT_MAP_BOUNDS,
    PRODUCT_MAX_BAISA,
    PRODUCT_MIN_BAISA,
    coordinates_in_muscat,
)
from bisa_domain import (
    DomainError,
    active_plan,
    authenticate,
    clean_text,
    connect,
    dumps,
    loads,
    new_id,
    normalize_phone,
    now_iso,
    omr,
    resolve_entitlements,
    settings,
)
from bisa_security import require_permission


MERCHANT_ROLES = {"merchant_owner", "merchant_manager", "merchant_staff"}
ADMIN_ROLES = {
    "support_admin", "catalog_moderator", "merchant_reviewer", "finance",
    "advertising_manager", "admin", "super_admin",
}
PUBLIC_PRODUCT_WHERE = """
 p.status='approved' AND p.active=1 AND p.moderation_status='approved'
 AND p.archived_at='' AND m.status='approved' AND m.active=1
 AND b.status='approved' AND b.active=1 AND b.public_visible=1 AND i.active=1
 AND NOT (i.freshness_status='stale' AND i.stale_enforcement IN('hide_stale','pause_stale'))
"""
PUBLIC_BRANCH_WHERE = """
 m.status='approved' AND m.active=1 AND b.status='approved' AND b.active=1 AND b.public_visible=1
"""
ORDER_TRANSITIONS = {
    "pending_store_confirmation": {"accepted", "rejected", "cancelled", "expired"},
    "accepted": {"preparing", "cancelled"},
    "preparing": {"ready_for_pickup", "out_for_delivery", "cancelled"},
    "ready_for_pickup": {"completed"},
    "out_for_delivery": {"completed"},
    "completed": set(),
    "rejected": set(),
    "cancelled": set(),
    "expired": set(),
}
REPORT_REASONS = {
    "incorrect_price", "out_of_stock", "misleading_image", "unsafe_item",
    "prohibited_item", "other",
}
TRACKED_EVENTS = {
    "store_view", "product_view", "save", "favorite", "share",
    "directions_click", "contact_click", "add_to_cart", "checkout", "order",
    "search", "bundle_view", "ad_impression", "ad_click",
    "action_prompt_shown", "action_prompt_opened", "action_prompt_snoozed",
    "action_prompt_completed",
}
ACTION_PROMPT_EVENTS = {
    "action_prompt_shown", "action_prompt_opened", "action_prompt_snoozed",
    "action_prompt_completed",
}
AD_EVENT_NAMES = {"ad_impression": "impression", "ad_click": "click"}
OPAQUE_EVENT_ID = re.compile(r"^[A-Za-z0-9._:-]{8,90}$")
ADMIN_DEFAULT_PERMISSIONS = {
    "support_admin": {
        "overview.view", "merchant.read", "order.read", "support.manage",
        "report.read", "notification.manage",
    },
    "catalog_moderator": {
        "overview.view", "catalog.read", "catalog.moderate", "report.read",
        "report.manage",
    },
    "merchant_reviewer": {
        "overview.view", "merchant.read", "merchant.review",
    },
    "finance": {
        "overview.view", "plan.read",
    },
    "advertising_manager": {
        "overview.view", "ad.manage", "supplier.manage", "supplier_campaign.review",
    },
    "admin": {
        "overview.view", "merchant.read", "merchant.review", "merchant.manage",
        "catalog.read", "catalog.moderate", "category.manage", "order.read",
        "inventory.read", "bundle.manage", "plan.read", "ad.manage",
        "supplier.manage", "supplier_campaign.review", "report.read", "report.manage", "settings.read", "settings.manage",
        "plan.manage", "roles.manage", "notification.manage", "audit.read", "location.read", "location.manage",
    },
    "super_admin": {"*"},
}
ADMIN_RESOURCE_MAP = {
    "applications": ("merchant_applications", "merchant.read", "submitted_at"),
    "merchant_applications": ("merchant_applications", "merchant.read", "submitted_at"),
    "merchants": ("merchants", "merchant.read", "updated_at"),
    "branches": ("store_branches", "merchant.read", "updated_at"),
    "products": ("products", "catalog.read", "updated_at"),
    "categories": ("product_categories", "catalog.read", "sort_order"),
    "orders": ("orders", "order.read", "created_at"),
    "bundles": ("bundles", "catalog.read", "updated_at"),
    "plans": ("subscription_plans", "plan.read", "sort_order"),
    "ads": ("ad_campaigns", "ad.manage", "created_at"),
    "suppliers": ("suppliers", "supplier_campaign.review", "updated_at"),
    "supplier_campaigns": ("supplier_campaigns", "supplier_campaign.review", "updated_at"),
    "reports": ("product_reports", "report.read", "created_at"),
    "audit": ("admin_audit_logs", "audit.read", "created_at"),
    "inventory_audits": ("inventory_audits", "inventory.read", "created_at"),
    "inventory": ("inventory_audits", "inventory.read", "created_at"),
    "locations": ("locations", "location.read", "sort_order"),
    "advertising": ("ad_campaigns", "ad.manage", "created_at"),
    "notifications": ("notifications", "notification.manage", "created_at"),
    "settings": ("platform_settings", "settings.read", "updated_at"),
}


def _strict_baisa(value: Any, *, product: bool = False) -> int:
    """Convert exact OMR input without silently rounding sub-baisa values."""
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise DomainError("valid_price_required", 422) from exc
    if not amount.is_finite() or amount != amount.quantize(Decimal("0.001")):
        raise DomainError("price_precision_invalid", 422)
    baisa = int(amount * 1000)
    if product and not PRODUCT_MIN_BAISA <= baisa <= PRODUCT_MAX_BAISA:
        raise DomainError(
            "product_price_out_of_range", 422,
            {"minimum": omr(PRODUCT_MIN_BAISA), "maximum": omr(PRODUCT_MAX_BAISA)},
        )
    if not product and baisa < 0:
        raise DomainError("valid_price_required", 422)
    return baisa


def _bounded_int(value: Any, minimum: int, maximum: int, code: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise DomainError(code, 422) from exc
    if number < minimum or number > maximum:
        raise DomainError(code, 422, {"minimum": minimum, "maximum": maximum})
    return number


def _payload_hash(value: Any) -> str:
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _actor_hash(actor: dict | None) -> str:
    if not actor:
        return ""
    return hashlib.sha256(f"bisa:{actor.get('accountId', '')}".encode()).hexdigest()


def _parse_cursor(value: Any) -> int:
    if value in (None, ""):
        return 0
    return _bounded_int(value, 0, 10_000_000, "invalid_cursor")


def _iso_window(starts_at: Any, ends_at: Any) -> tuple[str, str]:
    start = clean_text(starts_at, 40)
    end = clean_text(ends_at, 40)
    parsed = []
    for value in (start, end):
        if not value:
            parsed.append(None)
            continue
        try:
            moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise DomainError("invalid_date_window", 422) from exc
        if moment.tzinfo is None:
            raise DomainError("timezone_required", 422)
        parsed.append(moment.astimezone(UTC))
    if parsed[0] and parsed[1] and parsed[1] <= parsed[0]:
        raise DomainError("invalid_date_window", 422)
    return (
        parsed[0].isoformat() if parsed[0] else "",
        parsed[1].isoformat() if parsed[1] else "",
    )


def _distance_km(lat1: float | None, lon1: float | None, lat2: float | None, lon2: float | None) -> float | None:
    if None in (lat1, lon1, lat2, lon2):
        return None
    radius = 6371.0088
    phi1, phi2 = math.radians(float(lat1)), math.radians(float(lat2))
    dphi = math.radians(float(lat2) - float(lat1))
    dlambda = math.radians(float(lon2) - float(lon1))
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return round(radius * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a)), 2)


def _is_open(hours_json: str, moment: datetime | None = None) -> bool | None:
    """Interpret a small, explicit hours shape; unknown schedules stay unknown."""
    hours = loads(hours_json, {})
    if not isinstance(hours, dict) or not hours:
        return None
    moment = moment or datetime.now(UTC) + timedelta(hours=4)
    day_keys = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
    slots = hours.get(day_keys[moment.weekday()])
    if slots is None:
        slots = hours.get(str(moment.weekday()))
    if slots in (None, []):
        return False
    if slots == "24h":
        return True
    if isinstance(slots, dict):
        slots = [slots]
    if not isinstance(slots, list):
        return None
    current = moment.hour * 60 + moment.minute
    for slot in slots:
        if not isinstance(slot, dict):
            continue
        try:
            start_h, start_m = map(int, str(slot["open"]).split(":"))
            end_h, end_m = map(int, str(slot["close"]).split(":"))
        except (KeyError, TypeError, ValueError):
            continue
        start, end = start_h * 60 + start_m, end_h * 60 + end_m
        if start <= end and start <= current < end:
            return True
        if start > end and (current >= start or current < end):
            return True
    return False


class MarketplaceMixin:
    """Safe commerce operations mixed into the foundation ``BisaService``."""

    # ---------- authorization and tenancy ----------

    def _active_plan(self, con, merchant_id: str) -> dict:
        try:
            plan = active_plan(con, merchant_id)
        except DomainError as exc:
            if exc.code != "active_plan_required":
                raise
            pending = con.execute(
                """SELECT p.*,s.status subscription_status FROM merchant_subscriptions s
                   JOIN subscription_plans p ON p.id=s.plan_id
                   WHERE s.merchant_id=? AND s.status='pending_payment'
                   ORDER BY s.created_at DESC LIMIT 1""",
                (merchant_id,),
            ).fetchone()
            if not pending:
                raise
            # A reviewed merchant must be able to enter the workspace and see
            # the honest payment state, but a pending paid plan grants no
            # commercial entitlement until a real payment adapter activates it.
            plan = dict(pending)
            plan["entitlements"] = {
                "products": 0, "branches": 0, "staff": 0, "bundles": 0,
                "mediaPerProduct": 0, "mediaTotalMb": 0,
                "analytics": "none", "bulkImport": False,
                "supplierHub": False, "canBuyAds": False,
                "includedAdCredits": 0, "includedBoosts": 0,
            }
            return plan
        if plan["id"] == "early_trial":
            basic = con.execute(
                "SELECT entitlements FROM subscription_plans WHERE id='basic_3m' AND active=1"
            ).fetchone()
            if not basic:
                raise DomainError("inherited_plan_unavailable", 409)
            # Trial is deliberately identical to Basic. Stale copied overrides in
            # legacy JSON must never diverge after an administrator edits Basic.
            plan["entitlements"] = resolve_entitlements(con, basic["entitlements"])
        plan["subscription_status"] = "active"
        return plan

    def authenticate_secure(self, token: str) -> dict | None:
        actor = authenticate(token)
        if not actor:
            return None
        try:
            with connect() as con:
                return self._require_actor(con, actor)
        except DomainError:
            return None

    def revalidate_actor(self, actor: dict, *roles: str) -> dict:
        with connect() as con:
            return self._require_actor(con, actor, roles or None)

    def _require_actor(
        self,
        con,
        actor: dict | None,
        roles: Iterable[str] | None = None,
        *,
        merchant_must_be_approved: bool = True,
    ) -> dict:
        if not actor or not actor.get("accountId") or not actor.get("role"):
            raise DomainError("authentication_required", 401)
        account = con.execute(
            "SELECT id,name,status FROM accounts WHERE id=?", (actor["accountId"],)
        ).fetchone()
        if not account or account["status"] != "active":
            raise DomainError("session_not_authorized", 401)
        merchant_id = str(actor.get("merchantId") or "")
        supplier_id = str(
            actor.get("supplierId")
            or (merchant_id if actor["role"] == "supplier_advertiser" else "")
        )
        binding_id = supplier_id if actor["role"] == "supplier_advertiser" else merchant_id
        binding = con.execute(
            """SELECT 1 FROM account_roles
               WHERE account_id=? AND role=? AND merchant_id=? AND active=1""",
            (actor["accountId"], actor["role"], binding_id),
        ).fetchone()
        if not binding:
            raise DomainError("session_not_authorized", 401)
        if roles and actor["role"] not in set(roles):
            raise DomainError("forbidden", 403)
        if actor["role"] in MERCHANT_ROLES:
            merchant = con.execute(
                "SELECT id,owner_account_id,status,active FROM merchants WHERE id=?", (merchant_id,)
            ).fetchone()
            if not merchant:
                raise DomainError("merchant_not_found", 404)
            if merchant_must_be_approved and (
                merchant["status"] != "approved" or not merchant["active"]
            ):
                raise DomainError("merchant_not_active", 403)
            if actor["role"] == "merchant_owner":
                if merchant["owner_account_id"] != actor["accountId"]:
                    raise DomainError("forbidden", 403)
            else:
                member = con.execute(
                    """SELECT status,role FROM merchant_members
                       WHERE merchant_id=? AND account_id=?""",
                    (merchant_id, actor["accountId"]),
                ).fetchone()
                if not member or member["status"] != "active" or member["role"] != actor["role"]:
                    raise DomainError("forbidden", 403)
        if actor["role"] == "supplier_advertiser":
            supplier = con.execute(
                """SELECT 1 FROM suppliers s JOIN supplier_members sm ON sm.supplier_id=s.id
                   WHERE s.id=? AND s.status='approved' AND sm.account_id=?
                     AND sm.role='supplier_advertiser' AND sm.status='active'""",
                (supplier_id, actor["accountId"]),
            ).fetchone()
            if not supplier:
                raise DomainError("session_not_authorized", 401)
        normalized = {
            "accountId": account["id"], "name": account["name"],
            "role": actor["role"], "merchantId": merchant_id,
        }
        if supplier_id:
            normalized["supplierId"] = supplier_id
        return normalized

    def _admin_permissions(self, con, actor: dict) -> set[str]:
        actor = self._require_actor(con, actor, ADMIN_ROLES)
        configured = {
            row["permission"] for row in con.execute(
                "SELECT permission FROM admin_role_permissions WHERE role=?", (actor["role"],)
            )
        }
        permissions = configured or set(ADMIN_DEFAULT_PERMISSIONS.get(actor["role"], set()))
        for row in con.execute(
            "SELECT permission,allowed FROM account_permission_overrides WHERE account_id=?",
            (actor["accountId"],),
        ):
            if row["allowed"]:
                permissions.add(row["permission"])
            else:
                permissions.discard(row["permission"])
        return permissions

    def _require_admin_permission(self, con, actor: dict, permission: str) -> dict:
        actor = self._require_actor(con, actor, ADMIN_ROLES)
        permissions = self._admin_permissions(con, actor)
        if "*" not in permissions and permission not in permissions:
            raise DomainError("admin_permission_required", 403, {"permission": permission})
        return actor

    # ---------- public catalog ----------

    def public_bootstrap(self, actor=None) -> dict:
        safe_actor = None
        if actor:
            safe_actor = self.revalidate_actor(actor)
        data = super().public_bootstrap(safe_actor)
        # The foundation area query checks only branches. Re-filter by an active
        # approved merchant as well so a suspended merchant cannot keep an area public.
        with connect() as con:
            public_branches = list(con.execute(
                f"""SELECT b.id,b.area_id,b.merchant_id FROM store_branches b
                    JOIN merchants m ON m.id=b.merchant_id
                    WHERE {PUBLIC_BRANCH_WHERE}"""
            ))
            visible_branch_ids = {row["id"] for row in public_branches}
            visible_merchant_ids = {row["merchant_id"] for row in public_branches}
            visible_areas = {row["area_id"] for row in public_branches if row["area_id"]}
            hidden_inventory = {
                (row["product_id"], row["branch_id"])
                for row in con.execute(
                    """SELECT product_id,branch_id FROM product_branch_inventory
                       WHERE freshness_status='stale'
                         AND stale_enforcement IN('hide_stale','pause_stale')"""
                )
            }
            public_entitlements = {
                "products", "branches", "staff", "bundles", "mediaPerProduct",
                "mediaTotalMb", "analytics", "bulkImport", "supplierHub",
                "canBuyAds", "includedAdCredits", "includedBoosts",
                "boostDurationDays", "supportTier", "gracePeriodDays",
            }
            data["plans"] = []
            for row in con.execute(
                """SELECT id,name_ar,name_en,price_baisa,duration_days,entitlements
                   FROM subscription_plans WHERE active=1 ORDER BY sort_order,id"""
            ):
                entitlements = resolve_entitlements(con, row["entitlements"])
                data["plans"].append({
                    "id": row["id"], "name_ar": row["name_ar"], "name_en": row["name_en"],
                    "price": omr(row["price_baisa"]), "duration_days": row["duration_days"],
                    "entitlements": {
                        key: value for key, value in entitlements.items()
                        if key in public_entitlements
                    },
                })
            map_provider = clean_text(settings(con).get("mapProvider"), 40) or "disabled"
            map_ready = map_provider == "openstreetmap"
            data["capabilities"] = {
                **(data.get("capabilities") or {}),
                "maps": {
                    "available": map_ready,
                    "configured": map_ready,
                    "status": "ready" if map_ready else "unavailable",
                    "provider": map_provider,
                    "tileUrlTemplate": (
                        "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
                        if map_ready else ""
                    ),
                    "attributionHtml": (
                        '&copy; <a href="https://www.openstreetmap.org/copyright" '
                        'target="_blank" rel="noopener noreferrer">OpenStreetMap contributors</a>'
                        if map_ready else ""
                    ),
                    "reportIssueUrl": "https://www.openstreetmap.org/fixthemap",
                    "muscatBounds": [list(point) for point in MUSCAT_MAP_BOUNDS],
                },
            }
        data["locations"] = [
            item for item in data.get("locations", [])
            if item.get("kind") != "area" or item.get("id") in visible_areas
        ]
        data["onboardingLocations"] = []
        if safe_actor and safe_actor["role"] == "shopper":
            # Browsing keeps the product rule "hide empty areas", while an
            # authenticated applicant must still be able to register the first
            # approved branch in a valid master-data area.
            with connect() as con:
                data["onboardingLocations"] = [dict(row) for row in con.execute(
                    """SELECT * FROM locations WHERE active=1
                       AND kind IN ('governorate','wilayat','area')
                       ORDER BY kind,sort_order,name_en"""
                )]
        data["stores"] = [
            item for item in data.get("stores", [])
            if item.get("branch_id") in visible_branch_ids
        ]
        data["products"] = [
            item for item in data.get("products", [])
            if item.get("branch_id") in visible_branch_ids
            and (item.get("id"), item.get("branch_id")) not in hidden_inventory
            and item.get("moderation_status", "approved") == "approved"
            and not item.get("archived_at")
        ]
        stamp = now_iso()
        data["bundles"] = [
            item for item in data.get("bundles", [])
            if item.get("branch_id") in visible_branch_ids
            and item.get("moderation_status", "approved") == "approved"
            and (not item.get("starts_at") or item["starts_at"] <= stamp)
            and (not item.get("ends_at") or item["ends_at"] > stamp)
        ]
        data["advertisements"] = [
            item for item in data.get("advertisements", [])
            if item.get("owner_id") in visible_merchant_ids
        ]
        data["favorites"] = []
        if safe_actor and safe_actor["role"] == "shopper":
            with connect() as con:
                data["favorites"] = [
                    {"entityKind": row["entity_kind"], "entityId": row["entity_id"]}
                    for row in con.execute(
                        """SELECT entity_kind,entity_id FROM favorites
                           WHERE account_id=? ORDER BY created_at DESC LIMIT 500""",
                        (safe_actor["accountId"],),
                    )
                ]
            for order in data.get("orders", []):
                order["allowedActions"] = self._order_allowed_actions(safe_actor, order)
        if safe_actor and safe_actor["role"] in ADMIN_ROLES:
            with connect() as con:
                permissions = self._admin_permissions(con, safe_actor)
                if "*" in permissions or "notification.manage" in permissions:
                    data["notifications"] = [dict(row) for row in con.execute(
                        """SELECT * FROM notifications WHERE target_kind='admin' AND target_id IN('admin',?)
                           ORDER BY requires_action DESC,priority DESC,created_at DESC LIMIT 100""",
                        (safe_actor["accountId"],),
                    )]
        elif safe_actor and safe_actor["role"] == "supplier_advertiser":
            # B2B notices are supplier-scoped.  The same account may also be a
            # shopper, so an account target would leak role-private information.
            with connect() as con:
                data["notifications"] = [dict(row) for row in con.execute(
                    """SELECT * FROM notifications
                       WHERE target_kind='supplier' AND target_id=?
                       ORDER BY requires_action DESC,priority DESC,created_at DESC LIMIT 100""",
                    (safe_actor["supplierId"],),
                )]
        data["contractVersion"] = "marketplace_v1"
        return data

    def discovery(self, filters: dict | None = None, actor: dict | None = None) -> dict:
        filters = filters or {}
        query = clean_text(filters.get("query"), 80)
        area_id = clean_text(filters.get("areaId"), 90)
        wilayah_id = clean_text(filters.get("wilayahId"), 90)
        category_id = clean_text(filters.get("categoryId"), 80)
        branch_id = clean_text(filters.get("branchId"), 90)
        sort = clean_text(filters.get("sort"), 30) or "newest"
        if sort not in {"nearest", "newest", "lowest_price", "popular"}:
            raise DomainError("invalid_sort", 422)
        limit = _bounded_int(filters.get("limit", 24), 1, 60, "invalid_limit")
        offset = _parse_cursor(filters.get("cursor"))
        min_price = _strict_baisa(filters.get("minPrice", "0.000"))
        max_price = _strict_baisa(filters.get("maxPrice", "999999.999"))
        if min_price > max_price:
            raise DomainError("invalid_price_range", 422)
        latitude = filters.get("latitude")
        longitude = filters.get("longitude")
        if (latitude is None) != (longitude is None):
            raise DomainError("complete_location_required", 422)
        if latitude is not None:
            try:
                latitude, longitude = float(latitude), float(longitude)
            except (TypeError, ValueError) as exc:
                raise DomainError("valid_location_required", 422) from exc
            if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
                raise DomainError("valid_location_required", 422)
        distance_limit = None
        if filters.get("maxDistanceKm") not in (None, ""):
            try:
                distance_limit = float(filters["maxDistanceKm"])
            except (TypeError, ValueError) as exc:
                raise DomainError("invalid_distance", 422) from exc
            if not math.isfinite(distance_limit) or distance_limit <= 0:
                raise DomainError("invalid_distance", 422)

        clauses = [PUBLIC_PRODUCT_WHERE, "p.price_baisa BETWEEN ? AND ?"]
        args: list[Any] = [min_price, max_price]
        if query:
            clauses.append("(p.name_ar LIKE ? OR p.name_en LIKE ? OR m.name_ar LIKE ? OR m.name_en LIKE ? OR c.name_ar LIKE ? OR c.name_en LIKE ?)")
            term = f"%{query}%"
            args.extend([term] * 6)
        if area_id:
            clauses.append("b.area_id=?")
            args.append(area_id)
        if wilayah_id:
            clauses.append("b.wilayah_id=?")
            args.append(wilayah_id)
        if category_id:
            clauses.append("p.category_id=?")
            args.append(category_id)
        if branch_id:
            clauses.append("b.id=?")
            args.append(branch_id)
        if filters.get("verified"):
            clauses.append("m.verified=1")
        if filters.get("inStock"):
            clauses.append("((i.stock_mode='tracked' AND i.quantity>0) OR (i.stock_mode!='tracked' AND i.availability IN('in_stock','low_stock')))")
        if filters.get("pickup"):
            clauses.append("f.pickup_enabled=1")
        if filters.get("officeDelivery"):
            clauses.append("f.office_enabled=1")
        if filters.get("homeDelivery"):
            clauses.append("f.home_enabled=1")
        if filters.get("freeDelivery"):
            clauses.append("((f.office_enabled=1 AND f.office_free_threshold_baisa>0) OR (f.home_enabled=1 AND f.home_free_threshold_baisa>0))")

        order_sql = {
            "newest": "p.updated_at DESC,p.id",
            "lowest_price": "p.price_baisa,p.id",
            "popular": "event_count DESC,p.updated_at DESC,p.id",
            # nearest is finalized in Python because stock SQLite has no trig extension.
            "nearest": "p.updated_at DESC,p.id",
        }[sort]
        has_python_filters = bool(filters.get("openNow")) or filters.get("maxDistanceKm") not in (None, "")
        if sort == "nearest":
            fetch_limit, sql_offset = 500, 0
        elif has_python_filters:
            fetch_limit, sql_offset = min(500, limit * 8 + 1), offset
        else:
            fetch_limit, sql_offset = limit + 1, offset
        with connect(immediate=bool(query)) as con:
            rows = con.execute(
                f"""SELECT p.id,p.merchant_id,p.category_id,p.name_ar,p.name_en,
                    p.description_ar,p.description_en,p.price_baisa,p.unit_text,p.images_json,p.updated_at,
                    m.name_ar merchant_name_ar,m.name_en merchant_name_en,m.verified,
                    b.id branch_id,b.name_ar branch_name_ar,b.name_en branch_name_en,b.area_id,b.wilayah_id,
                    b.address_text,b.latitude,b.longitude,b.hours_json,
                    i.stock_mode,i.quantity,i.availability,i.last_stock_verified_at,i.stale_at,
                f.pickup_enabled,f.office_enabled,f.office_fee_baisa,f.office_free_threshold_baisa,
                    f.home_enabled,f.home_fee_baisa,f.home_free_threshold_baisa,f.eta_text,
                    (SELECT COUNT(*) FROM analytics_events e WHERE e.entity_kind='product' AND e.entity_id=p.id AND e.event_type='product_view') event_count
                    FROM products p JOIN merchants m ON m.id=p.merchant_id
                    JOIN product_branch_inventory i ON i.product_id=p.id
                    JOIN store_branches b ON b.id=i.branch_id AND b.merchant_id=p.merchant_id
                    JOIN product_categories c ON c.id=p.category_id
                    LEFT JOIN fulfillment_profiles f ON f.branch_id=b.id
                    WHERE {' AND '.join(f'({clause.strip()})' for clause in clauses)}
                    ORDER BY {order_sql} LIMIT ? OFFSET ?""",
                (*args, fetch_limit, sql_offset),
            ).fetchall()
            products = []
            for row in rows:
                item = dict(row)
                item["price"] = omr(item.pop("price_baisa"))
                item["images"] = loads(item.pop("images_json"), [])
                item["openNow"] = _is_open(item.pop("hours_json"))
                item["distanceKm"] = _distance_km(latitude, longitude, item["latitude"], item["longitude"])
                if filters.get("openNow") and item["openNow"] is not True:
                    continue
                if distance_limit is not None:
                    if item["distanceKm"] is None or item["distanceKm"] > distance_limit:
                        continue
                products.append(item)
            if sort == "nearest":
                products.sort(key=lambda item: (item["distanceKm"] is None, item["distanceKm"] or 0, item["id"]))
            page = products[offset:offset + limit + 1] if sort == "nearest" else products[:limit + 1]
            has_more = len(page) > limit
            page = page[:limit]
            if query:
                con.execute(
                    """INSERT INTO search_events(id,actor_hash,query_normalized,result_count,wilayah_id,area_id,filters_json,created_at)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (new_id("search"), _actor_hash(actor), query.casefold(), len(page), wilayah_id, area_id,
                     dumps({key: value for key, value in filters.items() if key not in {"latitude", "longitude"}}), now_iso()),
                )
        stores = self._discovery_stores(filters, latitude, longitude, limit)
        return {
            "products": page,
            "stores": stores,
            "pagination": {"cursor": str(offset), "nextCursor": str(offset + limit) if has_more else None, "limit": limit, "hasMore": has_more},
            "filters": {"areaId": area_id, "wilayahId": wilayah_id, "categoryId": category_id, "sort": sort},
        }

    def _discovery_stores(self, filters: dict, latitude, longitude, limit: int) -> list[dict]:
        query = clean_text(filters.get("query"), 80)
        clauses = [PUBLIC_BRANCH_WHERE]
        args: list[Any] = []
        if query:
            clauses.append(
                """(m.name_ar LIKE ? OR m.name_en LIKE ? OR b.name_ar LIKE ? OR b.name_en LIKE ?
                    OR EXISTS(SELECT 1 FROM products px
                        JOIN product_branch_inventory ix ON ix.product_id=px.id
                        JOIN product_categories cx ON cx.id=px.category_id
                        WHERE px.merchant_id=m.id AND ix.branch_id=b.id
                          AND px.status='approved' AND px.active=1
                          AND px.moderation_status='approved' AND px.archived_at=''
                          AND ix.active=1
                          AND NOT (ix.freshness_status='stale'
                                   AND ix.stale_enforcement IN('hide_stale','pause_stale'))
                          AND (px.name_ar LIKE ? OR px.name_en LIKE ?
                               OR cx.name_ar LIKE ? OR cx.name_en LIKE ?)))"""
            )
            args.extend([f"%{query}%"] * 8)
        for key, column in (("areaId", "b.area_id"), ("wilayahId", "b.wilayah_id"), ("branchId", "b.id")):
            value = clean_text(filters.get(key), 90)
            if value:
                clauses.append(f"{column}=?")
                args.append(value)
        category_id = clean_text(filters.get("categoryId"), 80)
        if category_id:
            clauses.append(
                """EXISTS(SELECT 1 FROM products px
                    JOIN product_branch_inventory ix ON ix.product_id=px.id
                    WHERE px.merchant_id=m.id AND ix.branch_id=b.id AND px.category_id=?
                      AND px.status='approved' AND px.active=1
                      AND px.moderation_status='approved' AND px.archived_at=''
                      AND ix.active=1
                      AND NOT (ix.freshness_status='stale'
                               AND ix.stale_enforcement IN('hide_stale','pause_stale')))"""
            )
            args.append(category_id)
        if filters.get("verified"):
            clauses.append("m.verified=1")
        if filters.get("pickup"):
            clauses.append("f.pickup_enabled=1")
        if filters.get("officeDelivery"):
            clauses.append("f.office_enabled=1")
        if filters.get("homeDelivery"):
            clauses.append("f.home_enabled=1")
        if filters.get("freeDelivery"):
            clauses.append(
                """((f.office_enabled=1 AND f.office_free_threshold_baisa>0)
                    OR (f.home_enabled=1 AND f.home_free_threshold_baisa>0))"""
            )
        if filters.get("inStock"):
            clauses.append(
                """EXISTS(SELECT 1 FROM products px
                    JOIN product_branch_inventory ix ON ix.product_id=px.id
                    WHERE px.merchant_id=m.id AND ix.branch_id=b.id
                      AND px.status='approved' AND px.active=1
                      AND px.moderation_status='approved' AND px.archived_at=''
                      AND ix.active=1
                      AND ((ix.stock_mode='tracked' AND ix.quantity>0)
                           OR (ix.stock_mode!='tracked' AND ix.availability IN('in_stock','low_stock')))
                      AND NOT (ix.freshness_status='stale'
                               AND ix.stale_enforcement IN('hide_stale','pause_stale')))"""
            )
        with connect() as con:
            rows = con.execute(
                f"""SELECT m.id merchant_id,m.name_ar,m.name_en,m.logo_path,m.cover_path,m.verified,
                    b.id branch_id,b.name_ar branch_name_ar,b.name_en branch_name_en,b.area_id,b.wilayah_id,
                    b.address_text,b.latitude,b.longitude,b.hours_json,
                    f.pickup_enabled,f.office_enabled,f.office_fee_baisa,f.office_free_threshold_baisa,
                    f.home_enabled,f.home_fee_baisa,f.home_free_threshold_baisa,f.eta_text,
                    (SELECT COUNT(*) FROM products p JOIN product_branch_inventory i ON i.product_id=p.id
                     WHERE p.merchant_id=m.id AND i.branch_id=b.id AND p.status='approved' AND p.active=1
                     AND p.moderation_status='approved' AND p.archived_at='' AND i.active=1
                     AND NOT (i.freshness_status='stale'
                              AND i.stale_enforcement IN('hide_stale','pause_stale'))) product_count
                    FROM merchants m JOIN store_branches b ON b.merchant_id=m.id
                    LEFT JOIN fulfillment_profiles f ON f.branch_id=b.id
                    WHERE {' AND '.join(f'({clause.strip()})' for clause in clauses)} LIMIT ?""",
                (*args, max(limit * 3, 60)),
            ).fetchall()
        output = []
        max_distance = filters.get("maxDistanceKm")
        distance_limit = None
        if max_distance not in (None, ""):
            try:
                distance_limit = float(max_distance)
            except (TypeError, ValueError) as exc:
                raise DomainError("invalid_distance", 422) from exc
            if not math.isfinite(distance_limit) or distance_limit <= 0:
                raise DomainError("invalid_distance", 422)
        for row in rows:
            item = dict(row)
            item["openNow"] = _is_open(item.pop("hours_json"))
            item["distanceKm"] = _distance_km(latitude, longitude, item["latitude"], item["longitude"])
            if filters.get("openNow") and item["openNow"] is not True:
                continue
            if distance_limit is not None and (
                item["distanceKm"] is None or item["distanceKm"] > distance_limit
            ):
                continue
            output.append(item)
        if clean_text(filters.get("sort"), 30) == "nearest":
            output.sort(key=lambda item: (item["distanceKm"] is None, item["distanceKm"] or 0, item["branch_id"]))
        return output[:limit]

    def store_detail(self, branch_id: str, *, product_limit: int = 24, cursor: Any = 0) -> dict:
        branch_id = clean_text(branch_id, 90, True)
        product_limit = _bounded_int(product_limit, 1, 60, "invalid_limit")
        offset = _parse_cursor(cursor)
        with connect() as con:
            branch = con.execute(
                f"""SELECT m.*,b.id branch_id,b.name_ar branch_name_ar,b.name_en branch_name_en,
                    b.wilayah_id,b.area_id,b.address_text,b.latitude,b.longitude,b.hours_json,
                    f.pickup_enabled,f.office_enabled,f.office_fee_baisa,f.office_minimum_baisa,f.office_free_threshold_baisa,
                    f.home_enabled,f.home_fee_baisa,f.home_minimum_baisa,f.home_free_threshold_baisa,f.eta_text
                    FROM merchants m JOIN store_branches b ON b.merchant_id=m.id
                    LEFT JOIN fulfillment_profiles f ON f.branch_id=b.id
                    WHERE b.id=? AND {PUBLIC_BRANCH_WHERE}""",
                (branch_id,),
            ).fetchone()
            if not branch:
                raise DomainError("store_not_found", 404)
            result = dict(branch)
            result["openNow"] = _is_open(result.pop("hours_json"))
            result["fulfillment"] = {
                "pickup": bool(result.get("pickup_enabled")),
                "officeDelivery": bool(result.get("office_enabled")),
                "officeFee": omr(result.get("office_fee_baisa") or 0),
                "officeMinimum": omr(result.get("office_minimum_baisa") or 0),
                "officeFreeThreshold": omr(result.get("office_free_threshold_baisa") or 0),
                "homeDelivery": bool(result.get("home_enabled")),
                "homeFee": omr(result.get("home_fee_baisa") or 0),
                "homeMinimum": omr(result.get("home_minimum_baisa") or 0),
                "homeFreeThreshold": omr(result.get("home_free_threshold_baisa") or 0),
                "eta": result.get("eta_text") or "",
            }
            result["branches"] = [dict(row) for row in con.execute(
                f"""SELECT b.id,b.merchant_id,b.name_ar,b.name_en,b.wilayah_id,b.area_id,
                           b.address_text,b.latitude,b.longitude,b.hours_json
                      FROM store_branches b JOIN merchants m ON m.id=b.merchant_id
                     WHERE b.merchant_id=? AND {PUBLIC_BRANCH_WHERE}
                     ORDER BY CASE WHEN b.id=? THEN 0 ELSE 1 END,b.created_at,b.id""",
                (result["id"], branch_id),
            )]
            for public_branch in result["branches"]:
                public_branch["openNow"] = _is_open(public_branch.pop("hours_json"))
            products = self._public_products_by_branch(con, branch_id, product_limit + 1, offset)
            has_more = len(products) > product_limit
            result["products"] = products[:product_limit]
            result["bundles"] = self._public_bundles_by_branch(con, branch_id)
            policy = con.execute(
                "SELECT * FROM merchant_return_policies WHERE merchant_id=? AND active=1 ORDER BY version DESC LIMIT 1",
                (result["id"],),
            ).fetchone()
            result["returnPolicy"] = dict(policy) if policy else None
            result["pagination"] = {"nextCursor": str(offset + product_limit) if has_more else None, "hasMore": has_more}
            return result

    def product_detail(self, product_id: str, branch_id: str = "") -> dict:
        product_id = clean_text(product_id, 90, True)
        branch_id = clean_text(branch_id, 90)
        with connect() as con:
            branch_clause = "AND b.id=?" if branch_id else ""
            args = (product_id, branch_id) if branch_id else (product_id,)
            row = con.execute(
                f"""SELECT p.*,m.name_ar merchant_name_ar,m.name_en merchant_name_en,m.verified,
                    b.id branch_id,b.name_ar branch_name_ar,b.name_en branch_name_en,b.area_id,b.wilayah_id,
                    b.address_text,b.latitude,b.longitude,b.hours_json,
                    i.stock_mode,i.quantity,i.availability,i.last_stock_verified_at,i.stale_at,
                    f.pickup_enabled,f.office_enabled,f.office_fee_baisa,f.office_free_threshold_baisa,
                    f.home_enabled,f.home_fee_baisa,f.home_free_threshold_baisa,f.eta_text
                    FROM products p JOIN merchants m ON m.id=p.merchant_id
                    JOIN product_branch_inventory i ON i.product_id=p.id
                    JOIN store_branches b ON b.id=i.branch_id AND b.merchant_id=p.merchant_id
                    LEFT JOIN fulfillment_profiles f ON f.branch_id=b.id
                    WHERE p.id=? {branch_clause} AND {PUBLIC_PRODUCT_WHERE}
                    ORDER BY b.id LIMIT 1""",
                args,
            ).fetchone()
            if not row:
                raise DomainError("product_not_found", 404)
            item = dict(row)
            item["price"] = omr(item.pop("price_baisa"))
            item["images"] = loads(item.pop("images_json"), [])
            item["metadata"] = loads(item.pop("metadata_json"), {})
            item["tags"] = loads(item.pop("tags_json"), [])
            item["openNow"] = _is_open(item.pop("hours_json"))
            item["store"] = {
                "merchantId": item["merchant_id"], "branchId": item["branch_id"],
                "nameAr": item["merchant_name_ar"], "nameEn": item["merchant_name_en"],
                "branchNameAr": item["branch_name_ar"], "branchNameEn": item["branch_name_en"],
                "verified": bool(item["verified"]), "areaId": item["area_id"],
                "wilayahId": item["wilayah_id"], "address": item["address_text"],
                "latitude": item["latitude"], "longitude": item["longitude"],
                "openNow": item["openNow"],
            }
            item["fulfillment"] = {
                "pickup": bool(item.get("pickup_enabled")),
                "officeDelivery": bool(item.get("office_enabled")),
                "officeFee": omr(item.get("office_fee_baisa") or 0),
                "officeFreeThreshold": omr(item.get("office_free_threshold_baisa") or 0),
                "homeDelivery": bool(item.get("home_enabled")),
                "homeFee": omr(item.get("home_fee_baisa") or 0),
                "homeFreeThreshold": omr(item.get("home_free_threshold_baisa") or 0),
                "eta": item.get("eta_text") or "",
            }
            return item

    def resolve_public_product_media(self, media_id: str, variant: str = "") -> dict:
        """Resolve an opaque product-media URL only while its product is public.

        The returned path is an internal server value, never a JSON response.
        Revoking the media, product, merchant, branch, or freshness eligibility
        immediately makes the opaque URL resolve as 404.
        """
        media_id = clean_text(media_id, 90, True)
        with connect() as con:
            row = con.execute(
                f"""SELECT mo.id,mo.storage_key,mo.mime_type,mo.byte_size,mo.sha256_hex,
                           pm.thumbnail_path
                    FROM product_media pm
                    JOIN private_media_objects mo ON mo.id=pm.id
                    JOIN products p ON p.id=pm.product_id
                    JOIN merchants m ON m.id=p.merchant_id
                    JOIN product_branch_inventory i ON i.product_id=p.id
                    JOIN store_branches b ON b.id=i.branch_id AND b.merchant_id=p.merchant_id
                    WHERE pm.id=? AND pm.status='active'
                      AND mo.status='active' AND mo.owner_kind='merchant'
                      AND mo.owner_id=p.merchant_id AND mo.purpose='product_image'
                      AND mo.mime_type LIKE 'image/%' AND {PUBLIC_PRODUCT_WHERE}
                    ORDER BY b.id LIMIT 1""",
                (media_id,),
            ).fetchone()
        if not row:
            raise DomainError("product_media_not_found", 404)
        upload_root = Path(bisa_config.UPLOAD_DIR).resolve()
        use_thumbnail = clean_text(variant, 20).lower() == "thumbnail" and bool(row["thumbnail_path"])
        selected_key = row["thumbnail_path"] if use_thumbnail else row["storage_key"]
        candidate = (upload_root / selected_key).resolve()
        if upload_root not in candidate.parents or not candidate.is_file() or candidate.is_symlink():
            raise DomainError("product_media_not_found", 404)
        byte_size = candidate.stat().st_size if use_thumbnail else row["byte_size"]
        etag = hashlib.sha256(candidate.read_bytes()).hexdigest() if use_thumbnail else row["sha256_hex"]
        return {
            "path": candidate, "mimeType": "image/webp" if use_thumbnail else row["mime_type"],
            "byteSize": byte_size, "etag": etag,
        }

    def _public_products_by_branch(self, con, branch_id: str, limit: int, offset: int = 0) -> list[dict]:
        rows = con.execute(
            f"""SELECT p.*,i.stock_mode,i.quantity,i.availability,i.last_stock_verified_at,i.stale_at
                FROM products p JOIN merchants m ON m.id=p.merchant_id
                JOIN product_branch_inventory i ON i.product_id=p.id
                JOIN store_branches b ON b.id=i.branch_id AND b.merchant_id=p.merchant_id
                WHERE b.id=? AND {PUBLIC_PRODUCT_WHERE}
                ORDER BY p.updated_at DESC,p.id LIMIT ? OFFSET ?""",
            (branch_id, limit, offset),
        ).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            item["price"] = omr(item.pop("price_baisa"))
            item["images"] = loads(item.pop("images_json"), [])
            output.append(item)
        return output

    def _public_bundles_by_branch(self, con, branch_id: str) -> list[dict]:
        stamp = now_iso()
        rows = con.execute(
            """SELECT b.*,(SELECT COALESCE(SUM(p.price_baisa*bi.quantity),0)
                    FROM bundle_items bi JOIN products p ON p.id=bi.product_id
                    WHERE bi.bundle_id=b.id) normal_value_baisa
               FROM bundles b JOIN merchants m ON m.id=b.merchant_id
               JOIN store_branches sb ON sb.id=b.branch_id AND sb.merchant_id=b.merchant_id
               WHERE b.branch_id=? AND b.status='approved' AND b.moderation_status='approved'
                 AND m.status='approved' AND sb.status='approved' AND sb.active=1 AND sb.public_visible=1
                 AND (b.starts_at='' OR b.starts_at<=?) AND (b.ends_at='' OR b.ends_at>?)
               ORDER BY b.updated_at DESC LIMIT 50""",
            (branch_id, stamp, stamp),
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["price"] = omr(item.pop("selling_price_baisa"))
            item["normalValue"] = omr(item.pop("normal_value_baisa"))
            result.append(item)
        return result

    # ---------- merchant catalog ----------

    def upsert_product(self, actor, payload: dict) -> dict:
        price = _strict_baisa(payload.get("price"), product=True)
        product_id = clean_text(payload.get("id"), 90)
        branch_id = clean_text(payload.get("branchId"), 90, True)
        category_id = clean_text(payload.get("categoryId"), 80, True)
        if payload.get("images") not in (None, [], ""):
            raise DomainError("client_image_paths_not_allowed", 422)
        media_supplied = "imageMediaIds" in payload
        raw_media_ids = payload.get("imageMediaIds") if media_supplied else []
        if media_supplied and not isinstance(raw_media_ids, list):
            raise DomainError("product_image_media_must_be_list", 422)
        image_media_ids: list[str] = []
        for value in raw_media_ids or []:
            media_id = clean_text(value, 90, True)
            if media_id not in image_media_ids:
                image_media_ids.append(media_id)
        quantity = _bounded_int(payload.get("quantity", 0), 0, 1_000_000, "invalid_quantity")
        stock_mode = clean_text(payload.get("stockMode"), 20) or "tracked"
        if stock_mode not in {"tracked", "availability"}:
            raise DomainError("invalid_stock_mode", 422)
        stamp = now_iso()
        with connect(immediate=True) as con:
            actor = self._require_actor(con, actor, {"merchant_owner", "merchant_manager"})
            merchant_id = actor["merchantId"]
            branch = con.execute(
                "SELECT id FROM store_branches WHERE id=? AND merchant_id=? AND active=1",
                (branch_id, merchant_id),
            ).fetchone()
            if not branch:
                raise DomainError("branch_not_owned", 403)
            category = con.execute(
                "SELECT regulated_rules FROM product_categories WHERE id=? AND active=1", (category_id,)
            ).fetchone()
            if not category:
                raise DomainError("valid_category_required", 422)
            existing_any = None
            if product_id:
                existing_any = con.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
                if not existing_any:
                    raise DomainError("product_not_found", 404)
                if existing_any["merchant_id"] != merchant_id:
                    raise DomainError("product_not_found", 404)
            plan = self._active_plan(con, merchant_id)
            limit = _bounded_int(plan["entitlements"].get("products", 0), 0, 1_000_000, "invalid_plan_limit")
            media_limit = _bounded_int(
                plan["entitlements"].get("mediaPerProduct", 0), 0, 100,
                "invalid_plan_limit",
            )
            if len(image_media_ids) > media_limit:
                raise DomainError("plan_product_media_limit", 409, {"limit": media_limit})
            if not existing_any:
                active_count = con.execute(
                    "SELECT COUNT(*) n FROM products WHERE merchant_id=? AND active=1 AND archived_at=''",
                    (merchant_id,),
                ).fetchone()["n"]
                if active_count >= limit:
                    raise DomainError("plan_product_limit", 409, {"limit": limit})
                product_id = new_id("prod")
            media_rows: list[dict] = []
            if media_supplied:
                for media_id in image_media_ids:
                    media = con.execute(
                        """SELECT id,mime_type,storage_key FROM private_media_objects
                           WHERE id=? AND owner_kind='merchant' AND owner_id=?
                             AND purpose='product_image' AND status='active'
                             AND mime_type LIKE 'image/%'""",
                        (media_id, merchant_id),
                    ).fetchone()
                    if not media:
                        raise DomainError("product_image_media_not_found", 404, {"mediaId": media_id})
                    association = con.execute(
                        "SELECT product_id FROM product_media WHERE id=?", (media_id,)
                    ).fetchone()
                    if association and association["product_id"] != product_id:
                        raise DomainError("product_image_media_already_linked", 409, {"mediaId": media_id})
                    media_row = dict(media)
                    media_path = Path(media_row["storage_key"])
                    thumbnail_path = str(
                        media_path.with_name(f"{media_path.stem}.thumb.webp")
                    ).replace("\\", "/")
                    thumbnail_candidate = (Path(bisa_config.UPLOAD_DIR) / thumbnail_path).resolve()
                    upload_root = Path(bisa_config.UPLOAD_DIR).resolve()
                    media_row["thumbnail_path"] = (
                        thumbnail_path
                        if upload_root in thumbnail_candidate.parents and thumbnail_candidate.is_file()
                        else ""
                    )
                    media_rows.append(media_row)
            reserved = con.execute(
                """SELECT COALESCE(SUM(r.quantity),0) n FROM inventory_reservations r
                   WHERE r.product_id=? AND r.branch_id=? AND r.status='pending'""",
                (product_id, branch_id),
            ).fetchone()["n"] if existing_any else 0
            if stock_mode == "tracked" and quantity < reserved:
                raise DomainError("inventory_below_reserved", 409, {"reserved": reserved})
            rules = loads(category["regulated_rules"], {})
            moderated = bool(rules) or bool(payload.get("requiresModeration"))
            if existing_any and (
                existing_any["status"] in {"pending_review", "rejected", "suspended"}
                or existing_any["moderation_status"] in {"pending", "rejected", "suspended"}
            ):
                moderated = True
            status = "pending_review" if moderated else "approved"
            moderation_status = "pending" if moderated else "approved"
            values = {
                "id": product_id,
                "merchant_id": merchant_id,
                "category_id": category_id,
                "name_ar": clean_text(payload.get("nameAr"), 120, True),
                "name_en": clean_text(payload.get("nameEn"), 120, True),
                "description_ar": clean_text(payload.get("descriptionAr"), 800),
                "description_en": clean_text(payload.get("descriptionEn"), 800),
                "price_baisa": price,
                "unit_text": clean_text(payload.get("unit"), 40),
                "barcode": clean_text(payload.get("barcode"), 60),
                "images_json": (
                    dumps([{
                        "url": f"/api/media/products/{media['id']}",
                        **({
                            "thumbnailUrl": f"/api/media/products/{media['id']}?variant=thumbnail",
                        } if media.get("thumbnail_path") else {}),
                    } for media in media_rows])
                    if media_supplied else (existing_any["images_json"] if existing_any else "[]")
                ),
                "metadata_json": dumps(payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}),
                "tags_json": dumps(payload.get("tags") if isinstance(payload.get("tags"), list) else []),
            }
            if existing_any:
                updated = con.execute(
                    """UPDATE products SET category_id=:category_id,name_ar=:name_ar,name_en=:name_en,
                        description_ar=:description_ar,description_en=:description_en,price_baisa=:price_baisa,
                        unit_text=:unit_text,barcode=:barcode,images_json=:images_json,metadata_json=:metadata_json,
                        tags_json=:tags_json,status=:status,moderation_status=:moderation_status,
                        updated_at=:updated_at
                        WHERE id=:id AND merchant_id=:merchant_id""",
                    {**values, "status": status, "moderation_status": moderation_status, "updated_at": stamp},
                ).rowcount
                if updated != 1:
                    raise DomainError("product_update_conflict", 409)
            else:
                con.execute(
                    """INSERT INTO products(
                        id,merchant_id,category_id,name_ar,name_en,description_ar,description_en,price_baisa,
                        unit_text,barcode,images_json,status,active,created_at,updated_at,
                        metadata_json,tags_json,moderation_status,archived_at)
                        VALUES(:id,:merchant_id,:category_id,:name_ar,:name_en,:description_ar,:description_en,
                        :price_baisa,:unit_text,:barcode,:images_json,:status,1,:created_at,:updated_at,
                        :metadata_json,:tags_json,:moderation_status,'')""",
                    {**values, "status": status, "moderation_status": moderation_status,
                     "created_at": stamp, "updated_at": stamp},
                )
            availability = clean_text(payload.get("availability"), 30)
            if stock_mode == "tracked":
                availability = "in_stock" if quantity > 2 else "low_stock" if quantity else "out_of_stock"
            elif availability not in {"in_stock", "low_stock", "out_of_stock", "paused"}:
                availability = "in_stock" if payload.get("available", True) else "out_of_stock"
            con.execute(
                """INSERT INTO product_branch_inventory(
                    product_id,branch_id,stock_mode,quantity,availability,last_stock_verified_at,
                    stale_at,active,updated_at,freshness_status,stale_enforcement)
                    VALUES(?,?,?,?,?,'','',1,?,'unverified','')
                    ON CONFLICT(product_id,branch_id) DO UPDATE SET
                    stock_mode=excluded.stock_mode,quantity=excluded.quantity,
                    availability=excluded.availability,last_stock_verified_at='',stale_at='',
                    freshness_status='unverified',stale_enforcement='',updated_at=excluded.updated_at""",
                (product_id, branch_id, stock_mode, quantity, availability, stamp),
            )
            if media_supplied:
                con.execute(
                    "UPDATE product_media SET status='archived' WHERE product_id=? AND status='active'",
                    (product_id,),
                )
                for sort_order, media in enumerate(media_rows):
                    thumbnail_path = media.get("thumbnail_path") or ""
                    association = con.execute(
                        "SELECT product_id FROM product_media WHERE id=?", (media["id"],)
                    ).fetchone()
                    if association:
                        con.execute(
                            """UPDATE product_media SET private_path=?,thumbnail_path=?,mime_type=?,
                               sort_order=?,status='active' WHERE id=? AND product_id=?""",
                            (f"media:{media['id']}", thumbnail_path, media["mime_type"], sort_order,
                             media["id"], product_id),
                        )
                    else:
                        con.execute(
                            """INSERT INTO product_media(
                                id,product_id,private_path,thumbnail_path,mime_type,width,height,
                                sort_order,status,created_at)
                               VALUES(?,?,?,?,?,0,0,?,'active',?)""",
                            (media["id"], product_id, f"media:{media['id']}",
                             thumbnail_path, media["mime_type"], sort_order, stamp),
                        )
            return {
                "id": product_id, "branchId": branch_id, "price": omr(price),
                "quantity": quantity, "availability": availability,
                "status": status, "moderationStatus": moderation_status,
                "images": loads(values["images_json"], []),
            }

    def create_bundle(self, actor, payload: dict) -> dict:
        branch_id = clean_text(payload.get("branchId"), 90, True)
        raw_components = payload.get("components")
        if not isinstance(raw_components, list):
            raise DomainError("bundle_components_required", 422)
        selling = _strict_baisa(payload.get("price"))
        if selling <= 0:
            raise DomainError("valid_bundle_price_required", 422)
        starts_at, ends_at = _iso_window(payload.get("startsAt"), payload.get("endsAt"))
        with connect(immediate=True) as con:
            actor = self._require_actor(con, actor, {"merchant_owner", "merchant_manager"})
            merchant_id = actor["merchantId"]
            if not con.execute(
                "SELECT 1 FROM store_branches WHERE id=? AND merchant_id=? AND active=1",
                (branch_id, merchant_id),
            ).fetchone():
                raise DomainError("branch_not_owned", 403)
            maximum = _bounded_int(settings(con).get("bundleMaxComponents", 10), 2, 100, "invalid_bundle_limit")
            normalized: dict[str, int] = {}
            for component in raw_components:
                if not isinstance(component, dict):
                    raise DomainError("invalid_bundle_component", 422)
                product_id = clean_text(component.get("productId"), 90, True)
                quantity = _bounded_int(component.get("quantity", 1), 1, 100, "invalid_quantity")
                normalized[product_id] = normalized.get(product_id, 0) + quantity
                if normalized[product_id] > 100:
                    raise DomainError("invalid_quantity", 422)
            if len(normalized) < 2 or len(normalized) > maximum:
                raise DomainError("bundle_component_count", 422, {"maximum": maximum})
            plan = self._active_plan(con, merchant_id)
            plan_limit = _bounded_int(plan["entitlements"].get("bundles", 0), 0, 10_000, "invalid_plan_limit")
            count = con.execute(
                "SELECT COUNT(*) n FROM bundles WHERE merchant_id=? AND status!='archived'", (merchant_id,)
            ).fetchone()["n"]
            if count >= plan_limit:
                raise DomainError("plan_bundle_limit", 409, {"limit": plan_limit})
            normal = 0
            for product_id, quantity in normalized.items():
                row = con.execute(
                    """SELECT p.price_baisa FROM products p
                       JOIN product_branch_inventory i ON i.product_id=p.id
                       WHERE p.id=? AND p.merchant_id=? AND p.active=1 AND p.status='approved'
                         AND p.moderation_status='approved' AND p.archived_at=''
                         AND i.branch_id=? AND i.active=1""",
                    (product_id, merchant_id, branch_id),
                ).fetchone()
                if not row or not PRODUCT_MIN_BAISA <= row["price_baisa"] <= PRODUCT_MAX_BAISA:
                    raise DomainError("bundle_product_invalid", 422, {"productId": product_id})
                normal += row["price_baisa"] * quantity
            stamp = now_iso()
            bundle_id = new_id("bundle")
            con.execute(
                """INSERT INTO bundles(
                    id,merchant_id,branch_id,title_ar,title_en,description,selling_price_baisa,status,
                    starts_at,ends_at,created_at,updated_at,image_path,tags_json,moderation_status)
                    VALUES(?,?,?,?,?,?,?,'approved',?,?,?,?,?,?,'approved')""",
                (bundle_id, merchant_id, branch_id,
                 clean_text(payload.get("titleAr"), 120, True),
                 clean_text(payload.get("titleEn"), 120, True),
                 clean_text(payload.get("description"), 800), selling,
                 starts_at, ends_at,
                 stamp, stamp, clean_text(payload.get("imagePath"), 240),
                 dumps(payload.get("tags") if isinstance(payload.get("tags"), list) else [])),
            )
            con.executemany(
                "INSERT INTO bundle_items(bundle_id,product_id,quantity) VALUES(?,?,?)",
                [(bundle_id, product_id, quantity) for product_id, quantity in normalized.items()],
            )
            return {
                "id": bundle_id, "branchId": branch_id, "normalValue": omr(normal),
                "price": omr(selling), "saving": omr(max(0, normal - selling)),
                "componentCount": len(normalized),
            }

    # ---------- cart, checkout and orders ----------

    def _cart_view(self, con, account_id: str) -> dict | None:
        cart = con.execute(
            "SELECT * FROM carts WHERE account_id=? AND status='active'", (account_id,)
        ).fetchone()
        if not cart:
            return None
        result = dict(cart)
        lines = []
        subtotal = 0
        for row in con.execute(
            "SELECT * FROM cart_items WHERE cart_id=? ORDER BY rowid", (cart["id"],)
        ):
            item = dict(row)
            line_total = item["unit_price_baisa"] * item["quantity"]
            item["unitPrice"] = omr(item.pop("unit_price_baisa"))
            item["lineTotal"] = omr(line_total)
            lines.append(item)
            subtotal += line_total
        result["items"] = lines
        result["subtotal"] = omr(subtotal)
        return result

    def _public_cart_item(self, con, kind: str, item_id: str, branch_id: str):
        stamp = now_iso()
        if kind == "product":
            row = con.execute(
                f"""SELECT p.id,p.merchant_id,p.name_ar,p.name_en,p.price_baisa,
                    i.stock_mode,i.quantity,i.availability
                    FROM products p JOIN merchants m ON m.id=p.merchant_id
                    JOIN product_branch_inventory i ON i.product_id=p.id
                    JOIN store_branches b ON b.id=i.branch_id AND b.merchant_id=p.merchant_id
                    WHERE p.id=? AND b.id=? AND {PUBLIC_PRODUCT_WHERE}""",
                (item_id, branch_id),
            ).fetchone()
            if not row:
                return None
            return {**dict(row), "components": [(item_id, 1)]}
        row = con.execute(
            """SELECT bu.id,bu.merchant_id,bu.title_ar name_ar,bu.title_en name_en,
                bu.selling_price_baisa price_baisa
               FROM bundles bu JOIN merchants m ON m.id=bu.merchant_id
               JOIN store_branches b ON b.id=bu.branch_id AND b.merchant_id=bu.merchant_id
               WHERE bu.id=? AND b.id=? AND bu.status='approved' AND bu.moderation_status='approved'
                 AND m.status='approved' AND b.status='approved' AND b.active=1 AND b.public_visible=1
                 AND (bu.starts_at='' OR bu.starts_at<=?) AND (bu.ends_at='' OR bu.ends_at>?)""",
            (item_id, branch_id, stamp, stamp),
        ).fetchone()
        if not row:
            return None
        components = []
        for component in con.execute(
            """SELECT bi.product_id,bi.quantity,p.price_baisa,i.stock_mode,i.quantity inventory_quantity,
                i.availability FROM bundle_items bi JOIN products p ON p.id=bi.product_id
                JOIN product_branch_inventory i ON i.product_id=p.id
                WHERE bi.bundle_id=? AND p.merchant_id=? AND p.active=1 AND p.status='approved'
                  AND p.moderation_status='approved' AND p.archived_at='' AND i.branch_id=? AND i.active=1
                  AND NOT (i.freshness_status='stale'
                           AND i.stale_enforcement IN('hide_stale','pause_stale'))""",
            (item_id, row["merchant_id"], branch_id),
        ):
            components.append((component["product_id"], component["quantity"]))
        expected = con.execute(
            "SELECT COUNT(*) n FROM bundle_items WHERE bundle_id=?", (item_id,)
        ).fetchone()["n"]
        if expected < 2 or len(components) != expected:
            return None
        return {**dict(row), "components": components}

    def add_cart(self, actor, payload: dict) -> dict:
        kind = clean_text(payload.get("kind"), 20) or "product"
        if kind not in {"product", "bundle"}:
            raise DomainError("invalid_cart_item_kind", 422)
        item_id = clean_text(payload.get("itemId"), 90, True)
        branch_id = clean_text(payload.get("branchId"), 90, True)
        quantity = _bounded_int(payload.get("quantity", 1), 1, 100, "invalid_quantity")
        replace = payload.get("replaceCart") is True
        expected_version = payload.get("expectedVersion")
        with connect(immediate=True) as con:
            actor = self._require_actor(con, actor, {"shopper"})
            item = self._public_cart_item(con, kind, item_id, branch_id)
            if not item:
                raise DomainError("item_not_available", 404)
            cart = con.execute(
                "SELECT * FROM carts WHERE account_id=? AND status='active'", (actor["accountId"],)
            ).fetchone()
            if cart and expected_version not in (None, ""):
                if _bounded_int(expected_version, 1, 2_000_000_000, "invalid_cart_version") != cart["version"]:
                    raise DomainError("cart_version_conflict", 409, {"currentVersion": cart["version"]})
            if cart and (cart["merchant_id"] != item["merchant_id"] or cart["branch_id"] != branch_id):
                if not replace:
                    raise DomainError(
                        "cross_store_cart_confirmation_required", 409,
                        {
                            "currentMerchantId": cart["merchant_id"], "currentBranchId": cart["branch_id"],
                            "newMerchantId": item["merchant_id"], "newBranchId": branch_id,
                        },
                    )
                con.execute(
                    "UPDATE carts SET status='replaced',version=version+1,updated_at=? WHERE id=? AND status='active'",
                    (now_iso(), cart["id"]),
                )
                cart = None
            if not cart:
                cart_id = new_id("cart")
                con.execute(
                    """INSERT INTO carts(id,account_id,merchant_id,branch_id,status,version,updated_at)
                       VALUES(?,?,?,?,'active',1,?)""",
                    (cart_id, actor["accountId"], item["merchant_id"], branch_id, now_iso()),
                )
            else:
                cart_id = cart["id"]
            con.execute(
                """INSERT INTO cart_items(cart_id,item_kind,item_id,quantity,unit_price_baisa)
                   VALUES(?,?,?,?,?) ON CONFLICT(cart_id,item_kind,item_id) DO UPDATE SET
                   quantity=MIN(100,cart_items.quantity+excluded.quantity),
                   unit_price_baisa=excluded.unit_price_baisa""",
                (cart_id, kind, item_id, quantity, item["price_baisa"]),
            )
            con.execute(
                "UPDATE carts SET version=version+1,updated_at=? WHERE id=?", (now_iso(), cart_id)
            )
            con.execute(
                "INSERT INTO analytics_events(id,event_type,actor_hash,entity_kind,entity_id,context_json,created_at) VALUES(?,?,?,?,?,?,?)",
                (new_id("evt"), "add_to_cart", _actor_hash(actor), kind, item_id,
                 dumps({"branchId": branch_id}), now_iso()),
            )
            return self._cart_view(con, actor["accountId"])

    def _resolve_delivery(self, con, cart, mode: str, address: Any, subtotal: int) -> tuple[int, dict]:
        profile = con.execute(
            "SELECT * FROM fulfillment_profiles WHERE branch_id=?", (cart["branch_id"],)
        ).fetchone()
        if not profile:
            raise DomainError("fulfillment_not_available", 409)
        enabled_column = {
            "pickup": "pickup_enabled", "office_delivery": "office_enabled", "home_delivery": "home_enabled",
        }[mode]
        if not profile[enabled_column]:
            raise DomainError("fulfillment_not_available", 409)
        if mode == "pickup":
            return 0, {"mode": mode, "branchId": cart["branch_id"], "address": None, "feeBaisa": 0}
        if not isinstance(address, dict):
            raise DomainError("delivery_address_required", 422)
        address_id = clean_text(address.get("id") or address.get("addressId"), 90)
        if address_id:
            row = con.execute(
                "SELECT * FROM shopper_addresses WHERE id=? AND account_id=? AND active=1",
                (address_id, cart["account_id"]),
            ).fetchone()
            if not row:
                raise DomainError("delivery_address_not_found", 404)
            resolved = dict(row)
        else:
            resolved = {
                "id": "", "address_type": clean_text(address.get("type") or address.get("addressType"), 20, True),
                "wilayah_id": clean_text(address.get("wilayahId"), 90, True),
                "area_id": clean_text(address.get("areaId"), 90, True),
                "address_text": clean_text(address.get("addressText"), 300, True),
                "latitude": address.get("latitude"), "longitude": address.get("longitude"),
            }
        if resolved["address_type"] not in {"home", "office", "other"}:
            raise DomainError("invalid_address_type", 422)
        if mode == "office_delivery" and resolved["address_type"] != "office":
            raise DomainError("office_address_required", 422)
        if mode == "home_delivery" and resolved["address_type"] == "office":
            raise DomainError("home_address_required", 422)
        area = con.execute(
            """SELECT a.id,a.parent_id,w.kind FROM locations a
               JOIN locations w ON w.id=a.parent_id
               WHERE a.id=? AND a.kind='area' AND a.active=1 AND w.id=? AND w.kind='wilayat' AND w.active=1""",
            (resolved["area_id"], resolved["wilayah_id"]),
        ).fetchone()
        if not area:
            raise DomainError("invalid_delivery_area", 422)
        zone = con.execute(
            """SELECT * FROM branch_delivery_zones WHERE branch_id=? AND mode=? AND active=1
               AND (wilayah_id='' OR wilayah_id=?) AND (area_id='' OR area_id=?)
               ORDER BY CASE WHEN area_id<>'' THEN 2 WHEN wilayah_id<>'' THEN 1 ELSE 0 END DESC LIMIT 1""",
            (cart["branch_id"], mode, resolved["wilayah_id"], resolved["area_id"]),
        ).fetchone()
        prefix = "office" if mode == "office_delivery" else "home"
        if zone:
            fee = zone["fee_baisa"]
            minimum = zone["minimum_baisa"]
            free_threshold = zone["free_threshold_baisa"]
            zone_id = zone["id"]
            eta = zone["eta_text"] or profile["eta_text"]
        else:
            legacy_zones = loads(profile["zones_json"], [])
            if resolved["area_id"] not in legacy_zones and resolved["wilayah_id"] not in legacy_zones:
                raise DomainError("delivery_zone_not_served", 409)
            fee = profile[f"{prefix}_fee_baisa"]
            minimum = profile[f"{prefix}_minimum_baisa"]
            free_threshold = profile[f"{prefix}_free_threshold_baisa"]
            zone_id = "legacy"
            eta = profile["eta_text"]
        if subtotal < minimum:
            raise DomainError("minimum_order_not_met", 409, {"minimum": omr(minimum)})
        charged_fee = 0 if free_threshold and subtotal >= free_threshold else fee
        snapshot = {
            "mode": mode, "branchId": cart["branch_id"], "zoneId": zone_id,
            "address": resolved, "feeBaisa": charged_fee, "minimumBaisa": minimum,
            "freeThresholdBaisa": free_threshold, "eta": eta,
        }
        return charged_fee, snapshot

    def _checkout_replay(self, con, actor_id: str, key: str, request_hash: str):
        row = con.execute(
            """SELECT payload_hash,response_json FROM idempotency_records
               WHERE actor_id=? AND operation='checkout' AND idempotency_key=?""",
            (actor_id, key),
        ).fetchone()
        if not row:
            return None
        if row["payload_hash"] != request_hash:
            raise DomainError("idempotency_key_reused", 409)
        response = loads(row["response_json"], {})
        response["duplicate"] = True
        return response

    def checkout(self, actor, payload: dict) -> dict:
        idempotency_key = clean_text(payload.get("idempotencyKey"), 120, True)
        mode = clean_text(payload.get("fulfillmentMode"), 30) or "pickup"
        if mode not in {"pickup", "office_delivery", "home_delivery"}:
            raise DomainError("invalid_fulfillment_mode", 422)
        payment_method = clean_text(payload.get("paymentMethod"), 30)
        if not payment_method:
            payment_method = "pay_at_store" if mode == "pickup" else "cash_on_delivery"
        if payment_method not in {"pay_at_store", "cash_on_delivery", "online"}:
            raise DomainError("invalid_payment_method", 422)
        if mode == "pickup" and payment_method == "cash_on_delivery":
            raise DomainError("invalid_payment_method", 422)
        request_payload = {
            key: value for key, value in payload.items()
            if key not in {"idempotencyKey"}
        }
        request_hash = _payload_hash(request_payload)
        with connect(immediate=True) as con:
            actor = self._require_actor(con, actor, {"shopper"})
            replay = self._checkout_replay(con, actor["accountId"], idempotency_key, request_hash)
            if replay:
                return replay
            if payment_method == "online":
                configured = settings(con).get("paymentsEnabled", False)
                if not configured:
                    raise DomainError("payment_unavailable", 409)
                # A feature flag alone is not a PSP delivery confirmation.
                raise DomainError("payment_adapter_not_configured", 503)
            cart_row = con.execute(
                "SELECT * FROM carts WHERE account_id=? AND status='active'", (actor["accountId"],)
            ).fetchone()
            if not cart_row:
                raise DomainError("cart_empty", 409)
            expected_version = payload.get("expectedCartVersion")
            if expected_version not in (None, ""):
                expected = _bounded_int(expected_version, 1, 2_000_000_000, "invalid_cart_version")
                if expected != cart_row["version"]:
                    raise DomainError("cart_version_conflict", 409, {"currentVersion": cart_row["version"]})
            cart = dict(cart_row)
            raw_lines = list(con.execute(
                "SELECT * FROM cart_items WHERE cart_id=? ORDER BY rowid", (cart["id"],)
            ))
            if not raw_lines:
                raise DomainError("cart_empty", 409)
            validated_lines = []
            component_totals: dict[str, int] = {}
            subtotal = 0
            price_changes = []
            for line in raw_lines:
                item = self._public_cart_item(con, line["item_kind"], line["item_id"], cart["branch_id"])
                if not item or item["merchant_id"] != cart["merchant_id"]:
                    raise DomainError("cart_item_unavailable", 409, {"itemId": line["item_id"]})
                if item["price_baisa"] != line["unit_price_baisa"]:
                    price_changes.append({
                        "kind": line["item_kind"], "itemId": line["item_id"],
                        "oldPrice": omr(line["unit_price_baisa"]), "newPrice": omr(item["price_baisa"]),
                    })
                line_total = item["price_baisa"] * line["quantity"]
                subtotal += line_total
                components = []
                for product_id, component_quantity in item["components"]:
                    required = component_quantity * line["quantity"]
                    component_totals[product_id] = component_totals.get(product_id, 0) + required
                    components.append({"productId": product_id, "quantity": required})
                validated_lines.append({
                    "kind": line["item_kind"], "itemId": line["item_id"],
                    "name": {"ar": item["name_ar"], "en": item["name_en"]},
                    "quantity": line["quantity"], "unitPriceBaisa": item["price_baisa"],
                    "components": components,
                })
            if price_changes and payload.get("acceptPriceChanges") is not True:
                raise DomainError("cart_price_changed", 409, {"changes": price_changes})
            fee, fulfillment_snapshot = self._resolve_delivery(
                con, cart, mode, payload.get("address"), subtotal
            )
            policy = con.execute(
                """SELECT * FROM merchant_return_policies
                   WHERE merchant_id=? AND active=1 ORDER BY version DESC LIMIT 1""",
                (cart["merchant_id"],),
            ).fetchone()
            if not policy:
                raise DomainError("return_policy_required", 409)
            for product_id, required in component_totals.items():
                inventory = con.execute(
                    """SELECT stock_mode,quantity,availability FROM product_branch_inventory
                       WHERE product_id=? AND branch_id=? AND active=1""",
                    (product_id, cart["branch_id"]),
                ).fetchone()
                if not inventory:
                    raise DomainError("stock_unavailable", 409, {"productId": product_id})
                if inventory["stock_mode"] == "tracked":
                    held = con.execute(
                        """SELECT COALESCE(SUM(quantity),0) n FROM inventory_reservations
                           WHERE product_id=? AND branch_id=? AND status='pending'""",
                        (product_id, cart["branch_id"]),
                    ).fetchone()["n"]
                    if inventory["quantity"] - held < required:
                        raise DomainError("stock_unavailable", 409, {"productId": product_id})
                elif inventory["availability"] not in {"in_stock", "low_stock"}:
                    raise DomainError("stock_unavailable", 409, {"productId": product_id})
            stamp = now_iso()
            response_hours = _bounded_int(settings(con).get("merchantResponseHours", 4), 1, 168, "invalid_response_sla")
            due = (datetime.now(UTC) + timedelta(hours=response_hours)).isoformat()
            order_id = new_id("order")
            con.execute(
                """INSERT INTO orders(
                    id,account_id,merchant_id,branch_id,status,fulfillment_mode,address_snapshot,policy_snapshot,
                    subtotal_baisa,delivery_fee_baisa,total_baisa,idempotency_key,response_due_at,created_at,updated_at,
                    expires_at,payment_method,cancellation_reason,version)
                    VALUES(?,?,?,?,'pending_store_confirmation',?,?,?,?,?,?,?,?,?,?,?,?,?,1)""",
                (order_id, actor["accountId"], cart["merchant_id"], cart["branch_id"], mode,
                 dumps(fulfillment_snapshot.get("address") or {}), dumps(dict(policy)),
                 subtotal, fee, subtotal + fee, idempotency_key, due, stamp, stamp,
                 due, payment_method, ""),
            )
            for line in validated_lines:
                con.execute(
                    """INSERT INTO order_items(
                        order_id,item_kind,item_id,name_snapshot,quantity,unit_price_baisa,component_snapshot)
                       VALUES(?,?,?,?,?,?,?)""",
                    (order_id, line["kind"], line["itemId"], dumps(line["name"]),
                     line["quantity"], line["unitPriceBaisa"], dumps(line["components"])),
                )
            for product_id, quantity in component_totals.items():
                con.execute(
                    """INSERT INTO inventory_reservations(
                        id,order_id,product_id,branch_id,quantity,status,created_at)
                       VALUES(?,?,?,?,?,'pending',?)""",
                    (new_id("res"), order_id, product_id, cart["branch_id"], quantity, stamp),
                )
            policy_snapshot = dumps(dict(policy))
            con.execute(
                """INSERT INTO order_policy_snapshots(order_id,policy_id,policy_version,snapshot_json,created_at)
                   VALUES(?,?,?,?,?)""",
                (order_id, policy["id"], policy["version"], policy_snapshot, stamp),
            )
            con.execute(
                """INSERT INTO order_events(id,order_id,event_type,from_status,to_status,actor_kind,actor_id,detail_json,created_at)
                   VALUES(?,?,'checkout','','pending_store_confirmation','shopper',?,?,?)""",
                (new_id("oevt"), order_id, actor["accountId"],
                 dumps({"fulfillment": fulfillment_snapshot, "paymentMethod": payment_method}), stamp),
            )
            con.execute(
                "UPDATE carts SET status='checked_out',version=version+1,updated_at=? WHERE id=? AND status='active'",
                (stamp, cart["id"]),
            )
            self._insert_notification(
                con, "merchant", cart["merchant_id"], "طلب جديد", "New order",
                "أكد توفر المنتجات قبل انتهاء المهلة", "Confirm availability before the deadline",
                f"merchant:order:{order_id}", True, f"order:{order_id}:confirm", priority=100,
            )
            response = {
                "order": {
                    "id": order_id, "status": "pending_store_confirmation",
                    "subtotal": omr(subtotal), "deliveryFee": omr(fee), "total": omr(subtotal + fee),
                    "responseDueAt": due, "fulfillmentMode": mode,
                    "paymentMethod": payment_method, "version": 1,
                    "allowedActions": ["cancel"],
                },
                "duplicate": False, "repriced": bool(price_changes),
            }
            con.execute(
                """INSERT INTO idempotency_records(actor_id,operation,idempotency_key,payload_hash,response_json,created_at)
                   VALUES(?,'checkout',?,?,?,?)""",
                (actor["accountId"], idempotency_key, request_hash, dumps(response), stamp),
            )
            con.execute(
                "INSERT INTO analytics_events(id,event_type,actor_hash,entity_kind,entity_id,context_json,created_at) VALUES(?,?,?,?,?,?,?)",
                (new_id("evt"), "checkout", _actor_hash(actor), "order", order_id,
                 dumps({"merchantId": cart["merchant_id"], "branchId": cart["branch_id"], "mode": mode}), stamp),
            )
            return response

    def _insert_notification(
        self, con, target_kind: str, target_id: str, title_ar: str, title_en: str,
        body_ar: str, body_en: str, route: str, requires_action: bool,
        dedupe_key: str, *, priority: int = 0, expires_at: str = "",
    ) -> str:
        existing = con.execute(
            "SELECT id FROM notifications WHERE target_kind=? AND target_id=? AND dedupe_key=?",
            (target_kind, target_id, dedupe_key),
        ).fetchone()
        if existing:
            return existing["id"]
        notification_id = new_id("ntf")
        con.execute(
            """INSERT INTO notifications(
                id,target_kind,target_id,title_ar,title_en,body_ar,body_en,route,requires_action,
                dedupe_key,read_at,acted_at,created_at,seen_at,acknowledged_at,dismissed_at,expires_at,priority)
               VALUES(?,?,?,?,?,?,?,?,?,?, '', '',?, '', '', '',?,?)""",
            (notification_id, target_kind, target_id, title_ar, title_en, body_ar, body_en,
             route, 1 if requires_action else 0, dedupe_key, now_iso(), expires_at, priority),
        )
        return notification_id

    def _order_for_actor(self, con, actor: dict, order_id: str):
        order = con.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if not order:
            raise DomainError("order_not_found", 404)
        if actor["role"] == "shopper" and order["account_id"] != actor["accountId"]:
            raise DomainError("order_not_found", 404)
        if actor["role"] in MERCHANT_ROLES and order["merchant_id"] != actor["merchantId"]:
            raise DomainError("order_not_found", 404)
        return order

    @staticmethod
    def _order_allowed_actions(actor: dict, order: dict) -> list[str]:
        if actor.get("role") == "shopper" and order.get("status") in {
            "pending_store_confirmation", "accepted", "preparing",
        }:
            return ["cancel"]
        return []

    def order_detail(self, actor, order_id: str) -> dict:
        order_id = clean_text(order_id, 90, True)
        with connect() as con:
            actor = self._require_actor(con, actor, {"shopper", *MERCHANT_ROLES, *ADMIN_ROLES})
            if actor["role"] in ADMIN_ROLES:
                self._require_admin_permission(con, actor, "order.read")
            order = self._order_for_actor(con, actor, order_id) if actor["role"] not in ADMIN_ROLES else con.execute(
                "SELECT * FROM orders WHERE id=?", (order_id,)
            ).fetchone()
            if not order:
                raise DomainError("order_not_found", 404)
            result = dict(order)
            result["subtotal"] = omr(result.pop("subtotal_baisa"))
            result["deliveryFee"] = omr(result.pop("delivery_fee_baisa"))
            result["total"] = omr(result.pop("total_baisa"))
            result["address"] = loads(result.pop("address_snapshot"), {})
            result["returnPolicy"] = loads(result.pop("policy_snapshot"), {})
            result["allowedActions"] = self._order_allowed_actions(actor, result)
            result["items"] = []
            for row in con.execute("SELECT * FROM order_items WHERE order_id=?", (order_id,)):
                item = dict(row)
                item["name"] = loads(item.pop("name_snapshot"), {})
                item["components"] = loads(item.pop("component_snapshot"), [])
                item["unitPrice"] = omr(item.pop("unit_price_baisa"))
                result["items"].append(item)
            result["timeline"] = [
                {**dict(row), "detail": loads(row["detail_json"], {})}
                for row in con.execute("SELECT * FROM order_events WHERE order_id=? ORDER BY created_at,id", (order_id,))
            ]
            return result

    def decide_order(self, actor, order_id: str, decision: str) -> dict:
        target = {"accept": "accepted", "reject": "rejected"}.get(decision)
        if not target:
            raise DomainError("invalid_order_decision", 422)
        return self.transition_order(actor, order_id, target)

    def transition_order(
        self, actor, order_id: str, target_status: str, *, expected_version: Any = None,
        reason: str = "", idempotency_key: str = "",
    ) -> dict:
        order_id = clean_text(order_id, 90, True)
        target_status = clean_text(target_status, 40, True)
        reason = clean_text(reason, 500)
        idempotency_key = clean_text(idempotency_key, 120)
        stamp = now_iso()
        with connect(immediate=True) as con:
            actor = self._require_actor(con, actor, MERCHANT_ROLES)
            require_permission(actor, "order.manage", merchant_id=actor["merchantId"], con=con)
            order = self._order_for_actor(con, actor, order_id)
            operation = f"order_transition:{actor['merchantId']}:{order_id}"
            transition_hash = _payload_hash({
                "merchantId": actor["merchantId"], "orderId": order_id,
                "target": target_status, "expectedVersion": expected_version, "reason": reason,
            })
            if idempotency_key:
                replay = con.execute(
                    """SELECT payload_hash,response_json FROM idempotency_records
                       WHERE actor_id=? AND operation=? AND idempotency_key=?""",
                    (actor["accountId"], operation, idempotency_key),
                ).fetchone()
                if replay:
                    if replay["payload_hash"] != transition_hash:
                        raise DomainError("idempotency_key_reused", 409)
                    result = loads(replay["response_json"], {})
                    result["duplicate"] = True
                    return result
            if expected_version not in (None, ""):
                version = _bounded_int(expected_version, 1, 2_000_000_000, "invalid_order_version")
                if order["version"] != version:
                    raise DomainError("order_version_conflict", 409, {"currentVersion": order["version"]})
            if order["status"] == target_status:
                return {"id": order_id, "status": target_status, "version": order["version"], "duplicate": True}
            if target_status not in ORDER_TRANSITIONS.get(order["status"], set()):
                raise DomainError("order_stage_conflict", 409, {"current": order["status"], "target": target_status})
            if order["status"] == "pending_store_confirmation" and order["response_due_at"] and order["response_due_at"] <= stamp:
                self._release_order_inventory(con, order_id, restore_consumed=False)
                con.execute(
                    "UPDATE orders SET status='expired',version=version+1,updated_at=? WHERE id=?",
                    (stamp, order_id),
                )
                self._order_event(con, order, "expired", actor, "response_deadline", stamp)
                self._insert_notification(
                    con, "account", order["account_id"], "انتهت مهلة المتجر", "Store response expired",
                    "تم تحرير حجز المنتجات ولم يتم تحصيل أي مبلغ", "The stock hold was released and no payment was taken",
                    f"shopper:order:{order_id}", False, f"order:{order_id}:expired",
                )
                result = {"id": order_id, "status": "expired", "version": order["version"] + 1, "expired": True, "duplicate": False}
                if idempotency_key:
                    con.execute(
                        """INSERT INTO idempotency_records(actor_id,operation,idempotency_key,payload_hash,response_json,created_at)
                           VALUES(?,?,?,?,?,?)""",
                        (actor["accountId"], operation, idempotency_key,
                         transition_hash, dumps(result), stamp),
                    )
                return result
            if target_status == "ready_for_pickup" and order["fulfillment_mode"] != "pickup":
                raise DomainError("order_transition_not_valid_for_fulfillment", 409)
            if target_status == "out_for_delivery" and order["fulfillment_mode"] == "pickup":
                raise DomainError("order_transition_not_valid_for_fulfillment", 409)
            if target_status == "accepted":
                self._consume_order_inventory(con, order_id, stamp)
            elif target_status in {"rejected", "cancelled", "expired"}:
                self._release_order_inventory(con, order_id, restore_consumed=target_status == "cancelled")
            cancellation = reason if target_status == "cancelled" else order["cancellation_reason"]
            updated = con.execute(
                """UPDATE orders SET status=?,cancellation_reason=?,version=version+1,updated_at=?
                   WHERE id=? AND status=? AND version=?""",
                (target_status, cancellation, stamp, order_id, order["status"], order["version"]),
            ).rowcount
            if updated != 1:
                raise DomainError("order_version_conflict", 409)
            self._order_event(con, order, target_status, actor, reason, stamp)
            con.execute(
                """UPDATE notifications SET acted_at=? WHERE target_kind='merchant' AND target_id=?
                   AND dedupe_key=? AND acted_at=''""",
                (stamp, actor["merchantId"], f"order:{order_id}:confirm"),
            )
            labels = {
                "accepted": ("أكد المتجر طلبك", "Store confirmed your order"),
                "rejected": ("تعذر تأكيد الطلب", "Order was not confirmed"),
                "preparing": ("يتم تجهيز طلبك", "Your order is being prepared"),
                "ready_for_pickup": ("طلبك جاهز للاستلام", "Your order is ready for pickup"),
                "out_for_delivery": ("طلبك في الطريق", "Your order is out for delivery"),
                "completed": ("اكتمل طلبك", "Your order is complete"),
                "cancelled": ("تم إلغاء الطلب", "Order cancelled"),
            }
            title_ar, title_en = labels[target_status]
            self._insert_notification(
                con, "account", order["account_id"], title_ar, title_en,
                title_ar, title_en, f"shopper:order:{order_id}", False,
                f"order:{order_id}:{target_status}:v{order['version'] + 1}",
            )
            result = {"id": order_id, "status": target_status, "version": order["version"] + 1, "duplicate": False}
            if idempotency_key:
                con.execute(
                    """INSERT INTO idempotency_records(actor_id,operation,idempotency_key,payload_hash,response_json,created_at)
                       VALUES(?,?,?,?,?,?)""",
                    (actor["accountId"], operation, idempotency_key,
                     transition_hash, dumps(result), stamp),
                )
            return result

    def _consume_order_inventory(self, con, order_id: str, stamp: str) -> None:
        reservations = list(con.execute(
            "SELECT * FROM inventory_reservations WHERE order_id=? AND status='pending' ORDER BY product_id",
            (order_id,),
        ))
        if not reservations:
            consumed = con.execute(
                "SELECT COUNT(*) n FROM inventory_reservations WHERE order_id=? AND status='consumed'", (order_id,)
            ).fetchone()["n"]
            if consumed:
                return
            raise DomainError("inventory_reservation_missing", 409)
        for reservation in reservations:
            inventory = con.execute(
                """SELECT stock_mode,quantity,availability FROM product_branch_inventory
                   WHERE product_id=? AND branch_id=? AND active=1""",
                (reservation["product_id"], reservation["branch_id"]),
            ).fetchone()
            if not inventory:
                raise DomainError("stock_unavailable", 409, {"productId": reservation["product_id"]})
            if inventory["stock_mode"] == "tracked" and inventory["quantity"] < reservation["quantity"]:
                raise DomainError("stock_unavailable", 409, {"productId": reservation["product_id"]})
            if inventory["stock_mode"] != "tracked" and inventory["availability"] not in {"in_stock", "low_stock"}:
                raise DomainError("stock_unavailable", 409, {"productId": reservation["product_id"]})
        for reservation in reservations:
            inventory = con.execute(
                "SELECT stock_mode FROM product_branch_inventory WHERE product_id=? AND branch_id=?",
                (reservation["product_id"], reservation["branch_id"]),
            ).fetchone()
            if inventory["stock_mode"] == "tracked":
                changed = con.execute(
                    """UPDATE product_branch_inventory SET quantity=quantity-?,
                       availability=CASE WHEN quantity-?<=0 THEN 'out_of_stock' WHEN quantity-?<=2 THEN 'low_stock' ELSE 'in_stock' END,
                       updated_at=? WHERE product_id=? AND branch_id=? AND quantity>=?""",
                    (reservation["quantity"], reservation["quantity"], reservation["quantity"], stamp,
                     reservation["product_id"], reservation["branch_id"], reservation["quantity"]),
                ).rowcount
                if changed != 1:
                    raise DomainError("stock_unavailable", 409, {"productId": reservation["product_id"]})
        con.execute(
            "UPDATE inventory_reservations SET status='consumed' WHERE order_id=? AND status='pending'", (order_id,)
        )

    def _release_order_inventory(self, con, order_id: str, *, restore_consumed: bool) -> None:
        con.execute(
            "UPDATE inventory_reservations SET status='released' WHERE order_id=? AND status='pending'", (order_id,)
        )
        if not restore_consumed:
            return
        stamp = now_iso()
        consumed = list(con.execute(
            "SELECT * FROM inventory_reservations WHERE order_id=? AND status='consumed'", (order_id,)
        ))
        for reservation in consumed:
            inventory = con.execute(
                "SELECT stock_mode FROM product_branch_inventory WHERE product_id=? AND branch_id=?",
                (reservation["product_id"], reservation["branch_id"]),
            ).fetchone()
            if inventory and inventory["stock_mode"] == "tracked":
                con.execute(
                    """UPDATE product_branch_inventory SET quantity=quantity+?,
                       availability=CASE WHEN quantity+?>2 THEN 'in_stock' ELSE 'low_stock' END,updated_at=?
                       WHERE product_id=? AND branch_id=?""",
                    (reservation["quantity"], reservation["quantity"], stamp,
                     reservation["product_id"], reservation["branch_id"]),
                )
        con.execute(
            "UPDATE inventory_reservations SET status='restored' WHERE order_id=? AND status='consumed'", (order_id,)
        )

    def _order_event(self, con, order, target_status: str, actor: dict, reason: str, stamp: str) -> None:
        con.execute(
            """INSERT INTO order_events(id,order_id,event_type,from_status,to_status,actor_kind,actor_id,detail_json,created_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (new_id("oevt"), order["id"], "status_changed", order["status"], target_status,
             "merchant" if actor["role"] in MERCHANT_ROLES else actor["role"], actor["accountId"],
             dumps({"reason": reason} if reason else {}), stamp),
        )

    def cancel_order(self, actor, order_id: str, reason: str, expected_version: Any = None) -> dict:
        order_id = clean_text(order_id, 90, True)
        reason = clean_text(reason, 500, True)
        with connect(immediate=True) as con:
            actor = self._require_actor(con, actor, {"shopper", *MERCHANT_ROLES})
            if actor["role"] in MERCHANT_ROLES:
                require_permission(actor, "order.manage", merchant_id=actor["merchantId"], con=con)
            order = self._order_for_actor(con, actor, order_id)
            if expected_version not in (None, "") and order["version"] != _bounded_int(expected_version, 1, 2_000_000_000, "invalid_order_version"):
                raise DomainError("order_version_conflict", 409, {"currentVersion": order["version"]})
            if order["status"] == "cancelled":
                return {"id": order_id, "status": "cancelled", "version": order["version"], "duplicate": True}
            if order["status"] not in {"pending_store_confirmation", "accepted", "preparing"}:
                raise DomainError("order_cannot_be_cancelled", 409)
            stamp = now_iso()
            self._release_order_inventory(con, order_id, restore_consumed=order["status"] in {"accepted", "preparing"})
            changed = con.execute(
                """UPDATE orders SET status='cancelled',cancellation_reason=?,version=version+1,updated_at=?
                   WHERE id=? AND status=? AND version=?""",
                (reason, stamp, order_id, order["status"], order["version"]),
            ).rowcount
            if changed != 1:
                raise DomainError("order_version_conflict", 409)
            self._order_event(con, order, "cancelled", actor, reason, stamp)
            target_kind = "merchant" if actor["role"] == "shopper" else "account"
            target_id = order["merchant_id"] if target_kind == "merchant" else order["account_id"]
            self._insert_notification(
                con, target_kind, target_id, "تم إلغاء الطلب", "Order cancelled",
                "راجع الطلب لمعرفة السبب", "Open the order to review the reason",
                f"{'merchant' if target_kind == 'merchant' else 'shopper'}:order:{order_id}",
                False, f"order:{order_id}:cancelled:v{order['version'] + 1}",
            )
            return {"id": order_id, "status": "cancelled", "version": order["version"] + 1, "duplicate": False}

    def expire_orders(self, actor: dict | None = None, *, at: str | None = None, limit: int = 100) -> dict:
        limit = _bounded_int(limit, 1, 1000, "invalid_limit")
        stamp = at or now_iso()
        with connect(immediate=True) as con:
            if actor:
                self._require_admin_permission(con, actor, "order.read")
            orders = list(con.execute(
                """SELECT * FROM orders WHERE status='pending_store_confirmation'
                   AND response_due_at<>'' AND response_due_at<=? ORDER BY response_due_at LIMIT ?""",
                (stamp, limit),
            ))
            for order in orders:
                self._release_order_inventory(con, order["id"], restore_consumed=False)
                changed = con.execute(
                    """UPDATE orders SET status='expired',version=version+1,updated_at=?
                       WHERE id=? AND status='pending_store_confirmation' AND version=?""",
                    (stamp, order["id"], order["version"]),
                ).rowcount
                if not changed:
                    continue
                system_actor = {"role": "system", "accountId": "system"}
                self._order_event(con, order, "expired", system_actor, "response_deadline", stamp)
                self._insert_notification(
                    con, "account", order["account_id"], "انتهت مهلة المتجر", "Store response expired",
                    "تم تحرير حجز المنتجات", "The stock hold was released",
                    f"shopper:order:{order['id']}", False, f"order:{order['id']}:expired",
                )
            return {"expired": len(orders), "at": stamp}

    # ---------- inventory verification and merchant configuration ----------

    def inventory_action(self, actor, payload: dict) -> dict:
        branch_id = clean_text(payload.get("branchId"), 90, True)
        product_id = clean_text(payload.get("productId"), 90, True)
        action = clean_text(payload.get("action"), 30, True)
        if action not in {
            "increment", "decrement", "set_exact", "in_stock", "low_stock",
            "out_of_stock", "pause", "resume",
        }:
            raise DomainError("invalid_inventory_action", 422)
        stamp = now_iso()
        with connect(immediate=True) as con:
            actor = self._require_actor(con, actor, MERCHANT_ROLES)
            inventory = con.execute(
                """SELECT i.*,p.merchant_id,p.name_ar,p.name_en FROM product_branch_inventory i
                   JOIN products p ON p.id=i.product_id
                   JOIN store_branches b ON b.id=i.branch_id AND b.merchant_id=p.merchant_id
                   WHERE i.product_id=? AND i.branch_id=? AND p.merchant_id=?""",
                (product_id, branch_id, actor["merchantId"]),
            ).fetchone()
            if not inventory:
                raise DomainError("product_not_owned", 404)
            before = dict(inventory)
            quantity = inventory["quantity"]
            availability = inventory["availability"]
            active = inventory["active"]
            if action in {"increment", "decrement", "set_exact"}:
                amount = _bounded_int(payload.get("quantity", 1), 0, 1_000_000, "invalid_quantity")
                if action == "increment":
                    quantity = min(1_000_000, quantity + max(1, amount))
                elif action == "decrement":
                    quantity = max(0, quantity - max(1, amount))
                else:
                    quantity = amount
                pending = con.execute(
                    """SELECT COALESCE(SUM(quantity),0) n FROM inventory_reservations
                       WHERE product_id=? AND branch_id=? AND status='pending'""",
                    (product_id, branch_id),
                ).fetchone()["n"]
                if inventory["stock_mode"] == "tracked" and quantity < pending:
                    raise DomainError("inventory_below_reserved", 409, {"reserved": pending})
                availability = "in_stock" if quantity > 2 else "low_stock" if quantity else "out_of_stock"
            elif action in {"in_stock", "low_stock", "out_of_stock"}:
                availability = action
                if inventory["stock_mode"] == "tracked" and action == "out_of_stock":
                    pending = con.execute(
                        """SELECT COALESCE(SUM(quantity),0) n FROM inventory_reservations
                           WHERE product_id=? AND branch_id=? AND status='pending'""",
                        (product_id, branch_id),
                    ).fetchone()["n"]
                    if pending:
                        raise DomainError("inventory_below_reserved", 409, {"reserved": pending})
                    quantity = 0
            elif action == "pause":
                active = 0
                availability = "paused"
            elif action == "resume":
                active = 1
                availability = "in_stock" if inventory["stock_mode"] != "tracked" or quantity > 2 else "low_stock" if quantity else "out_of_stock"
            verified_at = stamp if payload.get("seenAndVerified") is True else inventory["last_stock_verified_at"]
            con.execute(
                """UPDATE product_branch_inventory SET quantity=?,availability=?,active=?,
                   last_stock_verified_at=?,stale_at=CASE WHEN ?<>'' THEN '' ELSE stale_at END,
                   freshness_status=CASE WHEN ?<>'' THEN 'fresh' ELSE freshness_status END,
                   stale_enforcement=CASE WHEN ?<>'' THEN '' ELSE stale_enforcement END,updated_at=?
                   WHERE product_id=? AND branch_id=?""",
                (quantity, availability, active, verified_at, verified_at, verified_at, verified_at,
                 stamp, product_id, branch_id),
            )
            audit_id = new_id("iaudit")
            con.execute(
                """INSERT INTO inventory_audits(
                    id,merchant_id,branch_id,status,due_at,confirmed_at,confirmed_by,summary,created_at)
                   VALUES(?,?,?,'partial',?,'','',?,?)""",
                (audit_id, actor["merchantId"], branch_id, stamp,
                 dumps({"action": action, "verified": payload.get("seenAndVerified") is True}), stamp),
            )
            after = {"quantity": quantity, "availability": availability, "active": active, "last_stock_verified_at": verified_at}
            con.execute(
                """INSERT INTO inventory_audit_events(
                    id,audit_id,product_id,event_type,before_json,after_json,actor_id,created_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (new_id("iaevt"), audit_id, product_id, action, dumps(before), dumps(after), actor["accountId"], stamp),
            )
            return {"productId": product_id, "branchId": branch_id, "auditId": audit_id, **after}

    def quick_stock(self, actor, branch_id: str) -> dict:
        branch_id = clean_text(branch_id, 90, True)
        with connect() as con:
            actor = self._require_actor(con, actor, MERCHANT_ROLES)
            if not con.execute(
                "SELECT 1 FROM store_branches WHERE id=? AND merchant_id=? AND active=1",
                (branch_id, actor["merchantId"]),
            ).fetchone():
                raise DomainError("branch_not_owned", 403)
            cadence = _bounded_int(settings(con).get("inventoryCadenceHours", 24), 1, 24 * 365, "invalid_inventory_cadence")
            cutoff = (datetime.now(UTC) - timedelta(hours=cadence)).isoformat()
            rows = [dict(row) for row in con.execute(
                """SELECT p.id,p.name_ar,p.name_en,i.stock_mode,i.quantity,i.availability,
                    i.last_stock_verified_at,i.stale_at,i.freshness_status,i.stale_enforcement,
                    CASE WHEN i.quantity<=2 THEN 100
                         WHEN i.last_stock_verified_at='' OR i.last_stock_verified_at<? THEN 80
                         ELSE 0 END priority
                   FROM products p JOIN product_branch_inventory i ON i.product_id=p.id
                   WHERE p.merchant_id=? AND i.branch_id=? AND p.active=1 AND p.archived_at='' AND i.active=1
                   ORDER BY priority DESC,i.last_stock_verified_at ASC,p.id LIMIT 200""",
                (cutoff, actor["merchantId"], branch_id),
            )]
            total = con.execute(
                "SELECT COUNT(*) n FROM product_branch_inventory WHERE branch_id=? AND active=1", (branch_id,)
            ).fetchone()["n"]
            return {"branchId": branch_id, "items": rows, "remainingCount": max(0, total - len(rows)), "cadenceHours": cadence}

    def confirm_stock(self, actor, branch_id: str, changes: list) -> dict:
        if not isinstance(changes, list):
            raise DomainError("inventory_changes_must_be_list", 422)
        branch_id = clean_text(branch_id, 90, True)
        if len(changes) > 500:
            raise DomainError("too_many_inventory_changes", 422)
        stamp = now_iso()
        with connect(immediate=True) as con:
            actor = self._require_actor(con, actor, MERCHANT_ROLES)
            if not con.execute(
                "SELECT 1 FROM store_branches WHERE id=? AND merchant_id=? AND active=1",
                (branch_id, actor["merchantId"]),
            ).fetchone():
                raise DomainError("branch_not_owned", 403)
            audit_id = new_id("iaudit")
            verified_ids: set[str] = set()
            events = []
            for change in changes:
                if not isinstance(change, dict):
                    raise DomainError("invalid_inventory_change", 422)
                product_id = clean_text(change.get("productId"), 90, True)
                if product_id in verified_ids:
                    raise DomainError("duplicate_inventory_product", 422, {"productId": product_id})
                verified_ids.add(product_id)
                inventory = con.execute(
                    """SELECT i.* FROM product_branch_inventory i JOIN products p ON p.id=i.product_id
                       WHERE i.product_id=? AND i.branch_id=? AND p.merchant_id=?""",
                    (product_id, branch_id, actor["merchantId"]),
                ).fetchone()
                if not inventory:
                    raise DomainError("product_not_owned", 404)
                quantity = _bounded_int(change.get("quantity", inventory["quantity"]), 0, 1_000_000, "invalid_quantity")
                pending = con.execute(
                    "SELECT COALESCE(SUM(quantity),0) n FROM inventory_reservations WHERE product_id=? AND branch_id=? AND status='pending'",
                    (product_id, branch_id),
                ).fetchone()["n"]
                if inventory["stock_mode"] == "tracked" and quantity < pending:
                    raise DomainError("inventory_below_reserved", 409, {"productId": product_id, "reserved": pending})
                availability = clean_text(change.get("availability"), 30)
                if inventory["stock_mode"] == "tracked":
                    availability = "in_stock" if quantity > 2 else "low_stock" if quantity else "out_of_stock"
                elif availability not in {"in_stock", "low_stock", "out_of_stock", "paused"}:
                    availability = inventory["availability"]
                con.execute(
                    """UPDATE product_branch_inventory SET quantity=?,availability=?,last_stock_verified_at=?,
                       stale_at='',freshness_status='fresh',stale_enforcement='',updated_at=?
                       WHERE product_id=? AND branch_id=?""",
                    (quantity, availability, stamp, stamp, product_id, branch_id),
                )
                after = {"quantity": quantity, "availability": availability, "last_stock_verified_at": stamp}
                events.append((new_id("iaevt"), audit_id, product_id, "verified", dumps(dict(inventory)), dumps(after), actor["accountId"], stamp))
            total = con.execute(
                "SELECT COUNT(*) n FROM product_branch_inventory WHERE branch_id=? AND active=1", (branch_id,)
            ).fetchone()["n"]
            status = "confirmed" if total and len(verified_ids) == total else "partial"
            con.execute(
                """INSERT INTO inventory_audits(
                    id,merchant_id,branch_id,status,due_at,confirmed_at,confirmed_by,summary,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (audit_id, actor["merchantId"], branch_id, status, stamp,
                 stamp if status == "confirmed" else "", actor["accountId"] if status == "confirmed" else "",
                 dumps({"verified": len(verified_ids), "total": total, "remaining": max(0, total - len(verified_ids))}), stamp),
            )
            if events:
                con.executemany(
                    """INSERT INTO inventory_audit_events(
                        id,audit_id,product_id,event_type,before_json,after_json,actor_id,created_at)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    events,
                )
            return {
                "ok": True, "auditId": audit_id, "status": status,
                "verifiedCount": len(verified_ids), "remainingCount": max(0, total - len(verified_ids)),
                "confirmedAt": stamp if status == "confirmed" else None,
            }

    def confirm_inventory_remaining(self, actor, audit_id: str) -> dict:
        """Explicitly confirm the unseen remainder unchanged; never done implicitly."""
        audit_id = clean_text(audit_id, 90, True)
        stamp = now_iso()
        with connect(immediate=True) as con:
            actor = self._require_actor(con, actor, MERCHANT_ROLES)
            audit = con.execute(
                "SELECT * FROM inventory_audits WHERE id=? AND merchant_id=?", (audit_id, actor["merchantId"])
            ).fetchone()
            if not audit:
                raise DomainError("inventory_audit_not_found", 404)
            if audit["status"] == "confirmed":
                return {"auditId": audit_id, "status": "confirmed", "duplicate": True, "confirmedAt": audit["confirmed_at"]}
            seen_ids = {
                row["product_id"] for row in con.execute(
                    "SELECT product_id FROM inventory_audit_events WHERE audit_id=?", (audit_id,)
                )
            }
            rows = list(con.execute(
                """SELECT i.product_id FROM product_branch_inventory i JOIN products p ON p.id=i.product_id
                   WHERE i.branch_id=? AND p.merchant_id=? AND i.active=1""",
                (audit["branch_id"], actor["merchantId"]),
            ))
            remaining = [row["product_id"] for row in rows if row["product_id"] not in seen_ids]
            if remaining:
                marks = ",".join("?" for _ in remaining)
                con.execute(
                    f"""UPDATE product_branch_inventory SET last_stock_verified_at=?,stale_at='',
                        freshness_status='fresh',stale_enforcement='',updated_at=?
                        WHERE branch_id=? AND product_id IN ({marks})""",
                    (stamp, stamp, audit["branch_id"], *remaining),
                )
            summary = loads(audit["summary"], {})
            summary["confirmedUnchanged"] = len(remaining)
            con.execute(
                """UPDATE inventory_audits SET status='confirmed',confirmed_at=?,confirmed_by=?,summary=?
                   WHERE id=? AND status!='confirmed'""",
                (stamp, actor["accountId"], dumps(summary), audit_id),
            )
            return {"auditId": audit_id, "status": "confirmed", "confirmedAt": stamp, "confirmedUnchanged": len(remaining), "duplicate": False}

    def configure_fulfillment(self, actor, branch_id: str, payload: dict) -> dict:
        branch_id = clean_text(branch_id, 90, True)
        modes = {}
        for key in ("pickup", "office", "home"):
            value = payload.get(key) or {}
            if not isinstance(value, dict):
                raise DomainError("invalid_fulfillment_configuration", 422)
            modes[key] = {
                "enabled": bool(value.get("enabled", key == "pickup")),
                "fee": _strict_baisa(value.get("fee", "0.000")),
                "minimum": _strict_baisa(value.get("minimum", "0.000")),
                "free": _strict_baisa(value.get("freeThreshold", "0.000")),
            }
            if modes[key]["free"] and modes[key]["free"] < modes[key]["minimum"]:
                raise DomainError("free_threshold_below_minimum", 422, {"mode": key})
        zones = payload.get("zones") or []
        if not isinstance(zones, list) or len(zones) > 200:
            raise DomainError("invalid_delivery_zones", 422)
        stamp = now_iso()
        with connect(immediate=True) as con:
            actor = self._require_actor(con, actor, {"merchant_owner", "merchant_manager"})
            if not con.execute(
                "SELECT 1 FROM store_branches WHERE id=? AND merchant_id=? AND active=1",
                (branch_id, actor["merchantId"]),
            ).fetchone():
                raise DomainError("branch_not_owned", 403)
            normalized_zones = []
            legacy_area_ids = []
            for zone in zones:
                if not isinstance(zone, dict):
                    raise DomainError("invalid_delivery_zone", 422)
                mode = clean_text(zone.get("mode"), 30, True)
                if mode not in {"office_delivery", "home_delivery"}:
                    raise DomainError("invalid_delivery_zone_mode", 422)
                wilayah_id = clean_text(zone.get("wilayahId"), 90)
                area_id = clean_text(zone.get("areaId"), 90)
                if not wilayah_id and not area_id:
                    raise DomainError("delivery_zone_location_required", 422)
                if area_id:
                    location = con.execute(
                        "SELECT parent_id FROM locations WHERE id=? AND kind='area' AND active=1", (area_id,)
                    ).fetchone()
                    if not location or (wilayah_id and location["parent_id"] != wilayah_id):
                        raise DomainError("invalid_delivery_area", 422)
                    wilayah_id = wilayah_id or location["parent_id"]
                    legacy_area_ids.append(area_id)
                elif not con.execute(
                    "SELECT 1 FROM locations WHERE id=? AND kind='wilayat' AND active=1", (wilayah_id,)
                ).fetchone():
                    raise DomainError("invalid_delivery_wilayah", 422)
                mode_key = "office" if mode == "office_delivery" else "home"
                normalized_zones.append({
                    "id": new_id("zone"), "mode": mode, "wilayah": wilayah_id, "area": area_id,
                    "fee": _strict_baisa(zone.get("fee", omr(modes[mode_key]["fee"]))),
                    "minimum": _strict_baisa(zone.get("minimum", omr(modes[mode_key]["minimum"]))),
                    "free": _strict_baisa(zone.get("freeThreshold", omr(modes[mode_key]["free"]))),
                    "eta": clean_text(zone.get("eta"), 80),
                })
            con.execute(
                """INSERT INTO fulfillment_profiles(
                    branch_id,pickup_enabled,office_enabled,office_fee_baisa,office_minimum_baisa,office_free_threshold_baisa,
                    home_enabled,home_fee_baisa,home_minimum_baisa,home_free_threshold_baisa,zones_json,eta_text,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(branch_id) DO UPDATE SET pickup_enabled=excluded.pickup_enabled,
                   office_enabled=excluded.office_enabled,office_fee_baisa=excluded.office_fee_baisa,
                   office_minimum_baisa=excluded.office_minimum_baisa,office_free_threshold_baisa=excluded.office_free_threshold_baisa,
                   home_enabled=excluded.home_enabled,home_fee_baisa=excluded.home_fee_baisa,
                   home_minimum_baisa=excluded.home_minimum_baisa,home_free_threshold_baisa=excluded.home_free_threshold_baisa,
                   zones_json=excluded.zones_json,eta_text=excluded.eta_text,updated_at=excluded.updated_at""",
                (branch_id, int(modes["pickup"]["enabled"]), int(modes["office"]["enabled"]),
                 modes["office"]["fee"], modes["office"]["minimum"], modes["office"]["free"],
                 int(modes["home"]["enabled"]), modes["home"]["fee"], modes["home"]["minimum"], modes["home"]["free"],
                 dumps(sorted(set(legacy_area_ids))), clean_text(payload.get("eta"), 80), stamp),
            )
            con.execute("DELETE FROM branch_delivery_zones WHERE branch_id=?", (branch_id,))
            for zone in normalized_zones:
                con.execute(
                    """INSERT INTO branch_delivery_zones(
                        id,branch_id,mode,wilayah_id,area_id,fee_baisa,minimum_baisa,free_threshold_baisa,
                        eta_text,active,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,1,?,?)""",
                    (zone["id"], branch_id, zone["mode"], zone["wilayah"], zone["area"], zone["fee"],
                     zone["minimum"], zone["free"], zone["eta"], stamp, stamp),
                )
            return {"branchId": branch_id, "pickup": modes["pickup"], "office": modes["office"], "home": modes["home"], "zones": normalized_zones}

    def save_return_policy(self, actor, payload: dict) -> dict:
        return_days = _bounded_int(payload.get("returnWindowDays", 0), 0, 365, "invalid_return_window")
        exchange_days = _bounded_int(payload.get("exchangeWindowDays", 0), 0, 365, "invalid_exchange_window")
        conditions = clean_text(payload.get("conditions"), 2000, True)
        excluded = payload.get("excludedCategories") or []
        if not isinstance(excluded, list) or len(excluded) > 100:
            raise DomainError("invalid_excluded_categories", 422)
        stamp = now_iso()
        with connect(immediate=True) as con:
            actor = self._require_actor(con, actor, {"merchant_owner", "merchant_manager"})
            for category_id in excluded:
                if not con.execute("SELECT 1 FROM product_categories WHERE id=?", (clean_text(category_id, 80, True),)).fetchone():
                    raise DomainError("invalid_excluded_category", 422)
            current = con.execute(
                "SELECT COALESCE(MAX(version),0) n FROM merchant_return_policies WHERE merchant_id=?",
                (actor["merchantId"],),
            ).fetchone()["n"]
            version = current + 1
            policy_id = new_id("policy")
            con.execute(
                "UPDATE merchant_return_policies SET active=0 WHERE merchant_id=? AND active=1",
                (actor["merchantId"],),
            )
            con.execute(
                """INSERT INTO merchant_return_policies(
                    id,merchant_id,version,return_window_days,exchange_window_days,conditions_text,
                    receipt_required,excluded_categories,contact_method,notes,active,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,1,?)""",
                (policy_id, actor["merchantId"], version, return_days, exchange_days, conditions,
                 int(payload.get("receiptRequired", True)), dumps(excluded),
                 clean_text(payload.get("contactMethod"), 120, True), clean_text(payload.get("notes"), 1000), stamp),
            )
            con.execute(
                "UPDATE merchants SET return_policy_id=?,updated_at=? WHERE id=?",
                (policy_id, stamp, actor["merchantId"]),
            )
            return {"id": policy_id, "version": version, "active": True, "legalFloor": "oman_consumer_rights_apply"}

    def create_branch(self, actor, payload: dict) -> dict:
        latitude, longitude = payload.get("latitude"), payload.get("longitude")
        if (latitude is None) != (longitude is None):
            raise DomainError("complete_location_required", 422)
        if latitude is not None:
            try:
                latitude, longitude = float(latitude), float(longitude)
            except (TypeError, ValueError) as exc:
                raise DomainError("valid_location_required", 422) from exc
            if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
                raise DomainError("valid_location_required", 422)
            if not coordinates_in_muscat(latitude, longitude):
                raise DomainError("coordinates_outside_muscat", 422)
        stamp = now_iso()
        with connect(immediate=True) as con:
            actor = self._require_actor(con, actor, {"merchant_owner"})
            plan = self._active_plan(con, actor["merchantId"])
            maximum = _bounded_int(plan["entitlements"].get("branches", 0), 0, 10_000, "invalid_plan_limit")
            count = con.execute(
                "SELECT COUNT(*) n FROM store_branches WHERE merchant_id=? AND active=1", (actor["merchantId"],)
            ).fetchone()["n"]
            if count >= maximum:
                raise DomainError("plan_branch_limit", 409, {"limit": maximum})
            wilayah_id = clean_text(payload.get("wilayahId"), 90, True)
            area_id = clean_text(payload.get("areaId"), 90, True)
            area = con.execute(
                """SELECT parent_id FROM locations WHERE id=? AND kind='area' AND active=1""", (area_id,)
            ).fetchone()
            if not area or area["parent_id"] != wilayah_id:
                raise DomainError("invalid_branch_location", 422)
            branch_id = new_id("branch")
            con.execute(
                """INSERT INTO store_branches(
                    id,merchant_id,name_ar,name_en,wilayah_id,area_id,address_text,latitude,longitude,hours_json,
                    status,active,public_visible,created_at,updated_at,phone,timezone,last_open_status_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,'draft',1,0,?,?,?,?,?)""",
                (branch_id, actor["merchantId"], clean_text(payload.get("nameAr"), 120, True),
                 clean_text(payload.get("nameEn"), 120, True), wilayah_id, area_id,
                 clean_text(payload.get("address"), 300, True), latitude, longitude,
                 dumps(payload.get("hours") if isinstance(payload.get("hours"), dict) else {}),
                 stamp, stamp, clean_text(payload.get("phone"), 30), "Asia/Muscat", ""),
            )
            con.execute(
                """INSERT INTO fulfillment_profiles(
                    branch_id,pickup_enabled,office_enabled,office_fee_baisa,office_minimum_baisa,office_free_threshold_baisa,
                    home_enabled,home_fee_baisa,home_minimum_baisa,home_free_threshold_baisa,zones_json,eta_text,updated_at)
                   VALUES(?,1,0,0,0,0,0,0,0,0,'[]','',?)""",
                (branch_id, stamp),
            )
            return {"id": branch_id, "status": "draft", "publicVisible": False, "planUsage": {"branches": count + 1, "limit": maximum}}

    def add_merchant_member(self, actor, payload: dict) -> dict:
        role = clean_text(payload.get("role"), 30, True)
        if role not in {"merchant_manager", "merchant_staff"}:
            raise DomainError("invalid_merchant_member_role", 422)
        stamp = now_iso()
        with connect(immediate=True) as con:
            actor = self._require_actor(con, actor, {"merchant_owner"})
            supplied_account_id = clean_text(payload.get("accountId"), 90)
            supplied_phone = clean_text(payload.get("accountPhone"), 30)
            if not supplied_account_id and not supplied_phone:
                raise DomainError("merchant_member_account_required", 422)

            phone_account = None
            if supplied_phone:
                phone = normalize_phone(supplied_phone)
                phone_account = con.execute(
                    "SELECT id,name,status FROM accounts WHERE phone=?", (phone,),
                ).fetchone()
                if not phone_account or phone_account["status"] != "active":
                    raise DomainError("account_not_found", 404)

            id_account = None
            if supplied_account_id:
                id_account = con.execute(
                    "SELECT id,name,status FROM accounts WHERE id=?", (supplied_account_id,),
                ).fetchone()
                if not id_account or id_account["status"] != "active":
                    raise DomainError("account_not_found", 404)

            if phone_account and id_account and phone_account["id"] != id_account["id"]:
                raise DomainError("merchant_member_identity_mismatch", 409)
            account = phone_account or id_account
            account_id = account["id"]
            if account_id == actor["accountId"]:
                raise DomainError("merchant_owner_cannot_be_member", 409)

            foreign_binding = con.execute(
                """SELECT 1 WHERE
                   EXISTS(SELECT 1 FROM account_roles
                          WHERE account_id=?
                            AND role IN('merchant_owner','merchant_manager','merchant_staff')
                            AND merchant_id<>? AND active=1)
                   OR EXISTS(SELECT 1 FROM merchant_members
                             WHERE account_id=? AND merchant_id<>? AND status='active')
                   OR EXISTS(SELECT 1 FROM merchants
                             WHERE owner_account_id=? AND id<>? AND active=1)""",
                (
                    account_id, actor["merchantId"], account_id, actor["merchantId"],
                    account_id, actor["merchantId"],
                ),
            ).fetchone()
            if foreign_binding:
                raise DomainError("merchant_member_cross_tenant", 409)
            plan = self._active_plan(con, actor["merchantId"])
            maximum = _bounded_int(plan["entitlements"].get("staff", 0), 0, 10_000, "invalid_plan_limit")
            count = con.execute(
                "SELECT COUNT(*) n FROM merchant_members WHERE merchant_id=? AND status='active'",
                (actor["merchantId"],),
            ).fetchone()["n"]
            existing = con.execute(
                "SELECT status FROM merchant_members WHERE merchant_id=? AND account_id=?",
                (actor["merchantId"], account_id),
            ).fetchone()
            if not existing and count >= maximum:
                raise DomainError("plan_staff_limit", 409, {"limit": maximum})
            con.execute(
                """INSERT INTO merchant_members(merchant_id,account_id,role,status,created_at)
                   VALUES(?,?,?,'active',?) ON CONFLICT(merchant_id,account_id) DO UPDATE SET
                   role=excluded.role,status='active'""",
                (actor["merchantId"], account_id, role, stamp),
            )
            con.execute(
                """UPDATE account_roles SET active=0
                   WHERE account_id=? AND merchant_id=?
                     AND role IN('merchant_manager','merchant_staff') AND role<>?""",
                (account_id, actor["merchantId"], role),
            )
            con.execute(
                """INSERT INTO account_roles(account_id,role,merchant_id,active) VALUES(?,?,?,1)
                   ON CONFLICT(account_id,role,merchant_id) DO UPDATE SET active=1""",
                (account_id, role, actor["merchantId"]),
            )
            return {
                "accountId": account_id, "name": account["name"],
                "merchantId": actor["merchantId"], "role": role, "status": "active",
                "planUsage": {"staff": count + (0 if existing else 1), "limit": maximum},
            }

    # ---------- supplier hub, engagement and notifications ----------

    def supplier_campaigns(self, actor) -> list:
        stamp = now_iso()
        with connect() as con:
            actor = self._require_actor(con, actor, MERCHANT_ROLES)
            require_permission(actor, "supplier_hub.read", merchant_id=actor["merchantId"], con=con)
            plan = self._active_plan(con, actor["merchantId"])
            if not plan["entitlements"].get("supplierHub"):
                raise DomainError("supplier_hub_not_in_plan", 403)
            branch_rows = list(con.execute(
                "SELECT wilayah_id,area_id FROM store_branches WHERE merchant_id=? AND active=1",
                (actor["merchantId"],),
            ))
            wilayats = {row["wilayah_id"] for row in branch_rows}
            areas = {row["area_id"] for row in branch_rows if row["area_id"]}
            categories = {
                row["category_id"] for row in con.execute(
                    "SELECT DISTINCT category_id FROM products WHERE merchant_id=? AND active=1 AND archived_at=''",
                    (actor["merchantId"],),
                )
            }
            rows = con.execute(
                """SELECT c.id,c.title_ar,c.title_en,c.payload,c.starts_at,c.ends_at,
                    s.id supplier_id,s.name_ar supplier_name_ar,s.name_en supplier_name_en,s.logo_path
                   FROM supplier_campaigns c JOIN suppliers s ON s.id=c.supplier_id
                   WHERE c.status='approved' AND s.status='approved'
                     AND (c.starts_at='' OR c.starts_at<=?) AND (c.ends_at='' OR c.ends_at>?)
                   ORDER BY c.created_at DESC""",
                (stamp, stamp),
            ).fetchall()
            output = []
            for row in rows:
                item = dict(row)
                payload = loads(item.pop("payload"), {})
                if not isinstance(payload, dict):
                    continue
                target_wilayats = set(payload.get("targetWilayats") or [])
                target_areas = set(payload.get("targetAreas") or [])
                target_categories = set(payload.get("targetCategories") or [])
                if target_wilayats and not target_wilayats.intersection(wilayats):
                    continue
                if target_areas and not target_areas.intersection(areas):
                    continue
                if target_categories and not target_categories.intersection(categories):
                    continue
                # Campaign payload is curated public B2B copy only. Private supplier
                # documents and merchant/customer data are never joined here.
                item["campaign"] = payload
                output.append(item)
            return output

    def create_supplier_lead(self, actor, campaign_id: str, action_kind: str, note: str = "", *, idempotency_key: str) -> dict:
        campaign_id = clean_text(campaign_id, 90, True)
        action_kind = clean_text(action_kind, 30, True)
        if action_kind not in {"quote_request", "contact", "whatsapp", "save_supplier"}:
            raise DomainError("invalid_supplier_action", 422)
        idempotency_key = clean_text(idempotency_key, 120, True)
        with connect(immediate=True) as con:
            actor = self._require_actor(con, actor, MERCHANT_ROLES)
            require_permission(actor, "supplier_hub.read", merchant_id=actor["merchantId"], con=con)
            plan = self._active_plan(con, actor["merchantId"])
            if not plan["entitlements"].get("supplierHub"):
                raise DomainError("supplier_hub_not_in_plan", 403)
            visible_ids = {item["id"] for item in self._supplier_campaign_rows(con, actor)}
            if campaign_id not in visible_ids:
                raise DomainError("supplier_campaign_not_found", 404)
            operation = f"supplier_lead:{actor['merchantId']}"
            request_hash = _payload_hash({
                "merchantId": actor["merchantId"], "campaignId": campaign_id,
                "action": action_kind, "note": note,
            })
            existing = con.execute(
                """SELECT payload_hash,response_json FROM idempotency_records
                   WHERE actor_id=? AND operation=? AND idempotency_key=?""",
                (actor["accountId"], operation, idempotency_key),
            ).fetchone()
            if existing:
                if existing["payload_hash"] != request_hash:
                    raise DomainError("idempotency_key_reused", 409)
                result = loads(existing["response_json"], {})
                result["duplicate"] = True
                return result
            lead_id = new_id("slead")
            stamp = now_iso()
            con.execute(
                """INSERT INTO supplier_leads(id,campaign_id,merchant_id,action_kind,note,created_at)
                   VALUES(?,?,?,?,?,?)""",
                (lead_id, campaign_id, actor["merchantId"], action_kind, clean_text(note, 500), stamp),
            )
            result = {"id": lead_id, "campaignId": campaign_id, "action": action_kind, "status": "recorded", "duplicate": False}
            con.execute(
                """INSERT INTO idempotency_records(actor_id,operation,idempotency_key,payload_hash,response_json,created_at)
                   VALUES(?,?,?,?,?,?)""",
                (actor["accountId"], operation, idempotency_key, request_hash, dumps(result), stamp),
            )
            return result

    def _supplier_campaign_rows(self, con, actor: dict) -> list[dict]:
        """Internal transaction-safe visibility check used by lead creation."""
        stamp = now_iso()
        branch_rows = list(con.execute(
            "SELECT wilayah_id,area_id FROM store_branches WHERE merchant_id=? AND active=1",
            (actor["merchantId"],),
        ))
        wilayats = {row["wilayah_id"] for row in branch_rows}
        areas = {row["area_id"] for row in branch_rows if row["area_id"]}
        categories = {row["category_id"] for row in con.execute(
            "SELECT DISTINCT category_id FROM products WHERE merchant_id=? AND active=1", (actor["merchantId"],)
        )}
        output = []
        for row in con.execute(
            """SELECT c.*,s.status supplier_status FROM supplier_campaigns c JOIN suppliers s ON s.id=c.supplier_id
               WHERE c.status='approved' AND s.status='approved'
                 AND (c.starts_at='' OR c.starts_at<=?) AND (c.ends_at='' OR c.ends_at>?)""",
            (stamp, stamp),
        ):
            payload = loads(row["payload"], {})
            if not isinstance(payload, dict):
                continue
            if payload.get("targetWilayats") and not set(payload["targetWilayats"]).intersection(wilayats):
                continue
            if payload.get("targetAreas") and not set(payload["targetAreas"]).intersection(areas):
                continue
            if payload.get("targetCategories") and not set(payload["targetCategories"]).intersection(categories):
                continue
            output.append(dict(row))
        return output

    def _public_ad_campaign(self, con, campaign_id: str, stamp: str):
        """Return a currently public ad or ``None`` without leaking why it is hidden."""
        campaign = con.execute(
            """SELECT a.* FROM ad_campaigns a JOIN merchants m ON m.id=a.owner_id
               WHERE a.id=? AND a.owner_kind='merchant' AND a.status='approved'
                 AND m.status='approved' AND m.active=1
                 AND (a.starts_at='' OR a.starts_at<=?)
                 AND (a.ends_at='' OR a.ends_at>?)""",
            (campaign_id, stamp, stamp),
        ).fetchone()
        if not campaign:
            return None
        landing_kind = campaign["landing_kind"]
        landing_id = campaign["landing_id"]
        merchant_id = campaign["owner_id"]
        if landing_kind == "store":
            public = con.execute(
                """SELECT 1 FROM store_branches
                   WHERE id=? AND merchant_id=? AND status='approved'
                     AND active=1 AND public_visible=1""",
                (landing_id, merchant_id),
            ).fetchone()
        elif landing_kind == "product":
            public = con.execute(
                f"""SELECT 1 FROM products p JOIN merchants m ON m.id=p.merchant_id
                    JOIN product_branch_inventory i ON i.product_id=p.id
                    JOIN store_branches b ON b.id=i.branch_id AND b.merchant_id=p.merchant_id
                    WHERE p.id=? AND p.merchant_id=? AND {PUBLIC_PRODUCT_WHERE} LIMIT 1""",
                (landing_id, merchant_id),
            ).fetchone()
        elif landing_kind == "bundle":
            public = con.execute(
                """SELECT 1 FROM bundles bu JOIN merchants m ON m.id=bu.merchant_id
                   JOIN store_branches b ON b.id=bu.branch_id AND b.merchant_id=bu.merchant_id
                   WHERE bu.id=? AND bu.merchant_id=? AND bu.status='approved'
                     AND bu.moderation_status='approved'
                     AND (bu.starts_at='' OR bu.starts_at<=?)
                     AND (bu.ends_at='' OR bu.ends_at>?)
                     AND m.status='approved' AND m.active=1
                     AND b.status='approved' AND b.active=1 AND b.public_visible=1""",
                (landing_id, merchant_id, stamp, stamp),
            ).fetchone()
        else:
            return None
        return campaign if public else None

    def _action_prompt_is_owned(self, con, actor: dict, notification_id: str) -> bool:
        if actor["role"] in ADMIN_ROLES:
            return bool(con.execute(
                """SELECT 1 FROM notifications WHERE id=? AND target_kind='admin'
                   AND target_id IN('admin',?)""",
                (notification_id, actor["accountId"]),
            ).fetchone())
        if actor["role"] in MERCHANT_ROLES:
            target_kind, target_id = "merchant", actor["merchantId"]
        elif actor["role"] == "supplier_advertiser":
            target_kind, target_id = "supplier", actor["supplierId"]
        else:
            target_kind, target_id = "account", actor["accountId"]
        return bool(con.execute(
            "SELECT 1 FROM notifications WHERE id=? AND target_kind=? AND target_id=?",
            (notification_id, target_kind, target_id),
        ).fetchone())

    def record_event(self, *args, actor: dict | None = None, context: dict | None = None) -> dict:
        """Record a bounded event.

        Supports both the domain-first call
        ``record_event(event_type, kind, id, actor=..., context=...)`` and the
        HTTP adapter call ``record_event(actor, event_type, kind, id, context)``.
        """
        if args and (args[0] is None or isinstance(args[0], dict)):
            if len(args) not in {4, 5}:
                raise DomainError("invalid_analytics_event", 422)
            actor, event_type, entity_kind, entity_id = args[:4]
            if len(args) == 5:
                context = args[4]
        elif len(args) == 3:
            event_type, entity_kind, entity_id = args
        else:
            raise DomainError("invalid_analytics_event", 422)
        event_type = clean_text(event_type, 40, True)
        entity_kind = clean_text(entity_kind, 30, True)
        entity_id = clean_text(entity_id, 90, True)
        if event_type not in TRACKED_EVENTS:
            raise DomainError("invalid_analytics_event", 422)
        if context is not None and not isinstance(context, dict):
            raise DomainError("invalid_analytics_context", 422)
        event_key = clean_text(context.get("eventId"), 90) if context else ""
        if event_key and not OPAQUE_EVENT_ID.fullmatch(event_key):
            raise DomainError("invalid_analytics_event_id", 422)
        safe_context = {}
        for key in ("branchId", "areaId", "wilayahId", "categoryId", "source", "placement", "campaignId"):
            if context and key in context:
                safe_context[key] = clean_text(context[key], 90)
        with connect(immediate=True) as con:
            safe_actor = self._require_actor(con, actor) if actor else None
            ad_event_id = ""
            normalized_ad_event = ""
            if event_type in ACTION_PROMPT_EVENTS:
                if not safe_actor:
                    raise DomainError("authentication_required", 401)
                if entity_kind not in {"notification", "action_prompt"}:
                    raise DomainError("invalid_action_prompt_entity", 422)
                if not self._action_prompt_is_owned(con, safe_actor, entity_id):
                    raise DomainError("notification_not_found", 404)
                entity_kind = "notification"
                source = safe_context.get("source", "")
                safe_context = {"source": source} if source in {
                    "foreground", "push", "app_resume", "polling", "deep_link",
                } else {}
            elif event_type in AD_EVENT_NAMES:
                if entity_kind not in {"ad", "advertisement", "campaign"}:
                    raise DomainError("invalid_ad_event_entity", 422)
                context_campaign_id = safe_context.get("campaignId", "")
                if context_campaign_id and context_campaign_id != entity_id:
                    raise DomainError("invalid_ad_event_campaign", 422)
                campaign_id = context_campaign_id or entity_id
                stamp = now_iso()
                campaign = self._public_ad_campaign(con, campaign_id, stamp)
                if not campaign:
                    raise DomainError("ad_campaign_not_found", 404)
                normalized_ad_event = AD_EVENT_NAMES[event_type]
                actor_hash = _actor_hash(safe_actor)
                event_reference = campaign["landing_id"]
                if event_key:
                    # The client key is never persisted. Its one-way fingerprint
                    # makes network retries idempotent without storing PII.
                    event_reference = "event:" + hashlib.sha256(
                        f"{campaign_id}:{normalized_ad_event}:{event_key}".encode("utf-8")
                    ).hexdigest()
                    duplicate = con.execute(
                        """SELECT id FROM ad_events
                           WHERE campaign_id=? AND event_type=? AND entity_id=?""",
                        (campaign_id, normalized_ad_event, event_reference),
                    ).fetchone()
                    if duplicate:
                        return {
                            "id": duplicate["id"], "recorded": False, "duplicate": True,
                            "campaignId": campaign_id, "adEvent": normalized_ad_event,
                        }
                if normalized_ad_event == "impression" and actor_hash:
                    current = datetime.now(UTC)
                    day_start = current.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
                    frequency_cap = max(1, min(int(campaign["frequency_cap"] or 1), 100))
                    impressions = con.execute(
                        """SELECT COUNT(*) n FROM ad_events
                           WHERE campaign_id=? AND event_type='impression'
                             AND actor_hash=? AND created_at>=?""",
                        (campaign_id, actor_hash, day_start),
                    ).fetchone()["n"]
                    if impressions >= frequency_cap:
                        return {
                            "id": "", "recorded": False, "duplicate": False,
                            "frequencyCapped": True, "campaignId": campaign_id,
                            "adEvent": normalized_ad_event,
                        }
                ad_event_id = new_id("adevt")
                con.execute(
                    """INSERT INTO ad_events(
                        id,campaign_id,event_type,actor_hash,entity_id,created_at)
                       VALUES(?,?,?,?,?,?)""",
                    (ad_event_id, campaign_id, normalized_ad_event, actor_hash, event_reference, stamp),
                )
                # Campaign and placement are sourced from the approved row, not
                # from an untrusted analytics payload.
                entity_kind, entity_id = "ad", campaign_id
                safe_context = {
                    "campaignId": campaign_id, "placement": campaign["placement"],
                }
            elif entity_kind == "product":
                if not self._public_cart_item(con, "product", entity_id, safe_context.get("branchId", "")):
                    # Product events require a public branch context to avoid tracking private IDs.
                    raise DomainError("product_not_found", 404)
            elif entity_kind in {"store", "branch"}:
                branch_id = safe_context.get("branchId") or entity_id
                if not con.execute(
                    f"""SELECT 1 FROM store_branches b JOIN merchants m ON m.id=b.merchant_id
                        WHERE b.id=? AND {PUBLIC_BRANCH_WHERE}""", (branch_id,)
                ).fetchone():
                    raise DomainError("store_not_found", 404)
            event_id = new_id("evt")
            con.execute(
                """INSERT INTO analytics_events(id,event_type,actor_hash,entity_kind,entity_id,context_json,created_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (event_id, event_type, _actor_hash(safe_actor), entity_kind, entity_id, dumps(safe_context), now_iso()),
            )
            result = {"id": event_id, "recorded": True}
            if ad_event_id:
                result.update({
                    "adEventId": ad_event_id, "campaignId": entity_id,
                    "adEvent": normalized_ad_event, "duplicate": False,
                })
            return result

    def set_favorite(self, actor, entity_kind: str, entity_id: str, *, branch_id: str = "", saved: bool = True) -> dict:
        if entity_kind not in {"product", "store", "bundle"}:
            raise DomainError("invalid_favorite_kind", 422)
        entity_id = clean_text(entity_id, 90, True)
        branch_id = clean_text(branch_id, 90)
        with connect(immediate=True) as con:
            actor = self._require_actor(con, actor, {"shopper"})
            if entity_kind == "product" and not self._public_cart_item(con, "product", entity_id, branch_id):
                raise DomainError("product_not_found", 404)
            if entity_kind == "bundle" and not self._public_cart_item(con, "bundle", entity_id, branch_id):
                raise DomainError("bundle_not_found", 404)
            if entity_kind == "store" and not con.execute(
                f"""SELECT 1 FROM store_branches b JOIN merchants m ON m.id=b.merchant_id
                    WHERE b.id=? AND {PUBLIC_BRANCH_WHERE}""", (entity_id,)
            ).fetchone():
                raise DomainError("store_not_found", 404)
            if saved:
                con.execute(
                    "INSERT OR IGNORE INTO favorites(account_id,entity_kind,entity_id,created_at) VALUES(?,?,?,?)",
                    (actor["accountId"], entity_kind, entity_id, now_iso()),
                )
            else:
                con.execute(
                    "DELETE FROM favorites WHERE account_id=? AND entity_kind=? AND entity_id=?",
                    (actor["accountId"], entity_kind, entity_id),
                )
            return {"entityKind": entity_kind, "entityId": entity_id, "saved": saved}

    def report_product(self, actor, product_id: str, reason: str, detail: str = "", *, branch_id: str) -> dict:
        product_id = clean_text(product_id, 90, True)
        reason = clean_text(reason, 40, True)
        branch_id = clean_text(branch_id, 90, True)
        if reason not in REPORT_REASONS:
            raise DomainError("invalid_report_reason", 422)
        with connect(immediate=True) as con:
            actor = self._require_actor(con, actor, {"shopper"})
            if not self._public_cart_item(con, "product", product_id, branch_id):
                raise DomainError("product_not_found", 404)
            duplicate = con.execute(
                """SELECT id FROM product_reports WHERE reporter_account_id=? AND product_id=?
                   AND reason=? AND status IN('open','under_review')""",
                (actor["accountId"], product_id, reason),
            ).fetchone()
            if duplicate:
                return {"id": duplicate["id"], "status": "open", "duplicate": True}
            report_id = new_id("report")
            stamp = now_iso()
            con.execute(
                """INSERT INTO product_reports(
                    id,reporter_account_id,product_id,reason,detail,status,reviewed_by,created_at,updated_at)
                   VALUES(?,?,?,?,?,'open','',?,?)""",
                (report_id, actor["accountId"], product_id, reason, clean_text(detail, 1000), stamp, stamp),
            )
            self._insert_notification(
                con, "admin", "admin", "بلاغ منتج جديد", "New product report",
                "يوجد بلاغ يحتاج المراجعة", "A product report needs review",
                f"admin:report:{report_id}", True, f"product-report:{report_id}", priority=90,
            )
            return {"id": report_id, "status": "open", "duplicate": False}

    def notification_action(self, actor, notification_id: str, action: str) -> dict:
        notification_id = clean_text(notification_id, 90, True)
        action = clean_text(action, 20, True)
        column = {"seen": "seen_at", "read": "read_at", "ack": "acknowledged_at", "dismiss": "dismissed_at"}.get(action)
        if not column:
            raise DomainError("invalid_notification_action", 422)
        with connect(immediate=True) as con:
            actor = self._require_actor(con, actor)
            if actor["role"] in ADMIN_ROLES:
                self._require_admin_permission(con, actor, "notification.manage")
                row = con.execute(
                    """SELECT * FROM notifications WHERE id=? AND target_kind='admin'
                       AND target_id IN('admin',?)""",
                    (notification_id, actor["accountId"]),
                ).fetchone()
                if not row:
                    raise DomainError("notification_not_found", 404)
                if action == "dismiss" and row["requires_action"] and not row["acted_at"]:
                    raise DomainError("required_notification_cannot_be_dismissed", 409)
                stamp = row[column] or now_iso()
                con.execute(f"UPDATE notifications SET {column}=? WHERE id=? AND {column}=''", (stamp, notification_id))
                return {"id": notification_id, "action": action, "at": stamp, "acted": bool(row["acted_at"])}
            target_kind = "account"
            target_id = actor["accountId"]
            if actor["role"] in MERCHANT_ROLES:
                target_kind, target_id = "merchant", actor["merchantId"]
            elif actor["role"] == "supplier_advertiser":
                target_kind, target_id = "supplier", actor["supplierId"]
            row = con.execute(
                "SELECT * FROM notifications WHERE id=? AND target_kind=? AND target_id=?",
                (notification_id, target_kind, target_id),
            ).fetchone()
            if not row:
                raise DomainError("notification_not_found", 404)
            if action == "dismiss" and row["requires_action"] and not row["acted_at"]:
                raise DomainError("required_notification_cannot_be_dismissed", 409)
            stamp = row[column] or now_iso()
            con.execute(f"UPDATE notifications SET {column}=? WHERE id=? AND {column}=''", (stamp, notification_id))
            return {"id": notification_id, "action": action, "at": stamp, "acted": bool(row["acted_at"])}

    def notifications(self, actor, *, pending_only: bool = False, limit: int = 100) -> list[dict]:
        limit = _bounded_int(limit, 1, 200, "invalid_limit")
        with connect() as con:
            actor = self._require_actor(con, actor)
            if actor["role"] in ADMIN_ROLES:
                self._require_admin_permission(con, actor, "notification.manage")
                pending = "AND acted_at='' AND dismissed_at='' AND (expires_at='' OR expires_at>?)" if pending_only else ""
                args: list[Any] = [actor["accountId"]]
                if pending_only:
                    args.append(now_iso())
                args.append(limit)
                return [dict(row) for row in con.execute(
                    f"""SELECT * FROM notifications WHERE target_kind='admin' AND target_id IN('admin',?) {pending}
                        ORDER BY requires_action DESC,priority DESC,created_at DESC LIMIT ?""", args
                )]
            if actor["role"] in MERCHANT_ROLES:
                target_kind, target_id = "merchant", actor["merchantId"]
            elif actor["role"] == "supplier_advertiser":
                target_kind, target_id = "supplier", actor["supplierId"]
            else:
                target_kind, target_id = "account", actor["accountId"]
            pending = "AND acted_at='' AND dismissed_at='' AND (expires_at='' OR expires_at>?)" if pending_only else ""
            args: list[Any] = [target_kind, target_id]
            if pending_only:
                args.append(now_iso())
            args.append(limit)
            return [dict(row) for row in con.execute(
                f"""SELECT * FROM notifications WHERE target_kind=? AND target_id=? {pending}
                    ORDER BY requires_action DESC,priority DESC,created_at DESC LIMIT ?""", args
            )]

    # ---------- merchant analytics ----------

    def merchant_dashboard(self, actor) -> dict:
        with connect() as con:
            actor = self._require_actor(con, actor, MERCHANT_ROLES)
            merchant_id = actor["merchantId"]
            merchant = con.execute("SELECT * FROM merchants WHERE id=?", (merchant_id,)).fetchone()
            branches = [dict(row) for row in con.execute(
                "SELECT * FROM store_branches WHERE merchant_id=? ORDER BY active DESC,created_at", (merchant_id,)
            )]
            products = []
            for row in con.execute(
                """SELECT p.*,i.branch_id,i.stock_mode,i.quantity,i.availability,i.last_stock_verified_at,i.stale_at
                   FROM products p LEFT JOIN product_branch_inventory i ON i.product_id=p.id
                   WHERE p.merchant_id=? AND p.active=1 AND p.archived_at='' ORDER BY p.updated_at DESC LIMIT 300""",
                (merchant_id,),
            ):
                item = dict(row)
                item["price"] = omr(item.pop("price_baisa"))
                item["images"] = loads(item.pop("images_json"), [])
                products.append(item)
            orders = [dict(row) for row in con.execute(
                "SELECT * FROM orders WHERE merchant_id=? ORDER BY created_at DESC LIMIT 100", (merchant_id,)
            )]
            bundles = []
            for row in con.execute(
                """SELECT * FROM bundles WHERE merchant_id=? AND status!='archived'
                   ORDER BY updated_at DESC LIMIT 100""",
                (merchant_id,),
            ):
                item = dict(row)
                item["price"] = omr(item.pop("selling_price_baisa"))
                item["tags"] = loads(item.pop("tags_json", "[]"), [])
                bundles.append(item)
            campaigns = []
            for row in con.execute(
                """SELECT * FROM ad_campaigns WHERE owner_kind='merchant' AND owner_id=?
                   ORDER BY updated_at DESC LIMIT 100""",
                (merchant_id,),
            ):
                item = dict(row)
                item["target"] = loads(item.pop("target_json"), {})
                item["title_ar"] = item["target"].get("titleAr", "")
                item["title_en"] = item["target"].get("titleEn", "")
                item["metrics"] = {
                    event["event_type"]: event["count"]
                    for event in con.execute(
                        "SELECT event_type,COUNT(*) count FROM ad_events WHERE campaign_id=? GROUP BY event_type",
                        (item["id"],),
                    )
                }
                campaigns.append(item)
            plan = self._active_plan(con, merchant_id)
            entitlements = plan["entitlements"]
            usage = {
                "products": con.execute("SELECT COUNT(*) n FROM products WHERE merchant_id=? AND active=1 AND archived_at=''", (merchant_id,)).fetchone()["n"],
                "branches": con.execute("SELECT COUNT(*) n FROM store_branches WHERE merchant_id=? AND active=1", (merchant_id,)).fetchone()["n"],
                "staff": con.execute("SELECT COUNT(*) n FROM merchant_members WHERE merchant_id=? AND status='active'", (merchant_id,)).fetchone()["n"],
                "bundles": con.execute("SELECT COUNT(*) n FROM bundles WHERE merchant_id=? AND status!='archived'", (merchant_id,)).fetchone()["n"],
            }
            limits = {key: int(entitlements.get(key, 0)) for key in usage}
            analytics = self._merchant_analytics(con, merchant_id, entitlements.get("analytics", "basic"))
            stale = con.execute(
                """SELECT COUNT(*) n FROM product_branch_inventory i JOIN products p ON p.id=i.product_id
                   WHERE p.merchant_id=? AND i.active=1 AND i.stale_at<>''""", (merchant_id,)
            ).fetchone()["n"]
            due_orders = sum(1 for order in orders if order["status"] == "pending_store_confirmation")
            return {
                "merchant": dict(merchant), "branches": branches, "products": products,
                "bundles": bundles, "campaigns": campaigns, "orders": orders,
                "plan": {**dict(plan), "entitlements": entitlements, "price": omr(plan["price_baisa"])},
                "planUsage": {key: {"used": usage[key], "limit": limits[key]} for key in usage},
                "today": {"ordersNeedingAction": due_orders, "staleProducts": stale},
                "metrics": analytics,
                "campaignMetrics": {
                    "campaigns": len(campaigns),
                    "impressions": sum(item["metrics"].get("impression", 0) for item in campaigns),
                    "clicks": sum(item["metrics"].get("click", 0) for item in campaigns),
                },
            }

    def _merchant_analytics(self, con, merchant_id: str, level: str) -> dict:
        branches = [row["id"] for row in con.execute("SELECT id FROM store_branches WHERE merchant_id=?", (merchant_id,))]
        products = [row["id"] for row in con.execute("SELECT id FROM products WHERE merchant_id=?", (merchant_id,))]
        entity_ids = [merchant_id, *branches, *products]
        metrics: dict[str, int] = {}
        if entity_ids:
            marks = ",".join("?" for _ in entity_ids)
            metrics = {
                row["event_type"]: row["n"] for row in con.execute(
                    f"SELECT event_type,COUNT(*) n FROM analytics_events WHERE entity_id IN ({marks}) GROUP BY event_type",
                    entity_ids,
                )
            }
        metrics["orders"] = con.execute("SELECT COUNT(*) n FROM orders WHERE merchant_id=?", (merchant_id,)).fetchone()["n"]
        metrics["completedOrders"] = con.execute("SELECT COUNT(*) n FROM orders WHERE merchant_id=? AND status='completed'", (merchant_id,)).fetchone()["n"]
        if level == "advanced":
            views = metrics.get("product_view", 0) + metrics.get("store_view", 0)
            metrics["conversionRate"] = round(metrics["orders"] / views, 4) if views else 0
            metrics["searchNoResult"] = con.execute("SELECT COUNT(*) n FROM search_events WHERE result_count=0").fetchone()["n"]
            metrics["cancellations"] = con.execute("SELECT COUNT(*) n FROM orders WHERE merchant_id=? AND status IN('cancelled','rejected','expired')", (merchant_id,)).fetchone()["n"]
        return metrics

    def merchant_analytics(self, actor) -> dict:
        with connect() as con:
            actor = self._require_actor(con, actor, MERCHANT_ROLES)
            plan = self._active_plan(con, actor["merchantId"])
            return {
                "level": plan["entitlements"].get("analytics", "basic"),
                "metrics": self._merchant_analytics(con, actor["merchantId"], plan["entitlements"].get("analytics", "basic")),
            }

    def merchant_settings(self, actor) -> dict:
        with connect() as con:
            actor = self._require_actor(con, actor, MERCHANT_ROLES)
            merchant_id = actor["merchantId"]
            merchant = con.execute("SELECT * FROM merchants WHERE id=?", (merchant_id,)).fetchone()
            policy = con.execute(
                "SELECT * FROM merchant_return_policies WHERE merchant_id=? AND active=1 ORDER BY version DESC LIMIT 1",
                (merchant_id,),
            ).fetchone()
            branches = []
            for branch in con.execute(
                "SELECT * FROM store_branches WHERE merchant_id=? ORDER BY created_at", (merchant_id,)
            ):
                item = dict(branch)
                profile = con.execute(
                    "SELECT * FROM fulfillment_profiles WHERE branch_id=?", (branch["id"],)
                ).fetchone()
                item["fulfillment"] = dict(profile) if profile else None
                item["deliveryZones"] = [dict(row) for row in con.execute(
                    "SELECT * FROM branch_delivery_zones WHERE branch_id=? ORDER BY mode,area_id,wilayah_id",
                    (branch["id"],),
                )]
                branches.append(item)
            plan = self._active_plan(con, merchant_id)
            members = [dict(row) for row in con.execute(
                """SELECT mm.account_id,a.name,mm.role,mm.status,mm.created_at
                   FROM merchant_members mm JOIN accounts a ON a.id=mm.account_id
                   WHERE mm.merchant_id=? ORDER BY mm.created_at,mm.account_id""",
                (merchant_id,),
            )]
            location_master = [dict(row) for row in con.execute(
                """SELECT id,parent_id,kind,name_ar,name_en,sort_order,active
                   FROM locations WHERE active=1
                     AND kind IN('governorate','wilayat','area')
                   ORDER BY kind,sort_order,name_en,id"""
            )]
            return {
                "merchant": dict(merchant), "branches": branches,
                "members": members, "locationMaster": location_master,
                "returnPolicy": dict(policy) if policy else None,
                "plan": {**dict(plan), "entitlements": plan["entitlements"], "price": omr(plan["price_baisa"])},
                "externalIntegrations": {
                    "payments": {"configured": False, "enabled": bool(settings(con).get("paymentsEnabled", False))},
                    "whatsapp": {"configured": False, "enabled": bool(settings(con).get("whatsappEnabled", False))},
                },
            }

    # ---------- administrator control plane ----------

    def admin_overview(self, actor) -> dict:
        with connect() as con:
            actor = self._require_admin_permission(con, actor, "overview.view")
            permissions = self._admin_permissions(con, actor)
            counts = {
                name: con.execute(f"SELECT COUNT(*) n FROM {table}").fetchone()["n"]
                for name, table in {
                    "merchants": "merchants", "branches": "store_branches", "products": "products",
                    "orders": "orders", "bundles": "bundles", "ads": "ad_campaigns",
                    "suppliers": "suppliers", "reports": "product_reports",
                }.items()
            }
            pending = []
            if "*" in permissions or "merchant.review" in permissions:
                pending = [dict(row) for row in con.execute(
                    """SELECT a.id,a.merchant_id,a.status,a.reviewer_note,a.submitted_at,
                              a.created_at,a.updated_at,m.name_ar merchant_name_ar,
                              m.name_en merchant_name_en
                       FROM merchant_applications a JOIN merchants m ON m.id=a.merchant_id
                       WHERE a.status IN('submitted','under_review','changes_requested')
                       ORDER BY a.submitted_at LIMIT 100"""
                )]
            stale = con.execute(
                "SELECT COUNT(*) n FROM product_branch_inventory WHERE active=1 AND stale_at<>''"
            ).fetchone()["n"]
            return {
                "counts": counts, "pendingApplications": pending,
                "inventory": {"staleProducts": stale},
                "permissions": sorted(permissions),
                "settings": settings(con) if "*" in permissions or "settings.read" in permissions else {},
            }

    @staticmethod
    def _redact_admin_resource_item(resource: str, item: dict) -> dict:
        item = dict(item)
        for key in {
            "pin_hash", "token_hash", "refresh_hash", "storage_key", "private_path",
            "address_snapshot", "policy_snapshot", "idempotency_key", "payload_hash",
            "response_json",
        }:
            item.pop(key, None)
        if resource in {"applications", "merchant_applications"}:
            snapshot = loads(item.pop("payload", "{}"), {})
            if isinstance(snapshot, dict):
                item["requestedPlan"] = clean_text(snapshot.get("requestedPlan"), 80)
                item["branchId"] = clean_text(snapshot.get("branchId"), 90)
            item["containsPrivateApplicationData"] = True
        if resource == "merchants":
            item.pop("owner_account_id", None)
        if resource == "orders":
            item.pop("account_id", None)
            item["containsPrivateOrderData"] = True
        return item

    def admin_resource(self, actor, resource: str, filters: dict | None = None) -> dict:
        resource = clean_text(resource, 40, True)
        if resource not in ADMIN_RESOURCE_MAP:
            raise DomainError("admin_resource_not_found", 404)
        table, permission, order_column = ADMIN_RESOURCE_MAP[resource]
        filters = filters or {}
        limit = _bounded_int(filters.get("limit", 50), 1, 200, "invalid_limit")
        offset = _parse_cursor(filters.get("cursor"))
        with connect() as con:
            self._require_admin_permission(con, actor, permission)
            columns = {row["name"] for row in con.execute(f"PRAGMA table_info({table})")}
            clauses = []
            args: list[Any] = []
            status = clean_text(filters.get("status"), 40)
            if status and "status" in columns:
                clauses.append("status=?")
                args.append(status)
            query = clean_text(filters.get("query"), 80)
            searchable = [column for column in ("name_ar", "name_en", "title_ar", "title_en", "id") if column in columns]
            if query and searchable:
                clauses.append("(" + " OR ".join(f"{column} LIKE ?" for column in searchable) + ")")
                args.extend([f"%{query}%"] * len(searchable))
            where = " WHERE " + " AND ".join(clauses) if clauses else ""
            rows = [self._redact_admin_resource_item(resource, dict(row)) for row in con.execute(
                f"SELECT * FROM {table}{where} ORDER BY {order_column} DESC LIMIT ? OFFSET ?",
                (*args, limit + 1, offset),
            )]
            has_more = len(rows) > limit
            return {
                "resource": resource, "items": rows[:limit],
                "pagination": {"nextCursor": str(offset + limit) if has_more else None, "hasMore": has_more, "limit": limit},
            }

    def admin_application_detail(self, actor, application_id: str) -> dict:
        application_id = clean_text(application_id, 90, True)
        with connect() as con:
            self._require_admin_permission(con, actor, "merchant.review")
            row = con.execute(
                """SELECT a.id,a.merchant_id,a.status,a.reviewer_note,a.submitted_at,
                          a.created_at,a.updated_at,a.payload,m.name_ar merchant_name_ar,
                          m.name_en merchant_name_en,m.merchant_type,m.status merchant_status
                   FROM merchant_applications a JOIN merchants m ON m.id=a.merchant_id
                   WHERE a.id=?""",
                (application_id,),
            ).fetchone()
            if not row:
                raise DomainError("application_not_found", 404)
            application = dict(row)
            application["snapshot"] = loads(application.pop("payload"), {})
            steps = [
                {
                    "key": step["step_key"], "data": loads(step["payload_json"], {}),
                    "completedAt": step["completed_at"], "updatedAt": step["updated_at"],
                }
                for step in con.execute(
                    """SELECT step_key,payload_json,completed_at,updated_at
                       FROM merchant_application_steps WHERE application_id=? ORDER BY updated_at,step_key""",
                    (application_id,),
                )
            ]
            documents = [dict(document) for document in con.execute(
                """SELECT d.id,d.kind,d.media_id,d.review_status,d.reviewed_at,d.review_note,
                          mo.mime_type,mo.byte_size,mo.original_name
                   FROM merchant_documents d
                   LEFT JOIN private_media_objects mo ON mo.id=d.media_id AND mo.status='active'
                   WHERE d.application_id=? ORDER BY d.kind,d.created_at""",
                (application_id,),
            )]
            return {"application": application, "steps": steps, "documents": documents}

    def admin_application_document_decision(
        self, actor, application_id: str, document_id: str, payload: dict,
    ) -> dict:
        application_id = clean_text(application_id, 90, True)
        document_id = clean_text(document_id, 90, True)
        decision = clean_text(payload.get("decision"), 20, True)
        note = clean_text(payload.get("note"), 500)
        if decision not in {"approve", "reject"}:
            raise DomainError("invalid_document_decision", 422)
        if decision == "reject" and not note:
            raise DomainError("document_rejection_note_required", 422)
        target_status = "approved" if decision == "approve" else "rejected"
        with connect(immediate=True) as con:
            actor = self._require_admin_permission(con, actor, "merchant.review")
            document = con.execute(
                """SELECT d.*,a.status application_status FROM merchant_documents d
                   JOIN merchant_applications a ON a.id=d.application_id
                   WHERE d.id=? AND d.application_id=?""",
                (document_id, application_id),
            ).fetchone()
            if not document:
                raise DomainError("merchant_document_not_found", 404)
            if document["application_status"] not in {"submitted", "under_review", "changes_requested"}:
                raise DomainError("merchant_application_locked", 409)
            if document["review_status"] == target_status and document["review_note"] == note:
                return {
                    "id": document_id, "applicationId": application_id,
                    "status": target_status, "duplicate": True,
                }
            stamp = now_iso()
            con.execute(
                """UPDATE merchant_documents SET review_status=?,reviewed_by=?,reviewed_at=?,review_note=?
                   WHERE id=? AND application_id=?""",
                (target_status, actor["accountId"], stamp, note, document_id, application_id),
            )
            con.execute(
                """INSERT INTO admin_audit_logs(
                    id,actor_id,action,target_kind,target_id,before_json,after_json,reason,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (new_id("audit"), actor["accountId"], "merchant_document_reviewed",
                 "merchant_document", document_id,
                 dumps({"status": document["review_status"]}),
                 dumps({"status": target_status}), note, stamp),
            )
            return {
                "id": document_id, "applicationId": application_id,
                "status": target_status, "duplicate": False,
            }

    def _approval_trial_eligibility(self, con, actor: dict, *, manual: bool) -> dict:
        configured = settings(con)
        if manual:
            permissions = self._admin_permissions(con, actor)
            if "*" not in permissions and "plan.manage" not in permissions:
                raise DomainError("admin_permission_required", 403, {"permission": "plan.manage"})
            return {"eligible": True, "manual": True, "reason": "manual_admin_grant"}
        if configured.get("trialEnabled") is not True:
            return {"eligible": False, "manual": False, "reason": "trial_disabled"}
        cutoff = clean_text(configured.get("trialCutoffAt"), 50)
        if cutoff:
            try:
                cutoff_at = datetime.fromisoformat(cutoff.replace("Z", "+00:00"))
            except ValueError as exc:
                raise DomainError("trial_configuration_invalid", 500) from exc
            if cutoff_at.tzinfo is None:
                raise DomainError("trial_configuration_invalid", 500)
            if cutoff_at.astimezone(UTC) <= datetime.now(UTC):
                return {"eligible": False, "manual": False, "reason": "trial_cutoff_passed"}
        try:
            maximum = _bounded_int(
                configured.get("trialFirstApprovedMerchants", 0), 0, 1_000_000,
                "trial_configuration_invalid",
            )
        except DomainError as exc:
            raise DomainError("trial_configuration_invalid", 500) from exc
        granted = con.execute(
            """SELECT COUNT(DISTINCT merchant_id) n FROM merchant_subscriptions
               WHERE plan_id='early_trial'"""
        ).fetchone()["n"]
        if granted >= maximum:
            return {
                "eligible": False, "manual": False, "reason": "trial_quota_reached",
                "limit": maximum,
            }
        return {
            "eligible": True, "manual": False, "reason": "automatic_trial",
            "remainingBeforeGrant": maximum - granted,
        }

    def _approval_snapshot(self, con, application) -> dict:
        snapshot = loads(application["payload"], {})
        if not isinstance(snapshot, dict):
            raise DomainError("merchant_application_not_ready", 409)
        required_steps = {
            "owner", "business", "brand", "location", "hours", "documents",
            "fulfillment", "policy", "categories", "plan", "review",
        }
        completed = {
            row["step_key"] for row in con.execute(
                """SELECT step_key FROM merchant_application_steps
                   WHERE application_id=? AND completed_at<>''""",
                (application["id"],),
            )
        }
        missing = sorted(required_steps - completed)
        if missing:
            raise DomainError("merchant_application_not_ready", 409, {"missingSteps": missing})
        branch_id = clean_text(snapshot.get("branchId"), 90, True)
        policy_id = clean_text(snapshot.get("policyId"), 90, True)
        branch = con.execute(
            """SELECT 1 FROM store_branches WHERE id=? AND merchant_id=?
               AND status='submitted' AND active=1""",
            (branch_id, application["merchant_id"]),
        ).fetchone()
        policy = con.execute(
            """SELECT 1 FROM merchant_return_policies WHERE id=? AND merchant_id=? AND active=1""",
            (policy_id, application["merchant_id"]),
        ).fetchone()
        document_rows = list(con.execute(
            """SELECT d.kind,d.review_status,d.media_id FROM merchant_documents d
               JOIN private_media_objects mo ON mo.id=d.media_id
                AND mo.owner_kind='merchant_application' AND mo.owner_id=d.application_id
                AND mo.status='active'
               WHERE d.application_id=?""",
            (application["id"],),
        ))
        document_kinds = {
            row["kind"] for row in document_rows
            if row["review_status"] == "approved" and row["media_id"]
        }
        required_documents = {"storefront", "commercial_registration", "license"}
        if not branch or not policy or not required_documents.issubset(document_kinds):
            raise DomainError("merchant_documents_review_required", 409, {
                "required": sorted(required_documents), "approved": sorted(document_kinds),
            })
        snapshot["branchId"] = branch_id
        snapshot["policyId"] = policy_id
        return snapshot

    def admin_application_decision(self, actor, payload: dict) -> dict:
        application_id = clean_text(payload.get("applicationId"), 90, True)
        decision = clean_text(payload.get("decision"), 30, True)
        note = clean_text(payload.get("note"), 500)
        if decision not in {"approve", "reject", "changes_requested"}:
            raise DomainError("invalid_application_decision", 422)
        if decision != "approve" and not note:
            raise DomainError("application_decision_note_required", 422)
        target_status = "approved" if decision == "approve" else decision
        stamp = now_iso()
        with connect(immediate=True) as con:
            actor = self._require_admin_permission(con, actor, "merchant.review")
            application = con.execute(
                """SELECT a.*,m.owner_account_id,m.status merchant_status
                   FROM merchant_applications a JOIN merchants m ON m.id=a.merchant_id
                   WHERE a.id=?""",
                (application_id,),
            ).fetchone()
            if not application:
                raise DomainError("application_not_found", 404)
            if application["status"] == target_status:
                subscription = con.execute(
                    """SELECT plan_id,status FROM merchant_subscriptions
                       WHERE merchant_id=? ORDER BY created_at DESC LIMIT 1""",
                    (application["merchant_id"],),
                ).fetchone()
                return {
                    "id": application_id, "merchantId": application["merchant_id"],
                    "status": target_status, "duplicate": True,
                    "planId": subscription["plan_id"] if subscription else "",
                    "subscriptionStatus": subscription["status"] if subscription else "",
                    "trialGranted": bool(subscription and subscription["plan_id"] == "early_trial"),
                }
            if application["status"] in {"approved", "rejected"}:
                raise DomainError("merchant_application_already_decided", 409)
            if application["status"] not in {"submitted", "under_review"}:
                raise DomainError("merchant_application_not_ready", 409)

            plan_id = ""
            subscription_status = ""
            trial_granted = False
            branch_id = ""
            if decision == "approve":
                snapshot = self._approval_snapshot(con, application)
                requested_plan = clean_text(snapshot.get("requestedPlan"), 80, True)
                override_plan = clean_text(payload.get("planId"), 80)
                plan_id = override_plan or requested_plan
                permissions = self._admin_permissions(con, actor)
                if override_plan and override_plan != requested_plan:
                    if "*" not in permissions and "plan.manage" not in permissions:
                        raise DomainError("admin_permission_required", 403, {"permission": "plan.manage"})
                plan = con.execute(
                    "SELECT * FROM subscription_plans WHERE id=? AND active=1", (plan_id,)
                ).fetchone()
                if not plan:
                    raise DomainError("requested_plan_unavailable", 409)
                manual_trial = payload.get("manualTrialGrant") is True
                if manual_trial and plan_id != "early_trial":
                    raise DomainError("manual_trial_requires_trial_plan", 422)
                if plan_id == "early_trial":
                    trial = self._approval_trial_eligibility(con, actor, manual=manual_trial)
                    if not trial["eligible"]:
                        raise DomainError("trial_not_eligible", 409, trial)
                    subscription_status = "active"
                    trial_granted = True
                    starts_at = stamp
                    ends_at = (datetime.now(UTC) + timedelta(days=int(plan["duration_days"]))).isoformat()
                else:
                    # Reviewing the merchant does not prove a paid subscription.
                    # Activation belongs to a real, separately idempotent payment flow.
                    subscription_status = "pending_payment"
                    starts_at = ""
                    ends_at = ""
                con.execute(
                    """INSERT INTO merchant_subscriptions(
                        id,merchant_id,plan_id,starts_at,ends_at,status,granted_by,created_at)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (new_id("sub"), application["merchant_id"], plan_id, starts_at, ends_at,
                     subscription_status, actor["accountId"] if trial_granted else "", stamp),
                )
                branch_id = snapshot["branchId"]
                con.execute(
                    """UPDATE merchants SET status='approved',active=1,verified=0,updated_at=?
                       WHERE id=?""",
                    (stamp, application["merchant_id"]),
                )
                con.execute(
                    """UPDATE store_branches SET status='approved',public_visible=?,updated_at=?
                       WHERE id=? AND merchant_id=? AND active=1""",
                    (int(subscription_status == "active"), stamp, branch_id, application["merchant_id"]),
                )
                con.execute(
                    """UPDATE account_roles SET active=1 WHERE account_id=?
                       AND role='merchant_owner' AND merchant_id=?""",
                    (application["owner_account_id"], application["merchant_id"]),
                )
                title_ar, title_en = "تم اعتماد متجرك", "Your store was approved"
                if subscription_status == "active":
                    body_ar = "يمكنك الآن دخول مساحة التاجر وبدء إعداد الكتالوج"
                    body_en = "You can now open the merchant workspace and prepare your catalog"
                else:
                    body_ar = "تم اعتماد المتجر. أكمل الاشتراك من شاشة الباقة لبدء النشر"
                    body_en = "Your store is approved. Complete the plan payment before publishing"
            elif decision == "changes_requested":
                con.execute(
                    "UPDATE merchants SET status='changes_requested',updated_at=? WHERE id=?",
                    (stamp, application["merchant_id"]),
                )
                title_ar, title_en = "طلب متجرك يحتاج تحديثاً", "Your application needs an update"
                body_ar, body_en = note, note
            else:
                con.execute(
                    "UPDATE merchants SET status='rejected',active=0,updated_at=? WHERE id=?",
                    (stamp, application["merchant_id"]),
                )
                con.execute(
                    """UPDATE store_branches SET status='rejected',public_visible=0,updated_at=?
                       WHERE merchant_id=?""",
                    (stamp, application["merchant_id"]),
                )
                title_ar, title_en = "تحديث طلب المتجر", "Merchant application update"
                body_ar, body_en = note, note

            con.execute(
                """UPDATE merchant_applications SET status=?,reviewer_note=?,updated_at=? WHERE id=?""",
                (target_status, note, stamp, application_id),
            )
            revision = con.execute(
                """SELECT COUNT(*) n FROM admin_audit_logs
                   WHERE target_kind='merchant_application' AND target_id=?""",
                (application_id,),
            ).fetchone()["n"] + 1
            con.execute(
                """INSERT INTO notifications(
                    id,target_kind,target_id,title_ar,title_en,body_ar,body_en,route,
                    requires_action,dedupe_key,read_at,acted_at,created_at,priority)
                   VALUES(?,?,?,?,?,?,?,?,0,?,'','',?,50)""",
                (new_id("ntf"), "account", application["owner_account_id"], title_ar, title_en,
                 body_ar, body_en, "shopper:merchant-application",
                 f"merchant-application:{application_id}:{target_status}:v{revision}", stamp),
            )
            after = {
                "status": target_status, "planId": plan_id,
                "subscriptionStatus": subscription_status, "branchId": branch_id,
            }
            con.execute(
                """INSERT INTO admin_audit_logs(
                    id,actor_id,action,target_kind,target_id,before_json,after_json,reason,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (new_id("audit"), actor["accountId"], f"merchant_application_{target_status}",
                 "merchant_application", application_id,
                 dumps({"status": application["status"]}), dumps(after), note, stamp),
            )
            return {
                "id": application_id, "merchantId": application["merchant_id"],
                "status": target_status, "duplicate": False, "planId": plan_id,
                "subscriptionStatus": subscription_status, "trialGranted": trial_granted,
                "branchId": branch_id,
            }

    def admin_action(self, actor, resource: str, action: str, payload: dict) -> dict:
        resource = clean_text(resource, 40, True)
        action = clean_text(action, 40, True)
        target_id = clean_text(payload.get("id"), 90)
        reason = clean_text(payload.get("reason"), 500)
        permission_map = {
            "product": "catalog.moderate", "merchant": "merchant.manage",
            "supplier_campaign": "supplier_campaign.review", "ad": "ad.manage",
            "supplier": "supplier.manage",
            "report": "report.manage", "setting": "settings.manage",
            "plan": "plan.manage", "category": "category.manage",
            "location": "location.manage",
        }
        permission = permission_map.get(resource)
        if not permission:
            raise DomainError("admin_action_not_supported", 404)
        stamp = now_iso()
        with connect(immediate=True) as con:
            actor = self._require_admin_permission(con, actor, permission)
            before: dict = {}
            after: dict = {}
            if resource == "product":
                row = con.execute("SELECT * FROM products WHERE id=?", (target_id,)).fetchone()
                if not row:
                    raise DomainError("product_not_found", 404)
                before = dict(row)
                if action not in {"approve", "reject", "suspend"}:
                    raise DomainError("invalid_admin_action", 422)
                status, moderation, active = {
                    "approve": ("approved", "approved", 1),
                    "reject": ("rejected", "rejected", 0),
                    "suspend": ("suspended", "suspended", 0),
                }[action]
                con.execute(
                    "UPDATE products SET status=?,moderation_status=?,active=?,updated_at=? WHERE id=?",
                    (status, moderation, active, stamp, target_id),
                )
                after = {"status": status, "moderation_status": moderation, "active": active}
            elif resource == "merchant":
                row = con.execute("SELECT * FROM merchants WHERE id=?", (target_id,)).fetchone()
                if not row:
                    raise DomainError("merchant_not_found", 404)
                before = dict(row)
                if action not in {"activate", "suspend"}:
                    raise DomainError("invalid_admin_action", 422)
                status = "approved" if action == "activate" else "suspended"
                con.execute("UPDATE merchants SET status=?,updated_at=? WHERE id=?", (status, stamp, target_id))
                if action == "suspend":
                    con.execute("UPDATE store_branches SET public_visible=0,updated_at=? WHERE merchant_id=?", (stamp, target_id))
                    con.execute("UPDATE sessions SET revoked_at=? WHERE merchant_id=? AND revoked_at=''", (stamp, target_id))
                after = {"status": status}
            elif resource in {"supplier_campaign", "ad"}:
                table = "supplier_campaigns" if resource == "supplier_campaign" else "ad_campaigns"
                row = con.execute(f"SELECT * FROM {table} WHERE id=?", (target_id,)).fetchone()
                if not row:
                    raise DomainError(f"{resource}_not_found", 404)
                before = dict(row)
                if action not in {"approve", "reject", "pause"}:
                    raise DomainError("invalid_admin_action", 422)
                if resource == "supplier_campaign" and not reason:
                    raise DomainError("admin_decision_reason_required", 422)
                if resource == "supplier_campaign":
                    allowed_from = {
                        "approve": {"pending_review"},
                        "reject": {"pending_review"},
                        "pause": {"approved"},
                    }
                    if row["status"] not in allowed_from[action]:
                        raise DomainError("supplier_campaign_stage_not_allowed", 409)
                status = {"approve": "approved", "reject": "rejected", "pause": "paused"}[action]
                con.execute(f"UPDATE {table} SET status=?,updated_at=? WHERE id=?", (status, stamp, target_id))
                after = {"status": status}
                if resource == "supplier_campaign":
                    con.execute(
                        """UPDATE notifications SET acted_at=?
                           WHERE target_kind='admin' AND route=? AND requires_action=1 AND acted_at=''""",
                        (stamp, f"admin:supplier-campaign:{target_id}"),
                    )
                    title_ar, title_en = {
                        "approved": ("تم اعتماد حملتك", "Your campaign was approved"),
                        "rejected": ("تحتاج حملتك إلى تعديل", "Your campaign needs changes"),
                        "paused": ("تم إيقاف حملتك مؤقتاً", "Your campaign was paused"),
                    }[status]
                    body_ar = reason or "افتح الحملة لمراجعة القرار."
                    body_en = reason or "Open the campaign to review the decision."
                    self._insert_notification(
                        con,
                        "supplier",
                        row["supplier_id"],
                        title_ar,
                        title_en,
                        body_ar,
                        body_en,
                        f"supplier:campaign:{target_id}",
                        False,
                        f"supplier-campaign:{target_id}:{status}:{stamp}",
                        priority=70,
                    )
            elif resource == "report":
                row = con.execute("SELECT * FROM product_reports WHERE id=?", (target_id,)).fetchone()
                if not row:
                    raise DomainError("report_not_found", 404)
                before = dict(row)
                if action not in {"under_review", "resolved", "dismissed"}:
                    raise DomainError("invalid_admin_action", 422)
                con.execute(
                    "UPDATE product_reports SET status=?,reviewed_by=?,updated_at=? WHERE id=?",
                    (action, actor["accountId"], stamp, target_id),
                )
                after = {"status": action, "reviewed_by": actor["accountId"]}
            elif resource == "setting":
                key = clean_text(payload.get("key") or target_id, 80, True)
                allowed_settings = {
                    "commissionRate": (int, 0, 100), "bundleMaxComponents": (int, 2, 100),
                    "merchantResponseHours": (int, 1, 168), "inventoryCadenceHours": (int, 1, 8760),
                    "inventoryReminderLeadHours": (int, 1, 168), "inventoryEnforcement": (str, None, None),
                    "trialEnabled": (bool, None, None), "trialCutoffAt": (str, None, None),
                    "trialFirstApprovedMerchants": (int, 0, 1_000_000), "paymentsEnabled": (bool, None, None),
                    "whatsappEnabled": (bool, None, None), "mapProvider": (str, None, None),
                }
                if key not in allowed_settings:
                    raise DomainError("setting_not_supported", 422)
                value = payload.get("value")
                expected_type, minimum, maximum = allowed_settings[key]
                if expected_type is bool:
                    if not isinstance(value, bool):
                        raise DomainError("invalid_setting_value", 422)
                elif expected_type is int:
                    value = _bounded_int(value, minimum, maximum, "invalid_setting_value")
                else:
                    value = clean_text(value, 100)
                    if key == "inventoryEnforcement" and value not in {"reminder_only", "mark_stale", "hide_stale", "pause_stale"}:
                        raise DomainError("invalid_setting_value", 422)
                    if key == "mapProvider" and value not in {"openstreetmap", "disabled"}:
                        raise DomainError("invalid_setting_value", 422)
                row = con.execute("SELECT value_json FROM platform_settings WHERE key=?", (key,)).fetchone()
                before = {"key": key, "value": loads(row["value_json"], None) if row else None}
                con.execute(
                    """INSERT INTO platform_settings(key,value_json,updated_at) VALUES(?,?,?)
                       ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at""",
                    (key, dumps(value), stamp),
                )
                target_id = key
                after = {"key": key, "value": value}
            elif resource == "location":
                def normalize_location(item: dict) -> dict:
                    if not isinstance(item, dict):
                        raise DomainError("invalid_location", 422)
                    kind = clean_text(item.get("kind"), 30, True)
                    if kind not in {"wilayat", "area"}:
                        raise DomainError("invalid_location_kind", 422)
                    parent_id = clean_text(item.get("parentId") or item.get("parent_id"), 90, True)
                    parent = con.execute("SELECT id,kind,active FROM locations WHERE id=?", (parent_id,)).fetchone()
                    expected_parent = "governorate" if kind == "wilayat" else "wilayat"
                    if not parent or parent["kind"] != expected_parent or not parent["active"]:
                        raise DomainError("invalid_location_parent", 422)
                    return {
                        "kind": kind,
                        "parent_id": parent_id,
                        "name_ar": clean_text(item.get("nameAr") or item.get("name_ar"), 120, True),
                        "name_en": clean_text(item.get("nameEn") or item.get("name_en"), 120, True),
                        "sort_order": _bounded_int(item.get("sortOrder", item.get("sort_order", 0)), 0, 1_000_000, "invalid_sort_order"),
                    }

                def insert_location(item: dict, *, skip_existing: bool) -> tuple[str, bool]:
                    normalized = normalize_location(item)
                    existing = con.execute(
                        """SELECT id FROM locations WHERE parent_id=? AND kind=?
                           AND lower(name_ar)=lower(?) AND lower(name_en)=lower(?)""",
                        (normalized["parent_id"], normalized["kind"], normalized["name_ar"], normalized["name_en"]),
                    ).fetchone()
                    if existing:
                        if skip_existing:
                            return existing["id"], False
                        raise DomainError("location_already_exists", 409, {"id": existing["id"]})
                    location_id = new_id("location")
                    con.execute(
                        """INSERT INTO locations(
                            id,parent_id,kind,name_ar,name_en,sort_order,active,created_at)
                           VALUES(?,?,?,?,?,?,1,?)""",
                        (location_id, normalized["parent_id"], normalized["kind"],
                         normalized["name_ar"], normalized["name_en"], normalized["sort_order"], stamp),
                    )
                    return location_id, True

                if action == "create":
                    target_id, created = insert_location(payload, skip_existing=False)
                    before = {}
                    after = {"id": target_id, "created": created, "active": 1}
                elif action == "bulk_import":
                    items = payload.get("items")
                    if not isinstance(items, list) or not 1 <= len(items) <= 200:
                        raise DomainError("invalid_location_import", 422, {"maximum": 200})
                    imported, existing_ids = [], []
                    for item in items:
                        location_id, created = insert_location(item, skip_existing=True)
                        (imported if created else existing_ids).append(location_id)
                    target_id = new_id("location_import")
                    before = {}
                    after = {"importedIds": imported, "existingIds": existing_ids}
                else:
                    row = con.execute("SELECT * FROM locations WHERE id=?", (target_id,)).fetchone()
                    if not row:
                        raise DomainError("location_not_found", 404)
                    if row["kind"] not in {"wilayat", "area"}:
                        raise DomainError("protected_location", 409)
                    before = dict(row)
                    in_use = con.execute(
                        "SELECT 1 FROM store_branches WHERE (area_id=? OR wilayah_id=?) AND active=1 LIMIT 1",
                        (target_id, target_id),
                    ).fetchone()
                    if action == "update":
                        normalized = normalize_location({
                            "kind": row["kind"],
                            "parentId": payload.get("parentId") or row["parent_id"],
                            "nameAr": payload.get("nameAr") or row["name_ar"],
                            "nameEn": payload.get("nameEn") or row["name_en"],
                            "sortOrder": payload.get("sortOrder", row["sort_order"]),
                        })
                        if in_use and normalized["parent_id"] != row["parent_id"]:
                            raise DomainError("location_in_use", 409)
                        con.execute(
                            """UPDATE locations SET parent_id=?,name_ar=?,name_en=?,sort_order=?
                               WHERE id=?""",
                            (normalized["parent_id"], normalized["name_ar"], normalized["name_en"],
                             normalized["sort_order"], target_id),
                        )
                        after = {"id": target_id, **normalized, "active": row["active"]}
                    elif action in {"activate", "deactivate"}:
                        if action == "deactivate" and in_use:
                            raise DomainError("location_in_use", 409)
                        active = int(action == "activate")
                        con.execute("UPDATE locations SET active=? WHERE id=?", (active, target_id))
                        after = {"id": target_id, "active": active}
                    else:
                        raise DomainError("invalid_admin_action", 422)
            elif resource == "supplier":
                if action == "create":
                    if target_id:
                        raise DomainError("client_supplier_id_forbidden", 422)
                    phone = normalize_phone(payload.get("accountPhone"))
                    account = con.execute(
                        "SELECT id,status FROM accounts WHERE phone=?", (phone,),
                    ).fetchone()
                    if not account or account["status"] != "active":
                        raise DomainError("supplier_account_not_found", 404)
                    if con.execute(
                        "SELECT 1 FROM supplier_members WHERE account_id=? AND status='active' LIMIT 1",
                        (account["id"],),
                    ).fetchone():
                        raise DomainError("supplier_account_already_assigned", 409)
                    target_id = new_id("supplier")
                    name_ar = clean_text(payload.get("nameAr"), 160, True)
                    name_en = clean_text(payload.get("nameEn"), 160, True)
                    con.execute(
                        """INSERT INTO suppliers(id,name_ar,name_en,status,created_at,updated_at)
                           VALUES(?,?,?,'approved',?,?)""",
                        (target_id, name_ar, name_en, stamp, stamp),
                    )
                    con.execute(
                        """INSERT INTO supplier_members(supplier_id,account_id,role,status,created_at)
                           VALUES(?,?,'supplier_advertiser','active',?)""",
                        (target_id, account["id"], stamp),
                    )
                    con.execute(
                        """INSERT INTO account_roles(account_id,role,merchant_id,active)
                           VALUES(?,'supplier_advertiser',?,1)""",
                        (account["id"], target_id),
                    )
                    before = {}
                    after = {"id": target_id, "status": "approved", "accountId": account["id"]}
                else:
                    row = con.execute("SELECT * FROM suppliers WHERE id=?", (target_id,)).fetchone()
                    if not row:
                        raise DomainError("supplier_not_found", 404)
                    before = dict(row)
                    if action == "update":
                        name_ar = clean_text(payload.get("nameAr") or row["name_ar"], 160, True)
                        name_en = clean_text(payload.get("nameEn") or row["name_en"], 160, True)
                        con.execute(
                            "UPDATE suppliers SET name_ar=?,name_en=?,updated_at=? WHERE id=?",
                            (name_ar, name_en, stamp, target_id),
                        )
                        after = {"id": target_id, "name_ar": name_ar, "name_en": name_en, "status": row["status"]}
                    elif action in {"activate", "suspend"}:
                        status = "approved" if action == "activate" else "suspended"
                        con.execute("UPDATE suppliers SET status=?,updated_at=? WHERE id=?", (status, stamp, target_id))
                        if action == "suspend":
                            con.execute(
                                """UPDATE sessions SET revoked_at=? WHERE merchant_id=? AND revoked_at=''""",
                                (stamp, target_id),
                            )
                        after = {"id": target_id, "status": status}
                    else:
                        raise DomainError("invalid_admin_action", 422)
            elif resource == "plan":
                row = con.execute("SELECT * FROM subscription_plans WHERE id=?", (target_id,)).fetchone()
                if not row:
                    raise DomainError("plan_not_found", 404)
                before = dict(row)
                if action != "update":
                    raise DomainError("invalid_admin_action", 422)
                if target_id == "early_trial":
                    raise DomainError("trial_inherits_basic", 409)
                entitlements = payload.get("entitlements")
                if not isinstance(entitlements, dict):
                    raise DomainError("invalid_plan_entitlements", 422)
                normalized = dict(entitlements)
                for key in ("products", "branches", "staff", "bundles"):
                    normalized[key] = _bounded_int(normalized.get(key, 0), 0, 1_000_000, "invalid_plan_limit")
                if normalized.get("analytics", "basic") not in {"basic", "advanced"}:
                    raise DomainError("invalid_plan_analytics", 422)
                price = _strict_baisa(payload.get("price"))
                duration = _bounded_int(payload.get("durationDays"), 1, 3650, "invalid_plan_duration")
                con.execute(
                    "UPDATE subscription_plans SET price_baisa=?,duration_days=?,entitlements=?,updated_at=? WHERE id=?",
                    (price, duration, dumps(normalized), stamp, target_id),
                )
                after = {"price_baisa": price, "duration_days": duration, "entitlements": normalized}
            else:  # category
                if action == "create":
                    if target_id:
                        raise DomainError("client_category_id_forbidden", 422)
                    target_id = new_id("category")
                    con.execute(
                        """INSERT INTO product_categories(
                            id,name_ar,name_en,icon,image_path,regulated_rules,sort_order,active,created_at,updated_at,
                            slug,description_ar,description_en)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (target_id, clean_text(payload.get("nameAr"), 100, True), clean_text(payload.get("nameEn"), 100, True),
                         clean_text(payload.get("icon"), 40), clean_text(payload.get("imagePath"), 240),
                         dumps(payload.get("regulatedRules") if isinstance(payload.get("regulatedRules"), dict) else {}),
                         _bounded_int(payload.get("sortOrder", 0), 0, 1_000_000, "invalid_sort_order"), 1, stamp, stamp,
                         clean_text(payload.get("slug"), 100), clean_text(payload.get("descriptionAr"), 500),
                         clean_text(payload.get("descriptionEn"), 500)),
                    )
                    before = {}
                    after = {"id": target_id, "active": 1}
                elif action in {"update", "activate", "deactivate"}:
                    row = con.execute("SELECT * FROM product_categories WHERE id=?", (target_id,)).fetchone()
                    if not row:
                        raise DomainError("category_not_found", 404)
                    before = dict(row)
                    if action == "update":
                        rules = payload.get("regulatedRules", loads(row["regulated_rules"], {}))
                        if not isinstance(rules, dict) or len(rules) > 50:
                            raise DomainError("invalid_category_rules", 422)
                        encoded_rules = dumps(rules)
                        if len(encoded_rules.encode("utf-8")) > 8_000:
                            raise DomainError("invalid_category_rules", 422)
                        slug = clean_text(payload.get("slug", row["slug"]), 100)
                        if slug and not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug):
                            raise DomainError("invalid_category_slug", 422)
                        if slug and con.execute(
                            "SELECT 1 FROM product_categories WHERE slug=? AND id<>? LIMIT 1",
                            (slug, target_id),
                        ).fetchone():
                            raise DomainError("category_slug_already_exists", 409)
                        normalized = {
                            "name_ar": clean_text(payload.get("nameAr", row["name_ar"]), 100, True),
                            "name_en": clean_text(payload.get("nameEn", row["name_en"]), 100, True),
                            "icon": clean_text(payload.get("icon", row["icon"]), 40),
                            "image_path": clean_text(payload.get("imagePath", row["image_path"]), 240),
                            "regulated_rules": encoded_rules,
                            "sort_order": _bounded_int(
                                payload.get("sortOrder", row["sort_order"]), 0, 1_000_000,
                                "invalid_sort_order",
                            ),
                            "slug": slug,
                            "description_ar": clean_text(
                                payload.get("descriptionAr", row["description_ar"]), 500,
                            ),
                            "description_en": clean_text(
                                payload.get("descriptionEn", row["description_en"]), 500,
                            ),
                        }
                        con.execute(
                            """UPDATE product_categories SET
                               name_ar=?,name_en=?,icon=?,image_path=?,regulated_rules=?,sort_order=?,
                               slug=?,description_ar=?,description_en=?,updated_at=? WHERE id=?""",
                            (
                                normalized["name_ar"], normalized["name_en"], normalized["icon"],
                                normalized["image_path"], normalized["regulated_rules"],
                                normalized["sort_order"], normalized["slug"],
                                normalized["description_ar"], normalized["description_en"], stamp,
                                target_id,
                            ),
                        )
                        after = {"id": target_id, **normalized, "active": row["active"]}
                        after["regulated_rules"] = rules
                    else:
                        active = 1 if action == "activate" else 0
                        con.execute(
                            "UPDATE product_categories SET active=?,updated_at=? WHERE id=?",
                            (active, stamp, target_id),
                        )
                        after = {"active": active}
                else:
                    raise DomainError("invalid_admin_action", 422)
            con.execute(
                """INSERT INTO admin_audit_logs(
                    id,actor_id,action,target_kind,target_id,before_json,after_json,reason,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (new_id("audit"), actor["accountId"], f"{resource}.{action}", resource, target_id,
                 dumps(before), dumps(after), reason, stamp),
            )
            return {"resource": resource, "action": action, "id": target_id, "result": after}
