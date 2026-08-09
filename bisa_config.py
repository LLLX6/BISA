"""Central BISA identity and runtime configuration.

The working brand can be changed here without a repository-wide replacement.
No source-project production namespace, database, cache, cookie, or environment value is used.
"""

from __future__ import annotations

import os
from pathlib import Path


APP_ID = "om.bisa.marketplace"
APP_NAME_EN = "BISA"
APP_NAME_AR = "بيسا"
BRAND_NAME = "BISA | بيسا"
TAGLINE_EN = "Your discoveries, close to you."
TAGLINE_AR = "اكتشافاتك، قريبة منك."
SHORT_DESCRIPTION_EN = "Discover products from 100 baisa to OMR 2 at stores near you."
SHORT_DESCRIPTION_AR = "اكتشف منتجات من 100 بيسة إلى 2 ر.ع من متاجر قريبة منك."
APP_VERSION = os.environ.get("BISA_RELEASE", "0.1.0")
NAMESPACE = "bisa"

PRODUCT_MIN_BAISA = 100
PRODUCT_MAX_BAISA = 2000
DEFAULT_CURRENCY = "OMR"

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
    return {"ready": not errors, "errors": errors, "environment": ENVIRONMENT}
