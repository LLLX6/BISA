import os
import shutil
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("BISA_DEMO_PIN", "1234")

import bisa_config
import bisa_domain
from bisa_application import BisaApplication
from bisa_domain import DomainError, authenticate, connect, init_db, new_id, now_iso, register_or_login


class BisaOperationsTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="bisa-operations-tests-"))
        self.original = {
            "config_data": bisa_config.DATA_DIR,
            "config_db": bisa_config.DB_PATH,
            "config_upload": bisa_config.UPLOAD_DIR,
            "config_backup": bisa_config.BACKUP_DIR,
            "domain_db": bisa_domain.DB_PATH,
            "domain_seed": bisa_domain.SEED_SAMPLE_DATA,
        }
        bisa_config.DATA_DIR = self.root
        bisa_config.DB_PATH = self.root / "operations.sqlite3"
        bisa_config.UPLOAD_DIR = self.root / "uploads"
        bisa_config.BACKUP_DIR = self.root / "backups"
        bisa_domain.DB_PATH = bisa_config.DB_PATH
        bisa_domain.SEED_SAMPLE_DATA = True
        init_db()
        self.app = BisaApplication()
        self.shopper = authenticate(register_or_login("96890000001", "1234", "", "shopper")["token"])
        self.other = authenticate(register_or_login("96890000002", "1234", "", "shopper")["token"])
        self.merchant = authenticate(register_or_login("96892000003", "1234", "", "merchant_owner")["token"])

    def tearDown(self):
        bisa_config.DATA_DIR = self.original["config_data"]
        bisa_config.DB_PATH = self.original["config_db"]
        bisa_config.UPLOAD_DIR = self.original["config_upload"]
        bisa_config.BACKUP_DIR = self.original["config_backup"]
        bisa_domain.DB_PATH = self.original["domain_db"]
        bisa_domain.SEED_SAMPLE_DATA = self.original["domain_seed"]
        shutil.rmtree(self.root, ignore_errors=True)

    def assertCode(self, code, callback):
        with self.assertRaises(DomainError) as caught:
            callback()
        self.assertEqual(code, caught.exception.code)

    def _save(self, application_id, step, data):
        return self.app.merchant_onboarding(self.shopper, {
            "action": "save_draft", "applicationId": application_id,
            "step": step, "data": data,
        })

    def _complete_application(self):
        started = self.app.merchant_onboarding(self.shopper, {"action": "start"})
        app_id = started["application"]["id"]
        merchant_id = started["application"]["merchantId"]
        stamp = now_iso()
        media = {}
        with connect(immediate=True) as con:
            for purpose in ("logo", "storefront", "commercial_registration", "license"):
                media_id = new_id("media")
                media[purpose] = media_id
                con.execute(
                    """INSERT INTO private_media_objects(
                        id,owner_kind,owner_id,purpose,storage_key,mime_type,byte_size,sha256_hex,
                        original_name,status,created_by,created_at,updated_at)
                       VALUES(?,'merchant_application',?,?,?,?,1,?,'document.webp','active',?,?,?)""",
                    (media_id, app_id, purpose, f"private/merchant_application/{media_id}.webp",
                     "image/webp", "0" * 64, self.shopper["accountId"], stamp, stamp),
                )
        steps = {
            "owner": {"contactName":"Owner","contactPhone":"96890000001","authorizedRole":"Owner"},
            "business": {"nameAr":"متجر الاختبار","nameEn":"Test shop","merchantType":"store","commercialRegistration":"1234567"},
            "brand": {"logoMediaId":media["logo"]},
            "location": {"branchNameAr":"فرع السيب","branchNameEn":"Seeb branch","wilayahId":"wilayat_seeb","areaId":"demo_area_seeb","addressText":"Seeb","latitude":23.60,"longitude":58.20},
            "hours": {"hours":{"sun":[{"open":"09:00","close":"21:00"}]}},
            "documents": {"documents":[
                {"kind":"storefront","mediaId":media["storefront"]},
                {"kind":"commercial_registration","mediaId":media["commercial_registration"]},
                {"kind":"license","mediaId":media["license"]},
            ]},
            "fulfillment": {"pickup":{"enabled":True},"office":{"enabled":True,"feeBaisa":1000,"minimumBaisa":2000,"freeThresholdBaisa":5000},"home":{"enabled":False},"zones":[{"mode":"office_delivery","wilayahId":"wilayat_seeb","areaId":"demo_area_seeb","feeBaisa":1000,"minimumBaisa":2000,"freeThresholdBaisa":5000,"eta":"60 min"}]},
            "policy": {"returnWindowDays":7,"exchangeWindowDays":14,"conditions":"Unused products","receiptRequired":True,"contactMethod":"Support"},
            "categories": {"categoryIds":["storage","stationery"]},
            "plan": {"planId":"early_trial"},
            "review": {"acceptedPolicies":True},
        }
        for step, data in steps.items():
            self._save(app_id, step, data)
        submitted = self.app.merchant_onboarding(self.shopper, {"action":"submit","applicationId":app_id})
        return app_id, merchant_id, submitted

    def test_onboarding_persists_validated_steps_private_media_and_delivery_zones(self):
        app_id, merchant_id, submitted = self._complete_application()
        self.assertEqual("submitted", submitted["application"]["status"])
        self.assertIsNone(submitted["nextStep"])
        with connect() as con:
            merchant = con.execute("SELECT * FROM merchants WHERE id=?", (merchant_id,)).fetchone()
            self.assertEqual((merchant["logo_path"], merchant["cover_path"]), ("", ""))
            zone = con.execute(
                "SELECT * FROM branch_delivery_zones WHERE branch_id=(SELECT id FROM store_branches WHERE merchant_id=?)",
                (merchant_id,),
            ).fetchone()
            self.assertEqual((zone["mode"], zone["fee_baisa"]), ("office_delivery", 1000))
            self.assertEqual(
                3, con.execute("SELECT COUNT(*) n FROM merchant_documents WHERE application_id=?", (app_id,)).fetchone()["n"],
            )
            attempt = con.execute(
                "SELECT status FROM external_action_attempts WHERE target_id=?", ("admin",)
            ).fetchone()
            self.assertEqual("unavailable", attempt["status"])

    def test_onboarding_rejects_foreign_media_and_resubmit_versions_policy(self):
        started = self.app.merchant_onboarding(self.shopper, {"action":"start"})
        self.assertCode("merchant_brand_media_not_found", lambda: self._save(
            started["application"]["id"], "brand", {"logoMediaId":"media_foreign"},
        ))
        app_id, merchant_id, _ = self._complete_application()
        with connect(immediate=True) as con:
            con.execute("UPDATE merchant_applications SET status='changes_requested' WHERE id=?", (app_id,))
            con.execute("UPDATE merchants SET status='changes_requested' WHERE id=?", (merchant_id,))
        self._save(app_id, "policy", {"returnWindowDays":10,"exchangeWindowDays":14,"conditions":"Updated","contactMethod":"Support"})
        self.app.merchant_onboarding(self.shopper, {"action":"submit","applicationId":app_id})
        with connect() as con:
            versions = [row["version"] for row in con.execute(
                "SELECT version FROM merchant_return_policies WHERE merchant_id=? ORDER BY version", (merchant_id,),
            )]
        self.assertEqual([1, 2], versions)

    def test_cart_quantity_address_scope_product_action_and_campaign_draft(self):
        cart = self.app.add_cart(self.shopper, {
            "kind":"product","itemId":"demo_product_seeb_1","branchId":"demo_branch_seeb","quantity":1,
        })
        updated = self.app.update_cart_item(self.shopper, "product", "demo_product_seeb_1", {
            "quantity":4,"expectedVersion":cart["version"],
        })
        self.assertEqual(4, updated["items"][0]["quantity"])
        emptied = self.app.update_cart_item(self.shopper, "product", "demo_product_seeb_1", {
            "quantity":0,"expectedVersion":updated["version"],
        })
        self.assertEqual([], emptied["items"])

        address = self.app.save_address(self.shopper, {
            "addressType":"office","label":"Office","wilayahId":"wilayat_seeb",
            "areaId":"demo_area_seeb","addressText":"Knowledge Oasis",
        })
        self.assertEqual("office", address["address_type"])
        self.assertCode("address_not_found", lambda: self.app.save_address(self.other, {
            "id":address["id"],"addressType":"home","wilayahId":"wilayat_seeb",
            "areaId":"demo_area_seeb","addressText":"Other",
        }))

        result = self.app.product_action(self.merchant, "demo_product_seeb_1", {"action":"pause"})
        self.assertEqual("updated", result["status"])
        self.app.product_action(self.merchant, "demo_product_seeb_1", {"action":"resume"})
        campaign_payload = {
            "action":"create_campaign","idempotencyKey":"campaign-test-1","payload":{
                "idempotencyKey":"campaign-test-1",
                "placement":"home_inline","landingKind":"product","landingId":"demo_product_seeb_1",
                "titleAr":"عرض","titleEn":"Offer",
            },
        }
        campaign = self.app.merchant_campaign_action(self.merchant, campaign_payload)
        self.assertEqual(("draft", True, "not_started"), (
            campaign["status"], campaign["requiresAdminApproval"], campaign["paymentStatus"],
        ))
        replay = self.app.merchant_campaign_action(self.merchant, campaign_payload)
        self.assertEqual(campaign["id"], replay["id"])
        self.assertTrue(replay["duplicate"])
        with connect() as con:
            operation = con.execute(
                "SELECT operation FROM idempotency_records WHERE idempotency_key='campaign-test-1'"
            ).fetchone()["operation"]
        self.assertEqual("campaign_create:demo_merchant_seeb", operation)


if __name__ == "__main__":
    unittest.main()
