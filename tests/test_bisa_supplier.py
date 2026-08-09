import hashlib
import shutil
import tempfile
import unittest
from pathlib import Path

import bisa_config
import bisa_domain
from bisa_application import BisaApplication
from bisa_domain import DomainError, connect, init_db
from bisa_security import SecurityError, register_private_media
from bisa_supplier import SUPPLIER_API_CONTRACTS, SupplierAdvertiserMixin


class SupplierTestApplication(BisaApplication):
    pass


class SupplierAdvertiserTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="bisa-supplier-tests-"))
        self.original = {
            "config_data": bisa_config.DATA_DIR,
            "config_db": bisa_config.DB_PATH,
            "config_upload": bisa_config.UPLOAD_DIR,
            "config_backup": bisa_config.BACKUP_DIR,
            "domain_db": bisa_domain.DB_PATH,
            "domain_seed": bisa_domain.SEED_SAMPLE_DATA,
        }
        bisa_config.DATA_DIR = self.root
        bisa_config.DB_PATH = self.root / "supplier.sqlite3"
        bisa_config.UPLOAD_DIR = self.root / "uploads"
        bisa_config.BACKUP_DIR = self.root / "backups"
        bisa_domain.DB_PATH = bisa_config.DB_PATH
        bisa_domain.SEED_SAMPLE_DATA = False
        init_db()
        self.app = SupplierTestApplication()
        self.supplier = self._create_supplier("one")
        self.other_supplier = self._create_supplier("two")
        self.admin = self._create_account("admin", "96897770000", "admin", "")
        with connect(immediate=True) as con:
            con.execute(
                "INSERT INTO locations(id,parent_id,kind,name_ar,name_en,sort_order,active,created_at) VALUES('wil_muscat','','wilayah','مسقط','Muscat',1,1,?)",
                ("2026-01-01T00:00:00+00:00",),
            )
            con.execute(
                "INSERT INTO locations(id,parent_id,kind,name_ar,name_en,sort_order,active,created_at) VALUES('area_qurum','wil_muscat','area','القرم','Qurum',1,1,?)",
                ("2026-01-01T00:00:00+00:00",),
            )
            con.execute(
                """INSERT INTO product_categories(
                    id,name_ar,name_en,sort_order,active,created_at,updated_at)
                   VALUES('cat_stationery','قرطاسية','Stationery',1,1,?,?)""",
                ("2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
            )
        self.media = self._create_creative(self.supplier, "one")

    def tearDown(self):
        bisa_config.DATA_DIR = self.original["config_data"]
        bisa_config.DB_PATH = self.original["config_db"]
        bisa_config.UPLOAD_DIR = self.original["config_upload"]
        bisa_config.BACKUP_DIR = self.original["config_backup"]
        bisa_domain.DB_PATH = self.original["domain_db"]
        bisa_domain.SEED_SAMPLE_DATA = self.original["domain_seed"]
        shutil.rmtree(self.root, ignore_errors=True)

    @staticmethod
    def _phone_for(key):
        suffix = sum(ord(ch) for ch in key) % 10_000_000
        return f"9689{suffix:07d}"

    def _create_account(self, key, phone, role, tenant_id):
        account_id = f"acct_{key}"
        stamp = "2026-01-01T00:00:00+00:00"
        with connect(immediate=True) as con:
            con.execute(
                "INSERT INTO accounts(id,phone,name,pin_hash,status,created_at) VALUES(?,?,?,?, 'active',?)",
                (account_id, phone, key, "test-only-hash", stamp),
            )
            con.execute(
                "INSERT INTO account_roles(account_id,role,merchant_id,active) VALUES(?,?,?,1)",
                (account_id, role, tenant_id),
            )
        return {"accountId": account_id, "name": key, "role": role, "merchantId": tenant_id}

    def _create_supplier(self, key):
        supplier_id = f"supplier_{key}"
        actor = self._create_account(key, self._phone_for(key), "supplier_advertiser", supplier_id)
        stamp = "2026-01-01T00:00:00+00:00"
        with connect(immediate=True) as con:
            con.execute(
                """INSERT INTO suppliers(id,name_ar,name_en,status,created_at,updated_at)
                   VALUES(?,?,?,'approved',?,?)""",
                (supplier_id, f"مورد {key}", f"Supplier {key}", stamp, stamp),
            )
            con.execute(
                """INSERT INTO supplier_members(supplier_id,account_id,role,status,created_at)
                   VALUES(?,?,'supplier_advertiser','active',?)""",
                (supplier_id, actor["accountId"], stamp),
            )
        actor["supplierId"] = supplier_id
        actor["merchantId"] = ""
        return actor

    def _create_creative(self, actor, key, *, purpose="supplier_campaign_creative", mime="image/png"):
        blob = b"\x89PNG\r\n\x1a\n" + key.encode("ascii")
        path = bisa_config.UPLOAD_DIR / "private" / "supplier" / f"{key}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(blob)
        return register_private_media(
            actor,
            owner_kind="supplier",
            owner_id=actor["supplierId"],
            purpose=purpose,
            storage_key=f"private/supplier/{key}.png",
            mime_type=mime,
            byte_size=len(blob),
            sha256_hex=hashlib.sha256(blob).hexdigest(),
            original_name=f"{key}.png",
        )

    def _payload(self, key="create-1", **overrides):
        payload = {
            "idempotencyKey": key,
            "titleAr": "عرض القرطاسية للمكاتب",
            "titleEn": "Office stationery offer",
            "wholesaleDescriptionAr": "توريد قرطاسية بالجملة للمتاجر المعتمدة.",
            "wholesaleDescriptionEn": "Wholesale stationery for approved merchants.",
            "offerAr": "صندوق مختار للتجار",
            "offerEn": "Curated merchant case",
            "minimumOrderQuantity": 12,
            "targetCategories": ["cat_stationery"],
            "targetWilayats": ["wil_muscat"],
            "targetAreas": ["area_qurum"],
            "termsAr": "تخضع الكمية للتوفر عند تأكيد العرض.",
            "termsEn": "Quantity is subject to confirmation.",
            "contactMode": "quote_request",
            "contactLabelAr": "اطلب عرضاً",
            "contactLabelEn": "Request a quote",
            "creativeMediaId": self.media["id"],
            "startsAt": "2027-01-01T08:00:00+04:00",
            "endsAt": "2027-02-01T20:00:00+04:00",
        }
        payload.update(overrides)
        return payload

    def assert_error(self, code, callback, status=None):
        with self.assertRaises((DomainError, SecurityError)) as caught:
            callback()
        self.assertEqual(code, caught.exception.code)
        if status is not None:
            self.assertEqual(status, caught.exception.status)

    def test_supplier_contracts_are_explicit(self):
        expected = {
            "GET /api/supplier/dashboard",
            "GET /api/supplier/campaigns",
            "GET /api/supplier/campaigns/{campaignId}",
            "POST /api/supplier/campaigns",
            "PUT /api/supplier/campaigns/{campaignId}",
            "POST /api/supplier/campaigns/{campaignId}/submit",
            "GET /api/supplier/campaigns/{campaignId}/creative",
            "GET /api/supplier/leads",
        }
        self.assertTrue(expected.issubset(SUPPLIER_API_CONTRACTS))
        bootstrap = self.app.public_bootstrap(self.supplier)
        self.assertEqual("supplier_advertiser", bootstrap["actor"]["role"])
        self.assertEqual(self.supplier["supplierId"], bootstrap["actor"]["supplierId"])

    def test_create_update_idempotency_version_and_media_scope(self):
        created = self.app.save_supplier_campaign(self.supplier, self._payload())
        campaign = created["campaign"]
        self.assertEqual((campaign["status"], campaign["minimumOrderQuantity"]), ("draft", 12))
        self.assertTrue(campaign["completion"]["readyForReview"])
        self.assertNotIn("path", str(campaign).lower())
        replay = self.app.save_supplier_campaign(self.supplier, self._payload())
        self.assertEqual(campaign["id"], replay["campaign"]["id"])
        self.assertTrue(replay["duplicate"])
        self.assert_error(
            "idempotency_key_reused",
            lambda: self.app.save_supplier_campaign(self.supplier, self._payload(titleAr="عنوان مختلف")),
            409,
        )

        update = self._payload(
            "update-1", id=campaign["id"], expectedUpdatedAt=campaign["updatedAt"],
            titleAr="عرض محدث",
        )
        updated = self.app.save_supplier_campaign(self.supplier, update)["campaign"]
        self.assertEqual("عرض محدث", updated["titleAr"])
        self.assert_error(
            "supplier_campaign_version_conflict",
            lambda: self.app.save_supplier_campaign(
                self.supplier,
                self._payload("update-stale", id=campaign["id"], expectedUpdatedAt=campaign["updatedAt"]),
            ),
            409,
        )
        self.assert_error(
            "supplier_campaign_not_found",
            lambda: self.app.save_supplier_campaign(
                self.other_supplier,
                self._payload("other-edit", id=campaign["id"], expectedUpdatedAt=updated["updatedAt"]),
            ),
            404,
        )
        foreign_media = self._create_creative(self.other_supplier, "two")
        self.assert_error(
            "supplier_campaign_creative_not_found",
            lambda: self.app.save_supplier_campaign(
                self.supplier, self._payload("foreign-media", creativeMediaId=foreign_media["id"]),
            ),
            404,
        )
        with connect(immediate=True) as con:
            con.execute("UPDATE private_media_objects SET status='archived' WHERE id=?", (self.media["id"],))
        # A completed mutation stays replayable even if referenced media later
        # changes lifecycle state; no duplicate campaign is created.
        archived_replay = self.app.save_supplier_campaign(self.supplier, self._payload())
        self.assertEqual(campaign["id"], archived_replay["campaign"]["id"])
        self.assertTrue(archived_replay["duplicate"])

    def test_submit_requires_admin_review_and_approved_campaign_cannot_be_edited(self):
        campaign = self.app.save_supplier_campaign(self.supplier, self._payload("submit-create"))["campaign"]
        self.assert_error(
            "supplier_campaign_stage_not_allowed",
            lambda: self.app.admin_action(
                self.admin, "supplier_campaign", "approve",
                {"id": campaign["id"], "reason": "Drafts cannot bypass supplier submission"},
            ),
            409,
        )
        submitted_payload = {
            "idempotencyKey": "submit-1", "expectedUpdatedAt": campaign["updatedAt"],
        }
        submitted = self.app.submit_supplier_campaign(self.supplier, campaign["id"], submitted_payload)
        self.assertEqual("pending_review", submitted["campaign"]["status"])
        self.assertTrue(submitted["requiresAdminApproval"])
        with connect() as con:
            pending = con.execute(
                """SELECT * FROM notifications
                   WHERE target_kind='admin' AND target_id='admin' AND route=?""",
                (f"admin:supplier-campaign:{campaign['id']}",),
            ).fetchone()
            self.assertIsNotNone(pending)
            self.assertEqual((1, ""), (pending["requires_action"], pending["acted_at"]))
        replay = self.app.submit_supplier_campaign(self.supplier, campaign["id"], submitted_payload)
        self.assertTrue(replay["duplicate"])
        self.assert_error(
            "supplier_campaign_not_editable",
            lambda: self.app.save_supplier_campaign(
                self.supplier,
                self._payload(
                    "edit-pending", id=campaign["id"],
                    expectedUpdatedAt=submitted["campaign"]["updatedAt"],
                ),
            ),
            409,
        )
        reviewed = self.app.admin_action(
            self.admin, "supplier_campaign", "approve",
            {"id": campaign["id"], "reason": "Reviewed test creative and terms"},
        )
        self.assertEqual("approved", reviewed["result"]["status"])
        self.assert_error(
            "supplier_campaign_stage_not_allowed",
            lambda: self.app.admin_action(
                self.admin, "supplier_campaign", "reject",
                {"id": campaign["id"], "reason": "Approved campaigns cannot be re-reviewed"},
            ),
            409,
        )
        with connect() as con:
            resolved = con.execute("SELECT acted_at FROM notifications WHERE id=?", (pending["id"],)).fetchone()
            supplier_notice = con.execute(
                """SELECT * FROM notifications
                   WHERE target_kind='supplier' AND target_id=? AND route=?""",
                (self.supplier["supplierId"], f"supplier:campaign:{campaign['id']}"),
            ).fetchone()
            self.assertTrue(resolved["acted_at"])
            self.assertIsNotNone(supplier_notice)
            self.assertEqual(0, supplier_notice["requires_action"])
        bootstrap = self.app.public_bootstrap(self.supplier)
        self.assertIn(supplier_notice["id"], {item["id"] for item in bootstrap["notifications"]})
        detail = self.app.supplier_campaign_detail(self.supplier, campaign["id"])["campaign"]
        self.assertEqual("approved", detail["status"])
        creative = self.app.resolve_supplier_campaign_creative(self.supplier, campaign["id"])
        self.assertEqual(("image/png", self.media["byteSize"]), (creative["mimeType"], creative["byteSize"]))
        self.assertTrue(creative["path"].is_file())
        admin_detail = self.app.supplier_campaign_detail(self.admin, campaign["id"])["campaign"]
        admin_creative = self.app.resolve_supplier_campaign_creative(self.admin, campaign["id"])
        self.assertEqual("Office stationery offer", admin_detail["titleEn"])
        self.assertEqual(self.media["id"], admin_detail["creativeMediaId"])
        self.assertEqual(creative["etag"], admin_creative["etag"])

    def test_dual_role_notification_namespaces_do_not_leak_or_allow_idor(self):
        with connect(immediate=True) as con:
            con.execute(
                """INSERT INTO account_roles(account_id,role,merchant_id,active)
                   VALUES(?,'shopper','',1)""",
                (self.supplier["accountId"],),
            )
            shopper_notice = self.app._insert_notification(
                con, "account", self.supplier["accountId"],
                "طلبك جاهز", "Your order is ready", "تفاصيل الطلب", "Order details",
                "shopper:order:dual-role", False, "dual-role-shopper-notice",
            )
            supplier_notice = self.app._insert_notification(
                con, "supplier", self.supplier["supplierId"],
                "قرار حملة", "Campaign decision", "تفاصيل المورد", "Supplier details",
                "supplier:campaign:dual-role", False, "dual-role-supplier-notice",
            )
        shopper = {
            "accountId": self.supplier["accountId"], "name": self.supplier["name"],
            "role": "shopper", "merchantId": "",
        }

        self.assertEqual(
            {shopper_notice}, {item["id"] for item in self.app.notifications(shopper)},
        )
        self.assertEqual(
            {supplier_notice}, {item["id"] for item in self.app.notifications(self.supplier)},
        )
        self.assertEqual(
            {shopper_notice},
            {item["id"] for item in self.app.public_bootstrap(shopper)["notifications"]},
        )
        self.assertEqual(
            {supplier_notice},
            {item["id"] for item in self.app.public_bootstrap(self.supplier)["notifications"]},
        )

        self.assert_error(
            "notification_not_found",
            lambda: self.app.notification_action(shopper, supplier_notice, "read"),
            404,
        )
        self.assert_error(
            "notification_not_found",
            lambda: self.app.notification_action(self.supplier, shopper_notice, "read"),
            404,
        )
        self.assert_error(
            "notification_not_found",
            lambda: self.app.notification_action(self.other_supplier, supplier_notice, "read"),
            404,
        )
        self.assert_error(
            "notification_not_found",
            lambda: self.app.record_event(
                shopper, "action_prompt_opened", "notification", supplier_notice, {},
            ),
            404,
        )
        event = self.app.record_event(
            self.supplier, "action_prompt_opened", "notification", supplier_notice,
            {"source": "foreground"},
        )
        self.assertTrue(event["recorded"])
        self.assertEqual("read", self.app.notification_action(
            self.supplier, supplier_notice, "read",
        )["action"])

    def test_dashboard_and_leads_are_tenant_and_permission_scoped(self):
        own = self.app.save_supplier_campaign(self.supplier, self._payload("lead-own"))["campaign"]
        other_media = self._create_creative(self.other_supplier, "lead-other")
        other = self.app.save_supplier_campaign(
            self.other_supplier,
            self._payload("lead-other", creativeMediaId=other_media["id"]),
        )["campaign"]
        stamp = "2026-01-03T00:00:00+00:00"
        with connect(immediate=True) as con:
            for key in ("alpha", "beta"):
                account_id = f"merchant_owner_{key}"
                merchant_id = f"merchant_{key}"
                con.execute(
                    "INSERT INTO accounts(id,phone,name,pin_hash,status,created_at) VALUES(?,?,?,?, 'active',?)",
                    (account_id, self._phone_for(f"merchant-{key}"), key, "test", stamp),
                )
                con.execute(
                    """INSERT INTO merchants(
                        id,owner_account_id,name_ar,name_en,status,verified,created_at,updated_at,active)
                       VALUES(?,?,?,?, 'approved',1,?,?,1)""",
                    (merchant_id, account_id, f"متجر {key}", f"Store {key}", stamp, stamp),
                )
            con.execute(
                "INSERT INTO supplier_leads(id,campaign_id,merchant_id,action_kind,note,created_at) VALUES('lead_own',?,?, 'quote_request','Need 20 cases',?)",
                (own["id"], "merchant_alpha", stamp),
            )
            con.execute(
                "INSERT INTO supplier_leads(id,campaign_id,merchant_id,action_kind,note,created_at) VALUES('lead_other',?,?, 'contact','Private other lead',?)",
                (other["id"], "merchant_beta", stamp),
            )
        dashboard = self.app.supplier_dashboard(self.supplier)
        self.assertEqual(self.supplier["supplierId"], dashboard["supplier"]["id"])
        self.assertEqual(1, dashboard["summary"]["leads"])
        leads = self.app.supplier_leads(self.supplier)
        self.assertEqual(["lead_own"], [item["id"] for item in leads["leads"]])
        self.assertNotIn("phone", str(leads).lower())
        self.assert_error(
            "supplier_campaign_not_found",
            lambda: self.app.supplier_leads(self.supplier, {"campaignId": other["id"]}),
            404,
        )
        with connect(immediate=True) as con:
            con.execute(
                """INSERT INTO account_permission_overrides(account_id,permission,allowed,updated_at)
                   VALUES(?, 'supplier_lead.read',0,?)""",
                (self.supplier["accountId"], stamp),
            )
        self.assert_error("forbidden", lambda: self.app.supplier_leads(self.supplier), 403)

    def test_incomplete_and_unavailable_whatsapp_campaigns_do_not_submit(self):
        incomplete = self.app.save_supplier_campaign(
            self.supplier,
            {"idempotencyKey": "incomplete", "titleAr": "مسودة", "titleEn": "Draft"},
        )["campaign"]
        self.assertFalse(incomplete["completion"]["readyForReview"])
        self.assert_error(
            "supplier_campaign_incomplete",
            lambda: self.app.submit_supplier_campaign(
                self.supplier, incomplete["id"],
                {"idempotencyKey": "incomplete-submit", "expectedUpdatedAt": incomplete["updatedAt"]},
            ),
            422,
        )
        whatsapp = self.app.save_supplier_campaign(
            self.supplier,
            self._payload("whatsapp", contactMode="whatsapp", contactValue="96891234567"),
        )["campaign"]
        self.assert_error(
            "supplier_whatsapp_unavailable",
            lambda: self.app.submit_supplier_campaign(
                self.supplier, whatsapp["id"],
                {"idempotencyKey": "whatsapp-submit", "expectedUpdatedAt": whatsapp["updatedAt"]},
            ),
            409,
        )


if __name__ == "__main__":
    unittest.main()
