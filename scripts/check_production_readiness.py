"""Secret-free production preflight for the independent BISA runtime."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
from pathlib import Path
import re
from urllib.parse import urlparse

from bisa_push import PyWebPushTransport


PATH_VARIABLES = (
    "BISA_DB_PATH",
    "BISA_UPLOAD_DIR",
    "BISA_BACKUP_DIR",
)
SECRET_MINIMUMS = {
    "BISA_AUTH_PEPPER": 32,
    "BISA_MEDIA_SIGNING_KEY": 32,
}
OPTIONAL_GROUPS = {
    "whatsapp": (
        "BISA_WHATSAPP_PHONE_NUMBER_ID",
        "BISA_WHATSAPP_ACCESS_TOKEN",
    ),
    "push": (
        "BISA_VAPID_PUBLIC_KEY",
        "BISA_VAPID_PRIVATE_KEY",
        "BISA_VAPID_SUBJECT",
    ),
    "email": (
        "BISA_SMTP_HOST",
        "BISA_SMTP_USER",
        "BISA_SMTP_PASSWORD",
        "BISA_SMTP_FROM_EMAIL",
    ),
}
DISABLED_VALUES = {"", "none", "null", "false", "disabled", "unconfigured"}
STABLE_RELEASE = re.compile(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\Z")


def truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _present(value: str | None) -> bool:
    return str(value or "").strip().lower() not in DISABLED_VALUES


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _public_host(parsed) -> bool:
    host = str(parsed.hostname or "").strip()
    if not host or "*" in host or any(character.isspace() for character in host):
        return False
    try:
        parsed.port
    except ValueError:
        return False
    try:
        return ipaddress.ip_address(host).is_global
    except ValueError:
        return "." in host and not host.startswith(".") and not host.endswith(".")


def check_environment(
    environment: dict[str, str], *, check_paths: bool = True
) -> dict[str, object]:
    """Validate BISA production declarations without returning any secret value."""
    errors: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    foreign_product = "khada" + "mati"

    def error(code: str, variable: str = "") -> None:
        errors.append({"code": code, **({"variable": variable} if variable else {})})

    def warning(code: str, variable: str = "") -> None:
        warnings.append({"code": code, **({"variable": variable} if variable else {})})

    if str(environment.get("BISA_ENV", "")).strip().lower() != "production":
        error("environment_not_production", "BISA_ENV")
    if truthy(environment.get("BISA_SEED_SAMPLE_DATA")):
        error("sample_seed_must_be_disabled", "BISA_SEED_SAMPLE_DATA")
    if str(environment.get("BISA_PHONE_VERIFICATION_MODE", "")).strip().lower() != "invite_only":
        error("phone_verification_flow_required", "BISA_PHONE_VERIFICATION_MODE")
    for variable in ("BISA_DEV_OTP_CODE", "BISA_DEV_PIN", "BISA_ADMIN_PIN"):
        if str(environment.get(variable, "")).strip():
            error("development_credential_must_be_removed", variable)

    release = str(environment.get("BISA_RELEASE", "")).strip()
    if not STABLE_RELEASE.fullmatch(release):
        error("stable_release_required", "BISA_RELEASE")

    public_url = str(environment.get("BISA_PUBLIC_URL", "")).strip()
    parsed_public = urlparse(public_url)
    if (
        parsed_public.scheme != "https"
        or not _public_host(parsed_public)
        or parsed_public.username
        or parsed_public.password
        or parsed_public.params
        or parsed_public.query
        or parsed_public.fragment
    ):
        error("public_url_must_use_public_https", "BISA_PUBLIC_URL")

    origins = [
        item.strip().rstrip("/")
        for item in str(environment.get("BISA_ALLOWED_ORIGINS", "")).split(",")
        if item.strip()
    ]
    valid_origins: set[str] = set()
    if not origins:
        error("allowed_origins_required", "BISA_ALLOWED_ORIGINS")
    for origin in origins:
        parsed = urlparse(origin)
        if (
            parsed.scheme != "https"
            or not _public_host(parsed)
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.params
            or parsed.query
            or parsed.fragment
            or "*" in origin
        ):
            error("production_origin_invalid", "BISA_ALLOWED_ORIGINS")
            continue
        valid_origins.add(f"{parsed.scheme}://{parsed.netloc}".rstrip("/"))
    if parsed_public.scheme == "https" and parsed_public.netloc:
        public_origin = f"{parsed_public.scheme}://{parsed_public.netloc}".rstrip("/")
        if public_origin not in valid_origins:
            error("public_origin_not_allowed", "BISA_ALLOWED_ORIGINS")

    secret_states: dict[str, str] = {}
    for variable, minimum in SECRET_MINIMUMS.items():
        value = str(environment.get(variable, ""))
        valid = len(value) >= minimum and value.strip().lower() not in DISABLED_VALUES
        secret_states[variable] = "configured" if valid else "invalid"
        if not valid:
            error("secret_missing_or_too_short", variable)
    auth_key = str(environment.get("BISA_AUTH_PEPPER", ""))
    media_key = str(environment.get("BISA_MEDIA_SIGNING_KEY", ""))
    if auth_key and media_key and auth_key == media_key:
        error("security_keys_must_be_distinct")

    resolved_paths: dict[str, Path] = {}
    home = Path.home().resolve()
    for variable in PATH_VARIABLES:
        raw = str(environment.get(variable, "")).strip()
        if not raw:
            error("persistent_path_required", variable)
            continue
        path = Path(raw)
        if not path.is_absolute():
            error("persistent_path_must_be_absolute", variable)
            continue
        resolved = path.resolve()
        resolved_paths[variable] = resolved
        if resolved == Path(resolved.anchor) or resolved == home:
            error("persistent_path_too_broad", variable)
        if foreign_product in str(resolved).lower():
            error("foreign_product_path_forbidden", variable)
        if not check_paths:
            continue
        if variable == "BISA_DB_PATH":
            target = resolved.parent
            if resolved.exists() and not resolved.is_file():
                error("database_path_is_not_a_file", variable)
        else:
            target = resolved
            if target.exists() and not target.is_dir():
                error("persistent_directory_invalid", variable)
        if not target.exists():
            error("persistent_path_missing", variable)
        elif not os.access(target, os.W_OK):
            error("persistent_path_not_writable", variable)

    database = resolved_paths.get("BISA_DB_PATH")
    uploads = resolved_paths.get("BISA_UPLOAD_DIR")
    backups = resolved_paths.get("BISA_BACKUP_DIR")
    if uploads and backups and (uploads == backups or _inside(uploads, backups) or _inside(backups, uploads)):
        error("upload_and_backup_paths_must_be_separate")
    if database and uploads and _inside(database, uploads):
        error("database_must_not_be_inside_uploads", "BISA_DB_PATH")
    if database and backups and _inside(database, backups):
        error("database_must_not_be_inside_backups", "BISA_DB_PATH")

    integrations: dict[str, str] = {}
    for name, variables in OPTIONAL_GROUPS.items():
        states = [_present(environment.get(variable)) for variable in variables]
        if any(states) and not all(states):
            error("integration_configuration_incomplete", f"BISA_{name.upper()}_*")
            integrations[name] = "incomplete"
        elif all(states) and name == "push":
            adapter = PyWebPushTransport(
                public_key=str(environment.get("BISA_VAPID_PUBLIC_KEY") or ""),
                private_key=str(environment.get("BISA_VAPID_PRIVATE_KEY") or ""),
                subject=str(environment.get("BISA_VAPID_SUBJECT") or ""),
            )
            if adapter.configured:
                integrations[name] = "configured"
            else:
                integrations[name] = "invalid"
                error(adapter.reason or "push_vapid_configuration_invalid", "BISA_VAPID_*")
        elif all(states):
            # Credentials alone do not make the repository's intentionally
            # unavailable adapter capable of delivering an external action.
            integrations[name] = "credentials_present_but_adapter_unavailable"
            error("external_adapter_implementation_required", f"BISA_{name.upper()}_*")
        else:
            integrations[name] = "unconfigured"

    if integrations.get("push") != "unconfigured":
        subject = str(environment.get("BISA_VAPID_SUBJECT", "")).strip()
        if subject and not (subject.startswith("mailto:") or subject.startswith("https://")):
            error("vapid_subject_invalid", "BISA_VAPID_SUBJECT")
    if truthy(environment.get("BISA_PUSH_REQUIRED")) and integrations.get("push") != "configured":
        error("push_required_but_unavailable", "BISA_PUSH_REQUIRED")
    smtp_port = str(environment.get("BISA_SMTP_PORT", "587")).strip()
    if integrations.get("email") != "unconfigured" and (
        not smtp_port.isdigit() or not 1 <= int(smtp_port) <= 65535
    ):
        error("smtp_port_invalid", "BISA_SMTP_PORT")

    gateway = str(environment.get("BISA_PAYMENT_GATEWAY", "unconfigured")).strip()
    payment_secret = str(environment.get("BISA_PAYMENT_WEBHOOK_SECRET", "")).strip()
    gateway_enabled = gateway.lower() not in DISABLED_VALUES
    if gateway_enabled != bool(payment_secret):
        error("payment_configuration_incomplete", "BISA_PAYMENT_*")
        integrations["payment"] = "incomplete"
    elif gateway_enabled:
        integrations["payment"] = "credentials_present_but_adapter_unavailable"
        error("external_adapter_implementation_required", "BISA_PAYMENT_*")
    else:
        integrations["payment"] = "unconfigured"

    foreign_prefix = f"{foreign_product.upper()}_"
    if any(key.startswith(foreign_prefix) and str(value).strip() for key, value in environment.items()):
        warning("foreign_product_environment_present")

    return {
        "ok": not errors,
        "mode": "bisa_production_preflight",
        "releaseStable": bool(STABLE_RELEASE.fullmatch(release)),
        "pathsChecked": check_paths,
        "secretStates": secret_states,
        "integrations": integrations,
        "errors": errors,
        "warnings": warnings,
        "valuesExposed": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--skip-path-check",
        action="store_true",
        help="Validate path declarations without requiring mounted paths to exist.",
    )
    args = parser.parse_args()
    result = check_environment(dict(os.environ), check_paths=not args.skip_path_check)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
