"""Minimal production-shaped HTTP server for BISA.

The module deliberately uses only the Python standard library for the HTTP
surface. Business rules and authorization remain in ``bisa_domain``.
"""

from __future__ import annotations

import json
import mimetypes
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from bisa_config import ALLOWED_ORIGINS, APP_VERSION, BRAND, ENVIRONMENT, production_readiness
from bisa_domain import (
    BisaService,
    DomainError,
    authenticate,
    connect,
    init_db,
    register_or_login,
)


ROOT = Path(__file__).resolve().parent
PUBLIC = ROOT / "public"
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8080"))
MAX_JSON_BYTES = min(2_000_000, max(16_384, int(os.environ.get("BISA_MAX_JSON_BYTES", "524288"))))
SERVICE = BisaService()


def _json_bytes(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


class BisaHandler(BaseHTTPRequestHandler):
    server_version = "BISA/0.1"

    def log_message(self, fmt, *args):
        # Keep the normal access log but never log request bodies or bearer tokens.
        super().log_message(fmt, *args)

    def _origin_allowed(self) -> bool:
        origin = (self.headers.get("Origin") or "").rstrip("/")
        if not origin:
            return True
        if ENVIRONMENT != "production":
            parsed = urlparse(origin)
            if parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}:
                return True
        return origin in ALLOWED_ORIGINS

    def _cors(self):
        origin = (self.headers.get("Origin") or "").rstrip("/")
        if origin and self._origin_allowed():
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, Idempotency-Key")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def _send_json(self, status: int, payload):
        body = _json_bytes(payload)
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError as exc:
            raise DomainError("invalid_content_length", 400) from exc
        if length <= 0 or length > MAX_JSON_BYTES:
            raise DomainError("invalid_body_size", 413 if length > MAX_JSON_BYTES else 400)
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DomainError("invalid_json", 400) from exc
        if not isinstance(value, dict):
            raise DomainError("json_object_required", 400)
        return value

    def _actor(self, required=False):
        header = self.headers.get("Authorization") or ""
        token = header[7:].strip() if header.lower().startswith("bearer ") else ""
        actor = authenticate(token) if token else None
        if required and not actor:
            raise DomainError("authentication_required", 401)
        return actor

    def _run(self, fn):
        try:
            return fn()
        except DomainError as exc:
            self._send_json(exc.status, {"ok": False, "error": exc.code, "detail": exc.detail})
        except Exception:
            self._send_json(500, {"ok": False, "error": "internal_error"})

    def do_OPTIONS(self):
        if not self._origin_allowed():
            return self._send_json(403, {"ok": False, "error": "origin_not_allowed"})
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        if not self._origin_allowed():
            return self._send_json(403, {"ok": False, "error": "origin_not_allowed"})
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)

        def route():
            if path == "/healthz":
                return self._send_json(200, {"ok": True, "service": "bisa-api", "version": APP_VERSION})
            if path == "/readyz":
                result = production_readiness()
                return self._send_json(200 if result["ready"] else 503, {"ok": result["ready"], **result})
            if path == "/api/config":
                return self._send_json(200, {"ok": True, "brand": BRAND, "environment": ENVIRONMENT})
            if path == "/api/bootstrap":
                return self._send_json(200, {"ok": True, **SERVICE.public_bootstrap(self._actor())})
            if path == "/api/search":
                data = SERVICE.search((query.get("q") or [""])[0], (query.get("category") or [""])[0], (query.get("branch") or [""])[0])
                return self._send_json(200, {"ok": True, **data})
            if path == "/api/merchant/dashboard":
                return self._send_json(200, {"ok": True, **SERVICE.merchant_dashboard(self._actor(True))})
            if path == "/api/merchant/stock":
                branch = (query.get("branch") or [""])[0]
                return self._send_json(200, {"ok": True, **SERVICE.quick_stock(self._actor(True), branch)})
            if path == "/api/merchant/suppliers":
                return self._send_json(200, {"ok": True, "campaigns": SERVICE.supplier_campaigns(self._actor(True))})
            if path == "/api/admin/overview":
                return self._send_json(200, {"ok": True, **SERVICE.admin_overview(self._actor(True))})
            return self._serve_static(parsed.path)

        return self._run(route)

    def do_POST(self):
        if not self._origin_allowed():
            return self._send_json(403, {"ok": False, "error": "origin_not_allowed"})
        path = urlparse(self.path).path.rstrip("/")

        def route():
            payload = self._body()
            if path == "/api/auth":
                result = register_or_login(payload.get("phone"), payload.get("pin"), payload.get("name", ""), payload.get("role", "shopper"))
                return self._send_json(200, {"ok": True, **result})
            if path == "/api/auth/logout":
                actor = self._actor(True)
                with connect(immediate=True) as con:
                    con.execute("UPDATE sessions SET revoked_at=datetime('now') WHERE account_id=? AND role=?", (actor["accountId"], actor["role"]))
                return self._send_json(200, {"ok": True})
            actor = self._actor(True)
            if path == "/api/merchant/apply":
                result = SERVICE.merchant_apply(actor, payload)
            elif path == "/api/merchant/product":
                result = SERVICE.upsert_product(actor, payload)
            elif path == "/api/merchant/bundle":
                result = SERVICE.create_bundle(actor, payload)
            elif path == "/api/merchant/stock":
                result = SERVICE.confirm_stock(actor, str(payload.get("branchId") or ""), payload.get("changes") or [])
            elif path == "/api/cart":
                result = SERVICE.add_cart(actor, payload)
            elif path == "/api/checkout":
                payload["idempotencyKey"] = payload.get("idempotencyKey") or self.headers.get("Idempotency-Key") or ""
                result = SERVICE.checkout(actor, payload)
            elif path == "/api/merchant/order":
                result = SERVICE.decide_order(actor, str(payload.get("orderId") or ""), str(payload.get("decision") or ""))
            elif path == "/api/admin/merchant-application":
                result = SERVICE.admin_decide_application(actor, payload)
            elif path == "/api/admin/demo-data/purge":
                result = SERVICE.purge_demo_data(actor, str(payload.get("confirmation") or ""))
            else:
                raise DomainError("endpoint_not_found", 404)
            return self._send_json(200, {"ok": True, **(result if isinstance(result, dict) else {"data": result})})

        return self._run(route)

    def _serve_static(self, request_path: str):
        relative = request_path.lstrip("/") or "index.html"
        candidate = (PUBLIC / relative).resolve()
        public_root = PUBLIC.resolve()
        if public_root not in candidate.parents and candidate != public_root:
            return self._send_json(404, {"ok": False, "error": "not_found"})
        if candidate.is_dir():
            candidate = candidate / "index.html"
        if not candidate.exists() or not candidate.is_file():
            # History fallback is safe because the client does its own routing.
            candidate = PUBLIC / "index.html"
        body = candidate.read_bytes()
        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type + ("; charset=utf-8" if content_type.startswith("text/") else ""))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data: blob:; style-src 'self'; script-src 'self'; connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'")
        self.send_header("Cache-Control", "no-cache" if candidate.name in {"index.html", "service-worker.js"} else "public, max-age=86400")
        self.end_headers()
        self.wfile.write(body)


def main():
    init_db()
    readiness = production_readiness()
    if ENVIRONMENT == "production" and not readiness["ready"]:
        raise SystemExit("BISA is not production-ready: " + ", ".join(readiness["errors"]))
    httpd = ThreadingHTTPServer((HOST, PORT), BisaHandler)
    print(f"BISA running at http://{HOST}:{PORT}")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
