"""Commerce domain for BISA.

Amounts are stored as integer baisa. Every state-changing method owns its
transaction, checks the actor, and supports idempotency where retries matter.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from bisa_config import (
    DB_PATH, PRODUCT_MAX_BAISA, PRODUCT_MIN_BAISA, SEED_SAMPLE_DATA,
    SESSION_TTL_HOURS, ensure_runtime_directories,
)


class DomainError(Exception):
    def __init__(self, code: str, status: int = 400, detail: dict | None = None):
        super().__init__(code)
        self.code = code
        self.status = status
        self.detail = detail or {}


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def loads(value, fallback):
    if value in (None, ""):
        return fallback
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback


def clean_text(value, maximum=180, required=False) -> str:
    text = " ".join(str(value or "").strip().split())[:maximum]
    if required and not text:
        raise DomainError("required_value")
    return text


def normalize_phone(value) -> str:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    if digits.startswith("968"):
        digits = digits[3:]
    if len(digits) != 8:
        raise DomainError("valid_oman_phone_required")
    return f"968{digits}"


def hash_secret(value: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", value.encode(), salt.encode(), 210_000)
    return f"pbkdf2_sha256$210000${salt}${digest.hex()}"


def verify_secret(value: str, encoded: str) -> bool:
    try:
        algorithm, rounds, salt, expected = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac("sha256", value.encode(), salt.encode(), int(rounds)).hex()
        return hmac.compare_digest(digest, expected)
    except (ValueError, TypeError):
        return False


def to_baisa(value) -> int:
    """Convert OMR decimal input to integer baisa without binary float errors."""
    try:
        amount = Decimal(str(value)).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError, TypeError):
        raise DomainError("valid_price_required")
    return int(amount * 1000)


def omr(baisa: int) -> str:
    return f"{Decimal(int(baisa)) / Decimal(1000):.3f}"


def validate_product_price(value) -> int:
    baisa = to_baisa(value)
    if not PRODUCT_MIN_BAISA <= baisa <= PRODUCT_MAX_BAISA:
        raise DomainError("product_price_out_of_range", 422, {
            "minimum": omr(PRODUCT_MIN_BAISA), "maximum": omr(PRODUCT_MAX_BAISA)
        })
    return baisa


@contextmanager
def connect(immediate=False):
    ensure_runtime_directories()
    con = sqlite3.connect(DB_PATH, timeout=15, isolation_level=None)
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


SCHEMA = r"""
CREATE TABLE IF NOT EXISTS schema_migrations(
 version TEXT PRIMARY KEY, description TEXT NOT NULL, applied_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS demo_records(
 entity_kind TEXT NOT NULL, entity_id TEXT NOT NULL, created_at TEXT NOT NULL,
 PRIMARY KEY(entity_kind,entity_id));
CREATE TABLE IF NOT EXISTS accounts(
 id TEXT PRIMARY KEY, phone TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
 pin_hash TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active', created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS account_roles(
 account_id TEXT NOT NULL REFERENCES accounts(id), role TEXT NOT NULL,
 merchant_id TEXT NOT NULL DEFAULT '', active INTEGER NOT NULL DEFAULT 1,
 PRIMARY KEY(account_id,role,merchant_id));
CREATE TABLE IF NOT EXISTS sessions(
 token_hash TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(id),
 active_role TEXT NOT NULL, merchant_id TEXT NOT NULL DEFAULT '', expires_at TEXT NOT NULL,
 revoked_at TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS locations(
 id TEXT PRIMARY KEY, parent_id TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL,
 name_ar TEXT NOT NULL, name_en TEXT NOT NULL, sort_order INTEGER NOT NULL DEFAULT 0,
 active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS product_categories(
 id TEXT PRIMARY KEY, name_ar TEXT NOT NULL, name_en TEXT NOT NULL,
 icon TEXT NOT NULL DEFAULT '', image_path TEXT NOT NULL DEFAULT '',
 regulated_rules TEXT NOT NULL DEFAULT '{}', sort_order INTEGER NOT NULL DEFAULT 0,
 active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS merchants(
 id TEXT PRIMARY KEY, owner_account_id TEXT NOT NULL REFERENCES accounts(id),
 name_ar TEXT NOT NULL, name_en TEXT NOT NULL, merchant_type TEXT NOT NULL DEFAULT 'store',
 logo_path TEXT NOT NULL DEFAULT '', cover_path TEXT NOT NULL DEFAULT '',
 status TEXT NOT NULL DEFAULT 'draft', verified INTEGER NOT NULL DEFAULT 0,
 return_policy_id TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS merchant_applications(
 id TEXT PRIMARY KEY, merchant_id TEXT NOT NULL REFERENCES merchants(id),
 payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'draft', reviewer_note TEXT NOT NULL DEFAULT '',
 submitted_at TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS merchant_documents(
 id TEXT PRIMARY KEY, application_id TEXT NOT NULL REFERENCES merchant_applications(id),
 kind TEXT NOT NULL, private_path TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS store_branches(
 id TEXT PRIMARY KEY, merchant_id TEXT NOT NULL REFERENCES merchants(id),
 name_ar TEXT NOT NULL, name_en TEXT NOT NULL, wilayah_id TEXT NOT NULL REFERENCES locations(id),
 area_id TEXT NOT NULL DEFAULT '', address_text TEXT NOT NULL DEFAULT '', latitude REAL, longitude REAL,
 hours_json TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL DEFAULT 'draft', active INTEGER NOT NULL DEFAULT 1,
 public_visible INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS fulfillment_profiles(
 branch_id TEXT PRIMARY KEY REFERENCES store_branches(id), pickup_enabled INTEGER NOT NULL DEFAULT 1,
 office_enabled INTEGER NOT NULL DEFAULT 0, office_fee_baisa INTEGER NOT NULL DEFAULT 0,
 office_minimum_baisa INTEGER NOT NULL DEFAULT 0, office_free_threshold_baisa INTEGER NOT NULL DEFAULT 0,
 home_enabled INTEGER NOT NULL DEFAULT 0, home_fee_baisa INTEGER NOT NULL DEFAULT 0,
 home_minimum_baisa INTEGER NOT NULL DEFAULT 0, home_free_threshold_baisa INTEGER NOT NULL DEFAULT 0,
 zones_json TEXT NOT NULL DEFAULT '[]', eta_text TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS products(
 id TEXT PRIMARY KEY, merchant_id TEXT NOT NULL REFERENCES merchants(id), category_id TEXT NOT NULL REFERENCES product_categories(id),
 name_ar TEXT NOT NULL, name_en TEXT NOT NULL, description_ar TEXT NOT NULL DEFAULT '', description_en TEXT NOT NULL DEFAULT '',
 price_baisa INTEGER NOT NULL CHECK(price_baisa BETWEEN 100 AND 2000), unit_text TEXT NOT NULL DEFAULT '',
 barcode TEXT NOT NULL DEFAULT '', images_json TEXT NOT NULL DEFAULT '[]', status TEXT NOT NULL DEFAULT 'draft',
 active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS product_branch_inventory(
 product_id TEXT NOT NULL REFERENCES products(id), branch_id TEXT NOT NULL REFERENCES store_branches(id),
 stock_mode TEXT NOT NULL DEFAULT 'tracked', quantity INTEGER NOT NULL DEFAULT 0,
 availability TEXT NOT NULL DEFAULT 'out_of_stock', last_stock_verified_at TEXT NOT NULL DEFAULT '',
 stale_at TEXT NOT NULL DEFAULT '', active INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL,
 PRIMARY KEY(product_id,branch_id));
CREATE TABLE IF NOT EXISTS merchant_return_policies(
 id TEXT PRIMARY KEY, merchant_id TEXT NOT NULL REFERENCES merchants(id), version INTEGER NOT NULL,
 return_window_days INTEGER NOT NULL DEFAULT 0, exchange_window_days INTEGER NOT NULL DEFAULT 0,
 conditions_text TEXT NOT NULL, receipt_required INTEGER NOT NULL DEFAULT 1,
 excluded_categories TEXT NOT NULL DEFAULT '[]', contact_method TEXT NOT NULL DEFAULT '',
 notes TEXT NOT NULL DEFAULT '', active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL,
 UNIQUE(merchant_id,version));
CREATE TABLE IF NOT EXISTS bundles(
 id TEXT PRIMARY KEY, merchant_id TEXT NOT NULL REFERENCES merchants(id), branch_id TEXT NOT NULL REFERENCES store_branches(id),
 title_ar TEXT NOT NULL, title_en TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
 selling_price_baisa INTEGER NOT NULL CHECK(selling_price_baisa>0), status TEXT NOT NULL DEFAULT 'draft',
 starts_at TEXT NOT NULL DEFAULT '', ends_at TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS bundle_items(
 bundle_id TEXT NOT NULL REFERENCES bundles(id), product_id TEXT NOT NULL REFERENCES products(id),
 quantity INTEGER NOT NULL CHECK(quantity>0), PRIMARY KEY(bundle_id,product_id));
CREATE TABLE IF NOT EXISTS carts(
 id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(id), merchant_id TEXT NOT NULL REFERENCES merchants(id),
 branch_id TEXT NOT NULL REFERENCES store_branches(id), status TEXT NOT NULL DEFAULT 'active',
 version INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_cart_per_account ON carts(account_id) WHERE status='active';
CREATE TABLE IF NOT EXISTS cart_items(
 cart_id TEXT NOT NULL REFERENCES carts(id), item_kind TEXT NOT NULL, item_id TEXT NOT NULL,
 quantity INTEGER NOT NULL CHECK(quantity>0), unit_price_baisa INTEGER NOT NULL CHECK(unit_price_baisa>0),
 PRIMARY KEY(cart_id,item_kind,item_id));
CREATE TABLE IF NOT EXISTS orders(
 id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(id), merchant_id TEXT NOT NULL REFERENCES merchants(id),
 branch_id TEXT NOT NULL REFERENCES store_branches(id), status TEXT NOT NULL,
 fulfillment_mode TEXT NOT NULL, address_snapshot TEXT NOT NULL DEFAULT '{}', policy_snapshot TEXT NOT NULL DEFAULT '{}',
 subtotal_baisa INTEGER NOT NULL, delivery_fee_baisa INTEGER NOT NULL DEFAULT 0, total_baisa INTEGER NOT NULL,
 idempotency_key TEXT NOT NULL, response_due_at TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(account_id,idempotency_key));
CREATE TABLE IF NOT EXISTS order_items(
 order_id TEXT NOT NULL REFERENCES orders(id), item_kind TEXT NOT NULL, item_id TEXT NOT NULL,
 name_snapshot TEXT NOT NULL, quantity INTEGER NOT NULL, unit_price_baisa INTEGER NOT NULL,
 component_snapshot TEXT NOT NULL DEFAULT '[]', PRIMARY KEY(order_id,item_kind,item_id));
CREATE TABLE IF NOT EXISTS inventory_reservations(
 id TEXT PRIMARY KEY, order_id TEXT NOT NULL REFERENCES orders(id), product_id TEXT NOT NULL REFERENCES products(id),
 branch_id TEXT NOT NULL REFERENCES store_branches(id), quantity INTEGER NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL,
 UNIQUE(order_id,product_id,branch_id));
CREATE TABLE IF NOT EXISTS inventory_audits(
 id TEXT PRIMARY KEY, merchant_id TEXT NOT NULL REFERENCES merchants(id), branch_id TEXT NOT NULL REFERENCES store_branches(id),
 status TEXT NOT NULL DEFAULT 'due', due_at TEXT NOT NULL, confirmed_at TEXT NOT NULL DEFAULT '',
 confirmed_by TEXT NOT NULL DEFAULT '', summary TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS subscription_plans(
 id TEXT PRIMARY KEY, name_ar TEXT NOT NULL, name_en TEXT NOT NULL,
 price_baisa INTEGER NOT NULL, duration_days INTEGER NOT NULL, entitlements TEXT NOT NULL,
 active INTEGER NOT NULL DEFAULT 1, sort_order INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS merchant_subscriptions(
 id TEXT PRIMARY KEY, merchant_id TEXT NOT NULL REFERENCES merchants(id), plan_id TEXT NOT NULL REFERENCES subscription_plans(id),
 starts_at TEXT NOT NULL, ends_at TEXT NOT NULL, status TEXT NOT NULL, granted_by TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ad_campaigns(
 id TEXT PRIMARY KEY, owner_kind TEXT NOT NULL, owner_id TEXT NOT NULL, placement TEXT NOT NULL,
 target_json TEXT NOT NULL DEFAULT '{}', landing_kind TEXT NOT NULL, landing_id TEXT NOT NULL,
 label_ar TEXT NOT NULL DEFAULT 'إعلان', label_en TEXT NOT NULL DEFAULT 'Sponsored',
 status TEXT NOT NULL DEFAULT 'draft', starts_at TEXT NOT NULL DEFAULT '', ends_at TEXT NOT NULL DEFAULT '',
 frequency_cap INTEGER NOT NULL DEFAULT 3, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ad_events(
 id TEXT PRIMARY KEY, campaign_id TEXT NOT NULL REFERENCES ad_campaigns(id), event_type TEXT NOT NULL,
 actor_hash TEXT NOT NULL DEFAULT '', entity_id TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS suppliers(
 id TEXT PRIMARY KEY, name_ar TEXT NOT NULL, name_en TEXT NOT NULL, logo_path TEXT NOT NULL DEFAULT '',
 status TEXT NOT NULL DEFAULT 'draft', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS supplier_campaigns(
 id TEXT PRIMARY KEY, supplier_id TEXT NOT NULL REFERENCES suppliers(id), title_ar TEXT NOT NULL, title_en TEXT NOT NULL,
 payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'draft', starts_at TEXT NOT NULL DEFAULT '', ends_at TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS supplier_leads(
 id TEXT PRIMARY KEY, campaign_id TEXT NOT NULL REFERENCES supplier_campaigns(id), merchant_id TEXT NOT NULL REFERENCES merchants(id),
 action_kind TEXT NOT NULL, note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS favorites(
 account_id TEXT NOT NULL REFERENCES accounts(id), entity_kind TEXT NOT NULL, entity_id TEXT NOT NULL,
 created_at TEXT NOT NULL, PRIMARY KEY(account_id,entity_kind,entity_id));
CREATE TABLE IF NOT EXISTS analytics_events(
 id TEXT PRIMARY KEY, event_type TEXT NOT NULL, actor_hash TEXT NOT NULL DEFAULT '', entity_kind TEXT NOT NULL DEFAULT '',
 entity_id TEXT NOT NULL DEFAULT '', context_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS notifications(
 id TEXT PRIMARY KEY, target_kind TEXT NOT NULL, target_id TEXT NOT NULL, title_ar TEXT NOT NULL, title_en TEXT NOT NULL,
 body_ar TEXT NOT NULL, body_en TEXT NOT NULL, route TEXT NOT NULL, requires_action INTEGER NOT NULL DEFAULT 0,
 dedupe_key TEXT NOT NULL DEFAULT '', read_at TEXT NOT NULL DEFAULT '', acted_at TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS idx_notification_dedupe ON notifications(dedupe_key) WHERE dedupe_key<>'';
CREATE TABLE IF NOT EXISTS platform_settings(
 key TEXT PRIMARY KEY, value_json TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS admin_audit_logs(
 id TEXT PRIMARY KEY, actor_id TEXT NOT NULL, action TEXT NOT NULL, target_kind TEXT NOT NULL,
 target_id TEXT NOT NULL, before_json TEXT NOT NULL DEFAULT '{}', after_json TEXT NOT NULL DEFAULT '{}',
 reason TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL);
"""


PLAN_DEFAULTS = {
    "early_trial": {"name_ar": "التجريبية — أول المنضمين", "name_en": "Early trial", "price": 0, "duration": 90,
        "entitlements": {"inherits": "basic_3m", "products": 400, "branches": 2, "staff": 2, "bundles": 8, "analytics": "basic", "supplierHub": True}},
    "basic_3m": {"name_ar": "الأساسية", "name_en": "Basic", "price": 40000, "duration": 90,
        "entitlements": {"products": 400, "branches": 2, "staff": 2, "bundles": 8, "analytics": "basic", "supplierHub": True}},
    "advanced_3m": {"name_ar": "المتقدمة", "name_en": "Advanced", "price": 60000, "duration": 90,
        "entitlements": {"products": 900, "branches": 5, "staff": 6, "bundles": 25, "analytics": "advanced", "bulkImport": True, "supplierHub": True, "includedBoosts": 3}},
}


MUSCAT_WILAYATS = [
    ("muscat", "مسقط", "Muscat"), ("muttrah", "مطرح", "Muttrah"),
    ("bawshar", "بوشر", "Bawshar"), ("seeb", "السيب", "Seeb"),
    ("al_amerat", "العامرات", "Al Amerat"), ("qurayyat", "قريات", "Qurayyat"),
]


CATEGORIES = [
    ("kitchen", "أدوات المطبخ", "Kitchen", "🍳"), ("cookware", "الأواني", "Cookware", "🥘"),
    ("storage", "التخزين والتنظيم", "Storage & Organization", "🧺"), ("cleaning", "التنظيف", "Cleaning", "🧽"),
    ("snacks", "مكسرات وسناكس", "Snacks & Nuts", "🥜"), ("stationery", "قرطاسية", "Stationery", "✏️"),
    ("toys", "ألعاب", "Toys", "🪁"), ("decor", "ديكور", "Decor", "🪴"),
    ("party", "مستلزمات الحفلات", "Party", "🎉"), ("accessories", "إكسسوارات", "Accessories", "👜"),
    ("personal", "عناية شخصية", "Personal Care", "🧴"), ("car", "مستلزمات سيارات", "Car Accessories", "🚗"),
    ("seasonal", "موسمي", "Seasonal", "✨"),
]


def init_db() -> None:
    ensure_runtime_directories()
    with connect(immediate=True) as con:
        con.executescript(SCHEMA)
        stamp = now_iso()
        con.execute("INSERT OR IGNORE INTO schema_migrations VALUES('001','BISA local commerce foundation',?)", (stamp,))
        con.execute("INSERT OR IGNORE INTO locations VALUES(?,?,?,?,?,?,?,?)",
                    ("oman", "", "country", "عُمان", "Oman", 0, 1, stamp))
        con.execute("INSERT OR IGNORE INTO locations VALUES(?,?,?,?,?,?,?,?)",
                    ("muscat_governorate", "oman", "governorate", "محافظة مسقط", "Muscat Governorate", 0, 1, stamp))
        for order, (key, ar, en) in enumerate(MUSCAT_WILAYATS):
            con.execute("INSERT OR IGNORE INTO locations VALUES(?,?,?,?,?,?,?,?)",
                        (f"wilayat_{key}", "muscat_governorate", "wilayat", ar, en, order, 1, stamp))
        for order, (key, ar, en, icon) in enumerate(CATEGORIES):
            con.execute("""INSERT OR IGNORE INTO product_categories
                (id,name_ar,name_en,icon,image_path,regulated_rules,sort_order,active,created_at,updated_at)
                VALUES(?,?,?,?,?,'{}',?,1,?,?)""", (key, ar, en, icon, "", order, stamp, stamp))
        for order, (pid, plan) in enumerate(PLAN_DEFAULTS.items()):
            con.execute("""INSERT OR IGNORE INTO subscription_plans
                (id,name_ar,name_en,price_baisa,duration_days,entitlements,active,sort_order,updated_at)
                VALUES(?,?,?,?,?,?,1,?,?)""", (pid, plan["name_ar"], plan["name_en"], plan["price"], plan["duration"], dumps(plan["entitlements"]), order, stamp))
        defaults = {
            "commissionRate": 0, "bundleMaxComponents": 10, "merchantResponseHours": 4,
            "inventoryCadenceHours": 24, "inventoryReminderLeadHours": 3,
            "inventoryEnforcement": "mark_stale", "trialEnabled": True,
            "trialCutoffAt": "", "trialFirstApprovedMerchants": 100,
            "paymentsEnabled": False, "whatsappEnabled": False,
        }
        for key, value in defaults.items():
            con.execute("INSERT OR IGNORE INTO platform_settings VALUES(?,?,?)", (key, dumps(value), stamp))
        if SEED_SAMPLE_DATA:
            seed_demo(con)


def seed_demo(con) -> None:
    """Create clearly tagged showcase data across all six Muscat wilayats."""
    stamp = now_iso()

    def mark(kind: str, entity_id: str) -> None:
        con.execute("INSERT OR IGNORE INTO demo_records VALUES(?,?,?)", (kind, entity_id, stamp))

    shopper = "demo_account_shopper"
    con.execute("INSERT OR IGNORE INTO accounts VALUES(?,?,?,?,?,?)", (shopper, "96890000001", "متسوق بيسا التجريبي", hash_secret("1234"), "active", stamp))
    con.execute("INSERT OR IGNORE INTO account_roles VALUES(?,?,?,1)", (shopper, "shopper", ""))
    mark("account", shopper)

    showcases = [
        ("muscat", "مركز مسقط", "Muscat Centre", "لمسات الميناء", "Harbour Finds", 23.615, 58.594, "🪴"),
        ("muttrah", "مركز مطرح", "Muttrah Centre", "لقطات السوق", "Souq Pop", 23.618, 58.565, "🛍️"),
        ("bawshar", "الخوير", "Al Khuwair", "بيت بيسا", "BISA Home", 23.594, 58.424, "🏠"),
        ("seeb", "الموالح", "Al Mawaleh", "يوميات الموالح", "Mawaleh Daily", 23.596, 58.221, "✨"),
        ("al_amerat", "مركز العامرات", "Al Amerat Centre", "لمعة العامرات", "Amerat Spark", 23.500, 58.505, "🎁"),
        ("qurayyat", "مركز قريات", "Qurayyat Centre", "اختيارات الساحل", "Coast Picks", 23.263, 58.913, "🌊"),
    ]
    templates = [
        ("storage", "منظم يومي", "Daily organizer", 1300),
        ("kitchen", "أكواب ملوّنة", "Color cups", 500),
        ("stationery", "دفتر جيب", "Pocket notebook", 250),
        ("cleaning", "إسفنجة عملية", "Handy sponge", 100),
        ("snacks", "مكسرات مختارة", "Selected nuts", 1900),
        ("party", "زينة صغيرة", "Mini party decor", 2000),
    ]
    for order, (key, area_ar, area_en, store_ar, store_en, lat, lng, icon) in enumerate(showcases):
        account_id = f"demo_account_{key}"
        merchant_id = f"demo_merchant_{key}"
        area_id = f"demo_area_{key}"
        branch_id = f"demo_branch_{key}"
        policy_id = f"demo_policy_{key}"
        con.execute("INSERT OR IGNORE INTO accounts VALUES(?,?,?,?,?,?)", (account_id, f"96892{order:06d}", f"مالك {store_ar}", hash_secret("1234"), "active", stamp))
        con.execute("INSERT OR IGNORE INTO account_roles VALUES(?,?,?,1)", (account_id, "merchant_owner", merchant_id))
        con.execute("INSERT OR IGNORE INTO locations VALUES(?,?,?,?,?,?,?,?)", (area_id, f"wilayat_{key}", "area", area_ar, area_en, order + 1, 1, stamp))
        con.execute("""INSERT OR IGNORE INTO merchants
            (id,owner_account_id,name_ar,name_en,merchant_type,status,verified,created_at,updated_at)
            VALUES(?,?,?,?,'store','approved',1,?,?)""", (merchant_id, account_id, store_ar, store_en, stamp, stamp))
        con.execute("""INSERT OR IGNORE INTO store_branches
            (id,merchant_id,name_ar,name_en,wilayah_id,area_id,address_text,latitude,longitude,status,active,public_visible,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,'approved',1,1,?,?)""",
            (branch_id, merchant_id, f"فرع {area_ar}", f"{area_en} Branch", f"wilayat_{key}", area_id, area_ar, lat, lng, stamp, stamp))
        con.execute("""INSERT OR IGNORE INTO fulfillment_profiles
            (branch_id,pickup_enabled,office_enabled,office_fee_baisa,office_minimum_baisa,office_free_threshold_baisa,
             home_enabled,home_fee_baisa,home_minimum_baisa,home_free_threshold_baisa,zones_json,eta_text,updated_at)
            VALUES(?,1,1,1000,3000,10000,1,2000,5000,12000,?,'خلال 60–90 دقيقة',?)""", (branch_id, dumps([area_id]), stamp))
        con.execute("""INSERT OR IGNORE INTO merchant_return_policies VALUES(
            ?,?,1,15,15,'المنتج بحالته الأصلية مع إثبات الشراء',1,'[]','داخل التطبيق','حقوق المستهلك النظامية محفوظة',1,?)""", (policy_id, merchant_id, stamp))
        con.execute("UPDATE merchants SET return_policy_id=? WHERE id=?", (policy_id, merchant_id))
        subscription_id = f"demo_subscription_{key}"
        con.execute("INSERT OR IGNORE INTO merchant_subscriptions VALUES(?,?, 'advanced_3m',?,?, 'active','demo',?)", (subscription_id, merchant_id, stamp, (datetime.now(UTC)+timedelta(days=90)).isoformat(), stamp))
        product_ids = []
        for product_order in range(4):
            category, name_ar, name_en, price = templates[(order + product_order) % len(templates)]
            product_id = f"demo_product_{key}_{product_order + 1}"
            product_ids.append(product_id)
            con.execute("""INSERT OR IGNORE INTO products
                (id,merchant_id,category_id,name_ar,name_en,description_ar,description_en,price_baisa,images_json,status,active,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,'approved',1,?,?)""",
                (product_id, merchant_id, category, name_ar, name_en, f"{icon} اكتشاف تجريبي في {area_ar}", f"{icon} Demo discovery in {area_en}", price, "[]", stamp, stamp))
            con.execute("INSERT OR IGNORE INTO product_branch_inventory VALUES(?,?,'tracked',25,'in_stock',?,'',1,?)", (product_id, branch_id, stamp, stamp))
            mark("product", product_id)
        bundle_id = f"demo_bundle_{key}"
        con.execute("""INSERT OR IGNORE INTO bundles
            (id,merchant_id,branch_id,title_ar,title_en,description,selling_price_baisa,status,created_at,updated_at)
            VALUES(?,?,?,?,?,'باقة تجريبية مختارة',3100,'approved',?,?)""", (bundle_id, merchant_id, branch_id, f"باقة {area_ar}", f"{area_en} Bundle", stamp, stamp))
        con.executemany("INSERT OR IGNORE INTO bundle_items VALUES(?,?,?)", [(bundle_id, product_ids[0], 1), (bundle_id, product_ids[1], 2)])
        campaign_id = f"demo_ad_{key}"
        creative = {"areaId": area_id, "titleAr": f"اكتشف {store_ar}", "titleEn": f"Discover {store_en}", "bodyAr": f"اختيارات اليوم في {area_ar}", "bodyEn": f"Today's picks in {area_en}", "icon": icon}
        con.execute("""INSERT OR IGNORE INTO ad_campaigns
            (id,owner_kind,owner_id,placement,target_json,landing_kind,landing_id,label_ar,label_en,status,frequency_cap,created_at,updated_at)
            VALUES(?,'merchant',?,'home_hero',?,'store',?,'إعلان تجريبي','Demo sponsored','approved',3,?,?)""",
            (campaign_id, merchant_id, dumps(creative), branch_id, stamp, stamp))
        for kind, entity_id in (("account", account_id), ("area", area_id), ("merchant", merchant_id), ("branch", branch_id), ("policy", policy_id), ("subscription", subscription_id), ("bundle", bundle_id), ("ad", campaign_id)):
            mark(kind, entity_id)


def rowdict(row):
    return dict(row) if row else None


def settings(con) -> dict:
    return {r["key"]: loads(r["value_json"], None) for r in con.execute("SELECT * FROM platform_settings")}


def active_plan(con, merchant_id: str) -> dict:
    row = con.execute("""SELECT p.* FROM merchant_subscriptions s JOIN subscription_plans p ON p.id=s.plan_id
        WHERE s.merchant_id=? AND s.status='active' AND s.ends_at>? ORDER BY s.ends_at DESC LIMIT 1""", (merchant_id, now_iso())).fetchone()
    if not row:
        raise DomainError("active_plan_required", 403)
    data = rowdict(row); data["entitlements"] = loads(data["entitlements"], {})
    return data


def require_role(actor: dict, *roles: str, merchant_id: str = "") -> None:
    if not actor or actor.get("role") not in roles:
        raise DomainError("forbidden", 403)
    if merchant_id and actor.get("merchantId") != merchant_id and actor.get("role") not in {"admin", "super_admin"}:
        raise DomainError("forbidden", 403)


def authenticate(token: str) -> dict | None:
    if not token:
        return None
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    with connect() as con:
        row = con.execute("""SELECT s.*,a.name,a.status FROM sessions s JOIN accounts a ON a.id=s.account_id
            WHERE s.token_hash=? AND s.revoked_at='' AND s.expires_at>? AND a.status='active'""", (token_hash, now_iso())).fetchone()
        if not row:
            return None
        return {"accountId": row["account_id"], "name": row["name"], "role": row["active_role"], "merchantId": row["merchant_id"]}


def register_or_login(phone, pin, name="", role="shopper") -> dict:
    phone = normalize_phone(phone)
    pin = str(pin or "")
    if len(pin) < 4 or len(pin) > 8 or not pin.isdigit():
        raise DomainError("valid_pin_required")
    allowed_roles = {"shopper", "merchant_owner", "merchant_manager", "merchant_staff", "support_admin", "admin", "super_admin"}
    if role not in allowed_roles:
        raise DomainError("invalid_role")
    with connect(immediate=True) as con:
        row = con.execute("SELECT * FROM accounts WHERE phone=?", (phone,)).fetchone()
        if row:
            if not verify_secret(pin, row["pin_hash"]):
                raise DomainError("invalid_login", 403)
            account_id = row["id"]
        elif role == "shopper":
            account_id = new_id("acct")
            con.execute("INSERT INTO accounts VALUES(?,?,?,?,?,?)", (account_id, phone, clean_text(name, 80) or "BISA", hash_secret(pin), "active", now_iso()))
        else:
            # Privileged and merchant roles are provisioned only by an approved workflow.
            raise DomainError("invalid_login", 403)
        role_row = con.execute("SELECT merchant_id FROM account_roles WHERE account_id=? AND role=? AND active=1", (account_id, role)).fetchone()
        if not role_row and role == "shopper":
            con.execute("INSERT INTO account_roles VALUES(?,?,?,1)", (account_id, role, ""))
            merchant_id = ""
        elif role_row:
            merchant_id = role_row["merchant_id"]
        elif role.startswith("merchant_"):
            raise DomainError("merchant_application_required", 403)
        else:
            raise DomainError("invalid_login", 403)
        token = secrets.token_urlsafe(36)
        con.execute("INSERT INTO sessions VALUES(?,?,?,?,?,?,?)", (
            hashlib.sha256(token.encode()).hexdigest(), account_id, role, merchant_id,
            (datetime.now(UTC)+timedelta(hours=SESSION_TTL_HOURS)).isoformat(), "", now_iso()))
        return {"token": token, "account": {"id": account_id, "name": row["name"] if row else clean_text(name, 80) or "BISA", "role": role, "merchantId": merchant_id}}


class BisaService:
    def public_bootstrap(self, actor=None) -> dict:
        with connect() as con:
            location_rows = con.execute("""SELECT l.* FROM locations l WHERE l.active=1 AND (
                l.kind!='area' OR EXISTS(SELECT 1 FROM store_branches b WHERE b.area_id=l.id AND b.status='approved' AND b.active=1 AND b.public_visible=1))
                ORDER BY l.kind,l.sort_order,l.name_en""").fetchall()
            categories = [rowdict(r) for r in con.execute("SELECT * FROM product_categories WHERE active=1 ORDER BY sort_order")]
            stores = self._stores(con)
            products = self._products(con)
            bundles = self._bundles(con)
            advertisements = self._advertisements(con)
            plans = []
            if actor and actor.get("role") in {"merchant_owner", "merchant_manager", "merchant_staff", "admin", "super_admin"}:
                plans = [self._plan(r) for r in con.execute("SELECT * FROM subscription_plans WHERE active=1 ORDER BY sort_order")]
            cart = self._cart(con, actor["accountId"]) if actor and actor.get("role") == "shopper" else None
            orders = []
            notifications = []
            if actor:
                if actor.get("role") == "shopper":
                    orders = [dict(r) for r in con.execute("SELECT * FROM orders WHERE account_id=? ORDER BY created_at DESC LIMIT 100", (actor["accountId"],))]
                    target_kind, target_id = "account", actor["accountId"]
                elif actor.get("role", "").startswith("merchant_"):
                    target_kind, target_id = "merchant", actor.get("merchantId", "")
                else:
                    target_kind, target_id = actor.get("role", ""), actor.get("accountId", "")
                notifications = [dict(r) for r in con.execute("""SELECT * FROM notifications
                    WHERE target_kind=? AND target_id=? ORDER BY requires_action DESC, created_at DESC LIMIT 100""", (target_kind, target_id))]
            demo_counts = {r["entity_kind"]: r["n"] for r in con.execute("SELECT entity_kind,COUNT(*) n FROM demo_records GROUP BY entity_kind")}
            return {"locations": [rowdict(r) for r in location_rows], "categories": categories,
                    "stores": stores, "products": products, "bundles": bundles, "advertisements": advertisements,
                    "demoMode": bool(demo_counts), "demoCounts": demo_counts, "plans": plans, "cart": cart,
                    "orders": orders, "notifications": notifications,
                    "actor": actor, "settings": {"commissionRate": settings(con).get("commissionRate", 0), "paymentsEnabled": settings(con).get("paymentsEnabled", False)}}

    def _stores(self, con, query="") -> list:
        args = []
        where = "m.status='approved' AND b.status='approved' AND b.active=1 AND b.public_visible=1"
        if query:
            where += " AND (m.name_ar LIKE ? OR m.name_en LIKE ? OR b.name_ar LIKE ? OR b.name_en LIKE ?)"
            term = f"%{query}%"; args.extend([term]*4)
        rows = con.execute(f"""SELECT m.id merchant_id,m.name_ar,m.name_en,m.logo_path,m.cover_path,m.verified,
            b.id branch_id,b.name_ar branch_name_ar,b.name_en branch_name_en,b.area_id,b.address_text,b.latitude,b.longitude,
            f.pickup_enabled,f.office_enabled,f.office_fee_baisa,f.office_free_threshold_baisa,
            f.home_enabled,f.home_fee_baisa,f.home_free_threshold_baisa,f.eta_text,
            (SELECT COUNT(*) FROM products p JOIN product_branch_inventory i ON i.product_id=p.id
             WHERE p.merchant_id=m.id AND i.branch_id=b.id AND p.status='approved' AND p.active=1 AND i.active=1) product_count
            FROM merchants m JOIN store_branches b ON b.merchant_id=m.id
            LEFT JOIN fulfillment_profiles f ON f.branch_id=b.id WHERE {where} ORDER BY m.verified DESC,m.name_en""", args).fetchall()
        return [dict(r) for r in rows]

    def _products(self, con, query="", category="", branch_id="") -> list:
        where = ["p.status='approved'", "p.active=1", "m.status='approved'", "b.status='approved'", "b.active=1", "b.public_visible=1", "i.active=1"]
        args = []
        if query:
            where.append("(p.name_ar LIKE ? OR p.name_en LIKE ? OR m.name_ar LIKE ? OR m.name_en LIKE ?)")
            term=f"%{query}%"; args.extend([term]*4)
        if category:
            where.append("p.category_id=?"); args.append(category)
        if branch_id:
            where.append("b.id=?"); args.append(branch_id)
        rows = con.execute(f"""SELECT p.*,m.name_ar merchant_name_ar,m.name_en merchant_name_en,m.verified,
            b.id branch_id,b.name_ar branch_name_ar,b.name_en branch_name_en,b.area_id,b.address_text,
            i.quantity,i.availability,i.last_stock_verified_at,i.stale_at
            FROM products p JOIN merchants m ON m.id=p.merchant_id
            JOIN product_branch_inventory i ON i.product_id=p.id
            JOIN store_branches b ON b.id=i.branch_id WHERE {' AND '.join(where)}
            ORDER BY p.updated_at DESC LIMIT 200""", args).fetchall()
        output=[]
        for row in rows:
            item=dict(row); item["price"] = omr(item.pop("price_baisa")); item["images"] = loads(item.pop("images_json"), []); output.append(item)
        return output

    def _bundles(self, con) -> list:
        rows = con.execute("""SELECT b.*,m.name_ar merchant_name_ar,m.name_en merchant_name_en,m.verified,
            s.name_ar branch_name_ar,s.name_en branch_name_en,s.area_id,
            (SELECT COUNT(*) FROM bundle_items i WHERE i.bundle_id=b.id) component_count,
            (SELECT COALESCE(SUM(p.price_baisa*i.quantity),0) FROM bundle_items i JOIN products p ON p.id=i.product_id WHERE i.bundle_id=b.id) normal_value_baisa
            FROM bundles b JOIN merchants m ON m.id=b.merchant_id JOIN store_branches s ON s.id=b.branch_id
            WHERE b.status='approved' AND m.status='approved' AND s.status='approved' AND s.active=1 AND s.public_visible=1
            ORDER BY b.updated_at DESC LIMIT 100""").fetchall()
        output = []
        for row in rows:
            item = dict(row)
            item["price"] = omr(item.pop("selling_price_baisa"))
            item["normalValue"] = omr(item.pop("normal_value_baisa"))
            output.append(item)
        return output

    def _advertisements(self, con) -> list:
        rows = con.execute("""SELECT a.id,a.owner_id,a.placement,a.target_json,a.landing_kind,a.landing_id,
            a.label_ar,a.label_en,m.name_ar merchant_name_ar,m.name_en merchant_name_en,b.area_id
            FROM ad_campaigns a JOIN merchants m ON m.id=a.owner_id
            JOIN store_branches b ON b.id=a.landing_id
            WHERE a.status='approved' AND m.status='approved' AND b.status='approved' AND b.active=1 AND b.public_visible=1
              AND (a.starts_at='' OR a.starts_at<=?) AND (a.ends_at='' OR a.ends_at>?)
            ORDER BY a.created_at DESC LIMIT 50""", (now_iso(), now_iso())).fetchall()
        output=[]
        for row in rows:
            item=dict(row); item["creative"]=loads(item.pop("target_json"),{}); output.append(item)
        return output

    def search(self, query="", category="", branch_id="") -> dict:
        query = clean_text(query, 80)
        with connect() as con:
            products=self._products(con,query,clean_text(category,60),clean_text(branch_id,80))
            stores=self._stores(con,query)
            con.execute("INSERT INTO analytics_events VALUES(?,?,?,?,?,?,?)", (new_id("evt"), "search", "", "query", "", dumps({"query": query, "results": len(products)+len(stores)}), now_iso()))
            return {"products": products, "stores": stores}

    def merchant_apply(self, actor, payload: dict) -> dict:
        require_role(actor, "shopper")
        stamp=now_iso(); merchant_id=new_id("merchant"); app_id=new_id("mapp")
        name_ar=clean_text(payload.get("nameAr"),100,True); name_en=clean_text(payload.get("nameEn"),100,True)
        wilayah=clean_text(payload.get("wilayahId"),80,True); area=clean_text(payload.get("areaId"),80)
        with connect(immediate=True) as con:
            if not con.execute("SELECT 1 FROM locations WHERE id=? AND kind='wilayat' AND active=1",(wilayah,)).fetchone(): raise DomainError("valid_wilayah_required")
            if area and not con.execute("SELECT 1 FROM locations WHERE id=? AND kind='area' AND active=1",(area,)).fetchone(): raise DomainError("valid_area_required")
            con.execute("""INSERT INTO merchants(id,owner_account_id,name_ar,name_en,status,created_at,updated_at)
                VALUES(?,?,?,?, 'submitted',?,?)""",(merchant_id,actor["accountId"],name_ar,name_en,stamp,stamp))
            application={"ownerContact": clean_text(payload.get("ownerContact"),80), "crNumber": clean_text(payload.get("crNumber"),50), "returnPolicy": clean_text(payload.get("returnPolicy"),1000), "fulfillment": payload.get("fulfillment") or {}}
            con.execute("INSERT INTO merchant_applications VALUES(?,?,?,'submitted','',?,?,?)",(app_id,merchant_id,dumps(application),stamp,stamp,stamp))
            branch_id=new_id("branch")
            con.execute("""INSERT INTO store_branches(id,merchant_id,name_ar,name_en,wilayah_id,area_id,address_text,status,active,public_visible,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,'submitted',1,0,?,?)""",(branch_id,merchant_id,name_ar,name_en,wilayah,area,clean_text(payload.get("address"),240),stamp,stamp))
            con.execute("INSERT INTO account_roles VALUES(?,?,?,0)",(actor["accountId"],"merchant_owner",merchant_id))
            con.execute("INSERT INTO notifications VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",(new_id("ntf"),"admin","admin","طلب متجر جديد","New merchant application",f"{name_ar} — مراجعة الطلب",f"{name_en} — review application",f"admin:merchant-application:{app_id}",1,f"merchant-application:{app_id}","","",stamp))
            con.execute("INSERT INTO admin_audit_logs VALUES(?,?,?,?,?,?,?,?,?)",(new_id("audit"),actor["accountId"],"merchant_application_submitted","merchant_application",app_id,"{}",dumps({"merchantId":merchant_id,"branchId":branch_id}),"",stamp))
        return {"id":app_id,"merchantId":merchant_id,"branchId":branch_id,"status":"submitted","whatsappSent":False}

    def admin_decide_application(self, actor, payload: dict) -> dict:
        require_role(actor, "admin", "super_admin")
        application_id = clean_text(payload.get("applicationId"), 90, True)
        decision = clean_text(payload.get("decision"), 30, True)
        note = clean_text(payload.get("note"), 500)
        if decision not in {"approve", "reject", "changes_requested"}:
            raise DomainError("invalid_application_decision")
        with connect(immediate=True) as con:
            row = con.execute("""SELECT a.*,m.owner_account_id FROM merchant_applications a
                JOIN merchants m ON m.id=a.merchant_id WHERE a.id=?""", (application_id,)).fetchone()
            if not row:
                raise DomainError("application_not_found", 404)
            if row["status"] in {"approved", "rejected"}:
                return {"id": application_id, "status": row["status"], "duplicate": True}
            stamp = now_iso()
            status = "approved" if decision == "approve" else decision
            con.execute("UPDATE merchant_applications SET status=?,reviewer_note=?,updated_at=? WHERE id=?", (status, note, stamp, application_id))
            if decision == "approve":
                con.execute("UPDATE merchants SET status='approved',verified=0,updated_at=? WHERE id=?", (stamp, row["merchant_id"]))
                con.execute("""UPDATE store_branches SET status='approved',public_visible=1,updated_at=?
                    WHERE merchant_id=? AND active=1""", (stamp, row["merchant_id"]))
                con.execute("UPDATE account_roles SET active=1 WHERE account_id=? AND role='merchant_owner' AND merchant_id=?", (row["owner_account_id"], row["merchant_id"]))
                trial_ends = (datetime.now(UTC) + timedelta(days=90)).isoformat()
                con.execute("""INSERT OR IGNORE INTO merchant_subscriptions
                    (id,merchant_id,plan_id,starts_at,ends_at,status,granted_by,created_at)
                    VALUES(?,?,'early_trial',?,?,'active',?,?)""",
                    (new_id("sub"), row["merchant_id"], stamp, trial_ends, actor["accountId"], stamp))
                title_ar, title_en = "تم اعتماد متجرك", "Your store was approved"
                body_ar, body_en = "يمكنك الآن دخول مساحة التاجر وبدء إعداد الكتالوج", "You can now open the merchant workspace and prepare your catalog"
            elif decision == "changes_requested":
                con.execute("UPDATE merchants SET status='changes_requested',updated_at=? WHERE id=?", (stamp, row["merchant_id"]))
                title_ar, title_en = "طلب متجرك يحتاج تحديثاً", "Your application needs an update"
                body_ar, body_en = note or "راجع الملاحظات وأكمل البيانات", note or "Review the note and complete the details"
            else:
                con.execute("UPDATE merchants SET status='rejected',updated_at=? WHERE id=?", (stamp, row["merchant_id"]))
                con.execute("UPDATE store_branches SET status='rejected',public_visible=0,updated_at=? WHERE merchant_id=?", (stamp, row["merchant_id"]))
                title_ar, title_en = "تحديث طلب المتجر", "Merchant application update"
                body_ar, body_en = note or "تعذر اعتماد الطلب حالياً", note or "The application could not be approved"
            con.execute("INSERT INTO notifications VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (new_id("ntf"), "account", row["owner_account_id"], title_ar, title_en, body_ar, body_en,
                 "shopper:merchant-application", 0, f"merchant-application:{application_id}:{status}", "", "", stamp))
            con.execute("INSERT INTO admin_audit_logs VALUES(?,?,?,?,?,?,?,?,?)",
                (new_id("audit"), actor["accountId"], f"merchant_application_{status}", "merchant_application",
                 application_id, dumps({"status": row["status"]}), dumps({"status": status}), note, stamp))
            return {"id": application_id, "merchantId": row["merchant_id"], "status": status, "duplicate": False}

    def _plan(self,row):
        data=dict(row); data["price"]=omr(data.pop("price_baisa")); data["entitlements"]=loads(data["entitlements"],{}); return data

    def merchant_dashboard(self, actor) -> dict:
        require_role(actor,"merchant_owner","merchant_manager","merchant_staff")
        mid=actor["merchantId"]
        with connect() as con:
            merchant=con.execute("SELECT * FROM merchants WHERE id=?",(mid,)).fetchone()
            if not merchant: raise DomainError("merchant_not_found",404)
            branches=[dict(r) for r in con.execute("SELECT * FROM store_branches WHERE merchant_id=? ORDER BY created_at",(mid,))]
            products=[]
            for row in con.execute("""SELECT p.*,i.branch_id,i.quantity,i.availability,i.last_stock_verified_at FROM products p
                LEFT JOIN product_branch_inventory i ON i.product_id=p.id WHERE p.merchant_id=? AND p.active=1 ORDER BY p.updated_at DESC""",(mid,)):
                item=dict(row); item["price"]=omr(item.pop("price_baisa")); item["images"]=loads(item.pop("images_json"),[]); products.append(item)
            orders=[dict(r) for r in con.execute("SELECT * FROM orders WHERE merchant_id=? ORDER BY created_at DESC LIMIT 100",(mid,))]
            plan=active_plan(con,mid)
            metrics={r["event_type"]:r["n"] for r in con.execute("SELECT event_type,COUNT(*) n FROM analytics_events WHERE entity_id=? GROUP BY event_type",(mid,))}
            return {"merchant":dict(merchant),"branches":branches,"products":products,"orders":orders,"plan":self._plan(plan),"metrics":metrics}

    def upsert_product(self, actor, payload: dict) -> dict:
        require_role(actor,"merchant_owner","merchant_manager","merchant_staff")
        mid=actor["merchantId"]; pid=clean_text(payload.get("id"),90) or new_id("prod"); price=validate_product_price(payload.get("price")); branch=clean_text(payload.get("branchId"),90,True); category=clean_text(payload.get("categoryId"),80,True)
        stamp=now_iso(); quantity=max(0,min(1_000_000,int(payload.get("quantity",0) or 0)))
        with connect(immediate=True) as con:
            plan=active_plan(con,mid); limit=int(plan["entitlements"].get("products",0))
            existing=con.execute("SELECT * FROM products WHERE id=? AND merchant_id=?",(pid,mid)).fetchone()
            if not existing and con.execute("SELECT COUNT(*) n FROM products WHERE merchant_id=? AND active=1",(mid,)).fetchone()["n"]>=limit: raise DomainError("plan_product_limit",409,{"limit":limit})
            if not con.execute("SELECT 1 FROM store_branches WHERE id=? AND merchant_id=? AND active=1",(branch,mid)).fetchone(): raise DomainError("branch_not_owned",403)
            if not con.execute("SELECT 1 FROM product_categories WHERE id=? AND active=1",(category,)).fetchone(): raise DomainError("valid_category_required")
            values=(pid,mid,category,clean_text(payload.get("nameAr"),120,True),clean_text(payload.get("nameEn"),120,True),clean_text(payload.get("descriptionAr"),500),clean_text(payload.get("descriptionEn"),500),price,clean_text(payload.get("unit"),40),clean_text(payload.get("barcode"),60),dumps(payload.get("images") or []),"approved",1,stamp,stamp)
            con.execute("""INSERT INTO products VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                category_id=excluded.category_id,name_ar=excluded.name_ar,name_en=excluded.name_en,description_ar=excluded.description_ar,
                description_en=excluded.description_en,price_baisa=excluded.price_baisa,unit_text=excluded.unit_text,barcode=excluded.barcode,
                images_json=excluded.images_json,updated_at=excluded.updated_at""",values)
            availability="in_stock" if quantity>2 else "low_stock" if quantity>0 else "out_of_stock"
            con.execute("""INSERT INTO product_branch_inventory VALUES(?,?,'tracked',?,?,?,'',1,?)
                ON CONFLICT(product_id,branch_id) DO UPDATE SET quantity=excluded.quantity,availability=excluded.availability,updated_at=excluded.updated_at""",(pid,branch,quantity,availability,stamp,stamp))
        return {"id":pid,"price":omr(price),"quantity":quantity,"availability":availability}

    def quick_stock(self, actor, branch_id: str) -> dict:
        require_role(actor,"merchant_owner","merchant_manager","merchant_staff")
        mid=actor["merchantId"]
        with connect() as con:
            if not con.execute("SELECT 1 FROM store_branches WHERE id=? AND merchant_id=?",(branch_id,mid)).fetchone(): raise DomainError("branch_not_owned",403)
            rows=[dict(r) for r in con.execute("""SELECT p.id,p.name_ar,p.name_en,i.quantity,i.availability,i.last_stock_verified_at,
                CASE WHEN i.quantity<=2 THEN 1 ELSE 0 END priority FROM products p JOIN product_branch_inventory i ON i.product_id=p.id
                WHERE p.merchant_id=? AND i.branch_id=? AND p.active=1 AND i.active=1 ORDER BY priority DESC,i.last_stock_verified_at ASC LIMIT 200""",(mid,branch_id))]
            return {"branchId":branch_id,"items":rows,"remainingCount":max(0,con.execute("SELECT COUNT(*) n FROM product_branch_inventory WHERE branch_id=? AND active=1",(branch_id,)).fetchone()["n"]-len(rows))}

    def confirm_stock(self, actor, branch_id: str, changes: list) -> dict:
        require_role(actor,"merchant_owner","merchant_manager","merchant_staff")
        stamp=now_iso(); mid=actor["merchantId"]
        with connect(immediate=True) as con:
            if not con.execute("SELECT 1 FROM store_branches WHERE id=? AND merchant_id=?",(branch_id,mid)).fetchone(): raise DomainError("branch_not_owned",403)
            for change in changes[:500]:
                pid=clean_text(change.get("productId"),90,True); quantity=max(0,min(1_000_000,int(change.get("quantity",0) or 0)))
                owned=con.execute("SELECT 1 FROM products p JOIN product_branch_inventory i ON i.product_id=p.id WHERE p.id=? AND p.merchant_id=? AND i.branch_id=?",(pid,mid,branch_id)).fetchone()
                if not owned: raise DomainError("product_not_owned",403)
                availability="in_stock" if quantity>2 else "low_stock" if quantity else "out_of_stock"
                con.execute("UPDATE product_branch_inventory SET quantity=?,availability=?,last_stock_verified_at=?,stale_at='',updated_at=? WHERE product_id=? AND branch_id=?",(quantity,availability,stamp,stamp,pid,branch_id))
            con.execute("UPDATE product_branch_inventory SET last_stock_verified_at=?,stale_at='',updated_at=? WHERE branch_id=? AND active=1",(stamp,stamp,branch_id))
            audit_id=new_id("iaudit")
            con.execute("""INSERT INTO inventory_audits(
                id,merchant_id,branch_id,status,due_at,confirmed_at,confirmed_by,summary,created_at)
                VALUES(?,?,?,'confirmed',?,?,?,?,?)""",
                (audit_id,mid,branch_id,stamp,stamp,actor["accountId"],dumps({"changed":len(changes)}),stamp))
        return {"ok":True,"auditId":audit_id,"confirmedAt":stamp}

    def create_bundle(self, actor, payload: dict) -> dict:
        require_role(actor,"merchant_owner","merchant_manager")
        mid=actor["merchantId"]; branch=clean_text(payload.get("branchId"),90,True); components=payload.get("components") or []
        with connect(immediate=True) as con:
            limit=int(settings(con).get("bundleMaxComponents",10)); plan=active_plan(con,mid)
            if len(components)<2 or len(components)>limit: raise DomainError("bundle_component_count",422,{"maximum":limit})
            if con.execute("SELECT COUNT(*) n FROM bundles WHERE merchant_id=? AND status!='archived'",(mid,)).fetchone()["n"]>=int(plan["entitlements"].get("bundles",0)): raise DomainError("plan_bundle_limit",409)
            normal=0; normalized=[]
            for component in components:
                pid=clean_text(component.get("productId"),90,True); quantity=max(1,min(100,int(component.get("quantity",1))))
                row=con.execute("SELECT price_baisa FROM products WHERE id=? AND merchant_id=? AND active=1 AND status='approved'",(pid,mid)).fetchone()
                inv=con.execute("SELECT 1 FROM product_branch_inventory WHERE product_id=? AND branch_id=? AND active=1",(pid,branch)).fetchone()
                if not row or not inv: raise DomainError("bundle_product_invalid",422)
                if not PRODUCT_MIN_BAISA<=row["price_baisa"]<=PRODUCT_MAX_BAISA: raise DomainError("bundle_component_price_invalid",422)
                normal+=row["price_baisa"]*quantity; normalized.append((pid,quantity))
            selling=to_baisa(payload.get("price"))
            if selling<=0: raise DomainError("valid_bundle_price_required")
            bid=new_id("bundle"); stamp=now_iso()
            con.execute("INSERT INTO bundles VALUES(?,?,?,?,?,?,?,'approved','','',?,?)",(bid,mid,branch,clean_text(payload.get("titleAr"),120,True),clean_text(payload.get("titleEn"),120,True),clean_text(payload.get("description"),500),selling,stamp,stamp))
            con.executemany("INSERT INTO bundle_items VALUES(?,?,?)",[(bid,p,q) for p,q in normalized])
        return {"id":bid,"normalValue":omr(normal),"price":omr(selling),"saving":omr(max(0,normal-selling))}

    def _cart(self, con, account_id):
        row=con.execute("SELECT * FROM carts WHERE account_id=? AND status='active'",(account_id,)).fetchone()
        if not row: return None
        data=dict(row); items=[]; total=0
        for item in con.execute("SELECT * FROM cart_items WHERE cart_id=? ORDER BY rowid",(row["id"],)):
            x=dict(item); x["unitPrice"]=omr(x.pop("unit_price_baisa")); x["lineTotal"]=omr(item["unit_price_baisa"]*item["quantity"]); total+=item["unit_price_baisa"]*item["quantity"]; items.append(x)
        data["items"]=items; data["subtotal"]=omr(total); return data

    def add_cart(self, actor, payload: dict) -> dict:
        require_role(actor,"shopper")
        kind=clean_text(payload.get("kind"),20) or "product"; item_id=clean_text(payload.get("itemId"),90,True); branch=clean_text(payload.get("branchId"),90,True); quantity=max(1,min(100,int(payload.get("quantity",1)))); replace=bool(payload.get("replaceCart"))
        if kind not in {"product","bundle"}: raise DomainError("invalid_cart_item_kind")
        with connect(immediate=True) as con:
            if kind=="product": row=con.execute("SELECT p.merchant_id,p.price_baisa FROM products p JOIN product_branch_inventory i ON i.product_id=p.id WHERE p.id=? AND i.branch_id=? AND p.active=1 AND p.status='approved' AND i.active=1",(item_id,branch)).fetchone()
            else: row=con.execute("SELECT merchant_id,selling_price_baisa price_baisa FROM bundles WHERE id=? AND branch_id=? AND status='approved'",(item_id,branch)).fetchone()
            if not row: raise DomainError("item_not_available",404)
            merchant=row["merchant_id"]; cart=con.execute("SELECT * FROM carts WHERE account_id=? AND status='active'",(actor["accountId"],)).fetchone()
            if cart and cart["merchant_id"]!=merchant:
                if not replace: raise DomainError("cross_store_cart_confirmation_required",409,{"currentMerchantId":cart["merchant_id"],"newMerchantId":merchant})
                con.execute("UPDATE carts SET status='replaced',updated_at=? WHERE id=?",(now_iso(),cart["id"])); cart=None
            if not cart:
                cid=new_id("cart"); con.execute("INSERT INTO carts VALUES(?,?,?,?, 'active',1,?)",(cid,actor["accountId"],merchant,branch,now_iso()))
            else: cid=cart["id"]
            con.execute("""INSERT INTO cart_items VALUES(?,?,?,?,?) ON CONFLICT(cart_id,item_kind,item_id)
                DO UPDATE SET quantity=MIN(100,cart_items.quantity+excluded.quantity),unit_price_baisa=excluded.unit_price_baisa""",(cid,kind,item_id,quantity,row["price_baisa"]))
            con.execute("UPDATE carts SET version=version+1,updated_at=? WHERE id=?",(now_iso(),cid))
            return self._cart(con,actor["accountId"])

    def checkout(self, actor, payload: dict) -> dict:
        require_role(actor,"shopper")
        idem=clean_text(payload.get("idempotencyKey"),120,True); mode=clean_text(payload.get("fulfillmentMode"),30) or "pickup"
        if mode not in {"pickup","office_delivery","home_delivery"}: raise DomainError("invalid_fulfillment_mode")
        with connect(immediate=True) as con:
            replay=con.execute("SELECT * FROM orders WHERE account_id=? AND idempotency_key=?",(actor["accountId"],idem)).fetchone()
            if replay: return {"order":dict(replay),"duplicate":True}
            cart=self._cart(con,actor["accountId"])
            if not cart or not cart["items"]: raise DomainError("cart_empty",409)
            profile=con.execute("SELECT * FROM fulfillment_profiles WHERE branch_id=?",(cart["branch_id"],)).fetchone()
            if not profile or not profile[f"{mode.split('_')[0] if mode!='pickup' else 'pickup'}_enabled"]: raise DomainError("fulfillment_not_available",409)
            subtotal=sum(to_baisa(i["lineTotal"]) for i in cart["items"]); fee=0
            if mode=="office_delivery":
                if subtotal<profile["office_minimum_baisa"]: raise DomainError("minimum_order_not_met",409,{"minimum":omr(profile["office_minimum_baisa"])})
                fee=0 if profile["office_free_threshold_baisa"] and subtotal>=profile["office_free_threshold_baisa"] else profile["office_fee_baisa"]
            if mode=="home_delivery":
                if subtotal<profile["home_minimum_baisa"]: raise DomainError("minimum_order_not_met",409,{"minimum":omr(profile["home_minimum_baisa"])})
                fee=0 if profile["home_free_threshold_baisa"] and subtotal>=profile["home_free_threshold_baisa"] else profile["home_fee_baisa"]
            component_totals = {}
            for item in cart["items"]:
                if item["item_kind"] == "product":
                    component_totals[item["item_id"]] = component_totals.get(item["item_id"], 0) + item["quantity"]
                else:
                    bundle_rows = con.execute("SELECT product_id,quantity FROM bundle_items WHERE bundle_id=?", (item["item_id"],)).fetchall()
                    if not bundle_rows:
                        raise DomainError("bundle_not_available", 409)
                    for bundle_item in bundle_rows:
                        quantity = bundle_item["quantity"] * item["quantity"]
                        component_totals[bundle_item["product_id"]] = component_totals.get(bundle_item["product_id"], 0) + quantity
            for product_id, required_quantity in component_totals.items():
                inventory = con.execute("""SELECT quantity FROM product_branch_inventory
                    WHERE product_id=? AND branch_id=? AND active=1""", (product_id, cart["branch_id"])).fetchone()
                reserved = con.execute("""SELECT COALESCE(SUM(quantity),0) n FROM inventory_reservations
                    WHERE product_id=? AND branch_id=? AND status='pending'""", (product_id, cart["branch_id"])).fetchone()["n"]
                if not inventory or inventory["quantity"] - reserved < required_quantity:
                    raise DomainError("stock_unavailable", 409, {"productId": product_id})
            policy=con.execute("SELECT * FROM merchant_return_policies WHERE merchant_id=? AND active=1 ORDER BY version DESC LIMIT 1",(cart["merchant_id"],)).fetchone()
            oid=new_id("order"); stamp=now_iso(); due=(datetime.now(UTC)+timedelta(hours=int(settings(con).get("merchantResponseHours",4)))).isoformat()
            con.execute("""INSERT INTO orders(
                id,account_id,merchant_id,branch_id,status,fulfillment_mode,address_snapshot,policy_snapshot,
                subtotal_baisa,delivery_fee_baisa,total_baisa,idempotency_key,response_due_at,created_at,updated_at)
                VALUES(?,?,?,?,'pending_store_confirmation',?,?,?,?,?,?,?,?,?,?)""",
                (oid,actor["accountId"],cart["merchant_id"],cart["branch_id"],mode,
                 dumps(payload.get("address") or {}),dumps(dict(policy) if policy else {}),
                 subtotal,fee,subtotal+fee,idem,due,stamp,stamp))
            for item in cart["items"]:
                if item["item_kind"]=="product":
                    product=con.execute("SELECT name_ar,name_en FROM products WHERE id=?",(item["item_id"],)).fetchone(); components=[{"productId":item["item_id"],"quantity":item["quantity"]}]
                else:
                    product=con.execute("SELECT title_ar name_ar,title_en name_en FROM bundles WHERE id=?",(item["item_id"],)).fetchone(); components=[{"productId":r["product_id"],"quantity":r["quantity"]*item["quantity"]} for r in con.execute("SELECT * FROM bundle_items WHERE bundle_id=?",(item["item_id"],))]
                con.execute("INSERT INTO order_items VALUES(?,?,?,?,?,?,?)",(oid,item["item_kind"],item["item_id"],dumps(dict(product)),item["quantity"],to_baisa(item["unitPrice"]),dumps(components)))
            for product_id, reserved_quantity in component_totals.items():
                con.execute("INSERT INTO inventory_reservations VALUES(?,?,?,?,?,'pending',?)",
                    (new_id("res"),oid,product_id,cart["branch_id"],reserved_quantity,stamp))
            con.execute("UPDATE carts SET status='checked_out',updated_at=? WHERE id=?",(stamp,cart["id"]))
            con.execute("INSERT INTO notifications VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",(new_id("ntf"),"merchant",cart["merchant_id"],"طلب جديد","New order","أكد توفر المنتجات قبل انتهاء المهلة","Confirm stock before the deadline",f"merchant:order:{oid}",1,f"order:{oid}:confirm","","",stamp))
            return {"order":{"id":oid,"status":"pending_store_confirmation","subtotal":omr(subtotal),"deliveryFee":omr(fee),"total":omr(subtotal+fee),"responseDueAt":due},"duplicate":False}

    def decide_order(self, actor, order_id: str, decision: str) -> dict:
        require_role(actor,"merchant_owner","merchant_manager","merchant_staff")
        if decision not in {"accept","reject"}: raise DomainError("invalid_order_decision")
        with connect(immediate=True) as con:
            row=con.execute("SELECT * FROM orders WHERE id=? AND merchant_id=?",(order_id,actor["merchantId"])).fetchone()
            if not row: raise DomainError("order_not_found",404)
            if row["status"] in {"accepted","rejected"}: return {"id":order_id,"status":row["status"],"duplicate":True}
            if row["status"]!="pending_store_confirmation": raise DomainError("order_stage_conflict",409)
            stamp=now_iso()
            if decision=="accept":
                reservations=list(con.execute("SELECT * FROM inventory_reservations WHERE order_id=? AND status='pending'",(order_id,)))
                for res in reservations:
                    inv=con.execute("SELECT quantity FROM product_branch_inventory WHERE product_id=? AND branch_id=? AND active=1",(res["product_id"],res["branch_id"])).fetchone()
                    if not inv or inv["quantity"]<res["quantity"]: raise DomainError("stock_unavailable",409,{"productId":res["product_id"]})
                for res in reservations:
                    con.execute("UPDATE product_branch_inventory SET quantity=quantity-?,availability=CASE WHEN quantity-?<=0 THEN 'out_of_stock' WHEN quantity-?<=2 THEN 'low_stock' ELSE 'in_stock' END,updated_at=? WHERE product_id=? AND branch_id=?",(res["quantity"],res["quantity"],res["quantity"],stamp,res["product_id"],res["branch_id"]))
                con.execute("UPDATE inventory_reservations SET status='consumed' WHERE order_id=? AND status='pending'",(order_id,)); status="accepted"
            else:
                con.execute("UPDATE inventory_reservations SET status='released' WHERE order_id=? AND status='pending'",(order_id,)); status="rejected"
            con.execute("UPDATE orders SET status=?,updated_at=? WHERE id=? AND status='pending_store_confirmation'",(status,stamp,order_id))
            con.execute("UPDATE notifications SET acted_at=? WHERE target_kind='merchant' AND target_id=? AND dedupe_key=? AND acted_at=''", (stamp, actor["merchantId"], f"order:{order_id}:confirm"))
            if status == "accepted":
                title_ar, title_en, body_ar, body_en = "أكد المتجر طلبك", "Store confirmed your order", "يمكنك متابعة طريقة الاستلام والموعد من طلباتك", "Track fulfillment and timing in your orders"
            else:
                title_ar, title_en, body_ar, body_en = "تعذر تأكيد الطلب", "The order could not be confirmed", "لم تتوفر كل المنتجات ولم يتم تحصيل أي مبلغ", "Some items were unavailable and no payment was taken"
            con.execute("INSERT INTO notifications VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (new_id("ntf"), "account", row["account_id"], title_ar, title_en, body_ar, body_en, f"shopper:order:{order_id}", 0, f"order:{order_id}:{status}", "", "", stamp))
            return {"id":order_id,"status":status,"duplicate":False}

    def supplier_campaigns(self, actor) -> list:
        require_role(actor,"merchant_owner","merchant_manager","merchant_staff")
        with connect() as con:
            return [dict(r) for r in con.execute("""SELECT c.*,s.name_ar supplier_name_ar,s.name_en supplier_name_en
                FROM supplier_campaigns c JOIN suppliers s ON s.id=c.supplier_id WHERE c.status='approved' ORDER BY c.created_at DESC""")]

    def admin_overview(self, actor) -> dict:
        require_role(actor,"admin","super_admin")
        with connect() as con:
            return {"pendingApplications":[dict(r) for r in con.execute("SELECT * FROM merchant_applications WHERE status IN('submitted','under_review','changes_requested') ORDER BY submitted_at")],
                    "counts":{table:con.execute(f"SELECT COUNT(*) n FROM {table}").fetchone()["n"] for table in ("merchants","store_branches","products","orders","ad_campaigns","suppliers")},
                    "demoCounts":{r["entity_kind"]:r["n"] for r in con.execute("SELECT entity_kind,COUNT(*) n FROM demo_records GROUP BY entity_kind")},
                    "settings":settings(con),"plans":[self._plan(r) for r in con.execute("SELECT * FROM subscription_plans ORDER BY sort_order")]}

    def purge_demo_data(self, actor, confirmation: str) -> dict:
        require_role(actor,"admin","super_admin")
        if confirmation != "DELETE BISA DEMO":
            raise DomainError("demo_delete_confirmation_required", 422)
        with connect(immediate=True) as con:
            records=[dict(r) for r in con.execute("SELECT * FROM demo_records")]
            if not records:
                return {"ok":True,"deleted":0,"counts":{},"duplicate":True}
            grouped={}
            for row in records: grouped.setdefault(row["entity_kind"],[]).append(row["entity_id"])

            def ids(kind): return grouped.get(kind,[])
            def delete_where(table,column,values):
                if not values: return 0
                marks=','.join('?' for _ in values)
                return con.execute(f"DELETE FROM {table} WHERE {column} IN ({marks})",values).rowcount

            merchant_ids=ids("merchant"); account_ids=ids("account"); branch_ids=ids("branch")
            order_ids=[]; cart_ids=[]; application_ids=[]
            if merchant_ids or account_ids:
                merchant_marks=','.join('?' for _ in merchant_ids) or "NULL"
                account_marks=','.join('?' for _ in account_ids) or "NULL"
                params=merchant_ids+account_ids
                order_ids=[r["id"] for r in con.execute(f"SELECT id FROM orders WHERE merchant_id IN ({merchant_marks}) OR account_id IN ({account_marks})",params)]
                cart_ids=[r["id"] for r in con.execute(f"SELECT id FROM carts WHERE merchant_id IN ({merchant_marks}) OR account_id IN ({account_marks})",params)]
            if merchant_ids:
                marks=','.join('?' for _ in merchant_ids)
                application_ids=[r["id"] for r in con.execute(f"SELECT id FROM merchant_applications WHERE merchant_id IN ({marks})",merchant_ids)]
            counts={}
            for table,column,values in (
                ("ad_events","campaign_id",ids("ad")), ("ad_campaigns","id",ids("ad")),
                ("order_items","order_id",order_ids), ("inventory_reservations","order_id",order_ids), ("orders","id",order_ids),
                ("cart_items","cart_id",cart_ids), ("carts","id",cart_ids),
                ("favorites","entity_id",ids("product")+ids("bundle")+merchant_ids),
                ("inventory_audits","branch_id",branch_ids), ("bundle_items","bundle_id",ids("bundle")), ("bundles","id",ids("bundle")),
                ("product_branch_inventory","product_id",ids("product")), ("products","id",ids("product")),
                ("merchant_subscriptions","id",ids("subscription")), ("merchant_return_policies","id",ids("policy")),
                ("fulfillment_profiles","branch_id",branch_ids), ("merchant_documents","application_id",application_ids),
                ("merchant_applications","id",application_ids), ("store_branches","id",branch_ids),
                ("account_roles","merchant_id",merchant_ids), ("merchants","id",merchant_ids),
                ("sessions","account_id",account_ids), ("account_roles","account_id",account_ids), ("accounts","id",account_ids),
            ):
                removed=delete_where(table,column,values)
                if removed: counts[table]=counts.get(table,0)+removed
            for area_id in ids("area"):
                used=con.execute("SELECT EXISTS(SELECT 1 FROM store_branches WHERE area_id=? UNION ALL SELECT 1 FROM merchant_applications WHERE payload LIKE ?) n",(area_id,f"%{area_id}%")).fetchone()["n"]
                if not used:
                    counts["locations"]=counts.get("locations",0)+con.execute("DELETE FROM locations WHERE id=?",(area_id,)).rowcount
            con.execute("DELETE FROM notifications WHERE target_id LIKE 'demo_%' OR route LIKE '%demo_%' OR dedupe_key LIKE '%demo_%'")
            con.execute("DELETE FROM analytics_events WHERE entity_id LIKE 'demo_%'")
            con.execute("DELETE FROM demo_records")
            total=sum(counts.values())
            con.execute("INSERT INTO admin_audit_logs VALUES(?,?,?,?,?,?,?,?,?)",
                (new_id("audit"),actor["accountId"],"demo_data_purged","demo_dataset","all",dumps(grouped),dumps({"counts":counts}),"Owner requested demo cleanup",now_iso()))
            return {"ok":True,"deleted":total,"counts":counts,"duplicate":False}
