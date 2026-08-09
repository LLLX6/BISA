import hashlib
import os
import shutil
import tempfile
import threading
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path


TEST_ROOT = Path(tempfile.mkdtemp(prefix="bisa-marketplace-tests-"))
os.environ["BISA_DATA_DIR"] = str(TEST_ROOT)
os.environ["BISA_DB_PATH"] = str(TEST_ROOT / "marketplace.sqlite3")
os.environ["BISA_UPLOAD_DIR"] = str(TEST_ROOT / "uploads")
os.environ["BISA_BACKUP_DIR"] = str(TEST_ROOT / "backups")
os.environ["BISA_SEED_SAMPLE_DATA"] = "true"
os.environ["BISA_DEMO_PIN"] = "1234"

from bisa_application import API_CONTRACTS, BisaApplication  # noqa: E402
import bisa_config  # noqa: E402
import bisa_domain  # noqa: E402
from bisa_domain import (  # noqa: E402
    DomainError, authenticate, connect, dumps, hash_secret, init_db, loads, now_iso,
    register_or_login,
)
from bisa_security import SecurityError  # noqa: E402


DB_PATH = TEST_ROOT / "marketplace.sqlite3"
ORIGINAL_PATHS = {
    "data": bisa_config.DATA_DIR,
    "db": bisa_config.DB_PATH,
    "upload": bisa_config.UPLOAD_DIR,
    "backup": bisa_config.BACKUP_DIR,
    "domain_db": bisa_domain.DB_PATH,
    "domain_seed": bisa_domain.SEED_SAMPLE_DATA,
}


class BisaMarketplaceTests(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        bisa_config.DATA_DIR = ORIGINAL_PATHS["data"]
        bisa_config.DB_PATH = ORIGINAL_PATHS["db"]
        bisa_config.UPLOAD_DIR = ORIGINAL_PATHS["upload"]
        bisa_config.BACKUP_DIR = ORIGINAL_PATHS["backup"]
        bisa_domain.DB_PATH = ORIGINAL_PATHS["domain_db"]
        bisa_domain.SEED_SAMPLE_DATA = ORIGINAL_PATHS["domain_seed"]
        shutil.rmtree(TEST_ROOT, ignore_errors=True)

    def setUp(self):
        bisa_config.DATA_DIR = TEST_ROOT
        bisa_config.DB_PATH = DB_PATH
        bisa_config.UPLOAD_DIR = TEST_ROOT / "uploads"
        bisa_config.BACKUP_DIR = TEST_ROOT / "backups"
        bisa_domain.DB_PATH = DB_PATH
        bisa_domain.SEED_SAMPLE_DATA = True
        for suffix in ("", "-wal", "-shm"):
            try:
                Path(str(DB_PATH) + suffix).unlink()
            except FileNotFoundError:
                pass
        init_db()
        self.app = BisaApplication()
        self.shopper = authenticate(register_or_login("96890000001", "1234", "", "shopper")["token"])
        self.seeb = authenticate(register_or_login("96892000003", "1234", "", "merchant_owner")["token"])
        self.muscat = authenticate(register_or_login("96892000000", "1234", "", "merchant_owner")["token"])
        self.admin = self._admin("admin", "acct_market_admin", "96897770001")
        self.support = self._admin("support_admin", "acct_market_support", "96897770002")

    def _admin(self, role, account_id, phone):
        stamp = now_iso()
        with connect(immediate=True) as con:
            con.execute(
                """INSERT INTO accounts(id,phone,name,pin_hash,status,created_at)
                   VALUES(?,?,?,?,?,?)""",
                (account_id, phone, role, hash_secret("1234"), "active", stamp),
            )
            con.execute(
                "INSERT INTO account_roles(account_id,role,merchant_id,active) VALUES(?,?,?,1)",
                (account_id, role, ""),
            )
        return {"accountId": account_id, "name": role, "role": role, "merchantId": ""}

    def _merchant_member(self, account_id, phone, role="merchant_manager", merchant_id="demo_merchant_seeb"):
        stamp = now_iso()
        with connect(immediate=True) as con:
            con.execute(
                """INSERT INTO accounts(id,phone,name,pin_hash,status,created_at)
                   VALUES(?,?,?,?,?,?)""",
                (account_id, phone, account_id, hash_secret("1234"), "active", stamp),
            )
            con.execute(
                "INSERT INTO account_roles(account_id,role,merchant_id,active) VALUES(?,?,?,1)",
                (account_id, role, merchant_id),
            )
            con.execute(
                """INSERT INTO merchant_members(merchant_id,account_id,role,status,created_at)
                   VALUES(?,?,?,'active',?)""",
                (merchant_id, account_id, role, stamp),
            )
        return {
            "accountId": account_id, "name": account_id,
            "role": role, "merchantId": merchant_id,
        }

    def assertCode(self, code, callback):
        with self.assertRaises(DomainError) as caught:
            callback()
        self.assertEqual(code, caught.exception.code)
        return caught.exception

    def _add_seeb_product(self, quantity=1):
        return self.app.add_cart(self.shopper, {
            "kind": "product", "itemId": "demo_product_seeb_1",
            "branchId": "demo_branch_seeb", "quantity": quantity,
        })

    def _review_ready_application(self, suffix, requested_plan="early_trial"):
        stamp = now_iso()
        account_id = f"acct_application_{suffix}"
        merchant_id = f"merchant_application_{suffix}"
        application_id = f"application_{suffix}"
        branch_id = f"branch_application_{suffix}"
        policy_id = f"policy_application_{suffix}"
        with connect(immediate=True) as con:
            con.execute(
                """INSERT INTO accounts(id,phone,name,pin_hash,status,created_at)
                   VALUES(?,?,?,?,?,?)""",
                (account_id, f"9689666{suffix[-4:]}", "Applicant", hash_secret("1234"), "active", stamp),
            )
            con.execute(
                "INSERT INTO account_roles(account_id,role,merchant_id,active) VALUES(?,'shopper','',1)",
                (account_id,),
            )
            con.execute(
                """INSERT INTO merchants(
                    id,owner_account_id,name_ar,name_en,status,verified,created_at,updated_at,active)
                   VALUES(?,?,?,?,'submitted',0,?,?,1)""",
                (merchant_id, account_id, "متجر مقدم", "Applicant store", stamp, stamp),
            )
            con.execute(
                """INSERT INTO account_roles(account_id,role,merchant_id,active)
                   VALUES(?,'merchant_owner',?,0)""",
                (account_id, merchant_id),
            )
            snapshot = {
                "branchId": branch_id, "policyId": policy_id,
                "requestedPlan": requested_plan, "categories": {"categoryIds": ["toys"]},
            }
            con.execute(
                """INSERT INTO merchant_applications(
                    id,merchant_id,payload,status,reviewer_note,submitted_at,created_at,updated_at)
                   VALUES(?,?,?,'submitted','',?,?,?)""",
                (application_id, merchant_id, dumps(snapshot), stamp, stamp, stamp),
            )
            for step in (
                "owner", "business", "brand", "location", "hours", "documents",
                "fulfillment", "policy", "categories", "plan", "review",
            ):
                con.execute(
                    """INSERT INTO merchant_application_steps(
                        application_id,step_key,payload_json,completed_at,updated_at)
                       VALUES(?,?,?, ?,?)""",
                    (application_id, step, "{}", stamp, stamp),
                )
            con.execute(
                """INSERT INTO store_branches(
                    id,merchant_id,name_ar,name_en,wilayah_id,area_id,address_text,
                    latitude,longitude,status,active,public_visible,created_at,updated_at)
                   VALUES(?,?,?,?,'wilayat_seeb','demo_area_seeb','Seeb',23.60,58.20,
                          'submitted',1,0,?,?)""",
                (branch_id, merchant_id, "فرع مقدم", "Applicant branch", stamp, stamp),
            )
            con.execute(
                """INSERT INTO merchant_return_policies(
                    id,merchant_id,version,conditions_text,contact_method,active,created_at)
                   VALUES(?,?,1,'سياسة','support',1,?)""",
                (policy_id, merchant_id, stamp),
            )
            con.execute("UPDATE merchants SET return_policy_id=? WHERE id=?", (policy_id, merchant_id))
            for kind in ("storefront", "commercial_registration", "license"):
                media_id = f"media_{suffix}_{kind}"
                con.execute(
                    """INSERT INTO private_media_objects(
                        id,owner_kind,owner_id,purpose,storage_key,mime_type,byte_size,sha256_hex,
                        original_name,status,created_by,created_at,updated_at)
                       VALUES(?,'merchant_application',?,?,?,'application/pdf',1,?,?,'active',?,?,?)""",
                    (media_id, application_id, kind, f"private/merchant_application/{media_id}.pdf",
                     "0" * 64, f"{kind}.pdf", account_id, stamp, stamp),
                )
                con.execute(
                    """INSERT INTO merchant_documents(
                        id,application_id,kind,private_path,created_at,media_id,review_status,
                        reviewed_by,reviewed_at)
                       VALUES(?,?,?,?,?,?,'approved',?,?)""",
                    (f"doc_{suffix}_{kind}", application_id, kind, f"media:{media_id}", stamp,
                     media_id, "acct_market_admin", stamp),
                )
        return {
            "applicationId": application_id, "merchantId": merchant_id,
            "branchId": branch_id, "accountId": account_id,
        }

    def test_session_and_merchant_are_revalidated_on_every_operation(self):
        self.assertEqual("demo_merchant_seeb", self.app.revalidate_actor(self.seeb)["merchantId"])
        with connect(immediate=True) as con:
            con.execute(
                "UPDATE account_roles SET active=0 WHERE account_id=? AND role='merchant_owner' AND merchant_id=?",
                (self.seeb["accountId"], self.seeb["merchantId"]),
            )
        self.assertCode("session_not_authorized", lambda: self.app.revalidate_actor(self.seeb))

    def test_inactive_merchant_is_neither_public_nor_operational(self):
        with connect(immediate=True) as con:
            con.execute("UPDATE merchants SET active=0 WHERE id='demo_merchant_seeb'")
        self.assertCode("merchant_not_active", lambda: self.app.revalidate_actor(self.seeb))
        discovery = self.app.discovery({"branchId": "demo_branch_seeb"})
        self.assertEqual([], discovery["products"])
        self.assertEqual([], discovery["stores"])
        bootstrap = self.app.public_bootstrap()
        self.assertNotIn("demo_branch_seeb", {item["branch_id"] for item in bootstrap["stores"]})

    def test_product_update_is_tenant_scoped_and_precision_is_exact(self):
        with connect() as con:
            before = dict(con.execute(
                "SELECT merchant_id,name_en,price_baisa FROM products WHERE id='demo_product_muscat_1'"
            ).fetchone())
        payload = {
            "id": "demo_product_muscat_1", "branchId": "demo_branch_seeb", "categoryId": "toys",
            "nameAr": "عبث", "nameEn": "Tampered", "price": "0.100", "quantity": 1,
        }
        self.assertCode("product_not_found", lambda: self.app.upsert_product(self.seeb, payload))
        self.assertCode("price_precision_invalid", lambda: self.app.upsert_product(self.seeb, {
            **payload, "id": "demo_product_seeb_1", "price": "0.1004",
        }))
        with connect() as con:
            after = dict(con.execute(
                "SELECT merchant_id,name_en,price_baisa FROM products WHERE id='demo_product_muscat_1'"
            ).fetchone())
        self.assertEqual(before, after)

    def test_regulated_category_edit_returns_existing_product_to_moderation(self):
        with connect(immediate=True) as con:
            con.execute(
                "UPDATE product_categories SET regulated_rules=? WHERE id='storage'",
                (dumps({"requiresReview": True}),),
            )
        result = self.app.upsert_product(self.seeb, {
            "id":"demo_product_seeb_1","branchId":"demo_branch_seeb",
            "categoryId":"storage","nameAr":"منتج منظم","nameEn":"Regulated item",
            "price":"1.000","quantity":10,
        })
        self.assertEqual((result["status"], result["moderationStatus"]), ("pending_review", "pending"))
        with connect() as con:
            row = con.execute(
                "SELECT status,moderation_status FROM products WHERE id='demo_product_seeb_1'"
            ).fetchone()
        self.assertEqual((row["status"], row["moderation_status"]), ("pending_review", "pending"))
        self.assertCode(
            "product_not_found",
            lambda: self.app.product_detail("demo_product_seeb_1", "demo_branch_seeb"),
        )

    def test_merchant_staff_cannot_mutate_catalog_but_can_manage_inventory(self):
        stamp = now_iso()
        account_id = "acct_catalog_staff"
        actor = {
            "accountId": account_id, "name": "Catalog staff",
            "role": "merchant_staff", "merchantId": "demo_merchant_seeb",
        }
        with connect(immediate=True) as con:
            con.execute(
                """INSERT INTO accounts(id,phone,name,pin_hash,status,created_at)
                   VALUES(?,?,?,?,?,?)""",
                (account_id, "96895550111", "Catalog staff", hash_secret("1234"), "active", stamp),
            )
            con.execute(
                """INSERT INTO account_roles(account_id,role,merchant_id,active)
                   VALUES(?,'merchant_staff','demo_merchant_seeb',1)""",
                (account_id,),
            )
            con.execute(
                """INSERT INTO merchant_members(merchant_id,account_id,role,status,created_at)
                   VALUES('demo_merchant_seeb',?,'merchant_staff','active',?)""",
                (account_id, stamp),
            )
        self.assertCode("forbidden", lambda: self.app.upsert_product(actor, {
            "id": "demo_product_seeb_1", "branchId": "demo_branch_seeb",
            "categoryId": "storage", "nameAr": "منظم", "nameEn": "Organizer",
            "price": "1.200", "quantity": 12,
        }))
        with connect() as con:
            before_quantity = con.execute(
                """SELECT quantity FROM product_branch_inventory
                   WHERE product_id='demo_product_seeb_1' AND branch_id='demo_branch_seeb'"""
            ).fetchone()["quantity"]
        inventory = self.app.inventory_action(actor, {
            "branchId": "demo_branch_seeb", "productId": "demo_product_seeb_1",
            "action": "increment", "quantity": 1,
        })
        self.assertEqual(before_quantity + 1, inventory["quantity"])

    def test_product_images_require_owned_private_media_and_resolve_opaquely(self):
        stamp = now_iso()
        blob = b"\x89PNG\r\n\x1a\nproduct-image"
        media = {
            "media_product_owned": ("demo_merchant_seeb", "private/merchant/product-owned.png"),
            "media_product_foreign": ("demo_merchant_muscat", "private/merchant/product-foreign.png"),
        }
        with connect(immediate=True) as con:
            for media_id, (merchant_id, storage_key) in media.items():
                candidate = TEST_ROOT / "uploads" / storage_key
                candidate.parent.mkdir(parents=True, exist_ok=True)
                candidate.write_bytes(blob)
                con.execute(
                    """INSERT INTO private_media_objects(
                        id,owner_kind,owner_id,purpose,storage_key,mime_type,byte_size,sha256_hex,
                        original_name,status,created_by,created_at,updated_at)
                       VALUES(?,'merchant',?,'product_image',?,'image/png',?,?,?,'active',?,?,?)""",
                    (media_id, merchant_id, storage_key, len(blob), hashlib.sha256(blob).hexdigest(),
                     f"{media_id}.png", self.seeb["accountId"], stamp, stamp),
                )
        base = {
            "id": "demo_product_seeb_1", "branchId": "demo_branch_seeb",
            "categoryId": "storage", "nameAr": "منظم يومي", "nameEn": "Daily organizer",
            "price": "1.300", "quantity": 10,
        }
        self.assertCode("client_image_paths_not_allowed", lambda: self.app.upsert_product(
            self.seeb, {**base, "images": ["https://attacker.invalid/a.png"]},
        ))
        self.assertCode("product_image_media_not_found", lambda: self.app.upsert_product(
            self.seeb, {**base, "imageMediaIds": ["media_product_foreign"]},
        ))
        result = self.app.upsert_product(
            self.seeb, {**base, "imageMediaIds": ["media_product_owned"]},
        )
        self.assertEqual([{
            "url": "/api/media/products/media_product_owned",
        }], result["images"])
        resolved = self.app.resolve_public_product_media("media_product_owned")
        self.assertEqual("image/png", resolved["mimeType"])
        self.assertEqual(blob, resolved["path"].read_bytes())
        with connect(immediate=True) as con:
            con.execute("UPDATE private_media_objects SET status='archived' WHERE id='media_product_owned'")
        self.assertCode(
            "product_media_not_found",
            lambda: self.app.resolve_public_product_media("media_product_owned"),
        )

    def test_bundle_total_over_two_is_allowed_but_components_are_same_tenant_and_branch(self):
        bundle = self.app.create_bundle(self.seeb, {
            "branchId": "demo_branch_seeb", "titleAr": "باقة مكتب", "titleEn": "Office pack",
            "price": "3.500", "components": [
                {"productId": "demo_product_seeb_1", "quantity": 4},
                {"productId": "demo_product_seeb_2", "quantity": 4},
            ],
        })
        self.assertEqual("3.500", bundle["price"])
        self.assertGreater(float(bundle["normalValue"]), 2.0)
        self.assertCode("bundle_product_invalid", lambda: self.app.create_bundle(self.seeb, {
            "branchId": "demo_branch_seeb", "titleAr": "غير صالح", "titleEn": "Invalid",
            "price": "3.000", "components": [
                {"productId": "demo_product_seeb_1", "quantity": 1},
                {"productId": "demo_product_muscat_1", "quantity": 1},
            ],
        }))

    def test_cart_is_bound_to_exact_branch_even_for_same_merchant(self):
        stamp = now_iso()
        with connect(immediate=True) as con:
            con.execute(
                """INSERT INTO store_branches(
                    id,merchant_id,name_ar,name_en,wilayah_id,area_id,status,active,public_visible,created_at,updated_at)
                   VALUES('seeb_second','demo_merchant_seeb','ثان','Second','wilayat_seeb','demo_area_seeb','approved',1,1,?,?)""",
                (stamp, stamp),
            )
            con.execute(
                """INSERT INTO product_branch_inventory(
                    product_id,branch_id,stock_mode,quantity,availability,last_stock_verified_at,stale_at,active,updated_at)
                   VALUES('demo_product_seeb_1','seeb_second','tracked',10,'in_stock','','',1,?)""",
                (stamp,),
            )
        first = self._add_seeb_product()
        error = self.assertCode("cross_store_cart_confirmation_required", lambda: self.app.add_cart(self.shopper, {
            "kind": "product", "itemId": "demo_product_seeb_1", "branchId": "seeb_second", "quantity": 1,
            "expectedVersion": first["version"],
        }))
        self.assertEqual("demo_branch_seeb", error.detail["currentBranchId"])
        replaced = self.app.add_cart(self.shopper, {
            "kind": "product", "itemId": "demo_product_seeb_1", "branchId": "seeb_second", "quantity": 1,
            "expectedVersion": first["version"], "replaceCart": True,
        })
        self.assertEqual("seeb_second", replaced["branch_id"])

    def test_discovery_is_public_filtered_paginated_and_supports_details(self):
        page = self.app.discovery({"areaId": "demo_area_seeb", "limit": 2, "sort": "lowest_price"})
        self.assertEqual(2, len(page["products"]))
        self.assertTrue(page["pagination"]["hasMore"])
        self.assertTrue(all(item["area_id"] == "demo_area_seeb" for item in page["products"]))
        second_page = self.app.discovery({
            "areaId": "demo_area_seeb", "limit": 2, "sort": "lowest_price",
            "cursor": page["pagination"]["nextCursor"],
        })
        self.assertFalse(
            {item["id"] for item in page["products"]}.intersection(item["id"] for item in second_page["products"])
        )
        product = self.app.product_detail(page["products"][0]["id"], "demo_branch_seeb")
        self.assertEqual("demo_branch_seeb", product["branch_id"])
        self.assertIsNotNone(product["store"]["latitude"])
        self.assertIsNotNone(product["store"]["longitude"])
        with connect(immediate=True) as con:
            stamp = now_iso()
            con.execute(
                """INSERT INTO store_branches(
                    id,merchant_id,name_ar,name_en,wilayah_id,area_id,address_text,
                    latitude,longitude,hours_json,status,active,public_visible,created_at,updated_at)
                   VALUES('seeb_public_second','demo_merchant_seeb','فرع ثان','Second branch',
                    'wilayat_seeb','demo_area_seeb','Seeb',23.60,58.20,'{}','approved',1,1,?,?)""",
                (stamp, stamp),
            )
        store = self.app.store_detail("demo_branch_seeb", product_limit=2)
        self.assertEqual("demo_branch_seeb", store["branch_id"])
        self.assertLessEqual(len(store["products"]), 2)
        self.assertEqual(
            {"demo_branch_seeb", "seeb_public_second"},
            {branch["id"] for branch in store["branches"]},
        )
        product_search = self.app.discovery({
            "query": "Daily organizer", "categoryId": "storage", "inStock": True,
        })
        self.assertIn("demo_branch_seeb", {item["branch_id"] for item in product_search["stores"]})
        self.assertCode("invalid_distance", lambda: self.app.discovery({
            "latitude": 23.6, "longitude": 58.2, "maxDistanceKm": -1,
        }))
        with connect(immediate=True) as con:
            con.execute("UPDATE merchants SET status='suspended' WHERE id='demo_merchant_seeb'")
        self.assertCode("product_not_found", lambda: self.app.product_detail(product["id"], "demo_branch_seeb"))
        bootstrap = self.app.public_bootstrap()
        self.assertNotIn("demo_area_seeb", {item["id"] for item in bootstrap["locations"]})
        applicant_bootstrap = self.app.public_bootstrap(self.shopper)
        self.assertNotIn("demo_area_seeb", {item["id"] for item in applicant_bootstrap["locations"]})
        self.assertIn("demo_area_seeb", {item["id"] for item in applicant_bootstrap["onboardingLocations"]})

    def test_checkout_reprices_and_rejects_idempotency_key_reuse(self):
        cart = self._add_seeb_product(2)
        with connect(immediate=True) as con:
            con.execute(
                "UPDATE products SET price_baisa=200,updated_at=? WHERE id='demo_product_seeb_1'", (now_iso(),)
            )
        base = {"idempotencyKey": "repricing-key", "fulfillmentMode": "pickup", "expectedCartVersion": cart["version"]}
        error = self.assertCode("cart_price_changed", lambda: self.app.checkout(self.shopper, base))
        self.assertEqual("0.200", error.detail["changes"][0]["newPrice"])
        accepted_payload = {**base, "acceptPriceChanges": True}
        first = self.app.checkout(self.shopper, accepted_payload)
        second = self.app.checkout(self.shopper, accepted_payload)
        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])
        self.assertTrue(first["repriced"])
        self.assertCode("idempotency_key_reused", lambda: self.app.checkout(self.shopper, {
            **accepted_payload, "paymentMethod": "pay_at_store",
        }))

    def test_delivery_zone_minimum_fee_and_free_threshold_are_server_owned(self):
        self.app.configure_fulfillment(self.seeb, "demo_branch_seeb", {
            "pickup": {"enabled": True},
            "office": {"enabled": True, "fee": "1.000", "minimum": "0.200", "freeThreshold": "0.200"},
            "home": {"enabled": False},
            "zones": [{
                "mode": "office_delivery", "wilayahId": "wilayat_seeb", "areaId": "demo_area_seeb",
                "fee": "1.000", "minimum": "0.200", "freeThreshold": "0.200",
            }],
        })
        cart = self._add_seeb_product(2)
        valid = {
            "idempotencyKey": "delivery-valid", "expectedCartVersion": cart["version"],
            "fulfillmentMode": "office_delivery",
            "address": {"addressType": "office", "wilayahId": "wilayat_seeb", "areaId": "demo_area_seeb", "addressText": "Office"},
        }
        order = self.app.checkout(self.shopper, valid)["order"]
        self.assertEqual("0.000", order["deliveryFee"])

        second = authenticate(register_or_login("96895550001", "1234", "Second", "shopper")["token"])
        self.app.add_cart(second, {"kind": "product", "itemId": "demo_product_seeb_1", "branchId": "demo_branch_seeb", "quantity": 1})
        self.assertCode("delivery_zone_not_served", lambda: self.app.checkout(second, {
            "idempotencyKey": "delivery-outside", "fulfillmentMode": "office_delivery",
            "address": {"addressType": "office", "wilayahId": "wilayat_muscat", "areaId": "demo_area_muscat", "addressText": "Outside"},
        }))
        third = authenticate(register_or_login("96895550004", "1234", "Third", "shopper")["token"])
        self.app.add_cart(third, {"kind": "product", "itemId": "demo_product_seeb_1", "branchId": "demo_branch_seeb", "quantity": 1})
        self.assertCode("minimum_order_not_met", lambda: self.app.checkout(third, {
            "idempotencyKey": "delivery-minimum", "fulfillmentMode": "office_delivery",
            "address": {"addressType": "office", "wilayahId": "wilayat_seeb", "areaId": "demo_area_seeb", "addressText": "Office"},
        }))

    def test_order_lifecycle_consumes_once_and_cancel_restores_once(self):
        self._add_seeb_product(2)
        order_id = self.app.checkout(self.shopper, {"idempotencyKey": "lifecycle", "fulfillmentMode": "pickup"})["order"]["id"]
        accepted = self.app.transition_order(self.seeb, order_id, "accepted", expected_version=1)
        self.assertEqual(2, accepted["version"])
        with connect() as con:
            self.assertEqual(23, con.execute(
                "SELECT quantity FROM product_branch_inventory WHERE product_id='demo_product_seeb_1' AND branch_id='demo_branch_seeb'"
            ).fetchone()["quantity"])
        prepared = self.app.transition_order(self.seeb, order_id, "preparing", expected_version=2)
        cancelled = self.app.cancel_order(self.shopper, order_id, "Changed mind", expected_version=prepared["version"])
        repeated = self.app.cancel_order(self.shopper, order_id, "Changed mind")
        self.assertFalse(cancelled["duplicate"])
        self.assertTrue(repeated["duplicate"])
        with connect() as con:
            self.assertEqual(25, con.execute(
                "SELECT quantity FROM product_branch_inventory WHERE product_id='demo_product_seeb_1' AND branch_id='demo_branch_seeb'"
            ).fetchone()["quantity"])
            self.assertEqual("restored", con.execute(
                "SELECT status FROM inventory_reservations WHERE order_id=?", (order_id,)
            ).fetchone()["status"])

    def test_concurrent_marketplace_accept_consumes_inventory_once(self):
        self._add_seeb_product(2)
        order_id = self.app.checkout(self.shopper, {
            "idempotencyKey": "concurrent-accept", "fulfillmentMode": "pickup",
        })["order"]["id"]
        barrier = threading.Barrier(2)
        results = []

        def accept():
            barrier.wait()
            results.append(self.app.transition_order(self.seeb, order_id, "accepted"))

        workers = [threading.Thread(target=accept), threading.Thread(target=accept)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        self.assertEqual([False, True], sorted(result["duplicate"] for result in results))
        with connect() as con:
            self.assertEqual(23, con.execute(
                "SELECT quantity FROM product_branch_inventory WHERE product_id='demo_product_seeb_1' AND branch_id='demo_branch_seeb'"
            ).fetchone()["quantity"])

    def test_expiry_releases_pending_reservation(self):
        self._add_seeb_product(20)
        order_id = self.app.checkout(self.shopper, {"idempotencyKey": "expires", "fulfillmentMode": "pickup"})["order"]["id"]
        past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
        with connect(immediate=True) as con:
            con.execute("UPDATE orders SET response_due_at=?,expires_at=? WHERE id=?", (past, past, order_id))
        result = self.app.expire_orders(at=now_iso())
        self.assertEqual(1, result["expired"])
        with connect() as con:
            self.assertEqual("expired", con.execute("SELECT status FROM orders WHERE id=?", (order_id,)).fetchone()["status"])
            self.assertEqual("released", con.execute(
                "SELECT status FROM inventory_reservations WHERE order_id=?", (order_id,)
            ).fetchone()["status"])
            self.assertEqual(25, con.execute(
                "SELECT quantity FROM product_branch_inventory WHERE product_id='demo_product_seeb_1' AND branch_id='demo_branch_seeb'"
            ).fetchone()["quantity"])

    def test_transition_matrix_and_expected_version_are_enforced(self):
        self._add_seeb_product()
        order_id = self.app.checkout(self.shopper, {"idempotencyKey": "states", "fulfillmentMode": "pickup"})["order"]["id"]
        self.assertCode("order_stage_conflict", lambda: self.app.transition_order(self.seeb, order_id, "preparing"))
        self.assertCode("order_version_conflict", lambda: self.app.transition_order(self.seeb, order_id, "accepted", expected_version=99))
        accepted = self.app.transition_order(
            self.seeb, order_id, "accepted", expected_version=1, idempotency_key="accept-once",
        )
        replay = self.app.transition_order(
            self.seeb, order_id, "accepted", expected_version=1, idempotency_key="accept-once",
        )
        self.assertFalse(accepted["duplicate"])
        self.assertTrue(replay["duplicate"])
        self.assertCode("idempotency_key_reused", lambda: self.app.transition_order(
            self.seeb, order_id, "accepted", expected_version=1, reason="different", idempotency_key="accept-once",
        ))
        self.assertCode("order_stage_conflict", lambda: self.app.transition_order(self.seeb, order_id, "ready_for_pickup"))
        self.app.transition_order(self.seeb, order_id, "preparing")
        self.app.transition_order(self.seeb, order_id, "ready_for_pickup")
        final = self.app.transition_order(self.seeb, order_id, "completed")
        self.assertEqual("completed", final["status"])

    def test_order_permission_override_blocks_transition_and_merchant_cancel(self):
        self._add_seeb_product()
        order_id = self.app.checkout(self.shopper, {
            "idempotencyKey":"permission-order","fulfillmentMode":"pickup",
        })["order"]["id"]
        with connect(immediate=True) as con:
            con.execute(
                """INSERT INTO account_permission_overrides(account_id,permission,allowed,updated_at)
                   VALUES(?,'order.manage',0,?)""",
                (self.seeb["accountId"], now_iso()),
            )
        with self.assertRaises(SecurityError):
            self.app.transition_order(self.seeb, order_id, "accepted")
        with self.assertRaises(SecurityError):
            self.app.cancel_order(self.seeb, order_id, "Merchant cancel")

    def test_partial_stock_check_does_not_confirm_unseen_products(self):
        with connect(immediate=True) as con:
            con.execute(
                "UPDATE product_branch_inventory SET last_stock_verified_at='' WHERE branch_id='demo_branch_seeb'"
            )
        result = self.app.confirm_stock(self.seeb, "demo_branch_seeb", [{
            "productId": "demo_product_seeb_1", "quantity": 24,
        }])
        self.assertEqual("partial", result["status"])
        self.assertEqual(3, result["remainingCount"])
        with connect() as con:
            seen = con.execute(
                "SELECT last_stock_verified_at FROM product_branch_inventory WHERE product_id='demo_product_seeb_1' AND branch_id='demo_branch_seeb'"
            ).fetchone()["last_stock_verified_at"]
            unseen = con.execute(
                "SELECT last_stock_verified_at FROM product_branch_inventory WHERE product_id='demo_product_seeb_2' AND branch_id='demo_branch_seeb'"
            ).fetchone()["last_stock_verified_at"]
        self.assertTrue(seen)
        self.assertEqual("", unseen)
        completed = self.app.confirm_inventory_remaining(self.seeb, result["auditId"])
        self.assertEqual(3, completed["confirmedUnchanged"])
        with connect() as con:
            self.assertTrue(con.execute(
                "SELECT last_stock_verified_at FROM product_branch_inventory WHERE product_id='demo_product_seeb_2' AND branch_id='demo_branch_seeb'"
            ).fetchone()["last_stock_verified_at"])

    def test_inventory_freshness_enforcement_is_not_cosmetic(self):
        self.app.upsert_product(self.seeb, {
            "id": "demo_product_seeb_1", "branchId": "demo_branch_seeb",
            "categoryId": "storage", "nameAr": "منظم يومي", "nameEn": "Daily organizer",
            "price": "1.300", "quantity": 10,
        })
        with connect() as con:
            inventory = con.execute(
                """SELECT freshness_status,last_stock_verified_at FROM product_branch_inventory
                   WHERE product_id='demo_product_seeb_1' AND branch_id='demo_branch_seeb'"""
            ).fetchone()
        self.assertEqual("unverified", inventory["freshness_status"])
        self.assertEqual("", inventory["last_stock_verified_at"])

        self.app.confirm_stock(self.seeb, "demo_branch_seeb", [
            {"productId": "demo_product_seeb_1", "quantity": 10},
        ])
        with connect(immediate=True) as con:
            inventory = con.execute(
                """SELECT freshness_status,last_stock_verified_at FROM product_branch_inventory
                   WHERE product_id='demo_product_seeb_1' AND branch_id='demo_branch_seeb'"""
            ).fetchone()
            self.assertEqual("fresh", inventory["freshness_status"])
            self.assertTrue(inventory["last_stock_verified_at"])
            con.execute(
                """UPDATE product_branch_inventory SET freshness_status='stale',
                   stale_enforcement='hide_stale',stale_at=?
                   WHERE product_id='demo_product_seeb_1' AND branch_id='demo_branch_seeb'""",
                (now_iso(),),
            )
        discovery = self.app.discovery({"branchId": "demo_branch_seeb", "limit": 60})
        self.assertNotIn("demo_product_seeb_1", {item["id"] for item in discovery["products"]})
        self.assertCode("item_not_available", lambda: self._add_seeb_product())

    def test_merchant_settings_returns_scoped_team_and_active_location_master(self):
        current = self._merchant_member(
            "acct_settings_current", "96895550101", "merchant_staff", "demo_merchant_seeb",
        )
        self._merchant_member(
            "acct_settings_foreign", "96895550102", "merchant_staff", "demo_merchant_muscat",
        )
        with connect(immediate=True) as con:
            con.execute(
                """INSERT INTO locations(
                    id,parent_id,kind,name_ar,name_en,sort_order,active,created_at)
                   VALUES('inactive_settings_area','wilayat_seeb','area','مخفية','Hidden',999,0,?)""",
                (now_iso(),),
            )

        settings = self.app.merchant_settings(current)
        self.assertEqual(
            {"acct_settings_current"},
            {member["account_id"] for member in settings["members"]},
        )
        self.assertTrue(all("phone" not in member for member in settings["members"]))
        self.assertTrue(all(branch["merchant_id"] == "demo_merchant_seeb" for branch in settings["branches"]))
        location_ids = {location["id"] for location in settings["locationMaster"]}
        self.assertIn("demo_area_seeb", location_ids)
        self.assertIn("wilayat_seeb", location_ids)
        self.assertNotIn("inactive_settings_area", location_ids)
        self.assertTrue(all(location["active"] == 1 for location in settings["locationMaster"]))
        self.assertCode("forbidden", lambda: self.app.merchant_settings(self.shopper))

    def test_team_add_accepts_phone_keeps_account_id_and_blocks_cross_tenant(self):
        stamp = now_iso()
        with connect(immediate=True) as con:
            con.execute(
                """INSERT INTO accounts(id,phone,name,pin_hash,status,created_at)
                   VALUES('acct_phone_member','96895550103','Phone member',?,'active',?)""",
                (hash_secret("1234"), stamp),
            )
            con.execute(
                """INSERT INTO accounts(id,phone,name,pin_hash,status,created_at)
                   VALUES('acct_second_identity','96895550104','Other identity',?,'active',?)""",
                (hash_secret("1234"), stamp),
            )

        added = self.app.add_merchant_member(self.seeb, {
            "accountPhone": "+968 9555 0103", "role": "merchant_manager",
        })
        self.assertEqual(("acct_phone_member", "merchant_manager"), (added["accountId"], added["role"]))
        updated = self.app.add_merchant_member(self.seeb, {
            "accountId": "acct_phone_member", "role": "merchant_staff",
        })
        self.assertEqual("merchant_staff", updated["role"])
        with connect() as con:
            roles = {
                row["role"]: row["active"] for row in con.execute(
                    """SELECT role,active FROM account_roles
                       WHERE account_id='acct_phone_member' AND merchant_id='demo_merchant_seeb'"""
                )
            }
        self.assertEqual(0, roles["merchant_manager"])
        self.assertEqual(1, roles["merchant_staff"])

        self.assertCode("merchant_member_identity_mismatch", lambda: self.app.add_merchant_member(
            self.seeb, {
                "accountId": "acct_second_identity", "accountPhone": "96895550103",
                "role": "merchant_staff",
            },
        ))
        self.assertCode("merchant_member_cross_tenant", lambda: self.app.add_merchant_member(
            self.seeb, {"accountPhone": "96892000000", "role": "merchant_manager"},
        ))
        self.assertCode("merchant_owner_cannot_be_member", lambda: self.app.add_merchant_member(
            self.seeb, {"accountPhone": "96892000003", "role": "merchant_manager"},
        ))
        staff_actor = {
            "accountId": "acct_phone_member", "name": "Phone member",
            "role": "merchant_staff", "merchantId": "demo_merchant_seeb",
        }
        self.assertCode("forbidden", lambda: self.app.add_merchant_member(
            staff_actor, {"accountId": "acct_second_identity", "role": "merchant_staff"},
        ))

    def test_typed_branch_fulfillment_and_return_policy_enforce_tenant_rbac(self):
        manager = self._merchant_member(
            "acct_typed_manager", "96895550105", "merchant_manager", "demo_merchant_seeb",
        )
        staff = self._merchant_member(
            "acct_typed_staff", "96895550106", "merchant_staff", "demo_merchant_seeb",
        )
        self.assertCode("forbidden", lambda: self.app.create_branch(manager, {
            "nameAr": "فرع غير مفوض", "nameEn": "Not allowed", "wilayahId": "wilayat_seeb",
            "areaId": "demo_area_seeb", "address": "Seeb",
        }))
        self.assertCode("invalid_branch_location", lambda: self.app.create_branch(self.seeb, {
            "nameAr": "فرع خاطئ", "nameEn": "Wrong parent", "wilayahId": "wilayat_muscat",
            "areaId": "demo_area_seeb", "address": "Seeb",
        }))
        self.assertCode("coordinates_outside_muscat", lambda: self.app.create_branch(self.seeb, {
            "nameAr": "فرع خارج النطاق", "nameEn": "Outside launch bounds",
            "wilayahId": "wilayat_seeb", "areaId": "demo_area_seeb",
            "address": "Invalid pin", "latitude": 20.0, "longitude": 56.0,
        }))
        branch = self.app.create_branch(self.seeb, {
            "nameAr": "فرع السوق", "nameEn": "Market branch", "wilayahId": "wilayat_seeb",
            "areaId": "demo_area_seeb", "address": "Seeb market", "latitude": 23.6,
            "longitude": 58.2, "hours": {"sunday": {"open": "09:00", "close": "21:00"}},
        })
        self.assertEqual(("draft", False), (branch["status"], branch["publicVisible"]))

        configured = self.app.configure_fulfillment(manager, branch["id"], {
            "pickup": {"enabled": True, "fee": "0.000", "minimum": "0.000", "freeThreshold": "0.000"},
            "office": {"enabled": True, "fee": "1.000", "minimum": "3.000", "freeThreshold": "8.000"},
            "home": {"enabled": False, "fee": "2.000", "minimum": "5.000", "freeThreshold": "12.000"},
            "zones": [{
                "mode": "office_delivery", "wilayahId": "wilayat_seeb",
                "areaId": "demo_area_seeb", "fee": "1.250", "minimum": "3.000",
                "freeThreshold": "8.000", "eta": "60 minutes",
            }],
            "eta": "60-90 minutes",
        })
        self.assertEqual(1, len(configured["zones"]))
        self.assertEqual(1250, configured["zones"][0]["fee"])
        self.assertCode("branch_not_owned", lambda: self.app.configure_fulfillment(
            manager, "demo_branch_muscat", {
                "pickup": {"enabled": True}, "office": {}, "home": {}, "zones": [],
            },
        ))
        self.assertCode("forbidden", lambda: self.app.configure_fulfillment(
            staff, branch["id"], {
                "pickup": {"enabled": True}, "office": {}, "home": {}, "zones": [],
            },
        ))
        self.assertCode("free_threshold_below_minimum", lambda: self.app.configure_fulfillment(
            manager, branch["id"], {
                "pickup": {"enabled": True},
                "office": {"enabled": True, "minimum": "4.000", "freeThreshold": "3.000"},
                "home": {}, "zones": [],
            },
        ))

        policy = self.app.save_return_policy(manager, {
            "returnWindowDays": 7, "exchangeWindowDays": 14,
            "conditions": "Unused and sealed", "receiptRequired": True,
            "excludedCategories": ["personal"], "contactMethod": "in_app",
            "notes": "Oman consumer rights apply",
        })
        self.assertTrue(policy["active"])
        self.assertCode("forbidden", lambda: self.app.save_return_policy(staff, {
            "returnWindowDays": 1, "exchangeWindowDays": 1,
            "conditions": "No", "contactMethod": "in_app",
        }))
        settings = self.app.merchant_settings(manager)
        stored_branch = next(item for item in settings["branches"] if item["id"] == branch["id"])
        self.assertEqual(1000, stored_branch["fulfillment"]["office_fee_baisa"])
        self.assertEqual(1250, stored_branch["deliveryZones"][0]["fee_baisa"])
        self.assertEqual(policy["id"], settings["returnPolicy"]["id"])

    def test_branch_and_staff_plan_limits_are_server_enforced(self):
        with connect(immediate=True) as con:
            con.execute(
                "UPDATE subscription_plans SET entitlements=? WHERE id='advanced_3m'",
                (dumps({"products": 900, "branches": 1, "staff": 0, "bundles": 25, "analytics": "advanced", "supplierHub": True}),),
            )
            con.execute(
                """INSERT INTO accounts(id,phone,name,pin_hash,status,created_at)
                   VALUES('staff_limit','96895550002','Staff',?,'active',?)""",
                (hash_secret("1234"), now_iso()),
            )
        self.assertCode("plan_branch_limit", lambda: self.app.create_branch(self.seeb, {
            "nameAr": "فرع", "nameEn": "Branch", "wilayahId": "wilayat_seeb",
            "areaId": "demo_area_seeb", "address": "Address",
        }))
        self.assertCode("plan_staff_limit", lambda: self.app.add_merchant_member(self.seeb, {
            "accountId": "staff_limit", "role": "merchant_staff",
        }))

    def test_trial_resolves_exactly_to_current_basic_entitlements(self):
        current_basic = {
            "products": 321, "branches": 3, "staff": 4, "bundles": 9,
            "analytics": "basic", "supplierHub": True,
        }
        stale_trial = {
            "inherits": "basic_3m", "products": 999, "branches": 99,
            "staff": 99, "bundles": 99, "analytics": "advanced",
        }
        with connect(immediate=True) as con:
            con.execute("UPDATE subscription_plans SET entitlements=? WHERE id='basic_3m'", (dumps(current_basic),))
            con.execute("UPDATE subscription_plans SET entitlements=? WHERE id='early_trial'", (dumps(stale_trial),))
            con.execute("UPDATE merchant_subscriptions SET plan_id='early_trial' WHERE merchant_id='demo_merchant_seeb'")
        dashboard = self.app.merchant_dashboard(self.seeb)
        self.assertEqual(current_basic, dashboard["plan"]["entitlements"])
        self.assertEqual(321, dashboard["planUsage"]["products"]["limit"])
        self.assertCode("trial_inherits_basic", lambda: self.app.admin_action(
            self.admin, "plan", "update", {
                "id": "early_trial", "price": "0.000", "durationDays": 90,
                "entitlements": stale_trial,
            },
        ))

    def test_public_bootstrap_exposes_safe_current_plan_summaries(self):
        with connect(immediate=True) as con:
            con.execute(
                "UPDATE subscription_plans SET entitlements=? WHERE id='basic_3m'",
                (dumps({
                    "products": 222, "branches": 2, "staff": 2, "bundles": 8,
                    "analytics": "basic", "supplierHub": True,
                    "internalSecretLikeField": "must-not-leak",
                }),),
            )
        bootstrap = self.app.public_bootstrap()
        plans = {plan["id"]: plan for plan in bootstrap["plans"]}
        self.assertIn("basic_3m", plans)
        self.assertIn("early_trial", plans)
        self.assertEqual("40.000", plans["basic_3m"]["price"])
        self.assertEqual(90, plans["basic_3m"]["duration_days"])
        self.assertEqual(222, plans["early_trial"]["entitlements"]["products"])
        self.assertNotIn("internalSecretLikeField", plans["basic_3m"]["entitlements"])

    def test_return_policy_snapshot_is_immutable_per_order(self):
        self._add_seeb_product()
        order_id = self.app.checkout(self.shopper, {"idempotencyKey": "policy-v1", "fulfillmentMode": "pickup"})["order"]["id"]
        original = self.app.order_detail(self.shopper, order_id)["returnPolicy"]
        newer = self.app.save_return_policy(self.seeb, {
            "returnWindowDays": 3, "exchangeWindowDays": 2,
            "conditions": "New policy", "contactMethod": "in_app",
        })
        self.assertGreater(newer["version"], original["version"])
        unchanged = self.app.order_detail(self.shopper, order_id)["returnPolicy"]
        self.assertEqual(original["id"], unchanged["id"])
        self.assertEqual(original["version"], unchanged["version"])

    def test_supplier_hub_requires_approved_supplier_and_plan_entitlement(self):
        stamp = now_iso()
        with connect(immediate=True) as con:
            con.execute(
                "INSERT INTO suppliers(id,name_ar,name_en,status,created_at,updated_at) VALUES('supplier_1','مورد','Supplier','approved',?,?)",
                (stamp, stamp),
            )
            con.execute(
                """INSERT INTO supplier_campaigns(
                    id,supplier_id,title_ar,title_en,payload,status,starts_at,ends_at,created_at,updated_at)
                   VALUES('campaign_1','supplier_1','عرض','Offer',?,'approved','','',?,?)""",
                (dumps({"targetWilayats": ["wilayat_seeb"], "offer": "Wholesale"}), stamp, stamp),
            )
        campaigns = self.app.supplier_campaigns(self.seeb)
        self.assertEqual(["campaign_1"], [item["id"] for item in campaigns])
        self.assertCode("forbidden", lambda: self.app.supplier_campaigns(self.shopper))
        with connect(immediate=True) as con:
            con.execute("UPDATE suppliers SET status='suspended' WHERE id='supplier_1'")
        self.assertEqual([], self.app.supplier_campaigns(self.seeb))
        with connect(immediate=True) as con:
            con.execute("UPDATE suppliers SET status='approved' WHERE id='supplier_1'")
            con.execute(
                "UPDATE subscription_plans SET entitlements=? WHERE id='advanced_3m'",
                (dumps({"products": 900, "branches": 5, "staff": 6, "bundles": 25, "analytics": "advanced", "supplierHub": False}),),
            )
        self.assertCode("supplier_hub_not_in_plan", lambda: self.app.supplier_campaigns(self.seeb))

    def test_favorites_reports_notifications_and_merchant_metrics_are_real(self):
        saved = self.app.set_favorite(
            self.shopper, "product", "demo_product_seeb_1", branch_id="demo_branch_seeb", saved=True,
        )
        self.assertTrue(saved["saved"])
        self.assertIn(
            {"entityKind":"product","entityId":"demo_product_seeb_1"},
            self.app.public_bootstrap(self.shopper)["favorites"],
        )
        report = self.app.report_product(
            self.shopper, "demo_product_seeb_1", "incorrect_price", "Shelf differs",
            branch_id="demo_branch_seeb",
        )
        duplicate = self.app.report_product(
            self.shopper, "demo_product_seeb_1", "incorrect_price", "Again",
            branch_id="demo_branch_seeb",
        )
        self.assertEqual(report["id"], duplicate["id"])
        self.app.record_event(
            self.shopper, "product_view", "product", "demo_product_seeb_1",
            {"branchId": "demo_branch_seeb", "areaId": "demo_area_seeb"},
        )
        dashboard = self.app.merchant_dashboard(self.seeb)
        self.assertEqual(1, dashboard["metrics"]["product_view"])

        with connect(immediate=True) as con:
            notification_id = self.app._insert_notification(
                con, "account", self.shopper["accountId"], "عنوان", "Title", "نص", "Body",
                "shopper:order:x", False, "test-notification",
            )
        action = self.app.notification_action(self.shopper, notification_id, "read")
        self.assertEqual("read", action["action"])
        self.assertCode("notification_not_found", lambda: self.app.notification_action(
            authenticate(register_or_login("96895550003", "1234", "Other", "shopper")["token"]),
            notification_id, "read",
        ))

    def test_merchant_dashboard_reloads_bundles_campaigns_and_real_metrics(self):
        before = self.app.merchant_dashboard(self.seeb)
        self.assertTrue(before["bundles"])
        created = self.app.merchant_campaign_action(self.seeb, {
            "action": "create_campaign", "idempotencyKey": "dashboard-campaign-v1",
            "payload": {
                "placement": "home_inline", "landingKind": "product",
                "landingId": "demo_product_seeb_1", "titleAr": "حملة لوحة",
                "titleEn": "Dashboard campaign", "startsAt": "2027-01-01T08:00:00+04:00",
                "endsAt": "2027-02-01T08:00:00+04:00",
            },
        })
        reloaded = self.app.merchant_dashboard(self.seeb)
        self.assertIn(created["id"], {campaign["id"] for campaign in reloaded["campaigns"]})
        self.assertEqual(len(reloaded["campaigns"]), reloaded["campaignMetrics"]["campaigns"])
        self.assertIn("impressions", reloaded["campaignMetrics"])

    def test_approved_ads_support_store_product_and_bundle_landings(self):
        stamp = now_iso()
        with connect(immediate=True) as con:
            bundle_id = con.execute(
                "SELECT id FROM bundles WHERE merchant_id='demo_merchant_seeb' AND status='approved' LIMIT 1"
            ).fetchone()["id"]
            for campaign_id, landing_kind, landing_id in (
                ("ad_product_landing", "product", "demo_product_seeb_1"),
                ("ad_bundle_landing", "bundle", bundle_id),
            ):
                con.execute(
                    """INSERT INTO ad_campaigns(
                        id,owner_kind,owner_id,placement,target_json,landing_kind,landing_id,
                        label_ar,label_en,status,starts_at,ends_at,frequency_cap,created_at,updated_at)
                       VALUES(?,'merchant','demo_merchant_seeb','home_inline',?,?,?,
                        'إعلان','Sponsored','approved','','',3,?,?)""",
                    (campaign_id, dumps({"titleAr": campaign_id, "titleEn": campaign_id}),
                     landing_kind, landing_id, stamp, stamp),
                )
        advertisement_ids = {item["id"] for item in self.app.public_bootstrap()["advertisements"]}
        self.assertTrue({"ad_product_landing", "ad_bundle_landing"}.issubset(advertisement_ids))

    def test_ad_events_validate_public_campaign_dedupe_and_feed_dashboard_metrics(self):
        stamp = now_iso()
        campaign_id = "ad_metrics_product"
        with connect(immediate=True) as con:
            con.execute(
                """INSERT INTO ad_campaigns(
                    id,owner_kind,owner_id,placement,target_json,landing_kind,landing_id,
                    label_ar,label_en,status,starts_at,ends_at,frequency_cap,created_at,updated_at)
                   VALUES(?,'merchant','demo_merchant_seeb','home_inline',?,'product',
                    'demo_product_seeb_1','إعلان','Sponsored','approved','','',2,?,?)""",
                (campaign_id, dumps({"titleAr": "إعلان مقاس", "titleEn": "Measured ad"}), stamp, stamp),
            )
        first = self.app.record_event(
            self.shopper, "ad_impression", "ad", campaign_id,
            {
                "campaignId": campaign_id, "eventId": "impression-event-0001",
                "placement": "untrusted-placement", "phone": "96899999999",
            },
        )
        self.assertEqual((True, "impression"), (first["recorded"], first["adEvent"]))
        replay = self.app.record_event(
            self.shopper, "ad_impression", "ad", campaign_id,
            {"campaignId": campaign_id, "eventId": "impression-event-0001"},
        )
        self.assertEqual((False, True), (replay["recorded"], replay["duplicate"]))
        second = self.app.record_event(
            self.shopper, "ad_impression", "campaign", campaign_id,
            {"campaignId": campaign_id},
        )
        capped = self.app.record_event(
            self.shopper, "ad_impression", "advertisement", campaign_id,
            {"campaignId": campaign_id},
        )
        click = self.app.record_event(
            self.shopper, "ad_click", "ad", campaign_id,
            {
                "campaignId": campaign_id, "eventId": "click-event-00000001",
                "email": "must-not-persist@example.test", "address": "private",
            },
        )
        self.assertTrue(second["recorded"])
        self.assertTrue(capped["frequencyCapped"])
        self.assertEqual("click", click["adEvent"])
        dashboard = self.app.merchant_dashboard(self.seeb)
        campaign = next(item for item in dashboard["campaigns"] if item["id"] == campaign_id)
        self.assertEqual({"impression": 2, "click": 1}, campaign["metrics"])
        self.assertEqual(2, dashboard["campaignMetrics"]["impressions"])
        self.assertEqual(1, dashboard["campaignMetrics"]["clicks"])
        with connect() as con:
            events = list(con.execute(
                "SELECT * FROM ad_events WHERE campaign_id=? ORDER BY created_at,id", (campaign_id,)
            ))
            analytics = con.execute(
                "SELECT * FROM analytics_events WHERE id=?", (click["id"],)
            ).fetchone()
        self.assertEqual(["impression", "impression", "click"], [row["event_type"] for row in events])
        self.assertTrue(all(row["actor_hash"] and self.shopper["accountId"] not in row["actor_hash"] for row in events))
        self.assertNotIn("click-event-00000001", str(dict(analytics)))
        self.assertNotIn("must-not-persist", str(dict(analytics)))
        self.assertIn('"placement":"home_inline"', analytics["context_json"])

    def test_ad_event_rejects_hidden_window_and_nonpublic_landing(self):
        stamp = now_iso()
        rows = (
            ("ad_draft", "draft", "", "", "demo_product_seeb_1"),
            ("ad_expired", "approved", "2025-01-01T00:00:00+00:00", "2025-02-01T00:00:00+00:00", "demo_product_seeb_1"),
            ("ad_missing_landing", "approved", "", "", "missing-product"),
        )
        with connect(immediate=True) as con:
            for campaign_id, status, starts_at, ends_at, landing_id in rows:
                con.execute(
                    """INSERT INTO ad_campaigns(
                        id,owner_kind,owner_id,placement,target_json,landing_kind,landing_id,
                        label_ar,label_en,status,starts_at,ends_at,frequency_cap,created_at,updated_at)
                       VALUES(?,'merchant','demo_merchant_seeb','home_inline','{}','product',?,
                        'إعلان','Sponsored',?,?,?,3,?,?)""",
                    (campaign_id, landing_id, status, starts_at, ends_at, stamp, stamp),
                )
        for campaign_id, *_ in rows:
            self.assertCode("ad_campaign_not_found", lambda campaign_id=campaign_id: self.app.record_event(
                self.shopper, "ad_click", "ad", campaign_id,
                {"campaignId": campaign_id, "eventId": f"click-{campaign_id}-0001"},
            ))
        self.assertCode("invalid_ad_event_entity", lambda: self.app.record_event(
            self.shopper, "ad_click", "product", "demo_product_seeb_1",
            {"campaignId": "ad_draft", "eventId": "invalid-entity-0001"},
        ))
        self.assertCode("invalid_ad_event_campaign", lambda: self.app.record_event(
            self.shopper, "ad_click", "ad", "ad_draft",
            {"campaignId": "ad_expired", "eventId": "mismatch-event-0001"},
        ))

    def test_action_prompt_analytics_require_owned_notification_and_drop_pii(self):
        with connect(immediate=True) as con:
            notification_id = self.app._insert_notification(
                con, "account", self.shopper["accountId"], "إجراء", "Action",
                "راجع الطلب", "Review order", "shopper:order:test", True,
                "action-prompt-analytics-test",
            )
        event = self.app.record_event(
            self.shopper, "action_prompt_shown", "notification", notification_id,
            {"source": "foreground", "phone": "96899999999", "address": "private"},
        )
        with connect() as con:
            row = con.execute("SELECT * FROM analytics_events WHERE id=?", (event["id"],)).fetchone()
        self.assertEqual("notification", row["entity_kind"])
        self.assertEqual('{"source":"foreground"}', row["context_json"])
        other = authenticate(register_or_login("96895559999", "1234", "Other", "shopper")["token"])
        self.assertCode("notification_not_found", lambda: self.app.record_event(
            other, "action_prompt_opened", "notification", notification_id, {},
        ))
        self.assertCode("authentication_required", lambda: self.app.record_event(
            None, "action_prompt_completed", "notification", notification_id, {},
        ))

    def test_admin_rbac_resources_actions_and_audit(self):
        self.assertCode("admin_permission_required", lambda: self.app.admin_action(
            self.support, "product", "suspend", {"id": "demo_product_seeb_1", "reason": "Review"},
        ))
        result = self.app.admin_action(
            self.admin, "product", "suspend", {"id": "demo_product_seeb_1", "reason": "Review"},
        )
        self.assertEqual("suspended", result["result"]["status"])
        resources = self.app.admin_resource(self.admin, "products", {"status": "suspended", "limit": 10})
        self.assertIn("demo_product_seeb_1", {item["id"] for item in resources["items"]})
        for alias in ("applications", "locations", "inventory", "advertising", "notifications", "settings"):
            self.assertEqual(alias, self.app.admin_resource(self.admin, alias, {"limit": 2})["resource"])
        applications = self.app.admin_resource(self.admin, "applications", {"limit": 10})["items"]
        for application in applications:
            self.assertNotIn("payload", application)
            self.assertNotIn("private_path", application)
        merchants = self.app.admin_resource(self.admin, "merchants", {"limit": 10})["items"]
        self.assertTrue(all("owner_account_id" not in merchant for merchant in merchants))
        with connect() as con:
            audit = con.execute(
                "SELECT * FROM admin_audit_logs WHERE action='product.suspend' AND target_id='demo_product_seeb_1'"
            ).fetchone()
        self.assertIsNotNone(audit)
        self.assertEqual("Review", audit["reason"])

    def test_admin_location_master_supports_first_store_area_without_public_leak(self):
        created = self.app.admin_action(self.admin, "location", "create", {
            "kind": "area", "parentId": "wilayat_muscat",
            "nameAr": "منطقة إطلاق موثقة", "nameEn": "Verified launch area", "sortOrder": 12,
        })
        area_id = created["id"]
        self.assertNotIn(area_id, {item["id"] for item in self.app.public_bootstrap()["locations"]})
        applicant = self.app.public_bootstrap(self.shopper)
        self.assertIn(area_id, {item["id"] for item in applicant["onboardingLocations"]})
        updated = self.app.admin_action(self.admin, "location", "update", {
            "id": area_id, "parentId": "wilayat_muscat",
            "nameAr": "منطقة إطلاق معتمدة", "nameEn": "Approved launch area", "sortOrder": 13,
        })
        self.assertEqual("Approved launch area", updated["result"]["name_en"])
        imported = self.app.admin_action(self.admin, "location", "bulk_import", {
            "items": [
                {"kind": "area", "parentId": "wilayat_muscat", "nameAr": "منطقة إطلاق معتمدة", "nameEn": "Approved launch area", "sortOrder": 13},
                {"kind": "area", "parentId": "wilayat_muscat", "nameAr": "منطقة ثانية", "nameEn": "Second managed area", "sortOrder": 14},
            ],
        })
        self.assertEqual(1, len(imported["result"]["importedIds"]))
        self.assertEqual([area_id], imported["result"]["existingIds"])
        self.assertCode(
            "admin_permission_required",
            lambda: self.app.admin_action(self.support, "location", "create", {
                "kind": "area", "parentId": "wilayat_muscat",
                "nameAr": "غير مخول", "nameEn": "Unauthorized", "sortOrder": 99,
            }),
        )

    def test_admin_can_provision_and_suspend_a_real_supplier_account(self):
        phone = "96895557777"
        register_or_login(phone, "1234", "Supplier owner", "shopper")
        created = self.app.admin_action(self.admin, "supplier", "create", {
            "nameAr": "مورد الاختبار المعتمد", "nameEn": "Approved test supplier",
            "accountPhone": phone, "reason": "Verified supplier onboarding documents offline",
        })
        supplier_id = created["id"]
        actor = authenticate(register_or_login(phone, "1234", "", "supplier_advertiser")["token"])
        self.assertEqual(supplier_id, self.app.public_bootstrap(actor)["actor"]["supplierId"])
        resources = self.app.admin_resource(self.admin, "suppliers", {"limit": 200})
        self.assertIn(supplier_id, {item["id"] for item in resources["items"]})
        suspended = self.app.admin_action(self.admin, "supplier", "suspend", {
            "id": supplier_id, "reason": "Supplier access review",
        })
        self.assertEqual("suspended", suspended["result"]["status"])
        self.assertCode("session_not_authorized", lambda: self.app.public_bootstrap(actor))
        self.assertCode("admin_permission_required", lambda: self.app.admin_action(
            self.support, "supplier", "create", {
                "nameAr": "غير مخول", "nameEn": "Unauthorized", "accountPhone": phone,
            },
        ))

    def test_granular_admin_roles_are_permission_scoped(self):
        moderator = self._admin("catalog_moderator", "acct_catalog_moderator", "96897770003")
        reviewer = self._admin("merchant_reviewer", "acct_merchant_reviewer", "96897770004")
        advertising = self._admin("advertising_manager", "acct_ad_manager", "96897770005")

        products = self.app.admin_resource(moderator, "products", {"limit": 2})
        self.assertEqual("products", products["resource"])
        self.assertCode(
            "admin_permission_required",
            lambda: self.app.admin_resource(moderator, "merchants", {"limit": 2}),
        )
        merchants = self.app.admin_resource(reviewer, "merchants", {"limit": 2})
        self.assertEqual("merchants", merchants["resource"])
        self.assertCode(
            "admin_permission_required",
            lambda: self.app.admin_resource(reviewer, "products", {"limit": 2}),
        )
        ads = self.app.admin_resource(advertising, "ads", {"limit": 2})
        self.assertEqual("ads", ads["resource"])
        supplier_campaigns = self.app.admin_resource(advertising, "supplier_campaigns", {"limit": 2})
        self.assertEqual("supplier_campaigns", supplier_campaigns["resource"])
        self.assertCode(
            "admin_permission_required",
            lambda: self.app.order_detail(advertising, "missing-order"),
        )

    def test_typed_category_update_is_validated_permissioned_and_audited(self):
        moderator = self._admin("catalog_moderator", "acct_category_moderator", "96897770013")
        self.assertCode("admin_permission_required", lambda: self.app.admin_action(
            moderator, "category", "update", {"id": "storage", "nameEn": "Unauthorized"},
        ))
        updated = self.app.admin_action(self.admin, "category", "update", {
            "id": "storage", "nameAr": "التنظيم الذكي", "nameEn": "Smart storage",
            "icon": "boxes", "imagePath": "/assets/images/catalog/storage.webp",
            "regulatedRules": {"requiresReview": True, "requiredFields": ["material"]},
            "sortOrder": 27, "slug": "smart-storage",
            "descriptionAr": "حلول منظمة", "descriptionEn": "Organized solutions",
            "reason": "Catalog refinement",
        })
        self.assertEqual("Smart storage", updated["result"]["name_en"])
        self.assertEqual({"requiresReview": True, "requiredFields": ["material"]}, updated["result"]["regulated_rules"])
        with connect() as con:
            row = con.execute(
                "SELECT * FROM product_categories WHERE id='storage'",
            ).fetchone()
            audit = con.execute(
                """SELECT before_json,after_json FROM admin_audit_logs
                   WHERE action='category.update' AND target_id='storage'
                   ORDER BY created_at DESC LIMIT 1"""
            ).fetchone()
        self.assertEqual(("Smart storage", "smart-storage", 27), (row["name_en"], row["slug"], row["sort_order"]))
        self.assertEqual({"requiresReview": True, "requiredFields": ["material"]}, loads(row["regulated_rules"], {}))
        self.assertIsNotNone(audit)
        self.assertEqual("Storage & Organization", loads(audit["before_json"], {})["name_en"])
        self.assertCode("invalid_category_rules", lambda: self.app.admin_action(
            self.admin, "category", "update", {"id": "storage", "regulatedRules": ["unsafe"]},
        ))
        self.assertCode("invalid_category_slug", lambda: self.app.admin_action(
            self.admin, "category", "update", {"id": "storage", "slug": "Not Safe"},
        ))

    def test_typed_plan_and_platform_settings_enforce_limits_and_rbac(self):
        finance = self._admin("finance", "acct_typed_finance", "96897770014")
        self.assertCode("admin_permission_required", lambda: self.app.admin_action(
            finance, "plan", "update", {
                "id": "basic_3m", "price": "41.000", "durationDays": 91,
                "entitlements": {"products": 401, "branches": 2, "staff": 2, "bundles": 8},
            },
        ))
        plan = self.app.admin_action(self.admin, "plan", "update", {
            "id": "basic_3m", "price": "41.000", "durationDays": 91,
            "entitlements": {
                "products": 401, "branches": 3, "staff": 4, "bundles": 9,
                "analytics": "basic", "supplierHub": True,
            },
            "reason": "Approved commercial change",
        })
        self.assertEqual((41000, 91), (plan["result"]["price_baisa"], plan["result"]["duration_days"]))
        self.assertEqual(401, plan["result"]["entitlements"]["products"])
        self.assertCode("invalid_plan_limit", lambda: self.app.admin_action(
            self.admin, "plan", "update", {
                "id": "basic_3m", "price": "40.000", "durationDays": 90,
                "entitlements": {"products": -1, "branches": 2, "staff": 2, "bundles": 8},
            },
        ))

        self.assertCode("admin_permission_required", lambda: self.app.admin_action(
            self.support, "setting", "update", {"id": "merchantResponseHours", "value": 12},
        ))
        setting = self.app.admin_action(self.admin, "setting", "update", {
            "id": "merchantResponseHours", "value": 12, "reason": "SLA review",
        })
        self.assertEqual({"key": "merchantResponseHours", "value": 12}, setting["result"])
        self.assertCode("invalid_setting_value", lambda: self.app.admin_action(
            self.admin, "setting", "update", {"id": "merchantResponseHours", "value": "twelve"},
        ))
        self.assertCode("setting_not_supported", lambda: self.app.admin_action(
            self.admin, "setting", "update", {"id": "secretOverride", "value": True},
        ))
        map_setting = self.app.admin_action(self.admin, "setting", "update", {
            "id": "mapProvider", "value": "disabled", "reason": "Provider maintenance",
        })
        self.assertEqual({"key": "mapProvider", "value": "disabled"}, map_setting["result"])
        self.assertFalse(self.app.public_bootstrap()["capabilities"]["maps"]["available"])
        self.assertCode("invalid_setting_value", lambda: self.app.admin_action(
            self.admin, "setting", "update", {"id": "mapProvider", "value": "https://evil.invalid"},
        ))
        self.app.admin_action(self.admin, "setting", "update", {
            "id": "mapProvider", "value": "openstreetmap", "reason": "Provider restored",
        })
        map_capability = self.app.public_bootstrap()["capabilities"]["maps"]
        self.assertTrue(map_capability["available"])
        self.assertEqual(
            "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
            map_capability["tileUrlTemplate"],
        )
        self.assertEqual([[23.05, 57.95], [23.9, 59.3]], map_capability["muscatBounds"])
        overview = self.app.admin_overview(self.admin)
        self.assertEqual(12, overview["settings"]["merchantResponseHours"])

    def test_admin_overview_never_exposes_private_application_payload(self):
        application = self._review_ready_application("overview-private")
        support_overview = self.app.admin_overview(self.support)
        self.assertEqual([], support_overview["pendingApplications"])
        reviewer = self._admin("merchant_reviewer", "acct_overview_reviewer", "96897770009")
        reviewer_overview = self.app.admin_overview(reviewer)
        item = next(row for row in reviewer_overview["pendingApplications"] if row["id"] == application["applicationId"])
        self.assertNotIn("payload", item)
        self.assertNotIn("ownerContact", item)
        self.assertIn("merchant_name_en", item)

    def test_application_documents_must_be_reviewed_before_approval(self):
        application = self._review_ready_application("documents")
        reviewer = self._admin("merchant_reviewer", "acct_document_reviewer", "96897770010")
        with connect(immediate=True) as con:
            con.execute(
                "UPDATE merchant_documents SET review_status='pending',reviewed_by='',reviewed_at='' WHERE application_id=?",
                (application["applicationId"],),
            )
        self.assertCode("merchant_documents_review_required", lambda: self.app.admin_application_decision(
            reviewer, {"applicationId":application["applicationId"],"decision":"approve"},
        ))
        detail = self.app.admin_application_detail(reviewer, application["applicationId"])
        self.assertEqual(3, len(detail["documents"]))
        for document in detail["documents"]:
            decision = self.app.admin_application_document_decision(
                reviewer, application["applicationId"], document["id"],
                {"decision":"approve","note":"Verified"},
            )
            self.assertEqual("approved", decision["status"])
        approved = self.app.admin_application_decision(
            reviewer, {"applicationId":application["applicationId"],"decision":"approve"},
        )
        self.assertEqual("approved", approved["status"])

    def test_application_approval_rechecks_trial_and_is_atomic_and_idempotent(self):
        application = self._review_ready_application("trial")
        reviewer = self._admin("merchant_reviewer", "acct_trial_reviewer", "96897770006")
        result = self.app.admin_application_decision(reviewer, {
            "applicationId": application["applicationId"], "decision": "approve",
        })
        self.assertEqual("early_trial", result["planId"])
        self.assertEqual("active", result["subscriptionStatus"])
        self.assertTrue(result["trialGranted"])
        duplicate = self.app.admin_application_decision(reviewer, {
            "applicationId": application["applicationId"], "decision": "approve",
        })
        self.assertTrue(duplicate["duplicate"])
        with connect() as con:
            self.assertEqual(1, con.execute(
                "SELECT COUNT(*) n FROM merchant_subscriptions WHERE merchant_id=?",
                (application["merchantId"],),
            ).fetchone()["n"])
            branch = con.execute(
                "SELECT status,public_visible FROM store_branches WHERE id=?",
                (application["branchId"],),
            ).fetchone()
        self.assertEqual("approved", branch["status"])
        self.assertEqual(1, branch["public_visible"])

    def test_trial_manual_override_requires_plan_permission(self):
        application = self._review_ready_application("manual")
        reviewer = self._admin("merchant_reviewer", "acct_manual_reviewer", "96897770007")
        with connect(immediate=True) as con:
            con.execute(
                "UPDATE platform_settings SET value_json='false' WHERE key='trialEnabled'"
            )
        self.assertCode("trial_not_eligible", lambda: self.app.admin_application_decision(reviewer, {
            "applicationId": application["applicationId"], "decision": "approve",
        }))
        self.assertCode("admin_permission_required", lambda: self.app.admin_application_decision(reviewer, {
            "applicationId": application["applicationId"], "decision": "approve",
            "manualTrialGrant": True,
        }))
        result = self.app.admin_application_decision(self.admin, {
            "applicationId": application["applicationId"], "decision": "approve",
            "manualTrialGrant": True,
        })
        self.assertEqual("active", result["subscriptionStatus"])

    def test_paid_plan_approval_never_fakes_activation(self):
        application = self._review_ready_application("paid", "basic_3m")
        reviewer = self._admin("merchant_reviewer", "acct_paid_reviewer", "96897770008")
        result = self.app.admin_application_decision(reviewer, {
            "applicationId": application["applicationId"], "decision": "approve",
        })
        self.assertEqual("pending_payment", result["subscriptionStatus"])
        self.assertFalse(result["trialGranted"])
        merchant_actor = {
            "accountId": application["accountId"], "name": "Applicant",
            "role": "merchant_owner", "merchantId": application["merchantId"],
        }
        dashboard = self.app.merchant_dashboard(merchant_actor)
        self.assertEqual("pending_payment", dashboard["plan"]["subscription_status"])
        self.assertEqual(0, dashboard["plan"]["entitlements"]["products"])
        with connect() as con:
            branch = con.execute(
                "SELECT public_visible FROM store_branches WHERE id=?", (application["branchId"],)
            ).fetchone()
        self.assertEqual(0, branch["public_visible"])

    def test_api_contract_maps_only_existing_application_methods(self):
        required = {
            "GET /api/discovery", "GET /api/stores/{branchId}", "GET /api/products/{productId}",
            "POST /api/cart/items", "POST /api/checkout", "GET /api/orders/{orderId}",
            "GET /api/merchant/dashboard", "GET /api/admin/overview", "GET /api/notifications",
        }
        self.assertTrue(required.issubset(API_CONTRACTS))
        for contract in API_CONTRACTS.values():
            if "method" in contract:
                self.assertTrue(callable(getattr(self.app, contract["method"])))
            else:
                self.assertTrue(
                    str(contract.get("service", "")).startswith("BisaPushService.")
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)
