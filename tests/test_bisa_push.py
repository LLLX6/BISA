import base64
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from py_vapid import Vapid02

from bisa_domain import DomainError
from bisa_push import (
    PUSH_DELIVERY_TIMEOUT_SECONDS,
    BisaPushService,
    PyWebPushTransport,
    PushSendResult,
    UnavailablePushTransport,
    enqueue_notification,
    install_push_schema,
    validate_push_endpoint,
)


class TestDatabase:
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def connect(self, immediate=False):
        con = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield con
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()


class RecordingTransport:
    configured = True
    vapid_configured = True
    public_key = "test-public-vapid-key"

    def __init__(self, results=None):
        self.results = list(results or [])
        self.calls = []

    def send(self, **request):
        self.calls.append(request)
        if self.results:
            return self.results.pop(0)
        return PushSendResult(True, provider_reference="mock-http-201")


class MissingVapidTransport(RecordingTransport):
    vapid_configured = False


def public_resolver(host, port, **_kwargs):
    return [(2, 1, 6, "", ("8.8.8.8", port))]


def private_resolver(host, port, **_kwargs):
    return [(2, 1, 6, "", ("127.0.0.1", port))]


class BisaPushTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="bisa-push-")
        self.db = TestDatabase(Path(self.temp.name) / "push.sqlite3")
        with self.db.connect(immediate=True) as con:
            con.execute(
                """CREATE TABLE notifications(
                id TEXT PRIMARY KEY, target_kind TEXT NOT NULL, target_id TEXT NOT NULL,
                title_ar TEXT NOT NULL DEFAULT '', title_en TEXT NOT NULL DEFAULT '',
                body_ar TEXT NOT NULL DEFAULT '', body_en TEXT NOT NULL DEFAULT '',
                route TEXT NOT NULL DEFAULT '', expires_at TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL)"""
            )
            install_push_schema(con)
        self.now = datetime.now(UTC).replace(microsecond=0)
        self.endpoint = "https://fcm.googleapis.com/fcm/send/browser-token"
        p256dh = base64.urlsafe_b64encode(b"\x04" + b"A" * 64).decode().rstrip("=")
        auth = base64.urlsafe_b64encode(b"B" * 16).decode().rstrip("=")
        self.subscription = {
            "endpoint": self.endpoint,
            "keys": {"p256dh": p256dh, "auth": auth},
        }
        self.shopper = {
            "accountId": "acct_dual", "role": "shopper", "merchantId": ""
        }
        self.merchant = {
            "accountId": "acct_dual", "role": "merchant_owner",
            "merchantId": "merchant_one",
        }

    def tearDown(self):
        self.temp.cleanup()

    def service(self, transport=None, **kwargs):
        return BisaPushService(
            connection_factory=self.db.connect,
            transport=transport,
            resolver=public_resolver,
            **kwargs,
        )

    def notify(
        self,
        notification_id,
        target_kind="account",
        target_id="acct_dual",
        *,
        created_at=None,
        expires_at=None,
        title_ar="تنبيه",
        body_ar="افتح التطبيق",
    ):
        created = created_at or self.now
        expiry = expires_at if expires_at is not None else created + timedelta(hours=1)
        with self.db.connect(immediate=True) as con:
            con.execute(
                """INSERT INTO notifications(
                id,target_kind,target_id,title_ar,title_en,body_ar,body_en,route,
                expires_at,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    notification_id, target_kind, target_id, title_ar, "Alert",
                    body_ar, "Open the app", f"private:{target_id}",
                    expiry.isoformat() if isinstance(expiry, datetime) else str(expiry or ""),
                    created.isoformat(),
                ),
            )

    def row(self, sql, params=()):
        with self.db.connect() as con:
            result = con.execute(sql, params).fetchone()
            return dict(result) if result else None

    def scalar(self, sql, params=()):
        with self.db.connect() as con:
            return con.execute(sql, params).fetchone()[0]

    def assert_domain_code(self, code, callback):
        with self.assertRaises(DomainError) as caught:
            callback()
        self.assertEqual(code, caught.exception.code)

    def test_same_endpoint_has_independent_dual_role_bindings_and_scoped_logout(self):
        service = self.service(RecordingTransport())
        shopper_result = service.subscribe(self.shopper, self.subscription)
        merchant_result = service.subscribe(self.merchant, self.subscription)

        self.assertEqual(shopper_result["subscriptionId"], merchant_result["subscriptionId"])
        self.assertNotEqual(shopper_result["bindingId"], merchant_result["bindingId"])
        self.assertEqual(1, self.scalar("SELECT COUNT(*) FROM push_subscriptions"))
        self.assertEqual(2, self.scalar("SELECT COUNT(*) FROM push_subscription_bindings WHERE active=1"))

        self.notify("ntf_shopper_before")
        self.notify("ntf_merchant_before", "merchant", "merchant_one")
        self.assertEqual(2, self.scalar("SELECT COUNT(*) FROM push_delivery_outbox"))

        result = service.logout_scope(self.shopper, self.endpoint)
        self.assertEqual(1, result["deactivated"])
        self.assertEqual(
            1,
            self.scalar(
                "SELECT COUNT(*) FROM push_subscription_bindings WHERE active=1 AND role='merchant_owner'"
            ),
        )
        self.assertEqual(
            "cancelled",
            self.row(
                "SELECT status FROM push_delivery_outbox WHERE notification_id='ntf_shopper_before'"
            )["status"],
        )
        self.assertEqual(
            "pending",
            self.row(
                "SELECT status FROM push_delivery_outbox WHERE notification_id='ntf_merchant_before'"
            )["status"],
        )

        self.notify("ntf_shopper_after")
        self.notify("ntf_merchant_after", "merchant", "merchant_one")
        self.assertEqual(
            0,
            self.scalar(
                "SELECT COUNT(*) FROM push_delivery_outbox WHERE notification_id='ntf_shopper_after'"
            ),
        )
        self.assertEqual(
            1,
            self.scalar(
                "SELECT COUNT(*) FROM push_delivery_outbox WHERE notification_id='ntf_merchant_after'"
            ),
        )

    def test_same_browser_endpoint_cannot_cross_account_boundary(self):
        service = self.service(RecordingTransport())
        service.subscribe(self.shopper, self.subscription)
        other_account = {
            "accountId": "acct_other", "role": "shopper", "merchantId": "",
        }
        self.assert_domain_code(
            "push_subscription_already_bound",
            lambda: service.subscribe(other_account, self.subscription),
        )
        self.assertEqual(
            1,
            self.scalar("SELECT COUNT(*) FROM push_subscription_bindings WHERE active=1"),
        )

    def test_browser_keys_are_decoded_and_validated(self):
        service = self.service(RecordingTransport())
        malformed = {
            **self.subscription,
            "keys": {"p256dh": "A" * 87, "auth": self.subscription["keys"]["auth"]},
        }
        self.assert_domain_code(
            "invalid_push_p256dh",
            lambda: service.subscribe(self.shopper, malformed),
        )

    def test_vapid_adapter_requires_a_matching_key_pair_and_safe_subject(self):
        vapid = Vapid02()
        vapid.generate_keys()
        public_key = base64.urlsafe_b64encode(
            vapid.public_key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
        ).decode().rstrip("=")
        private_key = vapid.private_pem().decode()
        valid = PyWebPushTransport(
            public_key=public_key,
            private_key=private_key,
            subject="mailto:push-operator@example.com",
        )
        self.assertTrue(valid.configured)
        self.assertEqual(public_key, valid.public_key)
        mismatch = PyWebPushTransport(
            public_key=public_key[:-1] + ("A" if public_key[-1] != "A" else "B"),
            private_key=private_key,
            subject="mailto:push-operator@example.com",
        )
        self.assertFalse(mismatch.configured)
        self.assertEqual("push_vapid_key_mismatch", mismatch.reason)

    def test_notification_trigger_is_transactional_and_enqueue_is_deduplicated(self):
        service = self.service(RecordingTransport())
        service.subscribe(self.shopper, self.subscription)

        with self.assertRaises(RuntimeError):
            with self.db.connect(immediate=True) as con:
                con.execute(
                    """INSERT INTO notifications(
                    id,target_kind,target_id,expires_at,created_at)
                    VALUES('ntf_rollback','account','acct_dual',?,?)""",
                    ((self.now + timedelta(hours=1)).isoformat(), self.now.isoformat()),
                )
                self.assertEqual(
                    1,
                    con.execute(
                        "SELECT COUNT(*) FROM push_delivery_outbox WHERE notification_id='ntf_rollback'"
                    ).fetchone()[0],
                )
                raise RuntimeError("rollback")
        self.assertEqual(
            0,
            self.scalar(
                "SELECT COUNT(*) FROM push_delivery_outbox WHERE notification_id='ntf_rollback'"
            ),
        )

        self.notify("ntf_dedupe")
        with self.db.connect(immediate=True) as con:
            self.assertEqual(0, enqueue_notification(con, "ntf_dedupe"))
            self.assertEqual(0, enqueue_notification(con, "ntf_dedupe"))
        self.assertEqual(
            1,
            self.scalar(
                "SELECT COUNT(*) FROM push_delivery_outbox WHERE notification_id='ntf_dedupe'"
            ),
        )

    def test_retry_claim_token_lease_and_expiry_are_enforced(self):
        transport = RecordingTransport(
            [
                PushSendResult(False, error_code="temporary_network"),
                PushSendResult(True, provider_reference="mock-http-201"),
            ]
        )
        service = self.service(transport, lease_seconds=60)
        service.subscribe(self.shopper, self.subscription)
        self.notify("ntf_retry")

        first = service.run_once(now=self.now)
        self.assertEqual((1, 0, 1), (first["claimed"], first["delivered"], first["retried"]))
        retry_row = self.row(
            "SELECT status,attempts,available_at FROM push_delivery_outbox WHERE notification_id='ntf_retry'"
        )
        self.assertEqual(("pending", 1), (retry_row["status"], retry_row["attempts"]))
        second = service.run_once(now=self.now + timedelta(seconds=31))
        self.assertEqual((1, 1), (second["claimed"], second["delivered"]))

        self.notify("ntf_lease", created_at=self.now + timedelta(seconds=32))
        old_claim = service.claim_pending(now=self.now + timedelta(seconds=32))[0]
        self.assertEqual([], service.claim_pending(now=self.now + timedelta(seconds=91)))
        new_claim = service.claim_pending(now=self.now + timedelta(seconds=93))[0]
        self.assertNotEqual(old_claim.claim_token, new_claim.claim_token)
        self.assertEqual(
            "stale_claim",
            service.complete_claim(
                old_claim,
                PushSendResult(True, provider_reference="old-worker"),
                now=self.now + timedelta(seconds=93),
            ),
        )
        self.assertEqual(
            "delivered",
            service.complete_claim(
                new_claim,
                PushSendResult(True, provider_reference="new-worker"),
                now=self.now + timedelta(seconds=93),
            ),
        )

        self.notify(
            "ntf_expired",
            created_at=self.now - timedelta(minutes=2),
            expires_at=self.now - timedelta(minutes=1),
        )
        self.assertEqual([], service.claim_pending(now=self.now))
        self.assertEqual(
            "expired",
            self.row(
                "SELECT status FROM push_delivery_outbox WHERE notification_id='ntf_expired'"
            )["status"],
        )

    def test_delivery_payload_contains_only_notification_id_and_transport_is_bounded(self):
        transport = RecordingTransport()
        service = self.service(transport)
        service.subscribe(self.shopper, self.subscription)
        self.notify(
            "ntf_private",
            title_ar="رقم العميل 96899999999",
            body_ar="عنوان المنزل شارع 12، مسقط",
        )
        result = service.run_once(now=self.now)
        self.assertEqual(1, result["delivered"])
        self.assertEqual(1, len(transport.calls))
        call = transport.calls[0]
        self.assertEqual({"notificationId": "ntf_private"}, call["payload"])
        self.assertEqual(PUSH_DELIVERY_TIMEOUT_SECONDS, call["timeout_seconds"])
        self.assertIs(False, call["allow_redirects"])
        serialized = repr(call["payload"])
        self.assertNotIn("96899999999", serialized)
        self.assertNotIn("شارع", serialized)
        self.assertNotIn("acct_dual", serialized)

    def test_vendor_allowlist_and_public_dns_block_ssrf(self):
        valid = validate_push_endpoint(self.endpoint, resolver=public_resolver)
        self.assertEqual(self.endpoint, valid)
        self.assert_domain_code(
            "push_endpoint_host_not_allowed",
            lambda: validate_push_endpoint(
                "https://example.com/push", resolver=public_resolver
            ),
        )
        self.assert_domain_code(
            "push_endpoint_not_public",
            lambda: validate_push_endpoint(self.endpoint, resolver=private_resolver),
        )
        self.assert_domain_code(
            "invalid_push_endpoint",
            lambda: validate_push_endpoint(
                "http://fcm.googleapis.com/fcm/send/token", resolver=public_resolver
            ),
        )

    def test_unconfigured_transport_never_claims_or_reports_success(self):
        service = self.service(UnavailablePushTransport())
        subscribed = service.subscribe(self.shopper, self.subscription)
        self.assertFalse(subscribed["capability"]["available"])
        self.notify("ntf_unconfigured")
        result = service.run_once(now=self.now)
        self.assertEqual("unavailable", result["status"])
        self.assertEqual(0, result["claimed"])
        self.assertEqual(0, result["delivered"])
        self.assertEqual("push_not_configured", result["errorCode"])
        pending = self.row(
            "SELECT status,attempts FROM push_delivery_outbox WHERE notification_id='ntf_unconfigured'"
        )
        self.assertEqual(("pending", 0), (pending["status"], pending["attempts"]))

        # Even an installed adapter cannot become ready without explicitly
        # confirming that both sides of its VAPID configuration are present.
        missing_vapid = self.service(MissingVapidTransport())
        readiness = missing_vapid.run_once(now=self.now)
        self.assertEqual("unavailable", readiness["status"])
        self.assertFalse(readiness["configured"])
        self.assertEqual(0, readiness["claimed"])


if __name__ == "__main__":
    unittest.main()
