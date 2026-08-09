"""Composition root and HTTP contract map for the BISA marketplace.

The existing HTTP server can replace ``BisaService()`` with
``BisaApplication()`` incrementally.  Existing method names remain compatible;
new routes are intentionally explicit so financial, inventory and moderation
actions never use a generic client-controlled dispatcher.
"""

from __future__ import annotations

from bisa_domain import BisaService
from bisa_marketplace import MarketplaceMixin
from bisa_merchant_launch import MERCHANT_LAUNCH_ROUTE_CONTRACTS, MerchantLaunchMixin
from bisa_moderation import MODERATION_API_CONTRACTS, ModerationReviewMixin
from bisa_operations import OperationsMixin
from bisa_supplier import SUPPLIER_API_CONTRACTS, SupplierAdvertiserMixin


class BisaApplication(
    MerchantLaunchMixin,
    ModerationReviewMixin,
    SupplierAdvertiserMixin,
    OperationsMixin,
    MarketplaceMixin,
    BisaService,
):
    """Production-shaped BISA service with hardened marketplace invariants."""


APPLICATION = BisaApplication()


# Integration contract for ``bisa_server.py``.  Path parameters use braces;
# the server remains responsible for authentication and JSON/HTTP envelopes.
API_CONTRACTS = {
    "GET /api/bootstrap": {
        "method": "public_bootstrap",
        "response": [
            "contractVersion", "locations", "categories", "stores", "products",
            "bundles", "advertisements", "cart", "orders", "notifications", "actor", "settings", "plans", "favorites",
            "capabilities",
        ],
    },
    "GET /api/discovery": {
        "method": "discovery",
        "request": [
            "query", "areaId", "wilayahId", "categoryId", "branchId", "minPrice", "maxPrice",
            "openNow", "inStock", "pickup", "officeDelivery", "homeDelivery", "freeDelivery",
            "verified", "sort", "latitude", "longitude", "maxDistanceKm", "cursor", "limit",
        ],
        "response": ["products", "stores", "pagination", "filters"],
    },
    "GET /api/stores/{branchId}": {
        "method": "store_detail",
        "response": [
            "id", "branch_id", "name_ar", "name_en", "verified", "openNow", "products",
            "bundles", "returnPolicy", "pagination",
        ],
    },
    "GET /api/products/{productId}": {
        "method": "product_detail",
        "request": ["branchId"],
        "response": [
            "id", "merchant_id", "branch_id", "name_ar", "name_en", "price", "images",
            "availability", "quantity", "store", "fulfillment", "openNow",
        ],
    },
    "GET /api/media/products/{mediaId}": {
        "method": "resolve_public_product_media",
        "response": ["binary", "Content-Type", "Content-Length", "ETag", "Cache-Control"],
    },
    "POST /api/cart/items": {
        "method": "add_cart",
        "request": ["kind", "itemId", "branchId", "quantity", "expectedVersion", "replaceCart"],
        "response": ["id", "merchant_id", "branch_id", "version", "items", "subtotal"],
        "errors": ["cross_store_cart_confirmation_required", "cart_version_conflict", "item_not_available"],
    },
    "PUT /api/cart/items/{kind}/{itemId}": {
        "method": "update_cart_item",
        "request": ["quantity", "expectedVersion"],
        "response": ["id", "merchant_id", "branch_id", "version", "items", "subtotal"],
    },
    "GET /api/orders": {
        "method": "orders",
        "response": ["orders"],
    },
    "POST /api/checkout": {
        "method": "checkout",
        "request": [
            "idempotencyKey", "expectedCartVersion", "fulfillmentMode", "address",
            "paymentMethod", "acceptPriceChanges",
        ],
        "response": ["order", "duplicate", "repriced"],
        "order": [
            "id", "status", "subtotal", "deliveryFee", "total", "responseDueAt",
            "fulfillmentMode", "paymentMethod", "version",
        ],
        "errors": [
            "idempotency_key_reused", "cart_price_changed", "stock_unavailable",
            "delivery_zone_not_served", "minimum_order_not_met", "payment_adapter_not_configured",
        ],
    },
    "GET /api/orders/{orderId}": {
        "method": "order_detail",
        "response": [
            "id", "status", "version", "fulfillment_mode", "subtotal", "deliveryFee", "total",
            "address", "returnPolicy", "items", "timeline", "response_due_at",
        ],
    },
    "POST /api/orders/{orderId}/cancel": {
        "method": "cancel_order",
        "request": ["reason", "expectedVersion"],
        "response": ["id", "status", "version", "duplicate"],
    },
    "POST /api/merchant/orders/{orderId}/{action}": {
        "method": "transition_order",
        "actions": {
            "accept": "accepted", "reject": "rejected", "prepare": "preparing",
            "ready": "ready_for_pickup", "dispatch": "out_for_delivery", "complete": "completed",
        },
        "request": ["expectedVersion", "reason", "idempotencyKey"],
        "response": ["id", "status", "version", "duplicate", "expired"],
    },
    "GET /api/merchant/dashboard": {
        "method": "merchant_dashboard",
        "response": ["merchant", "branches", "products", "orders", "plan", "planUsage", "today", "metrics"],
    },
    "GET /api/merchant/analytics": {
        "method": "merchant_analytics",
        "response": ["level", "metrics"],
    },
    "GET /api/merchant/settings": {
        "method": "merchant_settings",
        "response": ["merchant", "branches", "returnPolicy", "plan", "externalIntegrations"],
    },
    "POST /api/merchant/promotions": {
        "method": "merchant_campaign_action",
        "request": ["action", "payload", "idempotencyKey"],
        "actions": ["create_campaign"],
        "response": ["id", "status", "placement", "requiresAdminApproval", "paymentStatus"],
    },
    "GET /api/merchant/onboarding": {
        "method": "merchant_onboarding",
        "request": ["action=status"],
        "response": ["application", "steps", "requiredSteps", "nextStep"],
    },
    "POST /api/merchant/onboarding": {
        "method": "merchant_onboarding",
        "request": ["action", "applicationId", "step", "data"],
        "actions": ["start", "status", "save_step", "save_draft", "submit"],
        "response": ["application", "steps", "requiredSteps", "nextStep"],
    },
    "POST /api/merchant/products": {
        "method": "upsert_product",
        "request": [
            "id", "branchId", "categoryId", "nameAr", "nameEn", "descriptionAr", "descriptionEn",
            "price", "unit", "barcode", "imageMediaIds", "metadata", "tags", "stockMode", "quantity", "availability",
        ],
        "response": ["id", "branchId", "price", "quantity", "availability", "status", "moderationStatus", "images"],
    },
    "POST /api/merchant/products/{productId}/action": {
        "method": "product_action",
        "request": ["action", "branchId", "quantity"],
        "actions": ["pause", "resume", "archive", "duplicate_to_branch"],
        "response": ["id", "action", "status"],
    },
    "POST /api/merchant/bundles": {
        "method": "create_bundle",
        "request": ["branchId", "titleAr", "titleEn", "description", "price", "components", "imagePath", "tags", "startsAt", "endsAt"],
        "response": ["id", "branchId", "normalValue", "price", "saving", "componentCount"],
    },
    "POST /api/merchant/inventory/action": {
        "method": "inventory_action",
        "request": ["branchId", "productId", "action", "quantity", "seenAndVerified"],
        "response": ["productId", "branchId", "auditId", "quantity", "availability", "active", "last_stock_verified_at"],
    },
    "GET /api/merchant/inventory": {
        "method": "quick_stock",
        "request": ["branch"],
        "response": ["branchId", "items", "remainingCount"],
    },
    "POST /api/merchant/inventory/verify": {
        "method": "confirm_stock",
        "request": ["branchId", "changes"],
        "response": ["auditId", "status", "verifiedCount", "remainingCount", "confirmedAt"],
    },
    "POST /api/merchant/inventory/audits/{auditId}/confirm-remaining": {
        "method": "confirm_inventory_remaining",
        "response": ["auditId", "status", "confirmedAt", "confirmedUnchanged", "duplicate"],
    },
    "POST /api/merchant/branches": {
        "method": "create_branch",
        "response": ["id", "status", "publicVisible", "planUsage"],
    },
    "PUT /api/merchant/branches/{branchId}/fulfillment": {
        "method": "configure_fulfillment",
        "request": ["pickup", "office", "home", "zones", "eta"],
        "response": ["branchId", "pickup", "office", "home", "zones"],
    },
    "POST /api/merchant/return-policies": {
        "method": "save_return_policy",
        "response": ["id", "version", "active", "legalFloor"],
    },
    "POST /api/merchant/members": {
        "method": "add_merchant_member",
        "response": ["accountId", "merchantId", "role", "status", "planUsage"],
    },
    "GET /api/merchant/suppliers": {
        "method": "supplier_campaigns",
        "response": ["campaigns"],
    },
    "POST /api/merchant/suppliers/{campaignId}/leads": {
        "method": "create_supplier_lead",
        "request": ["action", "note", "idempotencyKey"],
        "response": ["id", "campaignId", "action", "status", "duplicate"],
    },
    "POST /api/analytics/events": {
        "method": "record_event",
        "request": ["eventType", "entityKind", "entityId", "context"],
        "response": ["id", "recorded"],
    },
    "PUT /api/favorites/{kind}/{id}": {
        "method": "set_favorite",
        "request": ["branchId", "saved"],
        "response": ["entityKind", "entityId", "saved"],
    },
    "GET /api/addresses": {
        "method": "addresses",
        "response": ["addresses"],
    },
    "POST /api/addresses": {
        "method": "save_address",
        "request": ["id", "addressType", "label", "wilayahId", "areaId", "addressText", "latitude", "longitude"],
        "response": ["address"],
    },
    "POST /api/products/{productId}/reports": {
        "method": "report_product",
        "request": ["branchId", "reason", "detail"],
        "response": ["id", "status", "duplicate"],
    },
    "GET /api/notifications": {
        "method": "notifications",
        "request": ["pendingOnly", "limit"],
        "response": ["notifications"],
    },
    "POST /api/notifications/{notificationId}/{action}": {
        "method": "notification_action",
        "actions": ["seen", "read", "ack", "dismiss"],
        "response": ["id", "action", "at", "acted"],
    },
    "GET /api/push/status": {
        "service": "BisaPushService.status",
        "request": ["endpointHash"],
        "response": [
            "available", "configured", "status", "publicKey", "errorCode",
            "activeForCurrentRole", "role",
        ],
    },
    "POST /api/push/subscriptions": {
        "service": "BisaPushService.subscribe",
        "request": ["endpoint", "expirationTime", "keys.p256dh", "keys.auth"],
        "response": ["subscriptionId", "bindingId", "role", "scopeKind", "active", "capability"],
    },
    "DELETE /api/push/subscriptions": {
        "service": "BisaPushService.unsubscribe",
        "request": ["endpoint"],
        "response": ["deactivated", "role"],
        "note": "Deactivates only the active role binding; it does not unsubscribe the shared browser endpoint.",
    },
    "GET /api/admin/overview": {
        "method": "admin_overview",
        "response": ["counts", "pendingApplications", "inventory", "permissions", "settings"],
    },
    "GET /api/admin/resources/{resource}": {
        "method": "admin_resource",
        "request": ["status", "query", "cursor", "limit"],
        "response": ["resource", "items", "pagination"],
    },
    "GET /api/admin/merchant-applications/{id}": {
        "method": "admin_application_detail",
        "response": ["application", "steps", "documents"],
    },
    "POST /api/admin/merchant-applications/{id}/documents/{documentId}/decision": {
        "method": "admin_application_document_decision",
        "request": ["decision", "note"],
        "actions": ["approve", "reject"],
        "response": ["id", "applicationId", "status", "duplicate"],
    },
    "POST /api/admin/resources/{resource}/{action}": {
        "method": "admin_action",
        "request": ["id", "reason", "value", "entitlements"],
        "response": ["resource", "action", "id", "result"],
    },
    "POST /api/admin/merchant-applications/{id}/decision": {
        "method": "admin_application_decision",
        "request": ["applicationId", "decision", "note"],
        "response": ["id", "merchantId", "status", "duplicate"],
    },
}

API_CONTRACTS.update(SUPPLIER_API_CONTRACTS)
API_CONTRACTS.update(MERCHANT_LAUNCH_ROUTE_CONTRACTS)
for _path, _contract in MODERATION_API_CONTRACTS.items():
    if _path == "POST /api/admin/resources/{resource}/{action}":
        request_fields = API_CONTRACTS[_path].setdefault("request", [])
        for field in _contract.get("request", []):
            if field not in request_fields:
                request_fields.append(field)
        API_CONTRACTS[_path]["moderationResources"] = _contract.get("resources", [])
        API_CONTRACTS[_path]["moderationNote"] = _contract.get("note", "")
    else:
        API_CONTRACTS[_path] = _contract


def api_contracts() -> dict:
    """Return a shallow-copy safe for an internal diagnostics endpoint."""
    return {path: dict(contract) for path, contract in API_CONTRACTS.items()}
