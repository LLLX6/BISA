"""Minimal production-shaped HTTP server for BISA.

The module deliberately uses only the Python standard library for the HTTP
surface. Business rules and authorization remain in ``bisa_domain``.
"""

from __future__ import annotations

import json
import hashlib
import mimetypes
import os
import re
import threading
import uuid
import warnings
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from PIL import Image, ImageOps, UnidentifiedImageError

from bisa_application import APPLICATION, api_contracts
from bisa_config import (
    ALLOWED_ORIGINS, APP_VERSION, BRAND, ENVIRONMENT, UPLOAD_DIR,
    ensure_runtime_directories, production_readiness,
)
from bisa_domain import (
    DomainError,
    init_db,
    verify_or_register_account,
)
from bisa_integrations import default_registry
from bisa_push import BisaPushService, push_transport_from_environment
from bisa_security import (
    SecurityError,
    authenticate_access,
    clear_refresh_cookie_header,
    guarded_credentials,
    issue_session,
    list_account_sessions,
    logout_session,
    refresh_token_from_cookie,
    register_private_media,
    resolve_private_media_path,
    revoke_account_sessions,
    rotate_refresh_token,
    security_production_readiness,
    session_http_exchange,
    signed_private_media_route,
    validate_private_blob,
)


ROOT = Path(__file__).resolve().parent
PUBLIC = ROOT / "public"
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8080"))
MAX_JSON_BYTES = min(2_000_000, max(16_384, int(os.environ.get("BISA_MAX_JSON_BYTES", "524288"))))
MAX_PRIVATE_MEDIA_BYTES = min(
    25 * 1024 * 1024,
    max(1024, int(os.environ.get("BISA_PRIVATE_MEDIA_MAX_BYTES", str(10 * 1024 * 1024)))),
)
SERVICE = APPLICATION
INTEGRATIONS = default_registry()
PUSH = BisaPushService(transport=push_transport_from_environment())
PUSH_STOP = threading.Event()

MAX_PRODUCT_IMAGE_PIXELS = 25_000_000
MAX_PRODUCT_IMAGE_SIDE = 12_000
PRODUCT_IMAGE_OUTPUT_SIZE = (1600, 1600)
PRODUCT_THUMBNAIL_OUTPUT_SIZE = (480, 480)


def _normalize_product_image(blob: bytes, declared_mime: str) -> tuple[bytes, bytes, tuple[int, int]]:
    """Decode, orient and bound a product image before it reaches storage.

    The normalized WebP intentionally carries no client EXIF/profile metadata.
    Product images are never cropped; ``thumbnail`` preserves their aspect ratio.
    """

    expected_formats = {
        "image/jpeg": {"JPEG"},
        "image/png": {"PNG"},
        "image/webp": {"WEBP"},
    }
    if declared_mime not in expected_formats:
        raise DomainError("private_media_invalid", 422)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(blob)) as source:
                if (source.format or "").upper() not in expected_formats[declared_mime]:
                    raise DomainError("private_media_type_mismatch", 422)
                width, height = source.size
                if (
                    width < 1 or height < 1
                    or width > MAX_PRODUCT_IMAGE_SIDE or height > MAX_PRODUCT_IMAGE_SIDE
                    or width * height > MAX_PRODUCT_IMAGE_PIXELS
                ):
                    raise DomainError("product_image_dimensions_invalid", 422)
                if getattr(source, "n_frames", 1) != 1:
                    raise DomainError("product_image_animation_not_allowed", 422)
                source.load()
                normalized = ImageOps.exif_transpose(source)
                has_alpha = normalized.mode in {"RGBA", "LA"} or "transparency" in normalized.info
                normalized = normalized.convert("RGBA" if has_alpha else "RGB")
                normalized.thumbnail(PRODUCT_IMAGE_OUTPUT_SIZE, Image.Resampling.LANCZOS)
                output = BytesIO()
                normalized.save(output, "WEBP", quality=84, method=6, exact=has_alpha)
                thumbnail = normalized.copy()
                thumbnail.thumbnail(PRODUCT_THUMBNAIL_OUTPUT_SIZE, Image.Resampling.LANCZOS)
                thumbnail_output = BytesIO()
                thumbnail.save(thumbnail_output, "WEBP", quality=78, method=6, exact=has_alpha)
                return output.getvalue(), thumbnail_output.getvalue(), normalized.size
    except DomainError:
        raise
    except (Image.DecompressionBombError, UnidentifiedImageError, OSError, ValueError) as exc:
        raise DomainError("private_media_invalid", 422) from exc


def _runtime_readiness() -> dict:
    base = production_readiness()
    security = security_production_readiness()
    errors = [*base.get("errors", []), *security.get("errors", [])]
    push = PUSH.capability()
    push_values_present = any(os.environ.get(key) for key in (
        "BISA_VAPID_PUBLIC_KEY", "BISA_VAPID_PRIVATE_KEY", "BISA_VAPID_SUBJECT",
    ))
    push_required = os.environ.get("BISA_PUSH_REQUIRED", "false").strip().lower() in {
        "1", "true", "yes", "on",
    }
    if ENVIRONMENT == "production" and push_values_present and not push["available"]:
        errors.append(push["errorCode"] or "push_transport_not_ready")
    if ENVIRONMENT == "production" and push_required and not push["available"]:
        errors.append("push_required_but_unavailable")
    return {
        **base,
        "ready": not errors,
        "errors": errors,
        "security": security.get("checks", {}),
        "push": push,
    }


def _push_worker_loop() -> None:
    poll_seconds = max(2, min(int(os.environ.get("BISA_PUSH_POLL_SECONDS", "8")), 60))
    batch_size = max(1, min(int(os.environ.get("BISA_PUSH_BATCH_SIZE", "20")), 100))
    while not PUSH_STOP.wait(poll_seconds):
        try:
            PUSH.run_once(limit=batch_size)
        except Exception as exc:  # never log endpoints, payloads, or credentials
            print(f"BISA push worker skipped cycle: {type(exc).__name__}")


def _start_push_worker():
    if not PUSH.capability()["available"]:
        return None
    thread = threading.Thread(target=_push_worker_loop, name="bisa-push-worker", daemon=True)
    thread.start()
    return thread


def _flat_query(query: dict[str, list[str]]) -> dict:
    result = {key: values[-1] if values else "" for key, values in query.items()}
    for key in (
        "openNow", "inStock", "pickup", "officeDelivery", "homeDelivery",
        "freeDelivery", "verified", "pendingOnly",
    ):
        if key in result:
            result[key] = str(result[key]).strip().lower() in {"1", "true", "yes", "on"}
    return result


def _parts(path: str) -> list[str]:
    return [unquote(value) for value in path.strip("/").split("/") if value]


def _json_bytes(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


class BisaHandler(BaseHTTPRequestHandler):
    server_version = "BISA/0.2"

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
        self.send_header(
            "Access-Control-Allow-Headers",
            "Authorization, Content-Type, Idempotency-Key, X-BISA-Owner-Kind, "
            "X-BISA-Owner-Id, X-BISA-Purpose, X-BISA-Filename",
        )
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, PATCH, DELETE, OPTIONS")

    def _send_json(
        self, status: int, payload,
        *, headers: dict[str, str | list[str] | tuple[str, ...]] | None = None,
    ):
        body = _json_bytes(payload)
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        if ENVIRONMENT == "production":
            self.send_header("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        for key, value in (headers or {}).items():
            values = value if isinstance(value, (list, tuple)) else (value,)
            for item in values:
                self.send_header(key, item)
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

    def _private_media_body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError as exc:
            raise DomainError("invalid_content_length", 400) from exc
        if length <= 0 or length > MAX_PRIVATE_MEDIA_BYTES:
            raise DomainError("private_media_invalid", 413 if length > MAX_PRIVATE_MEDIA_BYTES else 400)
        blob = self.rfile.read(length)
        if len(blob) != length:
            raise DomainError("private_media_incomplete", 400)
        return blob

    def _store_private_media(self, actor: dict) -> dict:
        mime_type = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        owner_kind = (self.headers.get("X-BISA-Owner-Kind") or "").strip()
        owner_id = (self.headers.get("X-BISA-Owner-Id") or "").strip()
        purpose = (self.headers.get("X-BISA-Purpose") or "document").strip()[:80]
        original_name = (self.headers.get("X-BISA-Filename") or "upload").strip()[:180]
        blob = self._private_media_body()
        validate_private_blob(mime_type, blob)
        thumbnail_blob = b""
        if purpose == "product_image":
            blob, thumbnail_blob, _ = _normalize_product_image(blob, mime_type)
            mime_type = "image/webp"
            original_name = f"{Path(original_name).stem[:160] or 'product'}.webp"
        extensions = {
            "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
            "application/pdf": ".pdf",
        }
        if mime_type not in extensions:
            raise DomainError("private_media_invalid", 422)
        ensure_runtime_directories()
        storage_key = f"private/{owner_kind}/{uuid.uuid4().hex}{extensions[mime_type]}"
        candidate = (UPLOAD_DIR / storage_key).resolve()
        thumbnail_key = (
            str(Path(storage_key).with_name(f"{Path(storage_key).stem}.thumb.webp")).replace("\\", "/")
            if thumbnail_blob else ""
        )
        thumbnail_candidate = (UPLOAD_DIR / thumbnail_key).resolve() if thumbnail_key else None
        upload_root = UPLOAD_DIR.resolve()
        if upload_root not in candidate.parents or (
            thumbnail_candidate is not None and upload_root not in thumbnail_candidate.parents
        ):
            raise DomainError("private_media_path_invalid", 500)
        candidate.parent.mkdir(parents=True, exist_ok=True)
        try:
            with candidate.open("xb") as stream:
                stream.write(blob)
            if thumbnail_candidate is not None:
                with thumbnail_candidate.open("xb") as stream:
                    stream.write(thumbnail_blob)
            return register_private_media(
                actor, owner_kind=owner_kind, owner_id=owner_id, purpose=purpose,
                storage_key=storage_key, mime_type=mime_type, byte_size=len(blob),
                sha256_hex=hashlib.sha256(blob).hexdigest(), original_name=original_name,
            )
        except Exception:
            try:
                candidate.unlink(missing_ok=True)
                if thumbnail_candidate is not None:
                    thumbnail_candidate.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    def _token(self) -> str:
        header = self.headers.get("Authorization") or ""
        return header[7:].strip() if header.lower().startswith("bearer ") else ""

    def _actor(self, required=False):
        token = self._token()
        actor = authenticate_access(token) if token else None
        if required and not actor:
            raise DomainError("authentication_required", 401)
        return actor

    def _run(self, fn):
        try:
            return fn()
        except DomainError as exc:
            self._send_json(exc.status, {"ok": False, "error": exc.code, "detail": exc.detail})
        except SecurityError as exc:
            self._send_json(exc.status, {"ok": False, "error": exc.code, "detail": exc.detail})
        except Exception as exc:
            request_id = uuid.uuid4().hex
            self.log_error("request_failed id=%s type=%s", request_id, type(exc).__name__)
            self._send_json(500, {"ok": False, "error": "internal_error", "requestId": request_id})

    def _send_private_file(self, path: Path):
        body = path.read_bytes()
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "private, no-store")
        self.send_header("Content-Security-Policy", "default-src 'none'; sandbox")
        self.send_header("X-Content-Type-Options", "nosniff")
        if ENVIRONMENT == "production":
            self.send_header("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        self.send_header("Content-Disposition", "inline")
        self.end_headers()
        self.wfile.write(body)

    def _send_private_media_descriptor(self, media: dict):
        path = Path(media["path"])
        body = path.read_bytes()
        raw_etag = str(media.get("etag") or "")
        etag = raw_etag if raw_etag.startswith(('"', 'W/"')) else (f'"{raw_etag}"' if raw_etag else "")
        if etag and self.headers.get("If-None-Match") == etag:
            self.send_response(HTTPStatus.NOT_MODIFIED)
            self._cors()
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "private, no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            return
        self.send_response(HTTPStatus.OK)
        self._cors()
        self.send_header("Content-Type", str(media.get("mimeType") or "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "private, no-store")
        self.send_header("Content-Security-Policy", "default-src 'none'; sandbox")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Disposition", "inline")
        if etag:
            self.send_header("ETag", etag)
        if ENVIRONMENT == "production":
            self.send_header("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        self.end_headers()
        self.wfile.write(body)

    def _send_public_product_media(self, media: dict):
        path = Path(media["path"])
        raw_etag = str(media.get("etag") or "")
        etag = raw_etag if raw_etag.startswith(('"', 'W/"')) else (f'"{raw_etag}"' if raw_etag else "")
        if etag and self.headers.get("If-None-Match") == etag:
            self.send_response(HTTPStatus.NOT_MODIFIED)
            self._cors()
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "public, max-age=300, must-revalidate")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            return
        body = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self._cors()
        self.send_header("Content-Type", str(media.get("mimeType") or "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "public, max-age=300, must-revalidate")
        self.send_header("Content-Security-Policy", "default-src 'none'; sandbox")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if etag:
            self.send_header("ETag", etag)
        if ENVIRONMENT == "production":
            self.send_header("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        self.end_headers()
        self.wfile.write(body)

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
        query = _flat_query(parse_qs(parsed.query))
        parts = _parts(path)

        def route():
            if path == "/healthz":
                return self._send_json(200, {"ok": True, "service": "bisa-api", "version": APP_VERSION})
            if path == "/readyz":
                result = _runtime_readiness()
                return self._send_json(200 if result["ready"] else 503, {"ok": result["ready"], **result})
            if path == "/api/config":
                integrations = INTEGRATIONS.snapshot()
                integrations["push"] = PUSH.capability()
                return self._send_json(200, {
                    "ok": True, "brand": BRAND, "environment": ENVIRONMENT,
                    "integrations": integrations,
                })
            if path == "/api/contracts":
                return self._send_json(200, {"ok": True, "contracts": api_contracts()})
            if path == "/api/bootstrap":
                data = SERVICE.public_bootstrap(self._actor())
                capabilities = dict(data.get("capabilities") or {})
                integrations = INTEGRATIONS.snapshot()
                integrations["push"] = PUSH.capability()
                integrations["maps"] = capabilities.get("maps") or {
                    "available": False, "configured": False,
                    "status": "unavailable", "errorCode": "maps_not_configured",
                }
                capabilities["integrations"] = integrations
                data["capabilities"] = capabilities
                return self._send_json(200, {"ok": True, **data})
            if path in {"/api/discovery", "/api/search"}:
                filters = dict(query)
                if path == "/api/search":
                    filters.update({
                        "query": query.get("q", ""),
                        "categoryId": query.get("category", ""),
                        "branchId": query.get("branch", ""),
                    })
                data = SERVICE.discovery(filters, self._actor())
                return self._send_json(200, {"ok": True, **data})
            if len(parts) == 3 and parts[:2] == ["api", "stores"]:
                data = SERVICE.store_detail(parts[2], product_limit=query.get("limit", 24), cursor=query.get("cursor", 0))
                return self._send_json(200, {"ok": True, "store": data})
            if len(parts) == 3 and parts[:2] == ["api", "products"]:
                data = SERVICE.product_detail(parts[2], query.get("branchId", ""))
                return self._send_json(200, {"ok": True, "product": data})
            if len(parts) == 4 and parts[:3] == ["api", "media", "products"]:
                media = SERVICE.resolve_public_product_media(parts[3], query.get("variant", ""))
                return self._send_public_product_media(media)
            if len(parts) == 3 and parts[:2] == ["api", "merchant-assets"]:
                asset = SERVICE.resolve_merchant_brand_asset(self._actor(), parts[2])
                return self._send_private_media_descriptor({
                    "path": asset.path,
                    "mimeType": asset.mime_type,
                    "byteSize": asset.byte_size,
                    "etag": "",
                })
            if (
                len(parts) == 5 and parts[:2] == ["api", "merchants"]
                and parts[3] == "assets"
            ):
                data = SERVICE.merchant_brand_asset_descriptor(
                    self._actor(), parts[2], parts[4],
                )
                return self._send_json(200, {"ok": True, "asset": data})
            if len(parts) == 3 and parts[:2] == ["api", "orders"]:
                data = SERVICE.order_detail(self._actor(True), parts[2])
                return self._send_json(200, {"ok": True, "order": data})
            if path == "/api/orders":
                data = SERVICE.orders(self._actor(True), limit=query.get("limit", 100))
                return self._send_json(200, {"ok": True, "orders": data})
            if path == "/api/notifications":
                data = SERVICE.notifications(
                    self._actor(True), pending_only=bool(query.get("pendingOnly")),
                    limit=query.get("limit", 100),
                )
                return self._send_json(200, {"ok": True, "notifications": data})
            if path == "/api/push/status":
                data = PUSH.status(
                    self._actor(True), endpoint_hash=query.get("endpointHash", ""),
                )
                return self._send_json(200, {"ok": True, **data})
            if path == "/api/merchant/dashboard":
                return self._send_json(200, {"ok": True, **SERVICE.merchant_dashboard(self._actor(True))})
            if path == "/api/merchant/analytics":
                return self._send_json(200, {"ok": True, **SERVICE.merchant_analytics(self._actor(True))})
            if path == "/api/merchant/settings":
                return self._send_json(200, {"ok": True, **SERVICE.merchant_settings(self._actor(True))})
            if path == "/api/merchant/onboarding":
                return self._send_json(200, {"ok": True, **SERVICE.merchant_onboarding(self._actor(True), {"action":"status"})})
            if (
                len(parts) == 5 and parts[:3] == ["api", "merchant", "branches"]
                and parts[4] == "launch"
            ):
                return self._send_json(
                    200,
                    {"ok": True, **SERVICE.branch_launch_detail(self._actor(True), parts[3])},
                )
            if path in {"/api/merchant/stock", "/api/merchant/inventory"}:
                branch = query.get("branch", "") or query.get("branchId", "")
                return self._send_json(200, {"ok": True, **SERVICE.quick_stock(self._actor(True), branch)})
            if path == "/api/merchant/suppliers":
                return self._send_json(200, {"ok": True, "campaigns": SERVICE.supplier_campaigns(self._actor(True))})
            if path == "/api/supplier/dashboard":
                return self._send_json(200, {"ok": True, **SERVICE.supplier_dashboard(self._actor(True))})
            if path == "/api/supplier/campaigns":
                return self._send_json(200, {"ok": True, **SERVICE.own_supplier_campaigns(self._actor(True), query)})
            if len(parts) == 4 and parts[:3] == ["api", "supplier", "campaigns"]:
                return self._send_json(200, {"ok": True, **SERVICE.supplier_campaign_detail(self._actor(True), parts[3])})
            if len(parts) == 5 and parts[:3] == ["api", "supplier", "campaigns"] and parts[4] == "creative":
                media = SERVICE.resolve_supplier_campaign_creative(self._actor(True), parts[3])
                return self._send_private_media_descriptor(media)
            if path == "/api/supplier/leads":
                return self._send_json(200, {"ok": True, **SERVICE.supplier_leads(self._actor(True), query)})
            if path == "/api/admin/overview":
                return self._send_json(200, {"ok": True, **SERVICE.admin_overview(self._actor(True))})
            if (
                len(parts) == 5 and parts[:3] == ["api", "admin", "branches"]
                and parts[4] == "launch"
            ):
                return self._send_json(
                    200,
                    {"ok": True, **SERVICE.branch_launch_detail(self._actor(True), parts[3])},
                )
            if (
                len(parts) == 7
                and parts[:3] == ["api", "admin", "moderation"]
                and parts[5] == "media"
            ):
                media = SERVICE.resolve_moderation_media(
                    self._actor(True), parts[3], parts[4], parts[6],
                    str(query.get("reviewReceipt") or ""),
                )
                return self._send_private_media_descriptor(media)
            if len(parts) == 5 and parts[:3] == ["api", "admin", "moderation"]:
                return self._send_json(
                    200,
                    {
                        "ok": True,
                        **SERVICE.moderation_review_detail(
                            self._actor(True), parts[3], parts[4],
                        ),
                    },
                )
            if len(parts) == 4 and parts[:3] == ["api", "admin", "resources"]:
                return self._send_json(200, {"ok": True, **SERVICE.admin_resource(self._actor(True), parts[3], query)})
            if len(parts) == 4 and parts[:3] == ["api", "admin", "merchant-applications"]:
                return self._send_json(
                    200, {"ok": True, **SERVICE.admin_application_detail(self._actor(True), parts[3])},
                )
            if path == "/api/auth/sessions":
                return self._send_json(200, {"ok": True, "sessions": list_account_sessions(self._actor(True))})
            if path == "/api/addresses":
                return self._send_json(200, {"ok": True, "addresses": SERVICE.addresses(self._actor(True))})
            if len(parts) == 3 and parts[:2] == ["api", "private-media"]:
                actor = self._actor(True)
                resolved = resolve_private_media_path(
                    actor, parts[2], query.get("exp", ""), query.get("sig", ""),
                )
                return self._send_private_file(resolved)
            if path.startswith("/api/"):
                raise DomainError("endpoint_not_found", 404)
            return self._serve_static(parsed.path)

        return self._run(route)

    def do_POST(self):
        if not self._origin_allowed():
            return self._send_json(403, {"ok": False, "error": "origin_not_allowed"})
        path = urlparse(self.path).path.rstrip("/") or "/"
        parts = _parts(path)

        def route():
            if path == "/api/private-media":
                media = self._store_private_media(self._actor(True))
                return self._send_json(201, {"ok": True, "media": media})
            payload = self._body()
            if path == "/api/auth":
                phone = payload.get("phone")
                actor = guarded_credentials(
                    phone,
                    lambda: verify_or_register_account(
                        phone, payload.get("pin"), payload.get("name", ""), payload.get("role", "shopper"),
                    ),
                    source_id=self.client_address[0] if self.client_address else "",
                )
                result = issue_session(
                    actor["accountId"], actor["role"], actor.get("merchantId", ""),
                    str(payload.get("deviceId") or "")[:120],
                )
                body, cookie = session_http_exchange(result)
                return self._send_json(200, {"ok": True, **body}, headers={"Set-Cookie": cookie})
            if path == "/api/auth/refresh":
                role = str(payload.get("role") or "")
                result = rotate_refresh_token(
                    refresh_token_from_cookie(self.headers.get("Cookie") or "", role),
                    device_id=str(payload.get("deviceId") or "")[:120],
                )
                body, cookie = session_http_exchange(result)
                return self._send_json(200, {"ok": True, **body}, headers={"Set-Cookie": cookie})
            if path == "/api/auth/logout":
                actor = self._actor(True)
                endpoint = str(payload.get("endpoint") or "").strip()
                try:
                    push_result = (
                        PUSH.logout_scope(actor, endpoint) if endpoint
                        else {"deactivated": 0, "role": actor["role"]}
                    )
                except DomainError as exc:
                    # A stale or malformed browser endpoint must never trap an
                    # account in an authenticated session. Session revocation
                    # remains authoritative; no unrelated device is disabled.
                    push_result = {
                        "deactivated": 0, "role": actor["role"],
                        "errorCode": exc.code,
                    }
                revoked = logout_session(self._token())
                return self._send_json(
                    200, {"ok": True, "revoked": revoked, "push": push_result},
                    headers={"Set-Cookie": clear_refresh_cookie_header(actor["role"])},
                )
            if path == "/api/auth/logout-all":
                actor = self._actor(True)
                try:
                    push_result = PUSH.logout_account(actor["accountId"])
                except Exception:
                    push_result = {
                        "deactivated": 0, "accountId": actor["accountId"],
                        "errorCode": "push_logout_failed",
                    }
                count = revoke_account_sessions(actor["accountId"], reason="user_logout_all")
                roles = (
                    "shopper", "merchant_owner", "merchant_manager", "merchant_staff",
                    "supplier_advertiser", "support_admin", "catalog_moderator",
                    "merchant_reviewer", "finance", "advertising_manager", "admin", "super_admin",
                )
                return self._send_json(
                    200, {"ok": True, "revokedSessions": count, "push": push_result},
                    headers={"Set-Cookie": [clear_refresh_cookie_header(role) for role in roles]},
                )
            if path == "/api/auth/switch-role":
                actor = self._actor(True)
                result = issue_session(
                    actor["accountId"], str(payload.get("role") or ""),
                    str(payload.get("merchantId") or ""), str(payload.get("deviceId") or "")[:120],
                )
                body, cookie = session_http_exchange(result)
                return self._send_json(200, {"ok": True, **body}, headers={"Set-Cookie": cookie})
            if path == "/api/analytics/events":
                result = SERVICE.record_event(
                    self._actor(), str(payload.get("eventType") or ""),
                    str(payload.get("entityKind") or ""), str(payload.get("entityId") or ""),
                    payload.get("context"),
                )
                return self._send_json(200, {"ok": True, **result})
            actor = self._actor(True)
            if path == "/api/push/subscriptions":
                result = PUSH.subscribe(actor, payload)
            elif path == "/api/merchant/apply":
                raise DomainError("merchant_onboarding_required", 410, {"route":"/api/merchant/onboarding"})
            elif path == "/api/merchant/onboarding":
                result = SERVICE.merchant_onboarding(actor, payload)
            elif path in {"/api/merchant/product", "/api/merchant/products"}:
                result = SERVICE.upsert_product(actor, payload)
            elif len(parts) == 5 and parts[:3] == ["api", "merchant", "products"] and parts[4] == "action":
                result = SERVICE.product_action(actor, parts[3], payload)
            elif path in {"/api/merchant/bundle", "/api/merchant/bundles"}:
                result = SERVICE.create_bundle(actor, payload)
            elif path in {"/api/merchant/stock", "/api/merchant/inventory/verify"}:
                result = SERVICE.confirm_stock(actor, str(payload.get("branchId") or ""), payload.get("changes") or [])
            elif path == "/api/merchant/inventory/action":
                result = SERVICE.inventory_action(actor, payload)
            elif len(parts) == 6 and parts[:3] == ["api", "merchant", "inventory"] and parts[3] == "audits" and parts[5] == "confirm-remaining":
                result = SERVICE.confirm_inventory_remaining(actor, parts[4])
            elif path == "/api/merchant/branches":
                result = SERVICE.create_branch(actor, payload)
            elif (
                len(parts) == 5 and parts[:3] == ["api", "merchant", "branches"]
                and parts[4] == "submit"
            ):
                result = SERVICE.submit_branch_for_review(actor, parts[3], payload)
            elif path == "/api/merchant/return-policies":
                result = SERVICE.save_return_policy(actor, payload)
            elif path == "/api/merchant/members":
                result = SERVICE.add_merchant_member(actor, payload)
            elif len(parts) == 5 and parts[:3] == ["api", "merchant", "suppliers"] and parts[4] == "leads":
                result = SERVICE.create_supplier_lead(
                    actor, parts[3], str(payload.get("action") or ""), str(payload.get("note") or ""),
                    idempotency_key=str(payload.get("idempotencyKey") or self.headers.get("Idempotency-Key") or ""),
                )
            elif path in {"/api/cart", "/api/cart/items"}:
                if path == "/api/cart/items" and payload.get("action") == "set_quantity":
                    result = SERVICE.update_cart_item(
                        actor, str(payload.get("kind") or ""), str(payload.get("itemId") or ""), payload,
                    )
                else:
                    result = SERVICE.add_cart(actor, payload)
            elif path == "/api/addresses":
                result = SERVICE.save_address(actor, payload)
            elif path == "/api/merchant/promotions":
                payload["idempotencyKey"] = payload.get("idempotencyKey") or self.headers.get("Idempotency-Key") or ""
                result = SERVICE.merchant_campaign_action(actor, payload)
            elif path == "/api/merchant/settings":
                raise DomainError("merchant_settings_read_only", 405, {"route":"/api/merchant/promotions"})
            elif path == "/api/supplier/campaigns":
                payload["idempotencyKey"] = payload.get("idempotencyKey") or self.headers.get("Idempotency-Key") or ""
                result = SERVICE.save_supplier_campaign(actor, payload)
            elif len(parts) == 5 and parts[:3] == ["api", "supplier", "campaigns"] and parts[4] == "submit":
                payload["idempotencyKey"] = payload.get("idempotencyKey") or self.headers.get("Idempotency-Key") or ""
                result = SERVICE.submit_supplier_campaign(actor, parts[3], payload)
            elif path == "/api/checkout":
                payload["idempotencyKey"] = payload.get("idempotencyKey") or self.headers.get("Idempotency-Key") or ""
                result = SERVICE.checkout(actor, payload)
            elif path == "/api/merchant/order":
                result = SERVICE.decide_order(actor, str(payload.get("orderId") or ""), str(payload.get("decision") or ""))
            elif len(parts) == 5 and parts[:3] == ["api", "merchant", "orders"]:
                target = {
                    "accept": "accepted", "reject": "rejected", "prepare": "preparing",
                    "ready": "ready_for_pickup", "dispatch": "out_for_delivery", "complete": "completed",
                }.get(parts[4])
                if not target:
                    raise DomainError("invalid_order_action", 422)
                result = SERVICE.transition_order(
                    actor, parts[3], target, expected_version=payload.get("expectedVersion"),
                    reason=str(payload.get("reason") or ""),
                    idempotency_key=str(payload.get("idempotencyKey") or self.headers.get("Idempotency-Key") or ""),
                )
            elif len(parts) == 4 and parts[:2] == ["api", "orders"] and parts[3] == "cancel":
                result = SERVICE.cancel_order(
                    actor, parts[2], str(payload.get("reason") or ""), payload.get("expectedVersion"),
                )
            elif len(parts) == 4 and parts[:2] == ["api", "products"] and parts[3] == "reports":
                result = SERVICE.report_product(
                    actor, parts[2], str(payload.get("reason") or ""), str(payload.get("detail") or ""),
                    branch_id=str(payload.get("branchId") or ""),
                )
            elif len(parts) == 4 and parts[:2] == ["api", "notifications"]:
                result = SERVICE.notification_action(actor, parts[2], parts[3])
            elif len(parts) == 5 and parts[:3] == ["api", "admin", "merchant-applications"] and parts[4] == "decision":
                payload["applicationId"] = parts[3]
                result = SERVICE.admin_application_decision(actor, payload)
            elif (
                len(parts) == 7 and parts[:3] == ["api", "admin", "merchant-applications"]
                and parts[4] == "documents" and parts[6] == "decision"
            ):
                result = SERVICE.admin_application_document_decision(
                    actor, parts[3], parts[5], payload,
                )
            elif path == "/api/admin/merchant-application":
                result = SERVICE.admin_application_decision(actor, payload)
            elif len(parts) == 5 and parts[:3] == ["api", "admin", "resources"]:
                result = SERVICE.admin_action(actor, parts[3], parts[4], payload)
            elif (
                len(parts) == 5 and parts[:3] == ["api", "admin", "branches"]
                and parts[4] == "decision"
            ):
                result = SERVICE.admin_branch_decision(actor, parts[3], payload)
            elif path == "/api/admin/demo-data/purge":
                result = SERVICE.purge_demo_data(actor, str(payload.get("confirmation") or ""))
            elif len(parts) == 4 and parts[:2] == ["api", "private-media"] and parts[3] == "sign":
                result = signed_private_media_route(actor, parts[2], ttl_seconds=300)
            else:
                raise DomainError("endpoint_not_found", 404)
            return self._send_json(200, {"ok": True, **(result if isinstance(result, dict) else {"data": result})})

        return self._run(route)

    def do_DELETE(self):
        if not self._origin_allowed():
            return self._send_json(403, {"ok": False, "error": "origin_not_allowed"})
        path = urlparse(self.path).path.rstrip("/") or "/"

        def route():
            payload = self._body()
            actor = self._actor(True)
            if path == "/api/push/subscriptions":
                result = PUSH.unsubscribe(actor, payload.get("endpoint"))
            else:
                raise DomainError("endpoint_not_found", 404)
            return self._send_json(200, {"ok": True, **result})

        return self._run(route)

    def do_PUT(self):
        if not self._origin_allowed():
            return self._send_json(403, {"ok": False, "error": "origin_not_allowed"})
        path = urlparse(self.path).path.rstrip("/") or "/"
        parts = _parts(path)

        def route():
            payload = self._body()
            actor = self._actor(True)
            if len(parts) == 4 and parts[:2] == ["api", "favorites"]:
                result = SERVICE.set_favorite(
                    actor, parts[2], parts[3], branch_id=str(payload.get("branchId") or ""),
                    saved=payload.get("saved") is not False,
                )
            elif len(parts) == 5 and parts[:3] == ["api", "merchant", "branches"] and parts[4] == "fulfillment":
                result = SERVICE.configure_fulfillment(actor, parts[3], payload)
            elif len(parts) == 5 and parts[:3] == ["api", "cart", "items"]:
                result = SERVICE.update_cart_item(actor, parts[3], parts[4], payload)
            elif len(parts) == 4 and parts[:3] == ["api", "supplier", "campaigns"]:
                payload["id"] = parts[3]
                payload["idempotencyKey"] = payload.get("idempotencyKey") or self.headers.get("Idempotency-Key") or ""
                result = SERVICE.save_supplier_campaign(actor, payload)
            else:
                raise DomainError("endpoint_not_found", 404)
            return self._send_json(200, {"ok": True, **(result if isinstance(result, dict) else {"data": result})})

        return self._run(route)

    def do_PATCH(self):
        if not self._origin_allowed():
            return self._send_json(403, {"ok": False, "error": "origin_not_allowed"})
        path = urlparse(self.path).path.rstrip("/") or "/"
        parts = _parts(path)

        def route():
            payload = self._body()
            actor = self._actor(True)
            if (
                len(parts) == 5 and parts[:3] == ["api", "merchant", "branches"]
                and parts[4] == "hours"
            ):
                result = SERVICE.update_branch_hours(actor, parts[3], payload)
            else:
                raise DomainError("endpoint_not_found", 404)
            return self._send_json(
                200,
                {"ok": True, **(result if isinstance(result, dict) else {"data": result})},
            )

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
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data: blob: https://tile.openstreetmap.org; "
            "style-src 'self'; script-src 'self'; connect-src 'self'; font-src 'self'; "
            "object-src 'none'; base-uri 'self'; frame-ancestors 'none'",
        )
        if ENVIRONMENT == "production":
            self.send_header("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        self.send_header("Cache-Control", "no-cache" if candidate.name in {"index.html", "service-worker.js"} else "public, max-age=86400")
        self.end_headers()
        self.wfile.write(body)


def main():
    init_db()
    readiness = _runtime_readiness()
    if ENVIRONMENT == "production" and not readiness["ready"]:
        raise SystemExit("BISA is not production-ready: " + ", ".join(readiness["errors"]))
    httpd = ThreadingHTTPServer((HOST, PORT), BisaHandler)
    PUSH_STOP.clear()
    push_thread = _start_push_worker()
    print(f"BISA running at http://{HOST}:{PORT}")
    try:
        httpd.serve_forever()
    finally:
        PUSH_STOP.set()
        httpd.server_close()
        if push_thread is not None:
            push_thread.join(timeout=2)


if __name__ == "__main__":
    main()
