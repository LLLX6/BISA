"""Central BISA identity and runtime configuration.

The working brand can be changed here without a repository-wide replacement.
No source-project production namespace, database, cache, cookie, or environment value is used.
"""

from __future__ import annotations

import os
from pathlib import Path
import re


APP_ID = "om.bisa.marketplace"
APP_NAME_EN = "BISA"
APP_NAME_AR = "بيسا"
BRAND_NAME = "BISA | بيسا"
TAGLINE_EN = "Your discoveries, close to you."
TAGLINE_AR = "اكتشافاتك، قريبة منك."
SHORT_DESCRIPTION_EN = "Discover products from 100 baisa to OMR 2 at stores near you."
SHORT_DESCRIPTION_AR = "اكتشف منتجات من 100 بيسة إلى 2 ر.ع من متاجر قريبة منك."
APP_VERSION = os.environ.get("BISA_RELEASE", "0.2.0-dev")
NAMESPACE = "bisa"

PRODUCT_MIN_BAISA = 100
PRODUCT_MAX_BAISA = 2000
DEFAULT_CURRENCY = "OMR"

# One server-owned launch boundary is shared with the public map contract. It
# covers the six launch wilayats in Muscat Governorate and prevents storing a
# branch pin that the V1 discovery map would have to hide.
MUSCAT_MAP_BOUNDS = ((23.05, 57.95), (23.90, 59.30))

ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("BISA_DATA_DIR", ROOT / "data-bisa"))
DB_PATH = Path(os.environ.get("BISA_DB_PATH", DATA_DIR / "bisa.sqlite3"))
UPLOAD_DIR = Path(os.environ.get("BISA_UPLOAD_DIR", DATA_DIR / "uploads"))
BACKUP_DIR = Path(os.environ.get("BISA_BACKUP_DIR", DATA_DIR / "backups"))
ENVIRONMENT = os.environ.get("BISA_ENV", "development").strip().lower()
SEED_SAMPLE_DATA = os.environ.get("BISA_SEED_SAMPLE_DATA", "false").strip().lower() in {
    "1", "true", "yes", "on"
}
SESSION_COOKIE = "bisa_session"
SESSION_TTL_HOURS = int(os.environ.get("BISA_SESSION_TTL_HOURS", "168"))
PUBLIC_URL = os.environ.get("BISA_PUBLIC_URL", "http://127.0.0.1:8080")
ALLOWED_ORIGINS = {
    value.strip().rstrip("/")
    for value in os.environ.get("BISA_ALLOWED_ORIGINS", "").split(",")
    if value.strip()
}
WHATSAPP_CONFIGURED = bool(
    os.environ.get("BISA_WHATSAPP_PHONE_NUMBER_ID")
    and os.environ.get("BISA_WHATSAPP_ACCESS_TOKEN")
)
PAYMENT_GATEWAY = os.environ.get("BISA_PAYMENT_GATEWAY", "unconfigured").strip().lower()
PAYMENT_CONFIGURED = PAYMENT_GATEWAY not in {"", "none", "disabled", "unconfigured"} and bool(
    os.environ.get("BISA_PAYMENT_WEBHOOK_SECRET")
)
PUSH_CONFIGURED = bool(
    os.environ.get("BISA_VAPID_PUBLIC_KEY")
    and os.environ.get("BISA_VAPID_PRIVATE_KEY")
    and os.environ.get("BISA_VAPID_SUBJECT")
)
PHONE_VERIFICATION_MODE = os.environ.get(
    "BISA_PHONE_VERIFICATION_MODE", "development_bypass"
).strip().lower()
STABLE_RELEASE = re.compile(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\Z")

BRAND = {
    "appId": APP_ID,
    "name": {"ar": APP_NAME_AR, "en": APP_NAME_EN},
    "brandName": BRAND_NAME,
    "tagline": {"ar": TAGLINE_AR, "en": TAGLINE_EN},
    "description": {"ar": SHORT_DESCRIPTION_AR, "en": SHORT_DESCRIPTION_EN},
    "version": APP_VERSION,
    "priceRule": {
        "minimumBaisa": PRODUCT_MIN_BAISA,
        "maximumBaisa": PRODUCT_MAX_BAISA,
        "currency": DEFAULT_CURRENCY,
    },
    "palette": {
        "primary": "#FF5A36",
        "secondary": "#5B3DF5",
        "accent": "#FFC83D",
        "ink": "#17151F",
        "surface": "#FFFFFF",
        "surfaceWarm": "#FFF7F1",
        "surfaceViolet": "#F4F1FF",
        "success": "#129B69",
        "danger": "#E5484D",
    },
}


def coordinates_in_muscat(latitude: float, longitude: float) -> bool:
    """Return whether a coordinate pair belongs to the supported V1 map area."""
    (south, west), (north, east) = MUSCAT_MAP_BOUNDS
    return south <= latitude <= north and west <= longitude <= east


def ensure_runtime_directories() -> None:
    for directory in (DATA_DIR, UPLOAD_DIR, BACKUP_DIR):
        directory.mkdir(parents=True, exist_ok=True)


def production_readiness() -> dict:
    persistent = all(os.environ.get(key) for key in (
        "BISA_DB_PATH", "BISA_UPLOAD_DIR", "BISA_BACKUP_DIR"
    ))
    errors = []
    if ENVIRONMENT == "production" and not persistent:
        errors.append("persistent_storage_not_configured")
    if ENVIRONMENT == "production" and SEED_SAMPLE_DATA:
        errors.append("production_seed_forbidden")
    if ENVIRONMENT == "production" and not PUBLIC_URL.startswith("https://"):
        errors.append("public_https_required")
    if ENVIRONMENT == "production" and not ALLOWED_ORIGINS:
        errors.append("allowed_origins_required")
    if ENVIRONMENT == "production" and any(not origin.startswith("https://") for origin in ALLOWED_ORIGINS):
        errors.append("allowed_origins_must_use_https")
    if ENVIRONMENT == "production" and PHONE_VERIFICATION_MODE != "invite_only":
        # A real OTP provider/challenge flow has not been implemented in this
        # repository. Production may run only as an explicitly invite-only
        # environment until that external integration is completed and tested.
        errors.append("phone_verification_flow_required")
    if ENVIRONMENT == "production" and not STABLE_RELEASE.fullmatch(APP_VERSION):
        errors.append("stable_release_required")
    return {
        "ready": not errors,
        "errors": errors,
        "environment": ENVIRONMENT,
        "integrations": {
            "whatsapp": "configured" if WHATSAPP_CONFIGURED else "unavailable",
            "payments": "configured" if PAYMENT_CONFIGURED else "unavailable",
            "push": "configured" if PUSH_CONFIGURED else "unavailable",
        },
    }
