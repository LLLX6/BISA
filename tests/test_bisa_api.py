from __future__ import annotations

import hashlib
import base64
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from contextlib import closing
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def pin_hash(pin: str) -> str:
    salt = "bisa-api-test-salt"
    digest = hashlib.pbkdf2_hmac("sha256", pin.encode(), salt.encode(), 210_000)
    return f"pbkdf2_sha256$210000${salt}${digest.hex()}"


class BisaApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="bisa-api-tests-")
        cls.data = Path(cls.temp.name)
        cls.db = cls.data / "bisa-api.sqlite3"
        cls.port = free_port()
        cls.base = f"http://127.0.0.1:{cls.port}"
        environment = os.environ.copy()
        environment.update({
            "BISA_ENV": "development",
            "BISA_DATA_DIR": str(cls.data),
            "BISA_DB_PATH": str(cls.db),
            "BISA_UPLOAD_DIR": str(cls.data / "uploads"),
            "BISA_BACKUP_DIR": str(cls.data / "backups"),
            "BISA_SEED_SAMPLE_DATA": "true",
            "BISA_DEMO_PIN": "1234",
            "BISA_AUTH_PEPPER": "api-tests-auth-pepper-" + "a" * 32,
            "BISA_MEDIA_SIGNING_KEY": "api-tests-media-key-" + "m" * 32,
            "HOST": "127.0.0.1",
            "PORT": str(cls.port),
            "PYTHONIOENCODING": "utf-8",
        })
        # Replace DNS resolution inside the isolated test server so push
        # subscription validation never reaches the network.  The production
        # resolver and transport remain untouched.
        server_bootstrap = (
            "import socket,bisa_server;"
            "bisa_server.PUSH.resolver=lambda host,port,*args,**kwargs:"
            "[(socket.AF_INET,socket.SOCK_STREAM,socket.IPPROTO_TCP,'',(\"8.8.8.8\",port))];"
            "bisa_server.main()"
        )
        cls.process = subprocess.Popen(
            [sys.executable, "-c", server_bootstrap], cwd=ROOT, env=environment,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True,
        )
        for _ in range(120):
            try:
                if cls.request("GET", "/healthz")[0] == 200:
                    break
            except OSError:
                pass
            time.sleep(0.1)
        else:
            cls.process.terminate()
            raise AssertionError("BISA server did not start")
        stamp = "2026-08-09T00:00:00+00:00"
        # sqlite3.Connection's context manager commits/rolls back but does not
        # close the handle.  Explicit closing keeps Windows teardown reliable.
        with closing(sqlite3.connect(cls.db)) as con:
            con.execute(
                "INSERT INTO accounts(id,phone,name,pin_hash,status,created_at) VALUES(?,?,?,?,?,?)",
                ("api_admin", "96897777777", "API Admin", pin_hash("2468"), "active", stamp),
            )
            con.execute(
                "INSERT INTO account_roles(account_id,role,merchant_id,active) VALUES('api_admin','admin','',1)"
            )
            con.commit()

    @classmethod
    def tearDownClass(cls):
        cls.process.terminate()
        try:
            cls.process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            cls.process.kill()
            cls.process.wait(timeout=8)
        # Windows can retain the SQLite file handle for a few milliseconds
        # after TerminateProcess.  Retry only cleanup; never hide a persistent
        # leak or a test failure.
        last_error = None
        for attempt in range(20):
            try:
                cls.temp.cleanup()
                return
            except PermissionError as exc:
                last_error = exc
                time.sleep(0.05 * (attempt + 1))
        raise last_error

    @classmethod
    def request(
        cls, method: str, path: str, payload=None, token: str = "",
        origin: str | None = None, *, cookie: str = "", raw_body: bytes | None = None,
        extra_headers: dict | None = None,
    ):
        body = raw_body if raw_body is not None else (
            None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        )
        headers = {"Accept": "application/json"}
        if body is not None and raw_body is None:
            headers["Content-Type"] = "application/json"
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if origin is not None:
            headers["Origin"] = origin
        if cookie:
            headers["Cookie"] = cookie
        headers.update(extra_headers or {})
        request = urllib.request.Request(cls.base + path, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                raw = response.read()
                content_type = response.headers.get("Content-Type", "")
                data = json.loads(raw.decode("utf-8")) if "application/json" in content_type else raw
                return response.status, data, dict(response.headers)
        except urllib.error.HTTPError as error:
            raw = error.read()
            return error.code, json.loads(raw.decode("utf-8") or "{}"), dict(error.headers)

    @classmethod
    def login(cls, phone: str, role: str, *, pin: str = "1234", device: str = "api-device"):
        status, data, headers = cls.request("POST", "/api/auth", {
            "phone": phone, "pin": pin, "role": role, "deviceId": device,
        })
        assert status == 200, data
        data["_cookie"] = (headers.get("Set-Cookie") or "").split(";", 1)[0]
        return data

    @staticmethod
    def push_subscription(endpoint: str = "https://fcm.googleapis.com/fcm/send/api-http-test"):
        def encoded(value: bytes) -> str:
            return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")

        return {
            "endpoint": endpoint,
            "keys": {
                "p256dh": encoded(b"\x04" + b"P" * 64),
                "auth": encoded(b"A" * 16),
            },
        }

    @staticmethod
    def tiny_png() -> bytes:
        return (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
            b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDAT\x08\xd7c\xf8\xcf\xc0\xf0"
            b"\x1f\x00\x05\x00\x01\xff\x89\x99=\x1d\x00\x00\x00\x00IEND\xaeB`\x82"
        )

    def test_public_discovery_store_and_product_contracts(self):
        status, bootstrap, _ = self.request("GET", "/api/bootstrap")
        self.assertEqual(status, 200)
        self.assertEqual(bootstrap["contractVersion"], "marketplace_v1")
        self.assertGreaterEqual(len(bootstrap["stores"]), 6)
        status, discovery, _ = self.request("GET", "/api/discovery?areaId=demo_area_seeb&limit=3")
        self.assertEqual(status, 200)
        self.assertLessEqual(len(discovery["products"]), 3)
        self.assertTrue(all(item["area_id"] == "demo_area_seeb" for item in discovery["products"]))
        status, store, _ = self.request("GET", "/api/stores/demo_branch_seeb")
        self.assertEqual((status, store["store"]["branch_id"]), (200, "demo_branch_seeb"))
        status, product, _ = self.request(
            "GET", "/api/products/demo_product_seeb_1?branchId=demo_branch_seeb"
        )
        self.assertEqual((status, product["product"]["price"]), (200, "0.100"))

    def test_http_contract_catalog_exposes_review_branch_and_push_routes(self):
        status, response, _ = self.request("GET", "/api/contracts")
        self.assertEqual(status, 200, response)
        contracts = response["contracts"]
        for route in {
            "GET /api/admin/moderation/{resource}/{id}",
            "GET /api/admin/moderation/{resource}/{id}/media/{mediaId}",
            "POST /api/admin/resources/{resource}/{action}",
            "POST /api/merchant/branches/{branchId}/submit",
            "PATCH /api/merchant/branches/{branchId}/hours",
            "GET /api/admin/branches/{branchId}/launch",
            "POST /api/admin/branches/{branchId}/decision",
            "GET /api/push/status",
            "POST /api/push/subscriptions",
            "DELETE /api/push/subscriptions",
        }:
            self.assertIn(route, contracts)

    def test_secure_access_refresh_sessions_and_scoped_logout(self):
        login = self.login("96890000001", "shopper", device="phone-a")
        token, cookie = login["token"], login["_cookie"]
        self.assertNotIn("refreshToken", login)
        self.assertIn("bisa_shopper_refresh=", cookie)
        status, sessions, _ = self.request("GET", "/api/auth/sessions", token=token)
        self.assertEqual(status, 200)
        self.assertTrue(any(row["session_id"] == login["sessionId"] for row in sessions["sessions"]))
        status, rotated, _ = self.request("POST", "/api/auth/refresh", {
            "role": "shopper", "deviceId": "phone-a",
        }, cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(self.request("GET", "/api/auth/sessions", token=token)[0], 401)
        replacement = rotated["token"]
        self.assertEqual(self.request("GET", "/api/auth/sessions", token=replacement)[0], 200)
        self.assertEqual(self.request("POST", "/api/auth/logout", {}, token=replacement)[0], 200)
        self.assertEqual(self.request("GET", "/api/auth/sessions", token=replacement)[0], 401)

    def test_private_media_upload_is_owned_signed_and_never_exposes_storage_path(self):
        login = self.login("96890000001", "shopper", device="private-media")
        token = login["token"]
        status, started, _ = self.request(
            "POST", "/api/merchant/onboarding", {"action":"start"}, token,
        )
        self.assertEqual(status, 200)
        application_id = started["application"]["id"]
        png = b"\x89PNG\r\n\x1a\n" + b"BISA-private-test"
        upload_headers = {
            "Content-Type":"image/png",
            "X-BISA-Owner-Kind":"merchant_application",
            "X-BISA-Owner-Id":application_id,
            "X-BISA-Purpose":"commercial_registration",
            "X-BISA-Filename":"registration.png",
        }
        status, uploaded, _ = self.request(
            "POST", "/api/private-media", token=token, raw_body=png, extra_headers=upload_headers,
        )
        self.assertEqual(status, 201)
        media = uploaded["media"]
        self.assertNotIn("storageKey", media)
        self.assertNotIn("path", media)
        status, signed, _ = self.request(
            "POST", f"/api/private-media/{media['id']}/sign", {}, token,
        )
        self.assertEqual(status, 200)
        status, body, headers = self.request("GET", signed["route"], token=token)
        self.assertEqual((status, body, headers["Cache-Control"]), (200, png, "private, no-store"))

    def test_http_moderation_requires_authorized_review_and_one_time_receipt(self):
        merchant = self.login(
            "96892000003", "merchant_owner", device="http-moderation-merchant"
        )["token"]
        admin = self.login(
            "96897777777", "admin", pin="2468", device="http-moderation-admin"
        )["token"]
        image = self.tiny_png()
        status, uploaded, _ = self.request(
            "POST", "/api/private-media", token=merchant, raw_body=image,
            extra_headers={
                "Content-Type": "image/png",
                "X-BISA-Owner-Kind": "merchant",
                "X-BISA-Owner-Id": "demo_merchant_seeb",
                "X-BISA-Purpose": "product_image",
                "X-BISA-Filename": "moderation.png",
            },
        )
        self.assertEqual(status, 201, uploaded)
        media_id = uploaded["media"]["id"]
        status, created, _ = self.request(
            "POST", "/api/merchant/products", {
                "branchId": "demo_branch_seeb", "categoryId": "storage",
                "nameAr": "منتج مراجعة HTTP", "nameEn": "HTTP moderation item",
                "price": "0.500", "quantity": 3, "imageMediaIds": [media_id],
            }, merchant,
        )
        self.assertEqual(status, 200, created)
        product_id = created["id"]
        with closing(sqlite3.connect(self.db)) as con:
            con.execute(
                """UPDATE products SET status='pending_review',moderation_status='pending',
                   active=0,updated_at='2026-08-09T00:10:00+00:00' WHERE id=?""",
                (product_id,),
            )
            con.commit()

        status, denied, _ = self.request(
            "GET", f"/api/admin/moderation/product/{product_id}", token=merchant,
        )
        self.assertEqual((status, denied["error"]), (403, "forbidden"))
        status, review, _ = self.request(
            "GET", f"/api/admin/moderation/product/{product_id}", token=admin,
        )
        self.assertEqual(status, 200, review)
        self.assertEqual(review["resource"], "product")
        self.assertEqual(review["item"]["id"], product_id)
        serialized = json.dumps(review, ensure_ascii=False)
        self.assertNotIn("storageKey", serialized)
        self.assertNotIn("storage_key", serialized)
        self.assertNotIn(str(self.data), serialized)
        receipt = review["reviewReceipt"]
        media_url = (
            f"/api/admin/moderation/product/{product_id}/media/{media_id}"
            f"?reviewReceipt={urllib.parse.quote(receipt)}"
        )
        media_status, media_body, media_headers = self.request(
            "GET", media_url, token=admin,
        )
        self.assertEqual(media_status, 200)
        self.assertTrue(media_body.startswith(b"RIFF") and media_body[8:12] == b"WEBP")
        self.assertEqual(media_headers["Cache-Control"], "private, no-store")

        status, missing_receipt, _ = self.request(
            "POST", "/api/admin/resources/product/approve", {"id": product_id}, admin,
        )
        self.assertEqual(
            (status, missing_receipt["error"]), (409, "moderation_review_required")
        )
        decision = {"id": product_id, "reviewReceipt": receipt, "reason": "Reviewed"}
        status, approved, _ = self.request(
            "POST", "/api/admin/resources/product/approve", decision, admin,
        )
        self.assertEqual(
            (status, approved["status"], approved["moderationStatus"], approved["active"]),
            (200, "approved", "approved", True),
        )
        status, replayed, _ = self.request(
            "POST", "/api/admin/resources/product/approve", decision, admin,
        )
        self.assertEqual(
            (status, replayed["error"]),
            (409, "moderation_review_receipt_consumed"),
        )

    def test_http_branch_launch_stays_private_until_admin_approval(self):
        merchant = self.login(
            "96892000003", "merchant_owner", device="http-branch-merchant"
        )["token"]
        admin = self.login(
            "96897777777", "admin", pin="2468", device="http-branch-admin"
        )["token"]
        hours = {"sun": [{"open": "09:00", "close": "21:00"}]}
        status, created, _ = self.request(
            "POST", "/api/merchant/branches", {
                "nameAr": "فرع اختبار HTTP", "nameEn": "HTTP test branch",
                "wilayahId": "wilayat_seeb", "areaId": "demo_area_seeb",
                "address": "Seeb HTTP launch", "latitude": 23.610,
                "longitude": 58.220, "hours": hours,
            }, merchant,
        )
        self.assertEqual((status, created["status"], created["publicVisible"]), (200, "draft", False))
        branch_id = created["id"]
        status, uploaded, _ = self.request(
            "POST", "/api/private-media", token=merchant, raw_body=self.tiny_png(),
            extra_headers={
                "Content-Type": "image/png",
                "X-BISA-Owner-Kind": "merchant",
                "X-BISA-Owner-Id": "demo_merchant_seeb",
                "X-BISA-Purpose": f"branch:{branch_id}:storefront",
                "X-BISA-Filename": "storefront.png",
            },
        )
        self.assertEqual(status, 201, uploaded)
        document_id = uploaded["media"]["id"]
        status, submitted, _ = self.request(
            "POST", f"/api/merchant/branches/{branch_id}/submit", {
                "hours": hours,
                "documents": [{"kind": "storefront", "mediaId": document_id}],
            }, merchant,
        )
        self.assertEqual(
            (status, submitted["status"], submitted["publicVisible"]),
            (200, "pending_review", False),
        )
        status, merchant_review, _ = self.request(
            "GET", f"/api/merchant/branches/{branch_id}/launch", token=merchant,
        )
        self.assertEqual(status, 200, merchant_review)
        self.assertEqual(merchant_review["documents"][0]["mediaId"], document_id)
        self.assertNotIn("storage", json.dumps(merchant_review).lower())
        status, admin_review, _ = self.request(
            "GET", f"/api/admin/branches/{branch_id}/launch", token=admin,
        )
        self.assertEqual(status, 200, admin_review)
        self.assertFalse(admin_review["branch"]["publicVisible"])
        status, approved, _ = self.request(
            "POST", f"/api/admin/branches/{branch_id}/decision",
            {"decision": "approve", "note": "Verified launch"}, admin,
        )
        self.assertEqual(
            (status, approved["status"], approved["publicVisible"], approved["duplicate"]),
            (200, "approved", True, False),
        )
        status, changed, _ = self.request(
            "PATCH", f"/api/merchant/branches/{branch_id}/hours",
            {"hours": {"sun": [{"open": "10:00", "close": "22:00"}]}}, merchant,
        )
        self.assertEqual(status, 200, changed)
        self.assertEqual(changed["hours"]["sun"][0]["open"], "10:00")

    def test_http_push_status_subscribe_delete_and_logout_are_role_scoped(self):
        # The seeded Seeb owner gains a second, explicit shopper role so this
        # test exercises two scopes for the same account and browser endpoint.
        with closing(sqlite3.connect(self.db)) as con:
            con.execute(
                """INSERT OR IGNORE INTO account_roles(account_id,role,merchant_id,active)
                   VALUES('demo_account_seeb','shopper','',1)"""
            )
            con.commit()
        shopper = self.login(
            "96892000003", "shopper", device="push-dual-shopper"
        )["token"]
        merchant = self.login(
            "96892000003", "merchant_owner", device="push-dual-merchant"
        )["token"]
        subscription = self.push_subscription()
        endpoint = subscription["endpoint"]
        endpoint_hash = hashlib.sha256(endpoint.encode("utf-8")).hexdigest()

        status, unauthenticated, _ = self.request("GET", "/api/push/status")
        self.assertEqual((status, unauthenticated["error"]), (401, "authentication_required"))
        status, initial, _ = self.request("GET", "/api/push/status", token=shopper)
        self.assertEqual(status, 200, initial)
        self.assertEqual((initial["role"], initial["activeForCurrentRole"]), ("shopper", False))
        self.assertFalse(initial["available"])
        self.assertFalse(initial["configured"])
        self.assertEqual(initial["publicKey"], "")

        status, shopper_binding, _ = self.request(
            "POST", "/api/push/subscriptions", subscription, shopper,
        )
        self.assertEqual(status, 200, shopper_binding)
        status, merchant_binding, _ = self.request(
            "POST", "/api/push/subscriptions", subscription, merchant,
        )
        self.assertEqual(status, 200, merchant_binding)
        self.assertEqual(shopper_binding["subscriptionId"], merchant_binding["subscriptionId"])
        self.assertNotEqual(shopper_binding["bindingId"], merchant_binding["bindingId"])

        status, shopper_status, _ = self.request(
            "GET", f"/api/push/status?endpointHash={endpoint_hash}", token=shopper,
        )
        self.assertEqual((status, shopper_status["activeForCurrentRole"]), (200, True))
        status, merchant_status, _ = self.request(
            "GET", f"/api/push/status?endpointHash={endpoint_hash}", token=merchant,
        )
        self.assertEqual((status, merchant_status["activeForCurrentRole"]), (200, True))

        status, removed, _ = self.request(
            "DELETE", "/api/push/subscriptions", {"endpoint": endpoint}, shopper,
        )
        self.assertEqual((status, removed["deactivated"], removed["role"]), (200, 1, "shopper"))
        self.assertFalse(self.request(
            "GET", f"/api/push/status?endpointHash={endpoint_hash}", token=shopper,
        )[1]["activeForCurrentRole"])
        self.assertTrue(self.request(
            "GET", f"/api/push/status?endpointHash={endpoint_hash}", token=merchant,
        )[1]["activeForCurrentRole"])

        # Rebind the shopper scope, then regular logout deactivates only that
        # role and endpoint.  The merchant binding on the same browser survives.
        self.assertEqual(self.request(
            "POST", "/api/push/subscriptions", subscription, shopper,
        )[0], 200)
        status, logged_out, _ = self.request(
            "POST", "/api/auth/logout", {"endpoint": endpoint}, shopper,
        )
        self.assertEqual((status, logged_out["push"]["deactivated"]), (200, 1))
        self.assertEqual(self.request("GET", "/api/push/status", token=shopper)[0], 401)
        self.assertTrue(self.request(
            "GET", f"/api/push/status?endpointHash={endpoint_hash}", token=merchant,
        )[1]["activeForCurrentRole"])

        status, account_logout, _ = self.request(
            "POST", "/api/auth/logout-all", {}, merchant,
        )
        self.assertEqual(status, 200, account_logout)
        self.assertEqual(account_logout["push"]["deactivated"], 1)
        self.assertEqual(self.request("GET", "/api/push/status", token=merchant)[0], 401)

    def test_http_cart_checkout_and_pickup_order_lifecycle(self):
        shopper = self.login("96890000001", "shopper")["token"]
        merchant = self.login("96892000003", "merchant_owner")["token"]
        status, cart, _ = self.request("POST", "/api/cart/items", {
            "kind": "product", "itemId": "demo_product_seeb_1",
            "branchId": "demo_branch_seeb", "quantity": 2,
        }, shopper)
        self.assertEqual(status, 200)
        status, cart, _ = self.request("POST", "/api/cart/items", {
            "action":"set_quantity","kind":"product","itemId":"demo_product_seeb_1",
            "branchId":"demo_branch_seeb","quantity":3,"expectedVersion":cart["version"],
        }, shopper)
        self.assertEqual((status, cart["items"][0]["quantity"]), (200, 3))
        status, order_response, _ = self.request("POST", "/api/checkout", {
            "idempotencyKey": "api-checkout-one", "expectedCartVersion": cart["version"],
            "fulfillmentMode": "pickup", "paymentMethod": "pay_at_store",
        }, shopper)
        self.assertEqual(status, 200)
        order = order_response["order"]
        self.assertEqual(order["allowedActions"], ["cancel"])
        status, pending_detail, _ = self.request("GET", f"/api/orders/{order['id']}", token=shopper)
        self.assertEqual((status, pending_detail["order"]["allowedActions"]), (200, ["cancel"]))
        status, pending_orders, _ = self.request("GET", "/api/orders", token=shopper)
        pending_row = next(item for item in pending_orders["orders"] if item["id"] == order["id"])
        self.assertEqual(pending_row["allowedActions"], ["cancel"])
        status, accepted, _ = self.request("POST", f"/api/merchant/orders/{order['id']}/accept", {
            "expectedVersion": order["version"], "idempotencyKey": "accept-one",
        }, merchant)
        self.assertEqual((status, accepted["status"]), (200, "accepted"))
        version = accepted["version"]
        for action, expected in (("prepare", "preparing"), ("ready", "ready_for_pickup"), ("complete", "completed")):
            status, transition, _ = self.request("POST", f"/api/merchant/orders/{order['id']}/{action}", {
                "expectedVersion": version, "idempotencyKey": f"{action}-one",
            }, merchant)
            self.assertEqual((status, transition["status"]), (200, expected))
            version = transition["version"]
        status, detail, _ = self.request("GET", f"/api/orders/{order['id']}", token=shopper)
        self.assertEqual((status, detail["order"]["status"]), (200, "completed"))
        self.assertEqual(detail["order"]["allowedActions"], [])
        self.assertGreaterEqual(len(detail["order"]["timeline"]), 5)
        status, orders, _ = self.request("GET", "/api/orders", token=shopper)
        self.assertEqual(status, 200)
        self.assertIn(order["id"], {item["id"] for item in orders["orders"]})

    def test_shopper_addresses_and_merchant_operational_routes(self):
        shopper = self.login("96890000001", "shopper", device="address-route")["token"]
        status, address, _ = self.request("POST", "/api/addresses", {
            "addressType":"office","label":"API office","wilayahId":"wilayat_seeb",
            "areaId":"demo_area_seeb","addressText":"Knowledge Oasis",
        }, shopper)
        self.assertEqual((status, address["address_type"]), (200, "office"))
        status, addresses, _ = self.request("GET", "/api/addresses", token=shopper)
        self.assertIn(address["id"], {item["id"] for item in addresses["addresses"]})

        merchant = self.login("96892000003", "merchant_owner", device="operations-route")["token"]
        self.assertEqual(self.request("GET", "/api/merchant/settings", token=merchant)[0], 200)
        status, inventory, _ = self.request(
            "GET", "/api/merchant/inventory?branch=demo_branch_seeb", token=merchant,
        )
        self.assertEqual((status, inventory["branchId"]), (200, "demo_branch_seeb"))
        status, paused, _ = self.request(
            "POST", "/api/merchant/products/demo_product_seeb_1/action", {"action":"pause"}, merchant,
        )
        self.assertEqual((status, paused["status"]), (200, "updated"))
        self.assertEqual(self.request(
            "POST", "/api/merchant/products/demo_product_seeb_1/action", {"action":"resume"}, merchant,
        )[0], 200)
        status, campaign, _ = self.request("POST", "/api/merchant/promotions", {
            "action":"create_campaign","idempotencyKey":"api-campaign-1","payload":{
                "placement":"home_inline","landingKind":"product","landingId":"demo_product_seeb_1",
                "titleAr":"ترويج تجريبي","titleEn":"Test promotion",
            },
        }, merchant)
        self.assertEqual((status, campaign["status"], campaign["paymentStatus"]), (200, "draft", "not_started"))

    def test_product_media_upload_link_and_public_cache_contract(self):
        merchant = self.login("96892000003", "merchant_owner", device="product-media")['token']
        image = (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
            b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDAT\x08\xd7c\xf8\xcf\xc0\xf0"
            b"\x1f\x00\x05\x00\x01\xff\x89\x99=\x1d\x00\x00\x00\x00IEND\xaeB`\x82"
        )
        status, uploaded, _ = self.request(
            "POST", "/api/private-media", token=merchant, raw_body=image,
            extra_headers={
                "Content-Type":"image/png",
                "X-BISA-Owner-Kind":"merchant",
                "X-BISA-Owner-Id":"demo_merchant_seeb",
                "X-BISA-Purpose":"product_image",
                "X-BISA-Filename":"product.png",
            },
        )
        self.assertEqual(status, 201, uploaded)
        media_id = uploaded["media"]["id"]
        status, product, _ = self.request("POST", "/api/merchant/products", {
            "branchId":"demo_branch_seeb","categoryId":"storage",
            "nameAr":"منتج بصورة","nameEn":"Product with media","price":"0.500",
            "quantity":4,"imageMediaIds":[media_id],
        }, merchant)
        self.assertEqual(status, 200, product)
        self.assertEqual(product["images"], [{
            "url": f"/api/media/products/{media_id}",
            "thumbnailUrl": f"/api/media/products/{media_id}?variant=thumbnail",
        }])
        status, body, headers = self.request("GET", f"/api/media/products/{media_id}")
        self.assertEqual(status, 200)
        self.assertTrue(body.startswith(b"RIFF") and body[8:12] == b"WEBP")
        self.assertEqual(headers["Content-Type"], "image/webp")
        self.assertIn("max-age=300", headers["Cache-Control"])
        self.assertTrue(headers.get("ETag"))
        thumb_status, thumbnail, thumb_headers = self.request(
            "GET", f"/api/media/products/{media_id}?variant=thumbnail",
        )
        self.assertEqual(thumb_status, 200)
        self.assertTrue(thumbnail.startswith(b"RIFF") and thumbnail[8:12] == b"WEBP")
        self.assertEqual(thumb_headers["Content-Type"], "image/webp")

        status, rejected, _ = self.request(
            "POST", "/api/private-media", token=merchant, raw_body=image,
            extra_headers={
                "Content-Type":"image/jpeg",
                "X-BISA-Owner-Kind":"merchant",
                "X-BISA-Owner-Id":"demo_merchant_seeb",
                "X-BISA-Purpose":"product_image",
                "X-BISA-Filename":"disguised.jpg",
            },
        )
        self.assertEqual((status, rejected["error"]), (422, "private_media_signature_mismatch"))

        status, rejected, _ = self.request(
            "POST", "/api/private-media", token=merchant,
            raw_body=b"\x89PNG\r\n\x1a\nnot-a-decodable-image",
            extra_headers={
                "Content-Type":"image/png",
                "X-BISA-Owner-Kind":"merchant",
                "X-BISA-Owner-Id":"demo_merchant_seeb",
                "X-BISA-Purpose":"product_image",
                "X-BISA-Filename":"broken.png",
            },
        )
        self.assertEqual((status, rejected["error"]), (422, "private_media_invalid"))

    def test_merchant_onboarding_documents_review_and_role_activation_end_to_end(self):
        shopper_login = self.login("96893334444", "shopper", pin="2468", device="onboarding")
        shopper = shopper_login["token"]
        status, started, _ = self.request("POST", "/api/merchant/onboarding", {"action":"start"}, shopper)
        self.assertEqual(status, 200)
        application_id = started["application"]["id"]
        png = b"\x89PNG\r\n\x1a\n" + b"BISA-onboarding"

        def upload(purpose):
            upload_status, response, _ = self.request(
                "POST", "/api/private-media", token=shopper, raw_body=png,
                extra_headers={
                    "Content-Type":"image/png",
                    "X-BISA-Owner-Kind":"merchant_application",
                    "X-BISA-Owner-Id":application_id,
                    "X-BISA-Purpose":purpose,
                    "X-BISA-Filename":f"{purpose}.png",
                },
            )
            self.assertEqual(upload_status, 201, response)
            return response["media"]["id"]

        media = {key:upload(key) for key in ("logo","storefront","commercial_registration","license")}
        steps = {
            "owner":{"contactName":"API Owner","contactPhone":"96893334444","authorizedRole":"Owner"},
            "business":{"nameAr":"متجر واجهة API","nameEn":"API storefront","merchantType":"store","commercialRegistration":"CR-API-1"},
            "brand":{"logoMediaId":media["logo"]},
            "location":{"branchNameAr":"فرع مسقط","branchNameEn":"Muscat branch","wilayahId":"wilayat_muscat","areaId":"demo_area_muscat","addressText":"Muscat","latitude":23.61,"longitude":58.55},
            "hours":{"hours":{"sun":[{"open":"09:00","close":"21:00"}]}},
            "documents":{"documents":[
                {"kind":"storefront","mediaId":media["storefront"]},
                {"kind":"commercial_registration","mediaId":media["commercial_registration"]},
                {"kind":"license","mediaId":media["license"]},
            ]},
            "fulfillment":{"pickup":{"enabled":True},"office":{"enabled":False},"home":{"enabled":False},"zones":[]},
            "policy":{"returnWindowDays":7,"exchangeWindowDays":7,"conditions":"Unused","contactMethod":"Support"},
            "categories":{"categoryIds":["storage"]},
            "plan":{"planId":"early_trial"},
            "review":{"acceptedPolicies":True},
        }
        for step, data in steps.items():
            status, response, _ = self.request("POST", "/api/merchant/onboarding", {
                "action":"save_draft","applicationId":application_id,"step":step,"data":data,
            }, shopper)
            self.assertEqual(status, 200, response)
        status, submitted, _ = self.request("POST", "/api/merchant/onboarding", {
            "action":"submit","applicationId":application_id,
        }, shopper)
        self.assertEqual((status, submitted["application"]["status"]), (200, "submitted"))

        admin = self.login("96897777777", "admin", pin="2468", device="admin-review")["token"]
        status, review, _ = self.request(
            "GET", f"/api/admin/merchant-applications/{application_id}", token=admin,
        )
        self.assertEqual(status, 200, review)
        self.assertEqual({item["kind"] for item in review["documents"]}, {
            "storefront", "commercial_registration", "license",
        })
        for document in review["documents"]:
            status, signed, _ = self.request(
                "POST", f"/api/private-media/{document['media_id']}/sign", {}, admin,
            )
            self.assertEqual(status, 200, signed)
            self.assertEqual(self.request("GET", signed["route"], token=admin)[0], 200)
            status, decision, _ = self.request(
                "POST",
                f"/api/admin/merchant-applications/{application_id}/documents/{document['id']}/decision",
                {"decision":"approve","note":"Verified"}, admin,
            )
            self.assertEqual((status, decision["status"]), (200, "approved"))
        status, approved, _ = self.request(
            "POST", f"/api/admin/merchant-applications/{application_id}/decision",
            {"decision":"approve","note":"Verified"}, admin,
        )
        self.assertEqual((status, approved["status"], approved["subscriptionStatus"]), (200, "approved", "active"))
        merchant = self.login("96893334444", "merchant_owner", pin="2468", device="merchant-space")["token"]
        status, dashboard, _ = self.request("GET", "/api/merchant/dashboard", token=merchant)
        self.assertEqual(status, 200, dashboard)
        self.assertEqual(dashboard["merchant"]["id"], approved["merchantId"])

    def test_product_idor_and_admin_permissions_are_enforced_over_http(self):
        merchant = self.login("96892000003", "merchant_owner")["token"]
        status, denied, _ = self.request("POST", "/api/merchant/products", {
            "id": "demo_product_muscat_1", "branchId": "demo_branch_seeb",
            "categoryId": "storage", "nameAr": "استيلاء", "nameEn": "Hijack",
            "price": "0.100", "quantity": 1,
        }, merchant)
        self.assertEqual((status, denied["error"]), (404, "product_not_found"))
        admin = self.login("96897777777", "admin", pin="2468")["token"]
        status, overview, _ = self.request("GET", "/api/admin/overview", token=admin)
        self.assertEqual(status, 200)
        self.assertIn("merchant.review", overview["permissions"])
        self.assertEqual(self.request("GET", "/api/admin/overview", token=merchant)[0], 403)

    def test_untrusted_origin_is_not_reflected(self):
        status, data, headers = self.request(
            "GET", "/api/bootstrap", origin="https://attacker.invalid"
        )
        self.assertEqual((status, data["error"]), (403, "origin_not_allowed"))
        self.assertNotIn("Access-Control-Allow-Origin", headers)


if __name__ == "__main__":
    unittest.main(verbosity=2)
