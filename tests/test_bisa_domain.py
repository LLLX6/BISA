import os
import shutil
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path


TEST_ROOT = Path(tempfile.mkdtemp(prefix="bisa-domain-tests-"))
os.environ["BISA_DATA_DIR"] = str(TEST_ROOT)
os.environ["BISA_DB_PATH"] = str(TEST_ROOT / "bisa-tests.sqlite3")
os.environ["BISA_UPLOAD_DIR"] = str(TEST_ROOT / "uploads")
os.environ["BISA_BACKUP_DIR"] = str(TEST_ROOT / "backups")
os.environ["BISA_SEED_SAMPLE_DATA"] = "true"
os.environ["BISA_DEMO_PIN"] = "1234"

import bisa_config  # noqa: E402
import bisa_domain  # noqa: E402
from bisa_domain import (  # noqa: E402
    BisaService, DomainError, authenticate, connect, init_db,
    register_or_login, seed_demo, validate_product_price, verify_or_register_account,
)


DB_PATH = TEST_ROOT / "bisa-tests.sqlite3"
ORIGINAL_PATHS = {
    "data": bisa_config.DATA_DIR,
    "db": bisa_config.DB_PATH,
    "upload": bisa_config.UPLOAD_DIR,
    "backup": bisa_config.BACKUP_DIR,
    "domain_db": bisa_domain.DB_PATH,
    "domain_seed": bisa_domain.SEED_SAMPLE_DATA,
}


class BisaDomainTests(unittest.TestCase):
    def test_demo_seed_requires_an_explicit_local_pin(self):
        current = os.environ.pop("BISA_DEMO_PIN", None)
        try:
            with self.assertRaisesRegex(RuntimeError, "BISA_DEMO_PIN"):
                seed_demo(None)
        finally:
            if current is not None:
                os.environ["BISA_DEMO_PIN"] = current

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
        self.service = BisaService()
        self.shopper = authenticate(register_or_login("96890000001", "1234", "Shopper", "shopper")["token"])
        self.merchant = authenticate(register_or_login("96892000003", "1234", "", "merchant_owner")["token"])

    def assertCode(self, code, callback):
        with self.assertRaises(DomainError) as caught:
            callback()
        self.assertEqual(caught.exception.code, code)

    def test_product_price_boundaries_are_server_enforced(self):
        self.assertEqual(validate_product_price("0.100"), 100)
        self.assertEqual(validate_product_price("2.000"), 2000)
        self.assertCode("product_price_out_of_range", lambda: validate_product_price("0.099"))
        self.assertCode("product_price_out_of_range", lambda: validate_product_price("2.001"))
        self.assertCode("price_precision_invalid", lambda: validate_product_price("1.0004"))

    def test_invite_only_mode_never_auto_claims_an_unverified_phone(self):
        previous = bisa_config.PHONE_VERIFICATION_MODE
        bisa_config.PHONE_VERIFICATION_MODE = "invite_only"
        try:
            self.assertCode(
                "phone_verification_required",
                lambda: verify_or_register_account("96891112222", "1234", "Unverified", "shopper"),
            )
        finally:
            bisa_config.PHONE_VERIFICATION_MODE = previous

    def test_database_rejects_cross_tenant_inventory_and_cart_rows(self):
        with connect(immediate=True) as con:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "product_branch_tenant_mismatch"):
                con.execute(
                    """INSERT INTO product_branch_inventory(
                        product_id,branch_id,stock_mode,quantity,availability,
                        last_stock_verified_at,stale_at,active,updated_at)
                       VALUES('demo_product_muscat_1','demo_branch_seeb','tracked',1,
                        'in_stock','','',1,'2026-08-09T00:00:00+00:00')"""
                )
            with self.assertRaisesRegex(sqlite3.IntegrityError, "cart_branch_tenant_mismatch"):
                con.execute(
                    """INSERT INTO carts(id,account_id,merchant_id,branch_id,status,version,updated_at)
                       VALUES('cross_cart',?,'demo_merchant_muscat','demo_branch_seeb',
                        'active',1,'2026-08-09T00:00:00+00:00')""",
                    (self.shopper["accountId"],),
                )

    def test_merchant_cannot_update_another_merchants_product_by_id(self):
        stamp = "2026-01-01T00:00:00+00:00"
        with connect(immediate=True) as con:
            con.execute("INSERT INTO accounts(id,phone,name,pin_hash,status,created_at) VALUES(?,?,?,?,?,?)",
                        ("acct_competitor", "96895555555", "Competitor", "hash", "active", stamp))
            con.execute("INSERT INTO merchants(id,owner_account_id,name_ar,name_en,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                        ("merchant_competitor", "acct_competitor", "منافس", "Competitor", "approved", stamp, stamp))
            con.execute("INSERT INTO store_branches(id,merchant_id,name_ar,name_en,wilayah_id,area_id,status,active,public_visible,created_at,updated_at) VALUES(?,?,?,?,?,?,?,1,1,?,?)",
                        ("branch_competitor", "merchant_competitor", "فرع منافس", "Competitor branch", "wilayat_seeb", "demo_area_seeb", "approved", stamp, stamp))
            con.execute("INSERT INTO merchant_subscriptions(id,merchant_id,plan_id,starts_at,ends_at,status,created_at) VALUES(?,?,?,?,?,'active',?)",
                        ("sub_competitor", "merchant_competitor", "basic_3m", stamp, "2099-01-01T00:00:00+00:00", stamp))
        actor = {"accountId":"acct_competitor","role":"merchant_owner","merchantId":"merchant_competitor","name":"Competitor"}
        payload = {"id":"demo_product_seeb_1","branchId":"branch_competitor","categoryId":"toys",
                   "nameAr":"تم الاستيلاء","nameEn":"Hijacked","price":"0.100","quantity":1}
        self.assertCode("product_not_owned", lambda: self.service.upsert_product(actor, payload))
        with connect() as con:
            name = con.execute("SELECT name_en FROM products WHERE id='demo_product_seeb_1'").fetchone()["name_en"]
            self.assertNotEqual(name, "Hijacked")

    def test_same_merchant_different_branch_requires_explicit_cart_replacement(self):
        self.service.add_cart(self.shopper, {"kind":"product","itemId":"demo_product_seeb_1","branchId":"demo_branch_seeb","quantity":1})
        stamp = "2026-01-01T00:00:00+00:00"
        with connect(immediate=True) as con:
            con.execute("INSERT INTO store_branches(id,merchant_id,name_ar,name_en,wilayah_id,area_id,status,active,public_visible,created_at,updated_at) VALUES(?,?,?,?,?,?,?,1,1,?,?)",
                        ("demo_branch_seeb_two", "demo_merchant_seeb", "فرع ثان", "Second branch", "wilayat_seeb", "demo_area_seeb", "approved", stamp, stamp))
            con.execute("INSERT INTO product_branch_inventory(product_id,branch_id,stock_mode,quantity,availability,last_stock_verified_at,stale_at,active,updated_at) VALUES(?,?,'tracked',5,'in_stock',?,'',1,?)",
                        ("demo_product_seeb_1", "demo_branch_seeb_two", stamp, stamp))
        payload = {"kind":"product","itemId":"demo_product_seeb_1","branchId":"demo_branch_seeb_two","quantity":1}
        self.assertCode("cross_store_cart_confirmation_required", lambda: self.service.add_cart(self.shopper, payload))
        replaced = self.service.add_cart(self.shopper, {**payload, "replaceCart": True})
        self.assertEqual(replaced["branch_id"], "demo_branch_seeb_two")

    def test_disabling_merchant_role_invalidates_existing_session(self):
        session = register_or_login("96892000003", "1234", "", "merchant_owner")
        self.assertIsNotNone(authenticate(session["token"]))
        with connect(immediate=True) as con:
            con.execute("UPDATE account_roles SET active=0 WHERE account_id='demo_account_seeb' AND role='merchant_owner'")
        self.assertIsNone(authenticate(session["token"]))

    def test_public_area_requires_approved_active_public_branch(self):
        with connect(immediate=True) as con:
            con.execute("INSERT INTO locations VALUES('area_hidden','wilayat_seeb','area','مخفية','Hidden',2,1,datetime('now'))")
        locations = self.service.public_bootstrap()["locations"]
        ids = {row["id"] for row in locations}
        self.assertIn("demo_area_seeb", ids)
        self.assertNotIn("area_hidden", ids)

    def test_bundle_total_may_exceed_two_omr_but_components_may_not(self):
        result = self.service.create_bundle(self.merchant, {
            "branchId": "demo_branch_seeb", "titleAr": "باقة اكتشاف", "titleEn": "Discovery bundle",
            "price": "3.500", "components": [
                {"productId": "demo_product_seeb_1", "quantity": 4},
                {"productId": "demo_product_seeb_2", "quantity": 4},
            ],
        })
        self.assertEqual(result["price"], "3.500")
        with self.assertRaises(sqlite3.IntegrityError):
            with connect(immediate=True) as con:
                con.execute("UPDATE products SET price_baisa=2001 WHERE id='demo_product_seeb_1'")
        with connect() as con:
            invalid = con.execute("SELECT COUNT(*) n FROM products WHERE price_baisa<100 OR price_baisa>2000").fetchone()["n"]
        self.assertEqual(invalid, 0)

    def test_single_store_cart_requires_explicit_replace(self):
        first = self.service.add_cart(self.shopper, {"kind":"product","itemId":"demo_product_seeb_1","branchId":"demo_branch_seeb","quantity":1})
        self.assertEqual(first["merchant_id"], "demo_merchant_seeb")
        with connect(immediate=True) as con:
            stamp = "2026-01-01T00:00:00+00:00"
            con.execute("INSERT INTO accounts(id,phone,name,pin_hash,status,created_at) VALUES('acct_second','96890000003','Second','hash','active',?)", (stamp,))
            con.execute("INSERT INTO merchants(id,owner_account_id,name_ar,name_en,status,created_at,updated_at) VALUES('merchant_second','acct_second','متجر ثان','Second Store','approved',?,?)", (stamp,stamp))
            con.execute("INSERT INTO store_branches(id,merchant_id,name_ar,name_en,wilayah_id,area_id,status,active,public_visible,created_at,updated_at) VALUES('branch_second','merchant_second','ثان','Second','wilayat_seeb','demo_area_seeb','approved',1,1,?,?)", (stamp,stamp))
            con.execute("INSERT INTO products(id,merchant_id,category_id,name_ar,name_en,price_baisa,status,active,created_at,updated_at) VALUES('prod_second','merchant_second','toys','لعبة','Toy',100,'approved',1,?,?)", (stamp,stamp))
            con.execute("""INSERT INTO product_branch_inventory
                (product_id,branch_id,stock_mode,quantity,availability,last_stock_verified_at,stale_at,active,updated_at)
                VALUES('prod_second','branch_second','tracked',5,'in_stock',?,'',1,?)""", (stamp,stamp))
        payload={"kind":"product","itemId":"prod_second","branchId":"branch_second","quantity":1}
        self.assertCode("cross_store_cart_confirmation_required", lambda: self.service.add_cart(self.shopper,payload))
        replaced=self.service.add_cart(self.shopper,{**payload,"replaceCart":True})
        self.assertEqual(replaced["merchant_id"],"merchant_second")
        self.assertEqual(len(replaced["items"]),1)

    def test_checkout_is_idempotent_and_payment_is_not_faked(self):
        self.service.add_cart(self.shopper,{"kind":"product","itemId":"demo_product_seeb_1","branchId":"demo_branch_seeb","quantity":2})
        payload={"idempotencyKey":"checkout-one","fulfillmentMode":"pickup"}
        first=self.service.checkout(self.shopper,payload)
        second=self.service.checkout(self.shopper,payload)
        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(first["order"]["status"],"pending_store_confirmation")
        self.assertFalse(self.service.public_bootstrap(self.shopper)["settings"]["paymentsEnabled"])
        with connect() as con:
            self.assertEqual(con.execute("SELECT COUNT(*) n FROM orders").fetchone()["n"],1)

    def test_concurrent_order_accept_decrements_stock_once(self):
        self.service.add_cart(self.shopper,{"kind":"product","itemId":"demo_product_seeb_1","branchId":"demo_branch_seeb","quantity":2})
        order_id=self.service.checkout(self.shopper,{"idempotencyKey":"race-one","fulfillmentMode":"pickup"})["order"]["id"]
        barrier=threading.Barrier(2); results=[]
        def decide():
            barrier.wait(); results.append(self.service.decide_order(self.merchant,order_id,"accept"))
        workers=[threading.Thread(target=decide) for _ in range(2)]
        for worker in workers: worker.start()
        for worker in workers: worker.join()
        self.assertEqual({result["status"] for result in results},{"accepted"})
        with connect() as con:
            quantity=con.execute("SELECT quantity FROM product_branch_inventory WHERE product_id='demo_product_seeb_1' AND branch_id='demo_branch_seeb'").fetchone()["quantity"]
            self.assertEqual(quantity,23)

    def test_competing_checkouts_cannot_over_reserve_stock(self):
        second=authenticate(register_or_login("96894444444","1234","Second shopper","shopper")["token"])
        self.service.add_cart(self.shopper,{"kind":"product","itemId":"demo_product_seeb_1","branchId":"demo_branch_seeb","quantity":20})
        self.service.add_cart(second,{"kind":"product","itemId":"demo_product_seeb_1","branchId":"demo_branch_seeb","quantity":20})
        barrier=threading.Barrier(2); results=[]
        def checkout(actor,key):
            barrier.wait()
            try: results.append(("ok",self.service.checkout(actor,{"idempotencyKey":key,"fulfillmentMode":"pickup"})))
            except DomainError as error: results.append(("error",error.code))
        workers=[threading.Thread(target=checkout,args=(self.shopper,"reserve-a")),threading.Thread(target=checkout,args=(second,"reserve-b"))]
        for worker in workers: worker.start()
        for worker in workers: worker.join()
        self.assertEqual(sorted(kind for kind,_ in results),["error","ok"])
        self.assertEqual([value for kind,value in results if kind=="error"],["stock_unavailable"])
        with connect() as con:
            self.assertEqual(con.execute("SELECT SUM(quantity) n FROM inventory_reservations WHERE status='pending'").fetchone()["n"],20)

    def test_merchant_plan_product_limit_is_enforced(self):
        with connect(immediate=True) as con:
            con.execute("UPDATE subscription_plans SET entitlements=? WHERE id='advanced_3m'", ('{"products":4,"branches":5,"staff":6,"bundles":25}',))
        payload={"branchId":"demo_branch_seeb","categoryId":"toys","nameAr":"منتج إضافي","nameEn":"Extra","price":"0.100","quantity":1}
        self.assertCode("plan_product_limit",lambda:self.service.upsert_product(self.merchant,payload))

    def test_supplier_hub_is_merchant_only(self):
        self.assertCode("forbidden",lambda:self.service.supplier_campaigns(self.shopper))
        self.assertEqual(self.service.supplier_campaigns(self.merchant),[])

    def test_merchant_application_stays_private_and_whatsapp_is_honest(self):
        new_actor=authenticate(register_or_login("96891111111","1234","New owner","shopper")["token"])
        result=self.service.merchant_apply(new_actor,{"nameAr":"متجر جديد","nameEn":"New Store","wilayahId":"wilayat_seeb","areaId":"demo_area_seeb"})
        self.assertFalse(result["whatsappSent"])
        public_ids={store["merchant_id"] for store in self.service.public_bootstrap()["stores"]}
        self.assertNotIn(result["merchantId"],public_ids)

    def test_admin_approval_activates_trial_and_merchant_role_atomically(self):
        new_phone="96892222222"
        new_actor=authenticate(register_or_login(new_phone,"1234","Approved owner","shopper")["token"])
        application=self.service.merchant_apply(new_actor,{"nameAr":"متجر يعتمد","nameEn":"Approved Store","wilayahId":"wilayat_seeb","areaId":"demo_area_seeb"})
        with connect(immediate=True) as con:
            admin_id="acct_admin_test"
            con.execute("INSERT INTO accounts(id,phone,name,pin_hash,status,created_at) VALUES(?,?,?,?,?,?)",(admin_id,"96893333333","Admin","hash","active","2026-01-01T00:00:00+00:00"))
            con.execute("INSERT INTO account_roles VALUES(?,?, '',1)",(admin_id,"admin"))
        admin={"accountId":admin_id,"role":"admin","merchantId":"","name":"Admin"}
        result=self.service.admin_decide_application(admin,{"applicationId":application["id"],"decision":"approve"})
        self.assertEqual(result["status"],"approved")
        merchant_login=register_or_login(new_phone,"1234","","merchant_owner")
        dashboard=self.service.merchant_dashboard(authenticate(merchant_login["token"]))
        self.assertEqual(dashboard["plan"]["id"],"early_trial")
        self.assertEqual(dashboard["merchant"]["status"],"approved")

    def test_demo_catalog_covers_every_muscat_wilayat(self):
        bootstrap = self.service.public_bootstrap()
        self.assertEqual(len(bootstrap["stores"]), 6)
        self.assertEqual(len(bootstrap["products"]), 24)
        self.assertEqual(len(bootstrap["bundles"]), 6)
        self.assertEqual(len(bootstrap["advertisements"]), 6)
        areas = {row["area_id"] for row in bootstrap["stores"]}
        self.assertEqual(areas, {
            "demo_area_muscat", "demo_area_muttrah", "demo_area_bawshar",
            "demo_area_seeb", "demo_area_al_amerat", "demo_area_qurayyat",
        })
        self.assertTrue(bootstrap["demoMode"])
        self.assertEqual(bootstrap["demoCounts"]["product"], 24)

    def test_admin_can_purge_only_tagged_demo_data_with_exact_confirmation(self):
        with connect(immediate=True) as con:
            stamp = "2026-01-01T00:00:00+00:00"
            con.execute("INSERT INTO accounts(id,phone,name,pin_hash,status,created_at) VALUES(?,?,?,?,?,?)",("acct_demo_purge_admin","96898888888","Purge Admin","hash","active",stamp))
            con.execute("INSERT INTO account_roles VALUES(?,?, '',1)",("acct_demo_purge_admin","admin"))
        admin={"accountId":"acct_demo_purge_admin","role":"admin","merchantId":"","name":"Purge Admin"}
        self.assertCode("demo_delete_confirmation_required",lambda:self.service.purge_demo_data(admin,"DELETE DEMO"))
        result=self.service.purge_demo_data(admin,"DELETE BISA DEMO")
        self.assertFalse(result["duplicate"])
        self.assertGreater(result["deleted"], 0)
        bootstrap=self.service.public_bootstrap()
        self.assertEqual((bootstrap["stores"],bootstrap["products"],bootstrap["bundles"],bootstrap["advertisements"]),([],[],[],[]))
        with connect() as con:
            self.assertEqual(con.execute("SELECT COUNT(*) n FROM demo_records").fetchone()["n"],0)
            self.assertEqual(con.execute("SELECT COUNT(*) n FROM accounts WHERE id='acct_demo_purge_admin'").fetchone()["n"],1)
            self.assertEqual(con.execute("SELECT COUNT(*) n FROM admin_audit_logs WHERE action='demo_data_purged'").fetchone()["n"],1)
        self.assertTrue(self.service.purge_demo_data(admin,"DELETE BISA DEMO")["duplicate"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
