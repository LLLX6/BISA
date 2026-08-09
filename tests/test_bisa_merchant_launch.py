import hashlib
import os
import shutil
import tempfile
import unittest
from pathlib import Path


TEST_ROOT = Path(tempfile.mkdtemp(prefix="bisa-merchant-launch-tests-"))
os.environ["BISA_DATA_DIR"] = str(TEST_ROOT)
os.environ["BISA_DB_PATH"] = str(TEST_ROOT / "merchant-launch.sqlite3")
os.environ["BISA_UPLOAD_DIR"] = str(TEST_ROOT / "uploads")
os.environ["BISA_BACKUP_DIR"] = str(TEST_ROOT / "backups")
os.environ["BISA_SEED_SAMPLE_DATA"] = "true"
os.environ["BISA_DEMO_PIN"] = "1234"

import bisa_config  # noqa: E402
import bisa_domain  # noqa: E402
from bisa_application import BisaApplication  # noqa: E402
from bisa_domain import (  # noqa: E402
    DomainError,
    authenticate,
    connect,
    dumps,
    hash_secret,
    init_db,
    loads,
    now_iso,
    register_or_login,
)
from bisa_merchant_launch import (  # noqa: E402
    MERCHANT_LAUNCH_COMPOSITION,
    MERCHANT_LAUNCH_ROUTE_CONTRACTS,
    MerchantLaunchMixin,
)


DB_PATH = TEST_ROOT / "merchant-launch.sqlite3"
ORIGINAL_PATHS = {
    "data": bisa_config.DATA_DIR,
    "db": bisa_config.DB_PATH,
    "upload": bisa_config.UPLOAD_DIR,
    "backup": bisa_config.BACKUP_DIR,
    "domain_db": bisa_domain.DB_PATH,
    "domain_seed": bisa_domain.SEED_SAMPLE_DATA,
}


class LaunchApplication(BisaApplication):
    """Use the production composition, which already includes the mixin."""

    pass


class BisaMerchantLaunchTests(unittest.TestCase):
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
        self.app = LaunchApplication()
        self.seeb = authenticate(
            register_or_login("96892000003", "1234", "", "merchant_owner")["token"],
        )
        self.muscat = authenticate(
            register_or_login("96892000000", "1234", "", "merchant_owner")["token"],
        )
        self.admin = self._admin("admin", "acct_launch_admin", "96897771001")
        self.support = self._admin("support_admin", "acct_launch_support", "96897771002")

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

    def assertCode(self, code, callback, status=None):
        with self.assertRaises(DomainError) as caught:
            callback()
        self.assertEqual(code, caught.exception.code)
        if status is not None:
            self.assertEqual(status, caught.exception.status)
        return caught.exception

    def _media_blob(self, media_id, owner_kind, owner_id, purpose, *, mime="image/webp"):
        if mime == "image/webp":
            blob = b"RIFF\x04\x00\x00\x00WEBP"
            extension = "webp"
        else:
            blob = b"%PDF-1.4\n%%EOF"
            extension = "pdf"
        logical = f"private/{owner_kind}/{media_id}.{extension}"
        destination = Path(bisa_config.UPLOAD_DIR) / logical
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(blob)
        stamp = now_iso()
        with connect(immediate=True) as con:
            con.execute(
                """INSERT INTO private_media_objects(
                    id,owner_kind,owner_id,purpose,storage_key,mime_type,byte_size,sha256_hex,
                    original_name,status,created_by,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,'active',?,?,?)""",
                (
                    media_id, owner_kind, owner_id, purpose, logical, mime, len(blob),
                    hashlib.sha256(blob).hexdigest(), f"{purpose}.{extension}",
                    self.admin["accountId"], stamp, stamp,
                ),
            )
        return destination

    def _ready_application(self, suffix="brand"):
        stamp = now_iso()
        account_id = f"acct_launch_applicant_{suffix}"
        merchant_id = f"merchant_launch_applicant_{suffix}"
        application_id = f"application_launch_{suffix}"
        branch_id = f"branch_launch_application_{suffix}"
        policy_id = f"policy_launch_{suffix}"
        logo_id = f"media_brand_logo_{suffix}"
        cover_id = f"media_brand_cover_{suffix}"
        self._media_blob(logo_id, "merchant_application", application_id, "logo")
        self._media_blob(cover_id, "merchant_application", application_id, "cover")
        with connect(immediate=True) as con:
            con.execute(
                """INSERT INTO accounts(id,phone,name,pin_hash,status,created_at)
                   VALUES(?,?,?,?,?,?)""",
                (
                    account_id, f"9689665{int(hashlib.sha1(suffix.encode()).hexdigest()[:4], 16) % 10000:04d}",
                    "Launch applicant", hash_secret("1234"), "active", stamp,
                ),
            )
            con.execute(
                "INSERT INTO account_roles(account_id,role,merchant_id,active) VALUES(?,'shopper','',1)",
                (account_id,),
            )
            con.execute(
                """INSERT INTO merchants(
                    id,owner_account_id,name_ar,name_en,status,verified,created_at,updated_at,active)
                   VALUES(?,?,?,?,'submitted',0,?,?,1)""",
                (merchant_id, account_id, "متجر علامة", "Brand store", stamp, stamp),
            )
            con.execute(
                "INSERT INTO account_roles(account_id,role,merchant_id,active) VALUES(?,'merchant_owner',?,0)",
                (account_id, merchant_id),
            )
            snapshot = {
                "branchId": branch_id,
                "policyId": policy_id,
                "requestedPlan": "early_trial",
                "categories": {"categoryIds": ["toys"]},
                "brandMedia": {"logoMediaId": logo_id, "coverMediaId": cover_id},
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
                       VALUES(?,?,?,?,?)""",
                    (application_id, step, "{}", stamp, stamp),
                )
            con.execute(
                """INSERT INTO store_branches(
                    id,merchant_id,name_ar,name_en,wilayah_id,area_id,address_text,latitude,longitude,
                    hours_json,status,active,public_visible,created_at,updated_at)
                   VALUES(?,?,?,?,'wilayat_seeb','demo_area_seeb','Seeb',23.60,58.22,?,
                          'submitted',1,0,?,?)""",
                (
                    branch_id, merchant_id, "فرع العلامة", "Brand branch",
                    dumps({"sun": [{"open": "09:00", "close": "21:00"}]}), stamp, stamp,
                ),
            )
            con.execute(
                """INSERT INTO merchant_return_policies(
                    id,merchant_id,version,conditions_text,contact_method,active,created_at)
                   VALUES(?,?,1,'Unused products','support',1,?)""",
                (policy_id, merchant_id, stamp),
            )
            con.execute(
                "UPDATE merchants SET return_policy_id=? WHERE id=?", (policy_id, merchant_id),
            )
            for kind in ("storefront", "commercial_registration", "license"):
                media_id = f"media_application_document_{suffix}_{kind}"
                con.execute(
                    """INSERT INTO private_media_objects(
                        id,owner_kind,owner_id,purpose,storage_key,mime_type,byte_size,sha256_hex,
                        original_name,status,created_by,created_at,updated_at)
                       VALUES(?,'merchant_application',?,?,?,'application/pdf',1,?,?,'active',?,?,?)""",
                    (
                        media_id, application_id, kind,
                        f"private/merchant_application/{media_id}.pdf", "0" * 64,
                        f"{kind}.pdf", account_id, stamp, stamp,
                    ),
                )
                con.execute(
                    """INSERT INTO merchant_documents(
                        id,application_id,kind,private_path,created_at,media_id,review_status,
                        reviewed_by,reviewed_at)
                       VALUES(?,?,?,?,?,?,'approved',?,?)""",
                    (
                        f"doc_{suffix}_{kind}", application_id, kind, f"media:{media_id}",
                        stamp, media_id, self.admin["accountId"], stamp,
                    ),
                )
            # Make the trial deterministic for a standalone module test.
            con.execute(
                """INSERT INTO platform_settings(key,value_json,updated_at) VALUES(?,?,?)
                   ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at""",
                ("trialEnabled", "true", stamp),
            )
            con.execute(
                """INSERT INTO platform_settings(key,value_json,updated_at) VALUES(?,?,?)
                   ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at""",
                ("trialFirstApprovedMerchants", "1000", stamp),
            )
            con.execute(
                """INSERT INTO platform_settings(key,value_json,updated_at) VALUES(?,?,?)
                   ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at""",
                ("trialCutoffAt", '"2099-12-31T23:59:59+00:00"', stamp),
            )
        return {
            "applicationId": application_id,
            "merchantId": merchant_id,
            "branchId": branch_id,
            "accountId": account_id,
            "logoId": logo_id,
            "coverId": cover_id,
        }

    def _approve_application(self, application):
        return self.app.admin_application_decision(self.admin, {
            "applicationId": application["applicationId"],
            "decision": "approve",
            "note": "Reviewed",
        })

    def _branch_document(self, branch_id, kind="storefront", merchant_id="demo_merchant_seeb"):
        media_id = f"media_branch_{kind}_{branch_id[-12:]}"
        self._media_blob(
            media_id, "merchant", merchant_id, f"branch:{branch_id}:{kind}",
        )
        return {"kind": kind, "mediaId": media_id}

    @staticmethod
    def _hours(opening="09:00", closing="21:00"):
        return {"sun": [{"open": opening, "close": closing}]}

    def _new_branch(self):
        return self.app.create_branch(self.seeb, {
            "nameAr": "فرع المعبيلة",
            "nameEn": "Mabela branch",
            "wilayahId": "wilayat_seeb",
            "areaId": "demo_area_seeb",
            "address": "Al Mabela, Seeb",
            "latitude": 23.64,
            "longitude": 58.14,
            "hours": self._hours(),
            "phone": "96892000003",
        })

    # ---------- brand publication ----------

    def test_approval_binds_opaque_brand_assets_and_public_resolver_is_integrity_checked(self):
        application = self._ready_application("publish")
        with connect(immediate=True) as con:
            con.execute(
                """INSERT INTO private_media_access_grants(
                    media_id,grantee_kind,grantee_id,permission,expires_at,granted_by,created_at)
                   VALUES(?,'account',?,'read','',?,?)""",
                (
                    application["logoId"], self.muscat["accountId"],
                    self.admin["accountId"], now_iso(),
                ),
            )
        result = self._approve_application(application)
        self.assertEqual("approved", result["status"])
        self.assertEqual(
            {"logo", "cover"}, set(result["brandAssets"]),
        )
        descriptor = result["brandAssets"]["logo"]
        self.assertEqual(application["logoId"], descriptor["id"])
        self.assertEqual(
            f"/api/merchant-assets/{application['logoId']}", descriptor["url"],
        )
        self.assertFalse(
            {"storageKey", "storage_key", "path", "sha256Hex"}.intersection(descriptor),
        )
        self.assertNotIn("private/", str(descriptor))
        stream = self.app.resolve_merchant_brand_asset(None, application["logoId"])
        resolved = stream.path
        self.assertTrue(resolved.is_file())
        self.assertEqual(("image/webp", resolved.stat().st_size), (
            stream.mime_type, stream.byte_size,
        ))
        with connect() as con:
            merchant = con.execute(
                "SELECT logo_path,cover_path FROM merchants WHERE id=?",
                (application["merchantId"],),
            ).fetchone()
            media = con.execute(
                "SELECT owner_kind,owner_id,purpose,storage_key FROM private_media_objects WHERE id=?",
                (application["logoId"],),
            ).fetchone()
            audit = con.execute(
                """SELECT before_json,after_json FROM admin_audit_logs
                   WHERE action='merchant_brand_assets_bound' AND target_id=?""",
                (application["merchantId"],),
            ).fetchone()
            grants = con.execute(
                "SELECT COUNT(*) n FROM private_media_access_grants WHERE media_id=?",
                (application["logoId"],),
            ).fetchone()["n"]
        self.assertEqual(f"media:{application['logoId']}", merchant["logo_path"])
        self.assertEqual(f"media:{application['coverId']}", merchant["cover_path"])
        self.assertEqual(
            ("merchant", application["merchantId"], "public_merchant_logo"),
            (media["owner_kind"], media["owner_id"], media["purpose"]),
        )
        self.assertNotIn(media["storage_key"], audit["before_json"] + audit["after_json"])
        self.assertEqual(0, grants)
        public_descriptor = self.app.merchant_brand_asset_descriptor(
            None, application["merchantId"], "logo",
        )
        self.assertEqual(application["logoId"], public_descriptor["id"])

        # Binding is idempotent and does not create a second publication audit.
        duplicate = self.app.bind_approved_application_brand_assets(
            self.admin, application["applicationId"],
        )
        self.assertEqual(application["logoId"], duplicate["logo"]["id"])
        with connect() as con:
            self.assertEqual(1, con.execute(
                "SELECT COUNT(*) n FROM admin_audit_logs WHERE action='merchant_brand_assets_bound' AND target_id=?",
                (application["merchantId"],),
            ).fetchone()["n"])
        resolved.write_bytes(b"RIFF\x04\x00\x00\x00WEBX")
        self.assertCode(
            "merchant_asset_not_found",
            lambda: self.app.resolve_merchant_brand_asset_path(None, application["logoId"]),
            404,
        )

    def test_brand_asset_visibility_and_known_id_do_not_bypass_tenant_or_public_state(self):
        application = self._ready_application("idor")
        self._approve_application(application)
        owner = {
            "accountId": application["accountId"], "name": "Owner",
            "role": "merchant_owner", "merchantId": application["merchantId"],
        }
        with connect(immediate=True) as con:
            con.execute(
                "UPDATE store_branches SET public_visible=0 WHERE merchant_id=?",
                (application["merchantId"],),
            )
        self.assertCode("merchant_asset_not_found", lambda: self.app.merchant_brand_asset_descriptor(
            None, application["merchantId"], "logo",
        ), 404)
        self.assertCode("merchant_asset_not_found", lambda: self.app.resolve_merchant_brand_asset_path(
            self.muscat, application["logoId"],
        ), 404)
        self.assertCode("merchant_asset_not_found", lambda: self.app.merchant_brand_asset_descriptor(
            self.muscat, application["merchantId"], "logo",
        ), 404)
        self.assertEqual(
            application["logoId"],
            self.app.merchant_brand_asset_descriptor(
                owner, application["merchantId"], "logo",
            )["id"],
        )
        self.assertTrue(
            self.app.resolve_merchant_brand_asset_path(owner, application["logoId"]).is_file(),
        )
        self.assertEqual(
            application["logoId"],
            self.app.merchant_brand_asset_descriptor(
                self.admin, application["merchantId"], "logo",
            )["id"],
        )

    def test_approval_rejects_foreign_non_image_and_document_reused_brand_media_before_status_change(self):
        foreign = self._ready_application("foreign")
        with connect(immediate=True) as con:
            snapshot = loads(con.execute(
                "SELECT payload FROM merchant_applications WHERE id=?",
                (foreign["applicationId"],),
            ).fetchone()["payload"], {})
            snapshot["brandMedia"]["logoMediaId"] = "media_foreign_known_asset"
            con.execute(
                "UPDATE merchant_applications SET payload=? WHERE id=?",
                (dumps(snapshot), foreign["applicationId"]),
            )
            con.execute(
                """INSERT INTO private_media_objects(
                    id,owner_kind,owner_id,purpose,storage_key,mime_type,byte_size,sha256_hex,
                    original_name,status,created_by,created_at,updated_at)
                   VALUES(?,'merchant_application','some_other_application','logo',?,
                          'image/webp',1,?,'logo.webp','active',?,?,?)""",
                (
                    "media_foreign_known_asset", "private/foreign/logo.webp", "0" * 64,
                    self.admin["accountId"], now_iso(), now_iso(),
                ),
            )
        self.assertCode("merchant_brand_media_not_found", lambda: self._approve_application(foreign), 404)
        with connect() as con:
            self.assertEqual("submitted", con.execute(
                "SELECT status FROM merchant_applications WHERE id=?",
                (foreign["applicationId"],),
            ).fetchone()["status"])

        reused = self._ready_application("reused")
        with connect(immediate=True) as con:
            con.execute(
                """INSERT INTO merchant_documents(
                    id,application_id,kind,private_path,created_at,media_id,review_status)
                   VALUES(?,?,?,?,?,?,'approved')""",
                (
                    "doc_reused_brand", reused["applicationId"], "storefront_copy",
                    f"media:{reused['logoId']}", now_iso(), reused["logoId"],
                ),
            )
        self.assertCode(
            "merchant_brand_media_reused_as_document",
            lambda: self._approve_application(reused), 409,
        )

        non_image = self._ready_application("pdfbrand")
        with connect(immediate=True) as con:
            con.execute(
                "UPDATE private_media_objects SET mime_type='application/pdf' WHERE id=?",
                (non_image["coverId"],),
            )
        self.assertCode("merchant_brand_image_required", lambda: self._approve_application(non_image), 422)

    # ---------- branch lifecycle ----------

    def test_branch_submit_changes_resubmit_approve_pause_and_resume_are_audited_and_notified(self):
        branch = self._new_branch()
        document = self._branch_document(branch["id"])
        submitted = self.app.submit_branch_for_review(self.seeb, branch["id"], {
            "documents": [document], "hours": self._hours(),
        })
        self.assertEqual(("pending_review", False, 1), (
            submitted["status"], submitted["publicVisible"], submitted["revision"],
        ))
        self.assertCode("branch_submission_locked", lambda: self.app.submit_branch_for_review(
            self.seeb, branch["id"], {"documents": [document]},
        ), 409)
        changed = self.app.admin_branch_decision(self.admin, branch["id"], {
            "decision": "changes_requested", "note": "صحح بيانات المدخل",
        })
        self.assertEqual("changes_requested", changed["status"])
        resubmitted = self.app.submit_branch_for_review(self.seeb, branch["id"], {
            "documents": [document], "hours": {"sun": "24h"},
        })
        self.assertEqual(2, resubmitted["revision"])
        approved = self.app.admin_branch_decision(self.admin, branch["id"], {
            "decision": "approve", "note": "Reviewed storefront",
        })
        self.assertEqual(("approved", True), (approved["status"], approved["publicVisible"]))
        duplicate = self.app.admin_branch_decision(self.admin, branch["id"], {
            "decision": "approve", "note": "Reviewed storefront",
        })
        self.assertTrue(duplicate["duplicate"])
        paused = self.app.admin_branch_decision(self.admin, branch["id"], {
            "decision": "pause", "note": "Temporary compliance pause",
        })
        self.assertEqual(("paused", False), (paused["status"], paused["publicVisible"]))
        resumed = self.app.admin_branch_decision(self.admin, branch["id"], {
            "decision": "approve", "note": "Compliance issue resolved",
        })
        self.assertEqual(("approved", True), (resumed["status"], resumed["publicVisible"]))

        detail = self.app.branch_launch_detail(self.admin, branch["id"])
        self.assertEqual("approved", detail["branch"]["status"])
        self.assertEqual(document["mediaId"], detail["documents"][0]["mediaId"])
        self.assertNotIn("storage", str(detail).lower())
        self.assertNotIn("private/", str(detail))
        with connect() as con:
            actions = [row["action"] for row in con.execute(
                "SELECT action FROM admin_audit_logs WHERE target_kind='store_branch' AND target_id=?",
                (branch["id"],),
            )]
            owner_notice = con.execute(
                """SELECT route FROM notifications WHERE target_kind='account'
                   AND target_id=? AND route=? ORDER BY created_at DESC LIMIT 1""",
                (self.seeb["accountId"], f"merchant:branch:{branch['id']}"),
            ).fetchone()
            pending_admin = con.execute(
                """SELECT COUNT(*) n FROM notifications WHERE target_kind='admin' AND route=?
                   AND requires_action=1 AND acted_at=''""",
                (f"admin:branch-review:{branch['id']}",),
            ).fetchone()["n"]
        self.assertIn("branch_submitted_for_review", actions)
        self.assertIn("branch_changes_requested", actions)
        self.assertIn("branch_approved", actions)
        self.assertIn("branch_paused", actions)
        self.assertIsNotNone(owner_notice)
        self.assertEqual(0, pending_admin)

    def test_branch_requires_valid_location_hours_and_owned_documents_or_explicit_exception(self):
        branch = self._new_branch()
        self.assertCode("opening_day_required", lambda: self.app.submit_branch_for_review(
            self.seeb, branch["id"], {"hours": {"sun": []}},
        ), 422)
        self.assertCode("branch_documents_or_reason_required", lambda: self.app.submit_branch_for_review(
            self.seeb, branch["id"], {"hours": self._hours(), "documentExceptionReason": "short"},
        ), 422)
        foreign_document = self._branch_document(
            branch["id"], merchant_id="demo_merchant_muscat",
        )
        self.assertCode("branch_document_not_found", lambda: self.app.submit_branch_for_review(
            self.seeb, branch["id"], {"hours": self._hours(), "documents": [foreign_document]},
        ), 404)
        with connect(immediate=True) as con:
            con.execute(
                "UPDATE store_branches SET latitude=NULL WHERE id=?", (branch["id"],),
            )
        self.assertCode("branch_map_pin_required", lambda: self.app.submit_branch_for_review(
            self.seeb, branch["id"], {
                "hours": self._hours(),
                "documentExceptionReason": "Storefront photo unavailable pending landlord handover",
            },
        ), 422)

        with connect(immediate=True) as con:
            con.execute(
                "UPDATE store_branches SET latitude=20.0,longitude=56.0 WHERE id=?",
                (branch["id"],),
            )
        self.assertCode("branch_map_pin_outside_muscat", lambda: self.app.submit_branch_for_review(
            self.seeb, branch["id"], {
                "hours": self._hours(),
                "documentExceptionReason": "Storefront photo unavailable pending landlord handover",
            },
        ), 422)

    def test_document_exception_needs_an_admin_approval_reason_and_rejection_never_publishes(self):
        branch = self._new_branch()
        exception = "Storefront photo unavailable pending landlord handover"
        self.app.submit_branch_for_review(self.seeb, branch["id"], {
            "hours": self._hours(), "documentExceptionReason": exception,
        })
        self.assertCode(
            "branch_document_exception_approval_reason_required",
            lambda: self.app.admin_branch_decision(self.admin, branch["id"], {
                "decision": "approve", "note": "ok",
            }), 422,
        )
        approved = self.app.admin_branch_decision(self.admin, branch["id"], {
            "decision": "approve",
            "note": "Exception approved after physical verification",
        })
        self.assertTrue(approved["publicVisible"])

        # A fresh database is not needed to prove that reject always forces an
        # inactive, non-public branch; use another merchant with spare capacity.
        with connect(immediate=True) as con:
            plan_id = con.execute(
                """SELECT plan_id FROM merchant_subscriptions
                   WHERE merchant_id='demo_merchant_seeb' AND status='active'""",
            ).fetchone()["plan_id"]
            raw = loads(con.execute(
                "SELECT entitlements FROM subscription_plans WHERE id=?", (plan_id,),
            ).fetchone()["entitlements"], {})
            if raw.get("inherits"):
                plan_id = raw["inherits"]
                raw = loads(con.execute(
                    "SELECT entitlements FROM subscription_plans WHERE id=?", (plan_id,),
                ).fetchone()["entitlements"], {})
            raw["branches"] = max(3, int(raw.get("branches", 0)))
            con.execute(
                "UPDATE subscription_plans SET entitlements=? WHERE id=?", (dumps(raw), plan_id),
            )
        rejected_branch = self._new_branch()
        rejected_doc = self._branch_document(rejected_branch["id"])
        self.app.submit_branch_for_review(self.seeb, rejected_branch["id"], {
            "documents": [rejected_doc], "hours": self._hours(),
        })
        rejected = self.app.admin_branch_decision(self.admin, rejected_branch["id"], {
            "decision": "reject", "note": "Location documentation is not valid",
        })
        self.assertFalse(rejected["publicVisible"])
        with connect() as con:
            row = con.execute(
                "SELECT status,active,public_visible FROM store_branches WHERE id=?",
                (rejected_branch["id"],),
            ).fetchone()
        self.assertEqual(("rejected", 0, 0), tuple(row))

    def test_branch_plan_limit_is_rechecked_at_submission_and_admin_approval(self):
        branch = self._new_branch()
        document = self._branch_document(branch["id"])
        with connect(immediate=True) as con:
            subscription = con.execute(
                """SELECT plan_id FROM merchant_subscriptions
                   WHERE merchant_id=? AND status='active' ORDER BY created_at DESC LIMIT 1""",
                (self.seeb["merchantId"],),
            ).fetchone()
            plan_id = subscription["plan_id"]
            raw = loads(con.execute(
                "SELECT entitlements FROM subscription_plans WHERE id=?", (plan_id,),
            ).fetchone()["entitlements"], {})
            if raw.get("inherits"):
                plan_id = raw["inherits"]
                raw = loads(con.execute(
                    "SELECT entitlements FROM subscription_plans WHERE id=?", (plan_id,),
                ).fetchone()["entitlements"], {})
            raw["branches"] = 1
            con.execute(
                "UPDATE subscription_plans SET entitlements=? WHERE id=?", (dumps(raw), plan_id),
            )
        error = self.assertCode("plan_branch_limit", lambda: self.app.submit_branch_for_review(
            self.seeb, branch["id"], {"documents": [document], "hours": self._hours()},
        ), 409)
        self.assertEqual(1, error.detail["limit"])
        with connect() as con:
            self.assertEqual("draft", con.execute(
                "SELECT status FROM store_branches WHERE id=?", (branch["id"],),
            ).fetchone()["status"])

    def test_admin_revalidates_branch_documents_location_and_plan_before_publication(self):
        branch = self._new_branch()
        document = self._branch_document(branch["id"])
        self.app.submit_branch_for_review(self.seeb, branch["id"], {
            "documents": [document], "hours": self._hours(),
        })
        with connect(immediate=True) as con:
            con.execute(
                "UPDATE private_media_objects SET status='archived' WHERE id=?",
                (document["mediaId"],),
            )
        self.assertCode("branch_document_not_found", lambda: self.app.admin_branch_decision(
            self.admin, branch["id"], {"decision": "approve", "note": "Reviewed"},
        ), 404)
        with connect(immediate=True) as con:
            con.execute(
                "UPDATE private_media_objects SET status='active' WHERE id=?",
                (document["mediaId"],),
            )
            con.execute(
                "UPDATE store_branches SET area_id='demo_area_muscat' WHERE id=?",
                (branch["id"],),
            )
        self.assertCode("invalid_branch_location", lambda: self.app.admin_branch_decision(
            self.admin, branch["id"], {"decision": "approve", "note": "Reviewed"},
        ), 422)
        with connect(immediate=True) as con:
            con.execute(
                "UPDATE store_branches SET area_id='demo_area_seeb' WHERE id=?",
                (branch["id"],),
            )
            subscription = con.execute(
                """SELECT plan_id FROM merchant_subscriptions
                   WHERE merchant_id=? AND status='active' ORDER BY created_at DESC LIMIT 1""",
                (self.seeb["merchantId"],),
            ).fetchone()
            plan_id = subscription["plan_id"]
            raw = loads(con.execute(
                "SELECT entitlements FROM subscription_plans WHERE id=?", (plan_id,),
            ).fetchone()["entitlements"], {})
            if raw.get("inherits"):
                plan_id = raw["inherits"]
                raw = loads(con.execute(
                    "SELECT entitlements FROM subscription_plans WHERE id=?", (plan_id,),
                ).fetchone()["entitlements"], {})
            raw["branches"] = 1
            con.execute(
                "UPDATE subscription_plans SET entitlements=? WHERE id=?", (dumps(raw), plan_id),
            )
        self.assertCode("plan_branch_limit", lambda: self.app.admin_branch_decision(
            self.admin, branch["id"], {"decision": "approve", "note": "Reviewed"},
        ), 409)
        with connect() as con:
            state = con.execute(
                "SELECT status,public_visible FROM store_branches WHERE id=?", (branch["id"],),
            ).fetchone()
        self.assertEqual(("pending_review", 0), tuple(state))

    def test_typed_hours_update_preserves_public_state_and_blocks_idor_and_review_races(self):
        updated = self.app.update_branch_hours(self.seeb, "demo_branch_seeb", {
            "hours": {"sun": "24h", "mon": [{"open": "10:00", "close": "22:00"}]},
        })
        self.assertEqual(("approved", True, "24h"), (
            updated["status"], updated["publicVisible"], updated["hours"]["sun"],
        ))
        self.assertCode("opening_day_required", lambda: self.app.update_branch_hours(
            self.seeb, "demo_branch_seeb", {"hours": {"sun": "closed"}},
        ), 422)
        self.assertCode("branch_not_found", lambda: self.app.update_branch_hours(
            self.muscat, "demo_branch_seeb", {"hours": self._hours()},
        ), 404)
        branch = self._new_branch()
        document = self._branch_document(branch["id"])
        self.app.submit_branch_for_review(self.seeb, branch["id"], {
            "documents": [document], "hours": self._hours(),
        })
        self.assertCode("branch_under_review", lambda: self.app.update_branch_hours(
            self.seeb, branch["id"], {"hours": {"sun": "24h"}},
        ), 409)
        with connect() as con:
            audit = con.execute(
                """SELECT before_json,after_json FROM admin_audit_logs
                   WHERE action='branch_hours_updated' AND target_id='demo_branch_seeb'""",
            ).fetchone()
        self.assertIsNotNone(audit)
        self.assertEqual("24h", loads(audit["after_json"], {})["hours"]["sun"])

    def test_branch_review_permissions_transitions_and_route_contract_are_explicit(self):
        branch = self._new_branch()
        document = self._branch_document(branch["id"])
        self.assertCode("branch_not_found", lambda: self.app.submit_branch_for_review(
            self.muscat, branch["id"], {"documents": [document], "hours": self._hours()},
        ), 404)
        self.app.submit_branch_for_review(self.seeb, branch["id"], {
            "documents": [document], "hours": self._hours(),
        })
        self.assertCode("admin_permission_required", lambda: self.app.admin_branch_decision(
            self.support, branch["id"], {"decision": "approve", "note": "Reviewed"},
        ), 403)
        self.assertCode("branch_decision_note_required", lambda: self.app.admin_branch_decision(
            self.admin, branch["id"], {"decision": "reject", "note": ""},
        ), 422)
        self.assertCode("invalid_branch_transition", lambda: self.app.admin_branch_decision(
            self.admin, "demo_branch_seeb", {"decision": "reject", "note": "Cannot reject live branch"},
        ), 409)
        self.assertIn("MerchantLaunchMixin", MERCHANT_LAUNCH_COMPOSITION)
        self.assertEqual(
            "submit_branch_for_review",
            MERCHANT_LAUNCH_ROUTE_CONTRACTS[
                "POST /api/merchant/branches/{branchId}/submit"
            ]["method"],
        )
        self.assertEqual(
            "stream-only; never serialize the returned filesystem path",
            MERCHANT_LAUNCH_ROUTE_CONTRACTS[
                "GET /api/merchant-assets/{assetId}"
            ]["response"],
        )
        self.assertEqual(
            "nosniff",
            MERCHANT_LAUNCH_ROUTE_CONTRACTS[
                "GET /api/merchant-assets/{assetId}"
            ]["headers"]["X-Content-Type-Options"],
        )


if __name__ == "__main__":
    unittest.main()
