import hashlib
import os
import shutil
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import bisa_config
import bisa_domain
from bisa_domain import init_db
from bisa_integrations import AdapterRegistry, execute_external_action
from bisa_jobs import expire_pending_orders, mark_stale_inventory
from bisa_security import (
    SecurityError,
    authenticate_access,
    clear_refresh_cookie_header,
    clear_login_failures,
    ensure_login_allowed,
    has_permission,
    issue_session,
    logout_session,
    private_media_metadata,
    record_login_failure,
    refresh_token_from_cookie,
    security_production_readiness,
    register_private_media,
    resolve_private_media_path,
    rotate_refresh_token,
    security_connection,
    session_http_exchange,
    signed_private_media_route,
)


class BisaSecurityTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="bisa-security-tests-"))
        self.original = {
            "config_data": bisa_config.DATA_DIR,
            "config_db": bisa_config.DB_PATH,
            "config_upload": bisa_config.UPLOAD_DIR,
            "config_backup": bisa_config.BACKUP_DIR,
            "domain_db": bisa_domain.DB_PATH,
            "domain_seed": bisa_domain.SEED_SAMPLE_DATA,
        }
        bisa_config.DATA_DIR = self.root
        bisa_config.DB_PATH = self.root / "security.sqlite3"
        bisa_config.UPLOAD_DIR = self.root / "uploads"
        bisa_config.BACKUP_DIR = self.root / "backups"
        bisa_domain.DB_PATH = bisa_config.DB_PATH
        bisa_domain.SEED_SAMPLE_DATA = False
        self.environment = patch.dict(os.environ, {
            "BISA_AUTH_PEPPER": "a" * 40,
            "BISA_MEDIA_SIGNING_KEY": "m" * 40,
            "BISA_LOGIN_MAX_ATTEMPTS": "3",
            "BISA_LOGIN_SOURCE_MAX_ATTEMPTS": "10",
            "BISA_LOGIN_WINDOW_SECONDS": "600",
            "BISA_LOGIN_LOCK_SECONDS": "600",
        }, clear=False)
        self.environment.start()
        init_db()

    def tearDown(self):
        self.environment.stop()
        bisa_config.DATA_DIR = self.original["config_data"]
        bisa_config.DB_PATH = self.original["config_db"]
        bisa_config.UPLOAD_DIR = self.original["config_upload"]
        bisa_config.BACKUP_DIR = self.original["config_backup"]
        bisa_domain.DB_PATH = self.original["domain_db"]
        bisa_domain.SEED_SAMPLE_DATA = self.original["domain_seed"]
        shutil.rmtree(self.root, ignore_errors=True)

    def create_account(self, account_id, phone, role="shopper", merchant_id=""):
        stamp = "2026-01-01T00:00:00+00:00"
        with security_connection(immediate=True) as con:
            con.execute(
                """INSERT INTO accounts(id,phone,name,pin_hash,status,created_at)
                VALUES(?,?,?,?,?,?)""",
                (account_id, phone, account_id, "test-only-hash", "active", stamp),
            )
            con.execute(
                "INSERT INTO account_roles(account_id,role,merchant_id,active) VALUES(?,?,?,1)",
                (account_id, role, merchant_id),
            )
        return {"accountId": account_id, "name": account_id, "role": role, "merchantId": merchant_id}

    def create_merchant(self, key, *, active=1):
        account_id = f"acct_{key}"
        merchant_id = f"merchant_{key}"
        actor = self.create_account(account_id, f"9689{len(key):07d}", "merchant_owner", merchant_id)
        stamp = "2026-01-01T00:00:00+00:00"
        with security_connection(immediate=True) as con:
            con.execute(
                """INSERT INTO merchants(
                id,owner_account_id,name_ar,name_en,status,verified,created_at,updated_at,active)
                VALUES(?,?,?,?, 'approved',1,?,?,?)""",
                (merchant_id, account_id, f"متجر {key}", f"Store {key}", stamp, stamp, active),
            )
        return actor

    def test_login_lockout_is_persisted_and_uses_no_raw_subject(self):
        current = datetime(2026, 1, 2, tzinfo=UTC)
        for _ in range(3):
            record_login_failure("96891112222", source_id="203.0.113.7", now=current)
        with self.assertRaises(SecurityError) as caught:
            ensure_login_allowed("96891112222", source_id="203.0.113.7", now=current)
        self.assertEqual(caught.exception.code, "login_temporarily_locked")
        with security_connection() as con:
            values = [row[0] for row in con.execute("SELECT scope_hash FROM auth_login_buckets")]
        self.assertTrue(values)
        self.assertTrue(all("96891112222" not in value for value in values))
        clear_login_failures("96891112222")
        ensure_login_allowed("96891112222", now=current)

        # A shared carrier/NAT source gets a separate, deliberately higher
        # threshold so a few unrelated bad PINs cannot lock every shopper.
        for suffix in range(3):
            for _ in range(2):
                record_login_failure(f"9689000000{suffix}", source_id="198.51.100.5", now=current)
        ensure_login_allowed("96898888888", source_id="198.51.100.5", now=current)

    def test_sessions_recheck_role_and_merchant_and_logout_is_scoped(self):
        merchant = self.create_merchant("alpha")
        first = issue_session(merchant["accountId"], "merchant_owner", merchant["merchantId"], "phone-a")
        second = issue_session(merchant["accountId"], "merchant_owner", merchant["merchantId"], "phone-b")
        self.assertEqual(authenticate_access(first["token"])["merchantId"], merchant["merchantId"])
        self.assertTrue(logout_session(first["token"]))
        self.assertIsNone(authenticate_access(first["token"]))
        self.assertIsNotNone(authenticate_access(second["token"]))
        with security_connection(immediate=True) as con:
            con.execute("UPDATE merchants SET active=0 WHERE id=?", (merchant["merchantId"],))
        self.assertIsNone(authenticate_access(second["token"]))

    def test_staff_session_rechecks_merchant_membership(self):
        owner = self.create_merchant("membership")
        staff = self.create_account(
            "acct_membership_staff", "96897770000", "merchant_staff", owner["merchantId"]
        )
        stamp = "2026-01-01T00:00:00+00:00"
        with security_connection(immediate=True) as con:
            con.execute(
                """INSERT INTO merchant_members(merchant_id,account_id,role,status,created_at)
                VALUES(?,?,'merchant_staff','active',?)""",
                (owner["merchantId"], staff["accountId"], stamp),
            )
        session = issue_session(
            staff["accountId"], "merchant_staff", owner["merchantId"], "staff-phone"
        )
        self.assertIsNotNone(authenticate_access(session["token"]))
        with security_connection(immediate=True) as con:
            con.execute(
                "UPDATE merchant_members SET status='disabled' WHERE merchant_id=? AND account_id=?",
                (owner["merchantId"], staff["accountId"]),
            )
        self.assertIsNone(authenticate_access(session["token"]))

    def test_refresh_rotation_rejects_reuse_and_device_mismatch(self):
        actor = self.create_account("acct_shopper", "96891110000")
        session = issue_session(actor["accountId"], "shopper", "", "device-a")
        with self.assertRaises(SecurityError) as mismatch:
            rotate_refresh_token(session["refreshToken"], device_id="device-b")
        self.assertEqual(mismatch.exception.code, "refresh_device_mismatch")
        replacement = rotate_refresh_token(session["refreshToken"], device_id="device-a")
        self.assertIsNone(authenticate_access(session["token"]))
        self.assertIsNotNone(authenticate_access(replacement["token"]))
        with self.assertRaises(SecurityError) as reused:
            rotate_refresh_token(session["refreshToken"], device_id="device-a")
        self.assertEqual(reused.exception.code, "refresh_token_reused")
        self.assertIsNone(authenticate_access(replacement["token"]))

    def test_refresh_cookie_is_role_scoped_httponly_and_not_in_json(self):
        actor = self.create_account("acct_cookie", "96891114444")
        session = issue_session(actor["accountId"], "shopper", "", "cookie-device")
        payload, set_cookie = session_http_exchange(session, secure=True)
        self.assertNotIn("refreshToken", payload)
        self.assertIn("bisa_shopper_refresh=", set_cookie)
        self.assertIn("HttpOnly", set_cookie)
        self.assertIn("SameSite=Strict", set_cookie)
        self.assertIn("Secure", set_cookie)
        self.assertEqual(
            refresh_token_from_cookie(set_cookie, "shopper"), session["refreshToken"]
        )
        cleared = clear_refresh_cookie_header("shopper", secure=True)
        self.assertIn("Max-Age=0", cleared)
        self.assertNotIn("bisa_merchant_refresh", cleared)

    def test_production_readiness_requires_distinct_security_keys(self):
        missing = security_production_readiness({"BISA_ENV": "production"})
        self.assertFalse(missing["ready"])
        self.assertEqual(len(missing["errors"]), 2)
        shared = "s" * 40
        reused = security_production_readiness({
            "BISA_ENV": "production", "BISA_AUTH_PEPPER": shared,
            "BISA_MEDIA_SIGNING_KEY": shared,
        })
        self.assertFalse(reused["ready"])
        ready = security_production_readiness({
            "BISA_ENV": "production", "BISA_AUTH_PEPPER": "a" * 40,
            "BISA_MEDIA_SIGNING_KEY": "m" * 40,
        })
        self.assertTrue(ready["ready"])
        self.assertNotIn("a" * 40, str(ready))

    def test_cross_merchant_scope_and_permission_override_are_enforced(self):
        alpha = self.create_merchant("alpha")
        beta = self.create_merchant("beta")
        with self.assertRaises(SecurityError) as caught:
            issue_session(alpha["accountId"], "merchant_owner", beta["merchantId"], "device")
        self.assertEqual(caught.exception.code, "session_role_unavailable")
        self.assertTrue(has_permission(alpha, "team.manage"))
        with security_connection(immediate=True) as con:
            con.execute(
                "INSERT INTO account_permission_overrides(account_id,permission,allowed,updated_at) VALUES(?,?,0,?)",
                (alpha["accountId"], "team.manage", "2026-01-01T00:00:00+00:00"),
            )
        self.assertFalse(has_permission(alpha, "team.manage"))

    def test_supplier_advertiser_requires_approved_supplier_membership(self):
        actor = self.create_account(
            "acct_supplier", "96895550000", "supplier_advertiser", "supplier_one"
        )
        stamp = "2026-01-01T00:00:00+00:00"
        with security_connection(immediate=True) as con:
            con.execute(
                """INSERT INTO suppliers(id,name_ar,name_en,status,created_at,updated_at)
                VALUES('supplier_one','مورد','Supplier','draft',?,?)""",
                (stamp, stamp),
            )
            con.execute(
                """INSERT INTO supplier_members(supplier_id,account_id,role,status,created_at)
                VALUES('supplier_one',?,'supplier_advertiser','active',?)""",
                (actor["accountId"], stamp),
            )
        with self.assertRaises(SecurityError):
            issue_session(actor["accountId"], "supplier_advertiser", "supplier_one", "device")
        with security_connection(immediate=True) as con:
            con.execute("UPDATE suppliers SET status='approved' WHERE id='supplier_one'")
        session = issue_session(actor["accountId"], "supplier_advertiser", "supplier_one", "device")
        authenticated = authenticate_access(session["token"])
        self.assertEqual(authenticated["supplierId"], "supplier_one")
        self.assertEqual(authenticated["merchantId"], "")

        blob = b"%PDF-supplier-terms"
        path = bisa_config.UPLOAD_DIR / "private" / "supplier-docs" / "terms.pdf"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(blob)
        media = register_private_media(
            authenticated,
            owner_kind="supplier",
            owner_id="supplier_one",
            purpose="supplier_terms",
            storage_key="private/supplier-docs/terms.pdf",
            mime_type="application/pdf",
            byte_size=len(blob),
            sha256_hex=hashlib.sha256(blob).hexdigest(),
        )
        self.assertEqual(private_media_metadata(authenticated, media["id"])["ownerKind"], "supplier")

    def test_private_media_never_exposes_path_and_blocks_idor(self):
        owner = self.create_account("acct_owner", "96892220000")
        stranger = self.create_account("acct_stranger", "96893330000")
        blob = b"%PDF-private-test"
        path = bisa_config.UPLOAD_DIR / "private" / "merchant-docs" / "license.pdf"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(blob)
        media = register_private_media(
            owner,
            owner_kind="account",
            owner_id=owner["accountId"],
            purpose="merchant_license",
            storage_key="private/merchant-docs/license.pdf",
            mime_type="application/pdf",
            byte_size=len(blob),
            sha256_hex=hashlib.sha256(blob).hexdigest(),
            original_name="license.pdf",
        )
        self.assertNotIn("storageKey", media)
        self.assertNotIn("path", media)
        self.assertEqual(private_media_metadata(owner, media["id"])["id"], media["id"])
        with self.assertRaises(SecurityError) as denied:
            private_media_metadata(stranger, media["id"])
        self.assertEqual((denied.exception.code, denied.exception.status), ("private_media_not_found", 404))
        signed = signed_private_media_route(owner, media["id"], ttl_seconds=120)
        parsed = urlparse(signed["route"])
        query = parse_qs(parsed.query)
        resolved = resolve_private_media_path(owner, media["id"], query["exp"][0], query["sig"][0])
        self.assertEqual(resolved, path.resolve())
        with self.assertRaises(SecurityError):
            resolve_private_media_path(stranger, media["id"], query["exp"][0], query["sig"][0])

    def test_merchant_staff_cannot_read_private_merchant_documents(self):
        owner = self.create_merchant("private")
        staff = self.create_account(
            "acct_private_staff", "96896660000", "merchant_staff", owner["merchantId"]
        )
        stamp = "2026-01-01T00:00:00+00:00"
        with security_connection(immediate=True) as con:
            con.execute(
                """INSERT INTO merchant_members(merchant_id,account_id,role,status,created_at)
                VALUES(?,?,'merchant_staff','active',?)""",
                (owner["merchantId"], staff["accountId"], stamp),
            )
        blob = b"%PDF-private-merchant-license"
        path = bisa_config.UPLOAD_DIR / "private" / "merchant-docs" / "private-license.pdf"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(blob)
        media = register_private_media(
            owner,
            owner_kind="merchant",
            owner_id=owner["merchantId"],
            purpose="merchant_license",
            storage_key="private/merchant-docs/private-license.pdf",
            mime_type="application/pdf",
            byte_size=len(blob),
            sha256_hex=hashlib.sha256(blob).hexdigest(),
        )
        with self.assertRaises(SecurityError) as denied:
            private_media_metadata(staff, media["id"])
        self.assertEqual((denied.exception.code, denied.exception.status), ("private_media_not_found", 404))

    def test_private_media_manage_permission_does_not_bypass_merchant_scope(self):
        alpha = self.create_merchant("media_alpha")
        beta = self.create_merchant("media_beta")
        blob = b"%PDF-cross-merchant"
        path = bisa_config.UPLOAD_DIR / "private" / "merchant-docs" / "cross.pdf"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(blob)
        with self.assertRaises(SecurityError) as denied:
            register_private_media(
                alpha,
                owner_kind="merchant",
                owner_id=beta["merchantId"],
                purpose="merchant_license",
                storage_key="private/merchant-docs/cross.pdf",
                mime_type="application/pdf",
                byte_size=len(blob),
                sha256_hex=hashlib.sha256(blob).hexdigest(),
            )
        self.assertEqual((denied.exception.code, denied.exception.status), ("forbidden", 403))

    def test_unconfigured_adapters_never_report_success_or_store_payload(self):
        registry = AdapterRegistry(environment={})
        snapshot = registry.snapshot()
        self.assertEqual(set(snapshot), {"email", "payment", "push", "whatsapp"})
        self.assertTrue(all(not value["available"] for value in snapshot.values()))
        with security_connection(immediate=True) as con:
            result = execute_external_action(
                con,
                registry.get("whatsapp"),
                action_kind="merchant_application_submitted",
                target_kind="admin",
                target_id="admin",
                request={"phone": "96899999999", "secretDocument": "must-not-persist"},
            )
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "unavailable")
        with security_connection() as con:
            row = dict(con.execute("SELECT * FROM external_action_attempts").fetchone())
        self.assertNotIn("96899999999", str(row))
        self.assertNotIn("must-not-persist", str(row))
        self.assertEqual(row["provider_reference"], "")
        placeholder = AdapterRegistry(environment={
            "BISA_PAYMENT_GATEWAY": "unconfigured",
            "BISA_PAYMENT_WEBHOOK_SECRET": "",
        }).get("payment").status_result()
        self.assertEqual(placeholder.error_code, "adapter_not_configured")

    def test_order_expiry_releases_reservation_once(self):
        shopper = self.create_account("acct_buyer", "96894440000")
        merchant = self.create_merchant("expiry")
        stamp = "2026-01-01T00:00:00+00:00"
        with security_connection(immediate=True) as con:
            con.execute("INSERT INTO locations VALUES('area_expiry','wilayat_seeb','area','السيب','Seeb',1,1,?)", (stamp,))
            con.execute(
                """INSERT INTO store_branches(
                id,merchant_id,name_ar,name_en,wilayah_id,area_id,status,active,public_visible,created_at,updated_at)
                VALUES('branch_expiry',?,'فرع','Branch','wilayat_seeb','area_expiry','approved',1,1,?,?)""",
                (merchant["merchantId"], stamp, stamp),
            )
            con.execute(
                """INSERT INTO products(
                id,merchant_id,category_id,name_ar,name_en,price_baisa,status,active,created_at,updated_at)
                VALUES('product_expiry',?,'toys','لعبة','Toy',100,'approved',1,?,?)""",
                (merchant["merchantId"], stamp, stamp),
            )
            con.execute(
                """INSERT INTO product_branch_inventory(
                product_id,branch_id,stock_mode,quantity,availability,last_stock_verified_at,stale_at,active,updated_at)
                VALUES('product_expiry','branch_expiry','tracked',5,'in_stock',?,'',1,?)""",
                (stamp, stamp),
            )
            con.execute(
                """INSERT INTO orders(
                id,account_id,merchant_id,branch_id,status,fulfillment_mode,address_snapshot,policy_snapshot,
                subtotal_baisa,delivery_fee_baisa,total_baisa,idempotency_key,response_due_at,created_at,updated_at,expires_at)
                VALUES('order_expiry',?,?,?,'pending_store_confirmation','pickup','{}','{}',100,0,100,'expiry-key',?,?,?,?)""",
                (shopper["accountId"], merchant["merchantId"], "branch_expiry", stamp, stamp, stamp, stamp),
            )
            con.execute(
                """INSERT INTO inventory_reservations(
                id,order_id,product_id,branch_id,quantity,status,created_at)
                VALUES('reservation_expiry','order_expiry','product_expiry','branch_expiry',1,'pending',?)""",
                (stamp,),
            )
        result = expire_pending_orders(now=datetime(2026, 1, 2, tzinfo=UTC))
        self.assertEqual((result["expired"], result["releasedReservations"]), (1, 1))
        self.assertEqual(expire_pending_orders(now=datetime(2026, 1, 2, tzinfo=UTC))["expired"], 0)
        with security_connection() as con:
            order = con.execute("SELECT status FROM orders WHERE id='order_expiry'").fetchone()
            reservation = con.execute("SELECT status FROM inventory_reservations WHERE id='reservation_expiry'").fetchone()
            events = con.execute("SELECT COUNT(*) n FROM order_events WHERE order_id='order_expiry'").fetchone()["n"]
        self.assertEqual(order["status"], "expired")
        self.assertEqual(reservation["status"], "released")
        self.assertEqual(events, 1)

    def test_inventory_job_marks_stale_without_hiding_product(self):
        merchant = self.create_merchant("stale")
        stamp = "2026-01-01T00:00:00+00:00"
        with security_connection(immediate=True) as con:
            con.execute("INSERT INTO locations VALUES('area_stale','wilayat_seeb','area','المعبيلة','Al Maabilah',1,1,?)", (stamp,))
            con.execute(
                """INSERT INTO store_branches(
                id,merchant_id,name_ar,name_en,wilayah_id,area_id,status,active,public_visible,created_at,updated_at)
                VALUES('branch_stale',?,'فرع','Branch','wilayat_seeb','area_stale','approved',1,1,?,?)""",
                (merchant["merchantId"], stamp, stamp),
            )
            con.execute(
                """INSERT INTO products(
                id,merchant_id,category_id,name_ar,name_en,price_baisa,status,active,created_at,updated_at)
                VALUES('product_stale',?,'toys','لعبة','Toy',100,'approved',1,?,?)""",
                (merchant["merchantId"], stamp, stamp),
            )
            con.execute(
                """INSERT INTO product_branch_inventory(
                product_id,branch_id,stock_mode,quantity,availability,last_stock_verified_at,stale_at,active,updated_at)
                VALUES('product_stale','branch_stale','tracked',5,'in_stock',?,'',1,?)""",
                (stamp, stamp),
            )
        result = mark_stale_inventory(now=datetime(2026, 1, 3, tzinfo=UTC))
        self.assertEqual(result["markedStale"], 1)
        self.assertFalse(result["destructiveVisibilityChange"])
        self.assertEqual(mark_stale_inventory(now=datetime(2026, 1, 3, tzinfo=UTC))["markedStale"], 0)
        with security_connection() as con:
            inventory = con.execute(
                "SELECT freshness_status,active,stale_at FROM product_branch_inventory WHERE product_id='product_stale'"
            ).fetchone()
        self.assertEqual(inventory["freshness_status"], "stale")
        self.assertEqual(inventory["active"], 1)
        self.assertTrue(inventory["stale_at"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
