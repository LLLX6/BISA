"""End-to-end shopper and merchant operations not present in the foundation.

The mixin is composed before ``MarketplaceMixin``.  It keeps onboarding drafts,
cart editing, account addresses and merchant catalog controls server-owned.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from bisa_config import coordinates_in_muscat
from bisa_domain import DomainError, clean_text, connect, dumps, loads, new_id, now_iso, settings
from bisa_integrations import default_registry, execute_external_action
from bisa_marketplace import MERCHANT_ROLES, _bounded_int, _payload_hash, _strict_baisa, omr
from bisa_security import require_permission


ONBOARDING_STEPS = (
    "owner", "business", "brand", "location", "hours", "documents",
    "fulfillment", "policy", "categories", "plan", "review",
)
DOCUMENT_KINDS = {"storefront", "commercial_registration", "license"}
WEEK_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _time_minutes(value: Any) -> int:
    text = str(value or "")
    parts = text.split(":")
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        raise DomainError("invalid_opening_time", 422)
    hour, minute = map(int, parts)
    if hour > 23 or minute > 59:
        raise DomainError("invalid_opening_time", 422)
    return hour * 60 + minute


def _opening_hours(value: Any) -> dict:
    if not isinstance(value, dict) or not value:
        raise DomainError("opening_hours_required", 422)
    normalized = {}
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
        for item in raw:
            if not isinstance(item, dict):
                raise DomainError("invalid_opening_hours", 422)
            opening, closing = clean_text(item.get("open"), 5, True), clean_text(item.get("close"), 5, True)
            if _time_minutes(opening) == _time_minutes(closing):
                raise DomainError("invalid_opening_hours", 422)
            slots.append({"open": opening, "close": closing})
        normalized[day] = slots
        has_open_day = has_open_day or bool(slots)
    if not has_open_day:
        raise DomainError("opening_day_required", 422)
    return normalized


def _iso_datetime(value: Any, *, required: bool = False) -> datetime | None:
    text = clean_text(value, 50, required)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DomainError("invalid_datetime", 422) from exc
    if parsed.tzinfo is None:
        raise DomainError("timezone_required", 422)
    return parsed.astimezone(UTC)


def _coordinates(
    latitude: Any, longitude: Any, *, required: bool = False,
) -> tuple[float | None, float | None]:
    if latitude in (None, "") and longitude in (None, ""):
        if required:
            raise DomainError("branch_map_pin_required", 422)
        return None, None
    if latitude in (None, "") or longitude in (None, ""):
        raise DomainError("complete_coordinates_required", 422)
    try:
        latitude, longitude = float(latitude), float(longitude)
    except (TypeError, ValueError) as exc:
        raise DomainError("valid_location_required", 422) from exc
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        raise DomainError("valid_location_required", 422)
    if not coordinates_in_muscat(latitude, longitude):
        raise DomainError("coordinates_outside_muscat", 422)
    return latitude, longitude


class OperationsMixin:
    # ---------- merchant onboarding ----------

    def merchant_onboarding(self, actor: dict, payload: dict) -> dict:
        action = clean_text(payload.get("action"), 30) or "status"
        # The mobile wizard calls the autosave operation ``save_draft`` while
        # the domain calls it ``save_step``.  Both names share one validation
        # and persistence path; neither can bypass completion checks.
        if action == "save_draft":
            action = "save_step"
        if action not in {"start", "status", "save_step", "submit"}:
            raise DomainError("invalid_onboarding_action", 422)
        with connect(immediate=action != "status") as con:
            actor = self._require_actor(
                con, actor, {"shopper"}, merchant_must_be_approved=False,
            )
            record = con.execute(
                """SELECT a.*,m.owner_account_id,m.name_ar,m.name_en,m.status merchant_status
                   FROM merchant_applications a JOIN merchants m ON m.id=a.merchant_id
                   WHERE m.owner_account_id=? AND a.status IN('draft','submitted','under_review','changes_requested')
                   ORDER BY a.created_at DESC LIMIT 1""",
                (actor["accountId"],),
            ).fetchone()
            if not record:
                record = con.execute(
                    """SELECT a.*,m.owner_account_id,m.name_ar,m.name_en,m.status merchant_status
                       FROM merchant_applications a JOIN merchants m ON m.id=a.merchant_id
                       WHERE m.owner_account_id=? ORDER BY a.created_at DESC LIMIT 1""",
                    (actor["accountId"],),
                ).fetchone()
            if action == "start" and not record:
                stamp = now_iso()
                merchant_id, application_id = new_id("merchant"), new_id("mapp")
                con.execute(
                    """INSERT INTO merchants(
                        id,owner_account_id,name_ar,name_en,merchant_type,status,verified,
                        created_at,updated_at,active)
                       VALUES(?,?,? ,?,'store','draft',0,?,?,1)""",
                    (merchant_id, actor["accountId"], "متجر جديد", "New store", stamp, stamp),
                )
                con.execute(
                    """INSERT INTO merchant_applications(
                        id,merchant_id,payload,status,reviewer_note,submitted_at,created_at,updated_at)
                       VALUES(?,?,'{}','draft','','',?,?)""",
                    (application_id, merchant_id, stamp, stamp),
                )
                con.execute(
                    """INSERT OR IGNORE INTO account_roles(account_id,role,merchant_id,active)
                       VALUES(?,'merchant_owner',?,0)""",
                    (actor["accountId"], merchant_id),
                )
                record = con.execute(
                    """SELECT a.*,m.owner_account_id,m.name_ar,m.name_en,m.status merchant_status
                       FROM merchant_applications a JOIN merchants m ON m.id=a.merchant_id WHERE a.id=?""",
                    (application_id,),
                ).fetchone()
            if not record:
                if action == "status":
                    return {"application": None, "steps": [], "nextStep": "owner"}
                raise DomainError("onboarding_not_started", 409)
            if clean_text(payload.get("applicationId"), 90) not in {"", record["id"]}:
                raise DomainError("merchant_application_not_found", 404)
            if action == "save_step":
                if record["status"] not in {"draft", "changes_requested"}:
                    raise DomainError("merchant_application_locked", 409)
                step = clean_text(payload.get("step"), 40, True)
                if step not in ONBOARDING_STEPS:
                    raise DomainError("invalid_onboarding_step", 422)
                value = payload.get("data")
                if not isinstance(value, dict):
                    raise DomainError("onboarding_step_object_required", 422)
                normalized = self._validate_onboarding_step(con, record, step, value)
                con.execute(
                    """INSERT INTO merchant_application_steps(
                        application_id,step_key,payload_json,completed_at,updated_at)
                       VALUES(?,?,?,?,?) ON CONFLICT(application_id,step_key) DO UPDATE SET
                       payload_json=excluded.payload_json,completed_at=excluded.completed_at,
                       updated_at=excluded.updated_at""",
                    (record["id"], step, dumps(normalized), now_iso(), now_iso()),
                )
            elif action == "submit":
                if record["status"] not in {"draft", "changes_requested"}:
                    raise DomainError("merchant_application_locked", 409)
                self._submit_onboarding(con, actor, record)
                record = con.execute(
                    """SELECT a.*,m.owner_account_id,m.name_ar,m.name_en,m.status merchant_status
                       FROM merchant_applications a JOIN merchants m ON m.id=a.merchant_id WHERE a.id=?""",
                    (record["id"],),
                ).fetchone()
            return self._onboarding_view(con, record)

    def _validate_onboarding_step(self, con, application, step: str, value: dict) -> dict:
        if step == "owner":
            return {
                "contactName": clean_text(value.get("contactName"), 100, True),
                "contactPhone": clean_text(value.get("contactPhone"), 30, True),
                "authorizedRole": clean_text(value.get("authorizedRole"), 100),
            }
        if step == "business":
            merchant_type = clean_text(value.get("merchantType"), 20) or "store"
            if merchant_type not in {"store", "chain"}:
                raise DomainError("invalid_merchant_type", 422)
            return {
                "nameAr": clean_text(value.get("nameAr"), 100, True),
                "nameEn": clean_text(value.get("nameEn"), 100, True),
                "merchantType": merchant_type,
                "commercialRegistration": clean_text(value.get("commercialRegistration"), 80, True),
            }
        if step == "brand":
            result = {
                "logoMediaId": clean_text(value.get("logoMediaId"), 90),
                "coverMediaId": clean_text(value.get("coverMediaId"), 90),
            }
            for media_id in result.values():
                if media_id and not con.execute(
                    """SELECT 1 FROM private_media_objects
                       WHERE id=? AND owner_kind='merchant_application' AND owner_id=? AND status='active'""",
                    (media_id, application["id"]),
                ).fetchone():
                    raise DomainError("merchant_brand_media_not_found", 404)
            return result
        if step == "location":
            wilayah_id = clean_text(value.get("wilayahId"), 90, True)
            area_id = clean_text(value.get("areaId"), 90, True)
            hierarchy = con.execute(
                """SELECT 1 FROM locations a JOIN locations w ON w.id=a.parent_id
                   WHERE a.id=? AND a.kind='area' AND a.active=1
                     AND w.id=? AND w.kind='wilayat' AND w.active=1""",
                (area_id, wilayah_id),
            ).fetchone()
            if not hierarchy:
                raise DomainError("valid_area_required", 422)
            latitude, longitude = _coordinates(
                value.get("latitude"), value.get("longitude"), required=True,
            )
            return {
                "branchNameAr": clean_text(value.get("branchNameAr"), 100, True),
                "branchNameEn": clean_text(value.get("branchNameEn"), 100, True),
                "wilayahId": wilayah_id, "areaId": area_id,
                "addressText": clean_text(value.get("addressText"), 300, True),
                "latitude": latitude, "longitude": longitude,
            }
        if step == "hours":
            return {"hours": _opening_hours(value.get("hours"))}
        if step == "documents":
            documents = value.get("documents")
            if not isinstance(documents, list):
                raise DomainError("merchant_documents_required", 422)
            normalized = []
            for item in documents[:12]:
                if not isinstance(item, dict):
                    raise DomainError("invalid_merchant_document", 422)
                kind = clean_text(item.get("kind"), 50, True)
                if kind not in DOCUMENT_KINDS:
                    raise DomainError("invalid_merchant_document_kind", 422)
                media_id = clean_text(item.get("mediaId"), 90, True)
                media = con.execute(
                    """SELECT id FROM private_media_objects WHERE id=? AND owner_kind='merchant_application'
                       AND owner_id=? AND status='active'""",
                    (media_id, application["id"]),
                ).fetchone()
                if not media:
                    raise DomainError("merchant_document_not_found", 404)
                normalized.append({"kind": kind, "mediaId": media_id})
            kinds = {item["kind"] for item in normalized}
            if not DOCUMENT_KINDS.issubset(kinds):
                raise DomainError("merchant_documents_incomplete", 422, {"required": sorted(DOCUMENT_KINDS)})
            return {"documents": normalized}
        if step == "fulfillment":
            result = {}
            enabled = False
            for key in ("pickup", "office", "home"):
                raw = value.get(key) if isinstance(value.get(key), dict) else {}
                active = raw.get("enabled") is True
                enabled = enabled or active
                mode_config = {
                    "enabled": active,
                    "feeBaisa": _bounded_int(raw.get("feeBaisa", 0), 0, 100_000, "invalid_delivery_fee"),
                    "minimumBaisa": _bounded_int(raw.get("minimumBaisa", 0), 0, 1_000_000, "invalid_minimum_order"),
                    "freeThresholdBaisa": _bounded_int(raw.get("freeThresholdBaisa", 0), 0, 1_000_000, "invalid_free_threshold"),
                }
                if (
                    mode_config["freeThresholdBaisa"]
                    and mode_config["freeThresholdBaisa"] < mode_config["minimumBaisa"]
                ):
                    raise DomainError("free_threshold_below_minimum", 422)
                result[key] = mode_config
            if not enabled:
                raise DomainError("fulfillment_mode_required", 422)
            zones = value.get("zones") if isinstance(value.get("zones"), list) else []
            normalized_zones = []
            zone_keys = set()
            for zone in zones[:100]:
                if not isinstance(zone, dict):
                    raise DomainError("invalid_delivery_zone", 422)
                mode = clean_text(zone.get("mode"), 30, True)
                if mode not in {"office_delivery", "home_delivery"}:
                    raise DomainError("invalid_delivery_zone_mode", 422)
                mode_key = "office" if mode == "office_delivery" else "home"
                if not result[mode_key]["enabled"]:
                    raise DomainError("delivery_zone_mode_disabled", 422)
                wilayah_id = clean_text(zone.get("wilayahId"), 90)
                area_id = clean_text(zone.get("areaId"), 90)
                if area_id:
                    location = con.execute(
                        "SELECT parent_id FROM locations WHERE id=? AND kind='area' AND active=1",
                        (area_id,),
                    ).fetchone()
                    if not location or (wilayah_id and location["parent_id"] != wilayah_id):
                        raise DomainError("invalid_delivery_area", 422)
                    wilayah_id = wilayah_id or location["parent_id"]
                elif not wilayah_id or not con.execute(
                    "SELECT 1 FROM locations WHERE id=? AND kind='wilayat' AND active=1",
                    (wilayah_id,),
                ).fetchone():
                    raise DomainError("invalid_delivery_wilayah", 422)
                normalized_zone = {
                    "mode": mode, "wilayahId": wilayah_id, "areaId": area_id,
                    "feeBaisa": _bounded_int(zone.get("feeBaisa", result[mode_key]["feeBaisa"]), 0, 100_000, "invalid_delivery_fee"),
                    "minimumBaisa": _bounded_int(zone.get("minimumBaisa", result[mode_key]["minimumBaisa"]), 0, 1_000_000, "invalid_minimum_order"),
                    "freeThresholdBaisa": _bounded_int(zone.get("freeThresholdBaisa", result[mode_key]["freeThresholdBaisa"]), 0, 1_000_000, "invalid_free_threshold"),
                    "eta": clean_text(zone.get("eta"), 80),
                }
                if (
                    normalized_zone["freeThresholdBaisa"]
                    and normalized_zone["freeThresholdBaisa"] < normalized_zone["minimumBaisa"]
                ):
                    raise DomainError("free_threshold_below_minimum", 422)
                zone_key = (mode, wilayah_id, area_id)
                if zone_key in zone_keys:
                    raise DomainError("duplicate_delivery_zone", 409)
                zone_keys.add(zone_key)
                normalized_zones.append(normalized_zone)
            result["zones"] = normalized_zones
            result["eta"] = clean_text(value.get("eta"), 100)
            return result
        if step == "policy":
            excluded = value.get("excludedCategories")
            if excluded is None:
                excluded = []
            if not isinstance(excluded, list):
                raise DomainError("invalid_excluded_categories", 422)
            excluded = list(dict.fromkeys(
                clean_text(item, 80) for item in excluded if clean_text(item, 80)
            ))
            if len(excluded) > 50:
                raise DomainError("too_many_excluded_categories", 422)
            if excluded:
                marks = ",".join("?" for _ in excluded)
                count = con.execute(
                    f"SELECT COUNT(*) n FROM product_categories WHERE id IN ({marks}) AND active=1",
                    excluded,
                ).fetchone()["n"]
                if count != len(excluded):
                    raise DomainError("valid_category_required", 422)
            return {
                "returnWindowDays": _bounded_int(value.get("returnWindowDays", 0), 0, 365, "invalid_return_window"),
                "exchangeWindowDays": _bounded_int(value.get("exchangeWindowDays", 0), 0, 365, "invalid_exchange_window"),
                "conditions": clean_text(value.get("conditions"), 2000, True),
                "receiptRequired": value.get("receiptRequired") is not False,
                "excludedCategories": excluded,
                "contactMethod": clean_text(value.get("contactMethod"), 120, True),
                "notes": clean_text(value.get("notes"), 1000),
            }
        if step == "categories":
            categories = [clean_text(item, 80) for item in value.get("categoryIds", []) if clean_text(item, 80)]
            categories = list(dict.fromkeys(categories))[:20]
            if not categories:
                raise DomainError("initial_category_required", 422)
            marks = ",".join("?" for _ in categories)
            if con.execute(f"SELECT COUNT(*) n FROM product_categories WHERE id IN ({marks}) AND active=1", categories).fetchone()["n"] != len(categories):
                raise DomainError("valid_category_required", 422)
            return {"categoryIds": categories}
        if step == "plan":
            plan_id = clean_text(value.get("planId"), 80, True)
            if not con.execute("SELECT 1 FROM subscription_plans WHERE id=? AND active=1", (plan_id,)).fetchone():
                raise DomainError("valid_plan_required", 422)
            if plan_id == "early_trial":
                config = settings(con)
                if not config.get("trialEnabled", False):
                    raise DomainError("trial_not_available", 409)
                cutoff = clean_text(config.get("trialCutoffAt"), 50)
                if cutoff and _iso_datetime(cutoff, required=True) < datetime.now(UTC):
                    raise DomainError("trial_not_available", 409)
                maximum = _bounded_int(config.get("trialFirstApprovedMerchants", 0), 0, 1_000_000, "invalid_trial_limit")
                used = con.execute(
                    "SELECT COUNT(DISTINCT merchant_id) n FROM merchant_subscriptions WHERE plan_id='early_trial'",
                ).fetchone()["n"]
                if used >= maximum:
                    raise DomainError("trial_capacity_reached", 409)
            return {"planId": plan_id}
        if step == "review":
            if value.get("acceptedPolicies") is not True:
                raise DomainError("merchant_policy_acceptance_required", 422)
            return {"acceptedPolicies": True}
        raise DomainError("invalid_onboarding_step", 422)

    def _submit_onboarding(self, con, actor: dict, application) -> None:
        rows = {
            row["step_key"]: loads(row["payload_json"], {})
            for row in con.execute(
                "SELECT * FROM merchant_application_steps WHERE application_id=? AND completed_at<>''",
                (application["id"],),
            )
        }
        missing = [step for step in ONBOARDING_STEPS if step not in rows]
        if missing:
            raise DomainError("merchant_onboarding_incomplete", 409, {"missingSteps": missing})
        business, location = rows["business"], rows["location"]
        stamp = now_iso()
        # Logo/cover uploads remain private while the application is under
        # review.  A reviewed public derivative is published separately;
        # private media identifiers are never written into public URL fields.
        con.execute(
            """UPDATE merchants SET name_ar=?,name_en=?,merchant_type=?,status='submitted',
               logo_path=?,cover_path=?,updated_at=? WHERE id=? AND owner_account_id=?""",
            (business["nameAr"], business["nameEn"], business["merchantType"],
             "", "",
             stamp, application["merchant_id"], actor["accountId"]),
        )
        branch = con.execute("SELECT id FROM store_branches WHERE merchant_id=? ORDER BY created_at LIMIT 1", (application["merchant_id"],)).fetchone()
        branch_id = branch["id"] if branch else new_id("branch")
        if branch:
            con.execute(
                """UPDATE store_branches SET name_ar=?,name_en=?,wilayah_id=?,area_id=?,address_text=?,
                   latitude=?,longitude=?,hours_json=?,status='submitted',public_visible=0,updated_at=?
                   WHERE id=? AND merchant_id=?""",
                (location["branchNameAr"],location["branchNameEn"],location["wilayahId"],location["areaId"],
                 location["addressText"],location["latitude"],location["longitude"],dumps(rows["hours"]["hours"]),
                 stamp,branch_id,application["merchant_id"]),
            )
        else:
            con.execute(
                """INSERT INTO store_branches(
                    id,merchant_id,name_ar,name_en,wilayah_id,area_id,address_text,latitude,longitude,
                    hours_json,status,active,public_visible,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,'submitted',1,0,?,?)""",
                (branch_id,application["merchant_id"],location["branchNameAr"],location["branchNameEn"],
                 location["wilayahId"],location["areaId"],location["addressText"],location["latitude"],
                 location["longitude"],dumps(rows["hours"]["hours"]),stamp,stamp),
            )
        fulfillment = rows["fulfillment"]
        if not fulfillment["zones"]:
            for key, mode in (("office", "office_delivery"), ("home", "home_delivery")):
                if fulfillment[key]["enabled"]:
                    fulfillment["zones"].append({
                        "mode": mode, "wilayahId": location["wilayahId"],
                        "areaId": location["areaId"],
                        "feeBaisa": fulfillment[key]["feeBaisa"],
                        "minimumBaisa": fulfillment[key]["minimumBaisa"],
                        "freeThresholdBaisa": fulfillment[key]["freeThresholdBaisa"],
                        "eta": fulfillment["eta"],
                    })
        con.execute(
            """INSERT INTO fulfillment_profiles(
                branch_id,pickup_enabled,office_enabled,office_fee_baisa,office_minimum_baisa,
                office_free_threshold_baisa,home_enabled,home_fee_baisa,home_minimum_baisa,
                home_free_threshold_baisa,zones_json,eta_text,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(branch_id) DO UPDATE SET
               pickup_enabled=excluded.pickup_enabled,office_enabled=excluded.office_enabled,
               office_fee_baisa=excluded.office_fee_baisa,office_minimum_baisa=excluded.office_minimum_baisa,
               office_free_threshold_baisa=excluded.office_free_threshold_baisa,home_enabled=excluded.home_enabled,
               home_fee_baisa=excluded.home_fee_baisa,home_minimum_baisa=excluded.home_minimum_baisa,
               home_free_threshold_baisa=excluded.home_free_threshold_baisa,zones_json=excluded.zones_json,
               eta_text=excluded.eta_text,updated_at=excluded.updated_at""",
            (branch_id,int(fulfillment["pickup"]["enabled"]),int(fulfillment["office"]["enabled"]),
             fulfillment["office"]["feeBaisa"],fulfillment["office"]["minimumBaisa"],
             fulfillment["office"]["freeThresholdBaisa"],int(fulfillment["home"]["enabled"]),
             fulfillment["home"]["feeBaisa"],fulfillment["home"]["minimumBaisa"],
             fulfillment["home"]["freeThresholdBaisa"],dumps([zone.get("areaId") for zone in fulfillment["zones"] if isinstance(zone,dict) and zone.get("areaId")]),fulfillment["eta"],stamp),
        )
        con.execute("DELETE FROM branch_delivery_zones WHERE branch_id=?", (branch_id,))
        for zone in fulfillment["zones"]:
            con.execute(
                """INSERT INTO branch_delivery_zones(
                    id,branch_id,mode,wilayah_id,area_id,fee_baisa,minimum_baisa,
                    free_threshold_baisa,eta_text,active,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,1,?,?)""",
                (new_id("zone"), branch_id, zone["mode"], zone["wilayahId"], zone["areaId"],
                 zone["feeBaisa"], zone["minimumBaisa"], zone["freeThresholdBaisa"],
                 zone["eta"], stamp, stamp),
            )
        policy = rows["policy"]
        con.execute("UPDATE merchant_return_policies SET active=0 WHERE merchant_id=?", (application["merchant_id"],))
        policy_id = new_id("policy")
        policy_version = con.execute(
            "SELECT COALESCE(MAX(version),0)+1 n FROM merchant_return_policies WHERE merchant_id=?",
            (application["merchant_id"],),
        ).fetchone()["n"]
        con.execute(
            """INSERT INTO merchant_return_policies(
                id,merchant_id,version,return_window_days,exchange_window_days,conditions_text,
                receipt_required,excluded_categories,contact_method,notes,active,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,1,?)""",
            (policy_id,application["merchant_id"],policy_version,policy["returnWindowDays"],policy["exchangeWindowDays"],
             policy["conditions"],int(policy["receiptRequired"]),dumps(policy["excludedCategories"]),
             policy["contactMethod"],policy["notes"],stamp),
        )
        con.execute("UPDATE merchants SET return_policy_id=? WHERE id=?", (policy_id,application["merchant_id"]))
        con.execute("DELETE FROM merchant_documents WHERE application_id=?", (application["id"],))
        for document in rows["documents"]["documents"]:
            con.execute(
                """INSERT INTO merchant_documents(
                    id,application_id,kind,private_path,created_at,media_id,review_status)
                   VALUES(?,?,?,?,?,?,'pending')""",
                (new_id("mdoc"),application["id"],document["kind"],
                 f"media:{document['mediaId']}",stamp,document["mediaId"]),
            )
        snapshot = {
            "owner": rows["owner"], "business": business, "location": location,
            "categories": rows["categories"], "requestedPlan": rows["plan"]["planId"],
            "branchId": branch_id, "policyId": policy_id, "brandMedia": rows["brand"],
        }
        con.execute(
            """UPDATE merchant_applications SET payload=?,status='submitted',reviewer_note='',
               submitted_at=?,updated_at=? WHERE id=? AND merchant_id=?""",
            (dumps(snapshot),stamp,stamp,application["id"],application["merchant_id"]),
        )
        con.execute(
            """UPDATE notifications SET acted_at=?
               WHERE target_kind='admin' AND route=? AND requires_action=1 AND acted_at=''""",
            (stamp, f"admin:merchant-application:{application['id']}"),
        )
        con.execute(
            """INSERT INTO notifications(
                id,target_kind,target_id,title_ar,title_en,body_ar,body_en,route,requires_action,
                dedupe_key,read_at,acted_at,created_at,priority)
               VALUES(?,?,?,?,?,?,?,?,1,?,'','',?,100)""",
            (new_id("ntf"),"admin","admin","طلب متجر جديد","New merchant application",
             f"{business['nameAr']} — مراجعة الطلب",f"{business['nameEn']} — review application",
             f"admin:merchant-application:{application['id']}",
             f"merchant-application:{application['id']}:submitted:v{policy_version}",stamp),
        )
        con.execute(
            """INSERT INTO admin_audit_logs(
                id,actor_id,action,target_kind,target_id,before_json,after_json,reason,created_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (new_id("audit"),actor["accountId"],"merchant_application_submitted","merchant_application",
             application["id"],dumps({"status":application["status"]}),dumps({"status":"submitted"}),"",stamp),
        )
        execute_external_action(
            con, default_registry().get("whatsapp"), action_kind="merchant_application_submitted",
            target_kind="admin", target_id="admin",
            request={"applicationId":application["id"],"merchantName":business["nameEn"]},
        )

    def _onboarding_view(self, con, application) -> dict:
        steps = []
        completed = set()
        for row in con.execute(
            "SELECT step_key,payload_json,completed_at,updated_at FROM merchant_application_steps WHERE application_id=?",
            (application["id"],),
        ):
            completed.add(row["step_key"])
            steps.append({"key":row["step_key"],"data":loads(row["payload_json"],{}),"completedAt":row["completed_at"],"updatedAt":row["updated_at"]})
        next_step = next((step for step in ONBOARDING_STEPS if step not in completed), None)
        return {
            "application": {
                "id":application["id"],"merchantId":application["merchant_id"],"status":application["status"],
                "merchantStatus":application["merchant_status"],"reviewerNote":application["reviewer_note"],
                "submittedAt":application["submitted_at"],
            },
            "steps":steps,"requiredSteps":list(ONBOARDING_STEPS),"nextStep":next_step,
        }

    # ---------- shopper account, cart editing ----------

    def update_cart_item(self, actor: dict, kind: str, item_id: str, payload: dict) -> dict:
        if kind not in {"product", "bundle"}:
            raise DomainError("invalid_cart_item_kind", 422)
        quantity = _bounded_int(payload.get("quantity", 0), 0, 100, "invalid_quantity")
        with connect(immediate=True) as con:
            actor = self._require_actor(con, actor, {"shopper"})
            cart = con.execute("SELECT * FROM carts WHERE account_id=? AND status='active'", (actor["accountId"],)).fetchone()
            if not cart:
                raise DomainError("cart_empty", 409)
            expected = _bounded_int(payload.get("expectedVersion"), 1, 2_000_000_000, "invalid_cart_version")
            if expected != cart["version"]:
                raise DomainError("cart_version_conflict", 409, {"currentVersion":cart["version"]})
            line = con.execute(
                "SELECT 1 FROM cart_items WHERE cart_id=? AND item_kind=? AND item_id=?",
                (cart["id"],kind,item_id),
            ).fetchone()
            if not line:
                raise DomainError("cart_item_not_found", 404)
            if quantity:
                item = self._public_cart_item(con,kind,item_id,cart["branch_id"])
                if not item or item["merchant_id"] != cart["merchant_id"]:
                    raise DomainError("item_not_available",404)
                con.execute(
                    "UPDATE cart_items SET quantity=? WHERE cart_id=? AND item_kind=? AND item_id=?",
                    (quantity,cart["id"],kind,item_id),
                )
            else:
                con.execute("DELETE FROM cart_items WHERE cart_id=? AND item_kind=? AND item_id=?", (cart["id"],kind,item_id))
            changed = con.execute(
                "UPDATE carts SET version=version+1,updated_at=? WHERE id=? AND version=?",
                (now_iso(),cart["id"],expected),
            ).rowcount
            if changed != 1:
                raise DomainError("cart_version_conflict", 409)
            return self._cart_view(con,actor["accountId"])

    def save_address(self, actor: dict, payload: dict) -> dict:
        address_id = clean_text(payload.get("id"), 90)
        address_type = clean_text(payload.get("addressType"), 20, True)
        if address_type not in {"home","office","other"}:
            raise DomainError("invalid_address_type",422)
        wilayah_id, area_id = clean_text(payload.get("wilayahId"),90,True), clean_text(payload.get("areaId"),90,True)
        latitude, longitude = _coordinates(payload.get("latitude"), payload.get("longitude"))
        with connect(immediate=True) as con:
            actor=self._require_actor(con,actor,{"shopper"})
            if not con.execute(
                """SELECT 1 FROM locations a JOIN locations w ON w.id=a.parent_id
                   WHERE a.id=? AND a.kind='area' AND a.active=1
                     AND w.id=? AND w.kind='wilayat' AND w.active=1""",
                (area_id,wilayah_id),
            ).fetchone():
                raise DomainError("valid_area_required",422)
            stamp=now_iso()
            if address_id:
                changed=con.execute("""UPDATE shopper_addresses SET address_type=?,label=?,governorate_id='muscat_governorate',wilayah_id=?,area_id=?,address_text=?,latitude=?,longitude=?,updated_at=? WHERE id=? AND account_id=? AND active=1""",
                    (address_type,clean_text(payload.get("label"),80),wilayah_id,area_id,clean_text(payload.get("addressText"),300,True),latitude,longitude,stamp,address_id,actor["accountId"])).rowcount
                if changed!=1: raise DomainError("address_not_found",404)
            else:
                address_id=new_id("address")
                con.execute("""INSERT INTO shopper_addresses(id,account_id,address_type,label,governorate_id,wilayah_id,area_id,address_text,latitude,longitude,active,created_at,updated_at) VALUES(?,?,?,?, 'muscat_governorate',?,?,?,?,?,1,?,?)""",
                    (address_id,actor["accountId"],address_type,clean_text(payload.get("label"),80),wilayah_id,area_id,clean_text(payload.get("addressText"),300,True),latitude,longitude,stamp,stamp))
            return dict(con.execute("SELECT * FROM shopper_addresses WHERE id=? AND account_id=?",(address_id,actor["accountId"])).fetchone())

    def addresses(self, actor: dict) -> list[dict]:
        with connect() as con:
            actor=self._require_actor(con,actor,{"shopper"})
            return [dict(row) for row in con.execute("SELECT * FROM shopper_addresses WHERE account_id=? AND active=1 ORDER BY updated_at DESC",(actor["accountId"],))]

    def orders(self, actor: dict, *, limit: int = 100) -> list[dict]:
        """Return a role-scoped order list without exposing address snapshots."""
        limit = _bounded_int(limit, 1, 200, "invalid_limit")
        with connect() as con:
            actor = self._require_actor(con, actor, {"shopper", *MERCHANT_ROLES})
            if actor["role"] == "shopper":
                scope, value = "account_id", actor["accountId"]
            else:
                scope, value = "merchant_id", actor["merchantId"]
            rows = []
            for row in con.execute(
                f"""SELECT id,account_id,merchant_id,branch_id,status,fulfillment_mode,
                           subtotal_baisa,delivery_fee_baisa,total_baisa,response_due_at,
                           expires_at,payment_method,cancellation_reason,version,created_at,updated_at
                    FROM orders WHERE {scope}=? ORDER BY created_at DESC LIMIT ?""",
                (value, limit),
            ):
                item = dict(row)
                item["subtotal"] = omr(item.pop("subtotal_baisa"))
                item["deliveryFee"] = omr(item.pop("delivery_fee_baisa"))
                item["total"] = omr(item.pop("total_baisa"))
                item["allowedActions"] = self._order_allowed_actions(actor, item)
                rows.append(item)
            return rows

    # ---------- merchant catalog controls ----------

    def product_action(self, actor: dict, product_id: str, payload: dict) -> dict:
        action=clean_text(payload.get("action"),40,True)
        with connect(immediate=True) as con:
            actor=self._require_actor(con,actor,MERCHANT_ROLES)
            permission = "inventory.manage" if action == "duplicate_to_branch" else "catalog.manage"
            require_permission(actor, permission, merchant_id=actor["merchantId"], con=con)
            product=con.execute("SELECT * FROM products WHERE id=? AND merchant_id=?",(product_id,actor["merchantId"])).fetchone()
            if not product: raise DomainError("product_not_found",404)
            stamp=now_iso()
            if action=="archive":
                con.execute("UPDATE products SET active=0,status='archived',archived_at=?,updated_at=? WHERE id=? AND merchant_id=?",(stamp,stamp,product_id,actor["merchantId"]))
            elif action in {"pause","resume"}:
                if action == "resume" and product["status"] == "archived":
                    raise DomainError("archived_product_cannot_resume", 409)
                active=0 if action=="pause" else 1
                con.execute("UPDATE products SET active=?,updated_at=? WHERE id=? AND merchant_id=?",(active,stamp,product_id,actor["merchantId"]))
            elif action=="duplicate_to_branch":
                branch_id=clean_text(payload.get("branchId"),90,True)
                if not con.execute("SELECT 1 FROM store_branches WHERE id=? AND merchant_id=? AND active=1",(branch_id,actor["merchantId"])).fetchone(): raise DomainError("branch_not_owned",403)
                quantity=_bounded_int(payload.get("quantity",0),0,1_000_000,"invalid_quantity")
                reserved = con.execute(
                    """SELECT COALESCE(SUM(quantity),0) n FROM inventory_reservations
                       WHERE product_id=? AND branch_id=? AND status='pending'""",
                    (product_id, branch_id),
                ).fetchone()["n"]
                if quantity < reserved:
                    raise DomainError("inventory_below_reserved", 409, {"reserved": reserved})
                availability="in_stock" if quantity>2 else "low_stock" if quantity else "out_of_stock"
                con.execute("""INSERT INTO product_branch_inventory(
                    product_id,branch_id,stock_mode,quantity,availability,last_stock_verified_at,
                    stale_at,active,updated_at,freshness_status,stale_enforcement)
                    VALUES(?,?,'tracked',?,?,'','',1,?,'unverified','')
                    ON CONFLICT(product_id,branch_id) DO UPDATE SET
                    quantity=excluded.quantity,availability=excluded.availability,active=1,
                    last_stock_verified_at='',stale_at='',freshness_status='unverified',
                    stale_enforcement='',updated_at=excluded.updated_at""",
                    (product_id,branch_id,quantity,availability,stamp))
            else: raise DomainError("invalid_product_action",422)
            con.execute(
                """INSERT INTO admin_audit_logs(
                    id,actor_id,action,target_kind,target_id,before_json,after_json,reason,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (new_id("audit"), actor["accountId"], f"product_{action}", "product", product_id,
                 dumps(dict(product)), dumps({"action": action, "branchId": clean_text(payload.get("branchId"), 90)}),
                 clean_text(payload.get("reason"), 300), stamp),
            )
            return {"id":product_id,"action":action,"status":"archived" if action=="archive" else "updated"}

    # ---------- merchant promotions ----------

    def merchant_campaign_action(self, actor: dict, payload: dict) -> dict:
        action = clean_text(payload.get("action"), 40, True)
        if action != "create_campaign":
            raise DomainError("invalid_merchant_campaign_action", 422)
        campaign = payload.get("payload") if isinstance(payload.get("payload"), dict) else payload
        idempotency_key = clean_text(payload.get("idempotencyKey") or campaign.get("idempotencyKey"), 120, True)
        placement = clean_text(campaign.get("placement"), 50, True)
        landing_kind = clean_text(campaign.get("landingKind"), 30, True)
        landing_id = clean_text(campaign.get("landingId"), 90, True)
        if landing_kind not in {"store", "product", "bundle"}:
            raise DomainError("invalid_campaign_landing", 422)
        with connect(immediate=True) as con:
            actor = self._require_actor(con, actor, {"merchant_owner", "merchant_manager"})
            require_permission(actor, "promotion.manage", merchant_id=actor["merchantId"], con=con)
            operation = f"campaign_create:{actor['merchantId']}"
            request_hash = _payload_hash({
                "merchantId": actor["merchantId"],
                "campaign": {key: value for key, value in campaign.items() if key != "idempotencyKey"},
            })
            replay = con.execute(
                """SELECT payload_hash,response_json FROM idempotency_records
                   WHERE actor_id=? AND operation=? AND idempotency_key=?""",
                (actor["accountId"], operation, idempotency_key),
            ).fetchone()
            if replay:
                if replay["payload_hash"] != request_hash:
                    raise DomainError("idempotency_key_reused", 409)
                return {**loads(replay["response_json"], {}), "duplicate": True}
            plan = self._active_plan(con, actor["merchantId"])
            if not plan["entitlements"].get("canBuyAds"):
                raise DomainError("plan_advertising_not_available", 403)
            placement_row = con.execute(
                "SELECT key,enabled,frequency_cap FROM ad_placements WHERE key=?", (placement,)
            ).fetchone()
            if not placement_row or not placement_row["enabled"]:
                raise DomainError("campaign_placement_unavailable", 409)
            ownership_sql = {
                "store": "SELECT 1 FROM store_branches WHERE id=? AND merchant_id=? AND active=1 AND status='approved'",
                "product": "SELECT 1 FROM products WHERE id=? AND merchant_id=? AND active=1 AND status='approved'",
                "bundle": "SELECT 1 FROM bundles WHERE id=? AND merchant_id=? AND status='approved' AND moderation_status='approved'",
            }[landing_kind]
            if not con.execute(ownership_sql, (landing_id, actor["merchantId"])).fetchone():
                raise DomainError("campaign_landing_unavailable", 409)
            starts = _iso_datetime(campaign.get("startsAt"))
            ends = _iso_datetime(campaign.get("endsAt"))
            if starts and ends and starts >= ends:
                raise DomainError("invalid_campaign_dates", 422)
            starts_at = starts.isoformat() if starts else ""
            ends_at = ends.isoformat() if ends else ""
            creative_media_id = clean_text(campaign.get("creativeMediaId"), 90)
            if creative_media_id and not con.execute(
                """SELECT 1 FROM private_media_objects
                   WHERE id=? AND owner_kind='merchant' AND owner_id=?
                     AND purpose='ad_creative' AND status='active' AND mime_type LIKE 'image/%'""",
                (creative_media_id, actor["merchantId"]),
            ).fetchone():
                raise DomainError("campaign_creative_not_found", 404)
            wilayat_ids = list(dict.fromkeys(
                clean_text(item, 90) for item in campaign.get("wilayatIds", [])[:20] if clean_text(item, 90)
            ))
            area_ids = list(dict.fromkeys(
                clean_text(item, 90) for item in campaign.get("areaIds", [])[:100] if clean_text(item, 90)
            ))
            category_ids = list(dict.fromkeys(
                clean_text(item, 90) for item in campaign.get("categoryIds", [])[:30] if clean_text(item, 90)
            ))
            for ids, kind, error in (
                (wilayat_ids, "wilayat", "invalid_campaign_wilayat"),
                (area_ids, "area", "invalid_campaign_area"),
            ):
                if ids:
                    marks = ",".join("?" for _ in ids)
                    count = con.execute(
                        f"SELECT COUNT(*) n FROM locations WHERE id IN ({marks}) AND kind=? AND active=1",
                        (*ids, kind),
                    ).fetchone()["n"]
                    if count != len(ids):
                        raise DomainError(error, 422)
            if category_ids:
                marks = ",".join("?" for _ in category_ids)
                count = con.execute(
                    f"SELECT COUNT(*) n FROM product_categories WHERE id IN ({marks}) AND active=1",
                    category_ids,
                ).fetchone()["n"]
                if count != len(category_ids):
                    raise DomainError("invalid_campaign_category", 422)
            language = clean_text(campaign.get("language"), 10) or "all"
            if language not in {"all", "ar", "en"}:
                raise DomainError("invalid_campaign_language", 422)
            target = {
                "titleAr": clean_text(campaign.get("titleAr"), 120, True),
                "titleEn": clean_text(campaign.get("titleEn"), 120, True),
                "creativeMediaId": creative_media_id,
                # This is set by the server and cannot be promoted by the
                # merchant payload.  A real PSP or credit-grant workflow must
                # move it to an approvable state before public moderation.
                "paymentStatus": "not_started",
                "wilayatIds": wilayat_ids,
                "areaIds": area_ids,
                "categoryIds": category_ids,
                "language": language,
            }
            campaign_id, stamp = new_id("campaign"), now_iso()
            con.execute(
                """INSERT INTO ad_campaigns(
                    id,owner_kind,owner_id,placement,target_json,landing_kind,landing_id,
                    label_ar,label_en,status,starts_at,ends_at,frequency_cap,created_at,updated_at)
                   VALUES(?,'merchant',?,?,?,?,?,'إعلان','Sponsored','draft',?,?,?,?,?)""",
                (campaign_id, actor["merchantId"], placement, dumps(target), landing_kind,
                 landing_id, starts_at, ends_at, int(placement_row["frequency_cap"]), stamp, stamp),
            )
            con.execute(
                """INSERT INTO admin_audit_logs(
                    id,actor_id,action,target_kind,target_id,before_json,after_json,reason,created_at)
                   VALUES(?,?,?,?,?,'{}',?,'',?)""",
                (new_id("audit"), actor["accountId"], "campaign_draft_created", "ad_campaign",
                 campaign_id, dumps({"status":"draft","placement":placement}), stamp),
            )
            result = {
                "id": campaign_id, "status": "draft", "placement": placement,
                "requiresAdminApproval": True, "paymentStatus": "not_started",
            }
            con.execute(
                """INSERT INTO notifications(
                    id,target_kind,target_id,title_ar,title_en,body_ar,body_en,route,
                    requires_action,dedupe_key,read_at,acted_at,created_at,priority)
                   VALUES(?,?,?,?,?,?,?,?,1,?,'','',?,70)""",
                (new_id("ntf"), "admin", "admin", "حملة إعلانية للمراجعة", "Campaign pending review",
                 target["titleAr"], target["titleEn"], f"admin:campaign:{campaign_id}",
                 f"campaign:{campaign_id}:submitted:v1", stamp),
            )
            con.execute(
                """INSERT INTO idempotency_records(
                    actor_id,operation,idempotency_key,payload_hash,response_json,created_at)
                   VALUES(?,?,?,?,?,?)""",
                (actor["accountId"], operation, idempotency_key, request_hash, dumps(result), stamp),
            )
            return {**result, "duplicate": False}
