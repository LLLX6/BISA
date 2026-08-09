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

from bisa_config import DB_PATH  # noqa: E402
from bisa_domain import (  # noqa: E402
    BisaService, DomainError, authenticate, connect, init_db,
    register_or_login, validate_product_price,
)


class BisaDomainTests(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(TEST_ROOT, ignore_errors=True)

    def setUp(self):
        for suffix in ("", "-wal", "-shm"):
            try:
                Path(str(DB_PATH) + suffix).unlink()
            except FileNotFoundError:
                pass
        init_db()
        self.service = BisaService()
        self.shopper = authenticate(register_or_login("96890000001", "1234", "Shopper", "shopper")["token"])
        self.merchant = authenticate(register_or_login("96890000002", "1234", "", "merchant_owner")["token"])

    def assertCode(self, code, callback):
        with self.assertRaises(DomainError) as caught:
            callback()
        self.assertEqual(caught.exception.code, code)

    def test_product_price_boundaries_are_server_enforced(self):
        self.assertEqual(validate_product_price("0.100"), 100)
        self.assertEqual(validate_product_price("2.000"), 2000)
        self.assertCode("product_price_out_of_range", lambda: validate_product_price("0.099"))
        self.assertCode("product_price_out_of_range", lambda: validate_product_price("2.001"))

    def test_public_area_requires_approved_active_public_branch(self):
        with connect(immediate=True) as con:
            con.execute("INSERT INTO locations VALUES('area_hidden','wilayat_seeb','area','مخفية','Hidden',2,1,datetime('now'))")
        locations = self.service.public_bootstrap()["locations"]
        ids = {row["id"] for row in locations}
        self.assertIn("area_mawaleh", ids)
        self.assertNotIn("area_hidden", ids)

    def test_bundle_total_may_exceed_two_omr_but_components_may_not(self):
        result = self.service.create_bundle(self.merchant, {
            "branchId": "branch_demo", "titleAr": "باقة اكتشاف", "titleEn": "Discovery bundle",
            "price": "3.500", "components": [
                {"productId": "prod_cups", "quantity": 4},
                {"productId": "prod_notebook", "quantity": 4},
            ],
        })
        self.assertEqual(result["price"], "3.500")
        with self.assertRaises(sqlite3.IntegrityError):
            with connect(immediate=True) as con:
                con.execute("UPDATE products SET price_baisa=2001 WHERE id='prod_cups'")
        with connect() as con:
            invalid = con.execute("SELECT COUNT(*) n FROM products WHERE price_baisa<100 OR price_baisa>2000").fetchone()["n"]
        self.assertEqual(invalid, 0)

    def test_single_store_cart_requires_explicit_replace(self):
        first = self.service.add_cart(self.shopper, {"kind":"product","itemId":"prod_clean","branchId":"branch_demo","quantity":1})
        self.assertEqual(first["merchant_id"], "merchant_demo")
        with connect(immediate=True) as con:
            stamp = "2026-01-01T00:00:00+00:00"
            con.execute("INSERT INTO accounts VALUES('acct_second','96890000003','Second','hash','active',?)", (stamp,))
            con.execute("INSERT INTO merchants(id,owner_account_id,name_ar,name_en,status,created_at,updated_at) VALUES('merchant_second','acct_second','متجر ثان','Second Store','approved',?,?)", (stamp,stamp))
            con.execute("INSERT INTO store_branches(id,merchant_id,name_ar,name_en,wilayah_id,area_id,status,active,public_visible,created_at,updated_at) VALUES('branch_second','merchant_second','ثان','Second','wilayat_seeb','area_mawaleh','approved',1,1,?,?)", (stamp,stamp))
            con.execute("INSERT INTO products(id,merchant_id,category_id,name_ar,name_en,price_baisa,status,active,created_at,updated_at) VALUES('prod_second','merchant_second','toys','لعبة','Toy',100,'approved',1,?,?)", (stamp,stamp))
            con.execute("INSERT INTO product_branch_inventory VALUES('prod_second','branch_second','tracked',5,'in_stock',?,'',1,?)", (stamp,stamp))
        payload={"kind":"product","itemId":"prod_second","branchId":"branch_second","quantity":1}
        self.assertCode("cross_store_cart_confirmation_required", lambda: self.service.add_cart(self.shopper,payload))
        replaced=self.service.add_cart(self.shopper,{**payload,"replaceCart":True})
        self.assertEqual(replaced["merchant_id"],"merchant_second")
        self.assertEqual(len(replaced["items"]),1)

    def test_checkout_is_idempotent_and_payment_is_not_faked(self):
        self.service.add_cart(self.shopper,{"kind":"product","itemId":"prod_clean","branchId":"branch_demo","quantity":2})
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
        self.service.add_cart(self.shopper,{"kind":"product","itemId":"prod_clean","branchId":"branch_demo","quantity":2})
        order_id=self.service.checkout(self.shopper,{"idempotencyKey":"race-one","fulfillmentMode":"pickup"})["order"]["id"]
        barrier=threading.Barrier(2); results=[]
        def decide():
            barrier.wait(); results.append(self.service.decide_order(self.merchant,order_id,"accept"))
        workers=[threading.Thread(target=decide) for _ in range(2)]
        for worker in workers: worker.start()
        for worker in workers: worker.join()
        self.assertEqual({result["status"] for result in results},{"accepted"})
        with connect() as con:
            quantity=con.execute("SELECT quantity FROM product_branch_inventory WHERE product_id='prod_clean' AND branch_id='branch_demo'").fetchone()["quantity"]
            self.assertEqual(quantity,23)

    def test_competing_checkouts_cannot_over_reserve_stock(self):
        second=authenticate(register_or_login("96894444444","1234","Second shopper","shopper")["token"])
        self.service.add_cart(self.shopper,{"kind":"product","itemId":"prod_clean","branchId":"branch_demo","quantity":20})
        self.service.add_cart(second,{"kind":"product","itemId":"prod_clean","branchId":"branch_demo","quantity":20})
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
            con.execute("UPDATE subscription_plans SET entitlements=? WHERE id='advanced_3m'", ('{"products":6,"branches":5,"staff":6,"bundles":25}',))
        payload={"branchId":"branch_demo","categoryId":"toys","nameAr":"منتج إضافي","nameEn":"Extra","price":"0.100","quantity":1}
        self.assertCode("plan_product_limit",lambda:self.service.upsert_product(self.merchant,payload))

    def test_supplier_hub_is_merchant_only(self):
        self.assertCode("forbidden",lambda:self.service.supplier_campaigns(self.shopper))
        self.assertEqual(self.service.supplier_campaigns(self.merchant),[])

    def test_merchant_application_stays_private_and_whatsapp_is_honest(self):
        new_actor=authenticate(register_or_login("96891111111","1234","New owner","shopper")["token"])
        result=self.service.merchant_apply(new_actor,{"nameAr":"متجر جديد","nameEn":"New Store","wilayahId":"wilayat_seeb","areaId":"area_mawaleh"})
        self.assertFalse(result["whatsappSent"])
        public_ids={store["merchant_id"] for store in self.service.public_bootstrap()["stores"]}
        self.assertNotIn(result["merchantId"],public_ids)

    def test_admin_approval_activates_trial_and_merchant_role_atomically(self):
        new_phone="96892222222"
        new_actor=authenticate(register_or_login(new_phone,"1234","Approved owner","shopper")["token"])
        application=self.service.merchant_apply(new_actor,{"nameAr":"متجر يعتمد","nameEn":"Approved Store","wilayahId":"wilayat_seeb","areaId":"area_mawaleh"})
        with connect(immediate=True) as con:
            admin_id="acct_admin_test"
            con.execute("INSERT INTO accounts VALUES(?,?,?,?,?,?)",(admin_id,"96893333333","Admin","hash","active","2026-01-01T00:00:00+00:00"))
            con.execute("INSERT INTO account_roles VALUES(?,?, '',1)",(admin_id,"admin"))
        admin={"accountId":admin_id,"role":"admin","merchantId":"","name":"Admin"}
        result=self.service.admin_decide_application(admin,{"applicationId":application["id"],"decision":"approve"})
        self.assertEqual(result["status"],"approved")
        merchant_login=register_or_login(new_phone,"1234","","merchant_owner")
        dashboard=self.service.merchant_dashboard(authenticate(merchant_login["token"]))
        self.assertEqual(dashboard["plan"]["id"],"early_trial")
        self.assertEqual(dashboard["merchant"]["status"],"approved")


if __name__ == "__main__":
    unittest.main(verbosity=2)
