import hashlib
import os
import shutil
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

os.environ.setdefault("BISA_DEMO_PIN", "1234")

import bisa_config
import bisa_domain
from bisa_application import BisaApplication
from bisa_domain import DomainError, connect, dumps, hash_secret, init_db, now_iso
from bisa_moderation import ModerationReviewMixin


class ModeratedApplication(BisaApplication):
    """Use the production composition, which already includes the mixin."""

    pass


class BisaModerationReviewTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="bisa-moderation-tests-"))
        self.original = {
            "data": bisa_config.DATA_DIR,
            "db": bisa_config.DB_PATH,
            "upload": bisa_config.UPLOAD_DIR,
            "backup": bisa_config.BACKUP_DIR,
            "domain_db": bisa_domain.DB_PATH,
            "domain_seed": bisa_domain.SEED_SAMPLE_DATA,
        }
        bisa_config.DATA_DIR = self.root
        bisa_config.DB_PATH = self.root / "moderation.sqlite3"
        bisa_config.UPLOAD_DIR = self.root / "uploads"
        bisa_config.BACKUP_DIR = self.root / "backups"
        bisa_domain.DB_PATH = bisa_config.DB_PATH
        bisa_domain.SEED_SAMPLE_DATA = True
        init_db()
        self.app = ModeratedApplication()
        self.catalog = self._admin("catalog_moderator", "catalog_admin", "96898880001")
        self.catalog_other = self._admin("catalog_moderator", "catalog_admin_other", "96898880002")
        self.advertising = self._admin("advertising_manager", "ad_admin", "96898880003")
        self.support = self._admin("support_admin", "support_admin", "96898880004")

    def tearDown(self):
        bisa_config.DATA_DIR = self.original["data"]
        bisa_config.DB_PATH = self.original["db"]
        bisa_config.UPLOAD_DIR = self.original["upload"]
        bisa_config.BACKUP_DIR = self.original["backup"]
        bisa_domain.DB_PATH = self.original["domain_db"]
        bisa_domain.SEED_SAMPLE_DATA = self.original["domain_seed"]
        shutil.rmtree(self.root, ignore_errors=True)

    def _admin(self, role, account_id, phone):
        stamp = now_iso()
        with connect(immediate=True) as con:
            con.execute(
                """INSERT INTO accounts(id,phone,name,pin_hash,status,created_at)
                   VALUES(?,?,?,?,? ,?)""",
                (account_id, phone, role, hash_secret("1234"), "active", stamp),
            )
            con.execute(
                "INSERT INTO account_roles(account_id,role,merchant_id,active) VALUES(?,?,?,1)",
                (account_id, role, ""),
            )
        return {"accountId": account_id, "name": role, "role": role, "merchantId": ""}

    def assertCode(self, expected, callback):
        with self.assertRaises(DomainError) as caught:
            callback()
        self.assertEqual(expected, caught.exception.code)
        return caught.exception

    def _private_image(self, media_id, merchant_id, purpose="product_image"):
        blob = b"RIFF\x04\x00\x00\x00WEBP"
        relative = f"private/merchant/{media_id}.webp"
        absolute = bisa_config.UPLOAD_DIR / relative
        absolute.parent.mkdir(parents=True, exist_ok=True)
        absolute.write_bytes(blob)
        stamp = now_iso()
        with connect(immediate=True) as con:
            con.execute(
                """INSERT INTO private_media_objects(
                       id,owner_kind,owner_id,purpose,storage_key,mime_type,byte_size,sha256_hex,
                       original_name,status,created_by,created_at,updated_at)
                   VALUES(?,'merchant',?,?,?,'image/webp',?,?,?,'active',?,?,?)""",
                (
                    media_id, merchant_id, purpose, relative, len(blob),
                    hashlib.sha256(blob).hexdigest(), f"{media_id}.webp",
                    "fixture", stamp, stamp,
                ),
            )
        return absolute

    def _pending_product(self, product_id="demo_product_seeb_1", media_id="media_seeb_review"):
        self._private_image(media_id, "demo_merchant_seeb")
        stamp = now_iso()
        with connect(immediate=True) as con:
            con.execute(
                """UPDATE products SET status='pending_review',moderation_status='pending',updated_at=?
                   WHERE id=?""",
                (stamp, product_id),
            )
            con.execute(
                """INSERT INTO product_media(
                       id,product_id,private_path,thumbnail_path,mime_type,width,height,
                       sort_order,status,created_at)
                   VALUES(?,? ,?,'','image/webp',512,512,0,'active',?)""",
                (media_id, product_id, f"media:{media_id}", stamp),
            )
        return product_id, media_id

    def _campaign(self, landing_kind, landing_id, suffix):
        campaign_id = f"campaign_review_{suffix}"
        stamp = now_iso()
        target = {
            "titleAr": f"إعلان {suffix}", "titleEn": f"{suffix} ad",
            "creativeMediaId": "", "wilayatIds": ["wilayat_seeb"],
            "areaIds": ["demo_area_seeb"], "categoryIds": ["storage"],
            "language": "all", "paymentStatus": "paid",
        }
        with connect(immediate=True) as con:
            con.execute(
                """INSERT INTO ad_campaigns(
                       id,owner_kind,owner_id,placement,target_json,landing_kind,landing_id,
                       label_ar,label_en,status,starts_at,ends_at,frequency_cap,created_at,updated_at)
                   VALUES(?,'merchant','demo_merchant_seeb','home_inline',?,?,?,
                          'إعلان','Sponsored','draft','','',3,?,?)""",
                (campaign_id, dumps(target), landing_kind, landing_id, stamp, stamp),
            )
        return campaign_id

    @staticmethod
    def _assert_no_path_fields(testcase, value):
        forbidden = {"path", "storage_key", "storageKey", "private_path", "privatePath", "thumbnail_path"}
        if isinstance(value, dict):
            testcase.assertTrue(forbidden.isdisjoint(value), value)
            for nested in value.values():
                BisaModerationReviewTests._assert_no_path_fields(testcase, nested)
        elif isinstance(value, list):
            for nested in value:
                BisaModerationReviewTests._assert_no_path_fields(testcase, nested)

    def test_product_review_requires_catalog_permission_and_returns_safe_full_detail(self):
        product_id, media_id = self._pending_product()
        self.assertCode(
            "admin_permission_required",
            lambda: self.app.moderation_review_detail(self.support, "product", product_id),
        )
        self.assertCode(
            "admin_permission_required",
            lambda: self.app.moderation_review_detail(self.advertising, "product", product_id),
        )
        detail = self.app.moderation_review_detail(self.catalog, "product", product_id)
        self.assertEqual(("product", product_id), (detail["resource"], detail["item"]["id"]))
        self.assertEqual(media_id, detail["item"]["media"][0]["id"])
        self.assertEqual("0.100", detail["item"]["price"])
        self.assertTrue(detail["item"]["mediaIntegrity"]["valid"])
        self.assertTrue(detail["item"]["inventory"])
        self.assertTrue(detail["reviewReceipt"])
        self._assert_no_path_fields(self, detail)

    def test_product_approve_cannot_be_blind_and_receipt_is_actor_bound_one_time(self):
        product_id, _ = self._pending_product()
        self.assertCode(
            "moderation_review_required",
            lambda: self.app.admin_action(
                self.catalog, "product", "approve", {"id": product_id}
            ),
        )
        detail = self.app.moderation_review_detail(self.catalog, "product", product_id)
        self.assertCode(
            "moderation_review_receipt_not_found",
            lambda: self.app.admin_action(
                self.catalog_other, "product", "approve",
                {"id": product_id, "reviewReceipt": detail["reviewReceipt"]},
            ),
        )
        approved = self.app.admin_action(
            self.catalog, "product", "approve",
            {"id": product_id, "reviewReceipt": detail["reviewReceipt"]},
        )
        self.assertEqual(("approved", "approved", True), (
            approved["status"], approved["moderationStatus"], approved["active"],
        ))
        self.assertCode(
            "moderation_review_receipt_consumed",
            lambda: self.app.admin_action(
                self.catalog, "product", "approve",
                {"id": product_id, "reviewReceipt": detail["reviewReceipt"]},
            ),
        )

    def test_stale_and_expired_receipts_are_rejected(self):
        product_id, _ = self._pending_product()
        stale = self.app.moderation_review_detail(self.catalog, "product", product_id)
        with connect(immediate=True) as con:
            con.execute(
                "UPDATE products SET description_en='changed after review',updated_at=? WHERE id=?",
                (now_iso(), product_id),
            )
        self.assertCode(
            "moderation_review_stale",
            lambda: self.app.admin_action(
                self.catalog, "product", "approve",
                {"id": product_id, "reviewReceipt": stale["reviewReceipt"]},
            ),
        )
        fresh = self.app.moderation_review_detail(self.catalog, "product", product_id)
        with connect(immediate=True) as con:
            con.execute(
                "UPDATE moderation_review_receipts SET expires_at=? WHERE receipt_hash=?",
                (
                    (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
                    hashlib.sha256(fresh["reviewReceipt"].encode()).hexdigest(),
                ),
            )
        self.assertCode(
            "moderation_review_receipt_expired",
            lambda: self.app.admin_action(
                self.catalog, "product", "approve",
                {"id": product_id, "reviewReceipt": fresh["reviewReceipt"]},
            ),
        )

    def test_product_media_resolver_enforces_relation_owner_receipt_and_idor(self):
        product_id, media_id = self._pending_product()
        other_media = "media_muscat_private"
        self._private_image(other_media, "demo_merchant_muscat")
        detail = self.app.moderation_review_detail(self.catalog, "product", product_id)
        resolved = self.app.resolve_moderation_media(
            self.catalog, "product", product_id, media_id, detail["reviewReceipt"]
        )
        self.assertEqual(("image/webp", 12), (resolved["mimeType"], resolved["byteSize"]))
        self.assertTrue(resolved["path"].is_file())
        self.assertCode(
            "moderation_review_required",
            lambda: self.app.resolve_moderation_media(
                self.catalog, "product", product_id, media_id, ""
            ),
        )
        self.assertCode(
            "moderation_media_not_found",
            lambda: self.app.resolve_moderation_media(
                self.catalog, "product", product_id, other_media, detail["reviewReceipt"]
            ),
        )

        corrupt_id = "media_wrong_owner_relation"
        self._private_image(corrupt_id, "demo_merchant_muscat")
        with connect(immediate=True) as con:
            con.execute(
                """INSERT INTO product_media(
                       id,product_id,private_path,thumbnail_path,mime_type,width,height,
                       sort_order,status,created_at)
                   VALUES(?,? ,?,'','image/webp',1,1,9,'active',?)""",
                (corrupt_id, product_id, f"media:{corrupt_id}", now_iso()),
            )
        corrupt_detail = self.app.moderation_review_detail(self.catalog, "product", product_id)
        self.assertFalse(corrupt_detail["item"]["mediaIntegrity"]["valid"])
        self.assertEqual(1, corrupt_detail["item"]["mediaIntegrity"]["brokenAssociations"])
        self.assertCode(
            "moderation_media_not_found",
            lambda: self.app.resolve_moderation_media(
                self.catalog, "product", product_id, corrupt_id,
                corrupt_detail["reviewReceipt"],
            ),
        )
        self.assertCode(
            "moderation_media_integrity_failed",
            lambda: self.app.admin_action(
                self.catalog, "product", "approve",
                {"id": product_id, "reviewReceipt": corrupt_detail["reviewReceipt"]},
            ),
        )

    def test_ad_review_requires_ad_permission_and_resolves_store_product_bundle_destinations(self):
        cases = [
            ("store", "demo_branch_seeb", "store"),
            ("product", "demo_product_seeb_1", "product"),
            ("bundle", "demo_bundle_seeb", "bundle"),
        ]
        for landing_kind, landing_id, suffix in cases:
            with self.subTest(landing_kind=landing_kind):
                campaign_id = self._campaign(landing_kind, landing_id, suffix)
                self.assertCode(
                    "admin_permission_required",
                    lambda campaign_id=campaign_id: self.app.moderation_review_detail(
                        self.catalog, "ad", campaign_id
                    ),
                )
                detail = self.app.moderation_review_detail(
                    self.advertising, "ad", campaign_id
                )
                self.assertEqual(landing_kind, detail["item"]["landing"]["kind"])
                self.assertEqual(landing_id, detail["item"]["landing"]["id"])
                self.assertTrue(detail["item"]["landing"]["eligible"])
                self.assertEqual(["wilayat_seeb"], detail["item"]["target"]["wilayatIds"])
                self._assert_no_path_fields(self, detail)
                self.assertCode(
                    "moderation_review_required",
                    lambda campaign_id=campaign_id: self.app.admin_action(
                        self.advertising, "ad", "approve", {"id": campaign_id}
                    ),
                )
                approved = self.app.admin_action(
                    self.advertising, "ad", "approve",
                    {"id": campaign_id, "reviewReceipt": detail["reviewReceipt"]},
                )
                self.assertEqual("approved", approved["status"])

    def test_ad_review_hides_nonmerchant_campaign_and_blocks_foreign_landing(self):
        stamp = now_iso()
        with connect(immediate=True) as con:
            con.execute(
                """INSERT INTO ad_campaigns(
                       id,owner_kind,owner_id,placement,target_json,landing_kind,landing_id,
                       label_ar,label_en,status,starts_at,ends_at,frequency_cap,created_at,updated_at)
                   VALUES('foreign_ad','supplier','supplier_x','home_inline','{}','store',
                          'demo_branch_seeb','إعلان','Sponsored','draft','','',3,?,?)""",
                (stamp, stamp),
            )
        self.assertCode(
            "ad_not_found",
            lambda: self.app.moderation_review_detail(self.advertising, "ad", "foreign_ad"),
        )
        campaign_id = self._campaign("store", "demo_branch_muscat", "foreign_store")
        detail = self.app.moderation_review_detail(self.advertising, "ad", campaign_id)
        self.assertFalse(detail["item"]["landing"]["eligible"])
        self.assertIsNone(detail["item"]["landing"]["detail"])
        self.assertCode(
            "campaign_landing_unavailable",
            lambda: self.app.admin_action(
                self.advertising, "ad", "approve",
                {"id": campaign_id, "reviewReceipt": detail["reviewReceipt"]},
            ),
        )

    def test_unpaid_merchant_ad_cannot_be_approved_or_published(self):
        campaign_id = self._campaign("store", "demo_branch_seeb", "unpaid")
        with connect(immediate=True) as con:
            con.execute(
                "UPDATE ad_campaigns SET target_json=? WHERE id=?",
                (dumps({
                    "titleAr": "غير مدفوع", "titleEn": "Unpaid",
                    "creativeMediaId": "", "wilayatIds": [], "areaIds": [],
                    "categoryIds": [], "language": "all",
                    "paymentStatus": "not_started",
                }), campaign_id),
            )
        detail = self.app.moderation_review_detail(self.advertising, "ad", campaign_id)
        self.assertFalse(detail["item"]["commercial"]["eligibleForApproval"])
        self.assertCode(
            "ad_payment_or_credit_required",
            lambda: self.app.admin_action(
                self.advertising, "ad", "approve",
                {"id": campaign_id, "reviewReceipt": detail["reviewReceipt"]},
            ),
        )
        with connect() as con:
            status = con.execute(
                "SELECT status FROM ad_campaigns WHERE id=?", (campaign_id,),
            ).fetchone()["status"]
        self.assertEqual("draft", status)


if __name__ == "__main__":
    unittest.main()
