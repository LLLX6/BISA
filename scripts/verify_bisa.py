"""Deterministic release verifier for the independent BISA repository."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import struct
import subprocess  # nosec B404 - fixed local Git inspection only
import sys


ROOT = Path(__file__).resolve().parents[1]
TOP_LEVEL_MIRRORS = (
    "index.html",
    "manifest.webmanifest",
    "service-worker.js",
    "app-icon-192.png",
    "app-icon-512.png",
    "app-icon-maskable-192.png",
    "app-icon-maskable-512.png",
    "apple-touch-icon.png",
)
MIRRORED_TREES = (
    "assets/brand",
    "assets/styles",
    "assets/scripts",
    "assets/images",
    "vendor",
)
REQUIRED = (
    ".env.example",
    ".github/workflows/pages.yml",
    ".github/workflows/quality.yml",
    ".github/workflows/release.yml",
    ".gitignore",
    "AGENTS.md",
    "README.md",
    "LICENSE",
    "SECURITY.md",
    "THIRD_PARTY_NOTICES.md",
    "assets/images/bisa-hero-v1.webp",
    "assets/scripts/bisa-map.js",
    "bisa_application.py",
    "bisa_config.py",
    "bisa_domain.py",
    "bisa_integrations.py",
    "bisa_jobs.py",
    "bisa_marketplace.py",
    "bisa_merchant_launch.py",
    "bisa_migrations.py",
    "bisa_moderation.py",
    "bisa_operations.py",
    "bisa_push.py",
    "bisa_security.py",
    "bisa_server.py",
    "bisa_supplier.py",
    "migrations/002_marketplace_core.sql",
    "migrations/003_security_operations.sql",
    "migrations/004_marketplace_invariants.sql",
    "migrations/005_supplier_notification_isolation.sql",
    "migrations/006_moderation_review_receipts.sql",
    "migrations/007_role_scoped_push_outbox.sql",
    "requirements.txt",
    "package.json",
    "package-lock.json",
    "render.yaml",
    "scripts/__init__.py",
    "scripts/audit_database.py",
    "scripts/backup_sqlite.py",
    "scripts/build_bisa_icons.js",
    "scripts/check_production_readiness.py",
    "scripts/create_bisa_admin.py",
    "scripts/restore_sqlite_backup.py",
    "scripts/verify_repository.py",
    "tests/performance-bisa-ui.js",
    "tests/smoke-bisa-ui.js",
    "tests/test-bisa-map.js",
    "tests/test_bisa_api.py",
    "tests/test_bisa_domain.py",
    "tests/test_bisa_marketplace.py",
    "tests/test_bisa_merchant_launch.py",
    "tests/test_bisa_migrations.py",
    "tests/test_bisa_moderation.py",
    "tests/test_bisa_operations.py",
    "tests/test_bisa_push.py",
    "tests/test_bisa_release_tooling.py",
    "tests/test_bisa_security.py",
    "tests/test_bisa_supplier.py",
)
PROHIBITED_TRACKED = (
    re.compile(r"(^|/)\.env($|\.)", re.IGNORECASE),
    re.compile(r"\.(?:sqlite3?|db|log)(?:-|$)", re.IGNORECASE),
    re.compile(r"(^|/)(?:data-bisa|uploads|backups|node_modules|test-results|playwright-report)/", re.IGNORECASE),
)
SECRET_PATTERNS = (
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    re.compile(rb"\bsk-[A-Za-z0-9_-]{20,}"),
    re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(rb"AKIA[0-9A-Z]{16}"),
)
SEMVER = re.compile(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)(?:-[0-9A-Za-z.-]+)?\Z")


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def png_size(path: Path) -> tuple[int, int]:
    raw = path.read_bytes()[:24]
    if len(raw) != 24 or raw[:8] != b"\x89PNG\r\n\x1a\n" or raw[12:16] != b"IHDR":
        return (0, 0)
    return struct.unpack(">II", raw[16:24])


def tracked_files() -> list[str]:
    git = shutil.which("git")
    if not git:
        raise RuntimeError("git_required")
    result = subprocess.run(  # nosec B603
        [git, "ls-files", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return [
        value.decode("utf-8", errors="strict")
        for value in result.stdout.split(b"\0")
        if value
    ]


def relative_files(directory: Path) -> set[str]:
    if not directory.is_dir():
        return set()
    return {
        path.relative_to(directory).as_posix()
        for path in directory.rglob("*")
        if path.is_file()
    }


def parse_env_template(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def main() -> int:
    errors: list[dict[str, object]] = []
    warnings: list[dict[str, object]] = []

    def fail(kind: str, **details: object) -> None:
        errors.append({"type": kind, **details})

    for relative in REQUIRED:
        if not (ROOT / relative).is_file():
            fail("missing_required_file", file=relative)

    for relative in TOP_LEVEL_MIRRORS:
        source = ROOT / relative
        public = ROOT / "public" / relative
        if not source.is_file() or not public.is_file():
            fail("missing_mirror", file=relative)
        elif digest(source) != digest(public):
            fail("mirror_mismatch", file=relative)
    for filename, expected_size in (
        ("apple-touch-icon.png", (180, 180)),
        ("app-icon-192.png", (192, 192)),
        ("app-icon-512.png", (512, 512)),
        ("app-icon-maskable-192.png", (192, 192)),
        ("app-icon-maskable-512.png", (512, 512)),
    ):
        if (ROOT / filename).is_file() and png_size(ROOT / filename) != expected_size:
            fail("pwa_icon_dimensions_invalid", file=filename, expected=list(expected_size))

    mirrored_count = len(TOP_LEVEL_MIRRORS)
    for relative in MIRRORED_TREES:
        source = ROOT / relative
        public = ROOT / "public" / relative
        source_files = relative_files(source)
        public_files = relative_files(public)
        if not source_files and not public_files and relative == "vendor":
            continue
        if source_files != public_files:
            fail(
                "mirror_tree_file_set_mismatch",
                tree=relative,
                sourceOnly=sorted(source_files - public_files),
                publicOnly=sorted(public_files - source_files),
            )
            continue
        for filename in sorted(source_files):
            mirrored_count += 1
            if digest(source / filename) != digest(public / filename):
                fail("mirror_mismatch", file=f"{relative}/{filename}")

    try:
        package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
        lock = json.loads((ROOT / "package-lock.json").read_text(encoding="utf-8"))
        manifest = json.loads((ROOT / "manifest.webmanifest").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        fail("release_json_invalid", error=type(exc).__name__)
        package = {}
        lock = {}
        manifest = {}

    version = str(package.get("version", ""))
    if package.get("name") != "bisa-marketplace" or not SEMVER.fullmatch(version):
        fail("package_identity_or_version_invalid")
    root_lock = lock.get("packages", {}).get("", {}) if isinstance(lock.get("packages"), dict) else {}
    if lock.get("name") != package.get("name") or lock.get("version") != version or root_lock.get("version") != version:
        fail("package_lock_identity_mismatch")
    scripts = package.get("scripts", {}) if isinstance(package.get("scripts"), dict) else {}
    for script in ("build:pwa-icons", "check:js", "verify", "test:map", "test:ui", "test:performance", "test"):
        if script not in scripts:
            fail("package_script_missing", script=script)

    if (
        manifest.get("id") != "./"
        or manifest.get("name") != "BISA | بيسا"
        or manifest.get("scope") != "./"
        or manifest.get("start_url") != "./?source=pwa-bisa"
        or manifest.get("version") != version
        or manifest.get("dir") != "rtl"
        or manifest.get("lang") != "ar"
    ):
        fail("manifest_identity_or_version_invalid")
    icons = manifest.get("icons") if isinstance(manifest.get("icons"), list) else []
    icon_sizes = {str(item.get("sizes", "")) for item in icons if isinstance(item, dict)}
    if not {"192x192", "512x512", "any"}.issubset(icon_sizes):
        fail("manifest_install_icons_incomplete")
    purposes = {str(item.get("purpose", "")) for item in icons if isinstance(item, dict)}
    if "any" not in purposes or "maskable" not in purposes:
        fail("manifest_icon_purposes_incomplete")
    for item in [*icons, *(manifest.get("shortcuts") or [])]:
        if not isinstance(item, dict):
            fail("manifest_entry_invalid")
            continue
        candidates = item.get("icons", []) if "icons" in item else [item]
        for icon in candidates:
            source = str(icon.get("src", "")) if isinstance(icon, dict) else ""
            posix = PurePosixPath(source)
            if not source or posix.is_absolute() or ".." in posix.parts or not (ROOT / source).is_file():
                fail("manifest_icon_invalid", source=source)

    worker = (ROOT / "service-worker.js").read_text(encoding="utf-8", errors="replace")
    worker_requirements = (
        f"const CACHE_VERSION = '{version}'",
        "isApiPath(url.pathname)",
        "pathname.startsWith(ROOT_API_PREFIX)",
        "self.addEventListener('push'",
        "self.addEventListener('pushsubscriptionchange'",
        "notificationId",
        "assets/scripts/bisa-map.js",
        "self.registration.scope",
        "bisa:skip-waiting",
    )
    if any(token not in worker for token in worker_requirements):
        fail("service_worker_policy_or_version_invalid")

    config = (ROOT / "bisa_config.py").read_text(encoding="utf-8", errors="replace")
    if (
        'APP_ID = "om.bisa.marketplace"' not in config
        or not re.search(rf'APP_VERSION\s*=\s*os\.environ\.get\("BISA_RELEASE",\s*"{re.escape(version)}"\)', config)
        or "PRODUCT_MIN_BAISA = 100" not in config
        or "PRODUCT_MAX_BAISA = 2000" not in config
        or 'errors.append("stable_release_required")' not in config
    ):
        fail("central_config_identity_or_version_invalid")

    env_path = ROOT / ".env.example"
    env_text = env_path.read_text(encoding="utf-8", errors="replace")
    env = parse_env_template(env_path)
    if (
        env.get("BISA_RELEASE") != version
        or env.get("BISA_SEED_SAMPLE_DATA", "").lower() != "false"
        or env.get("BISA_DEMO_PIN") != ""
        or env.get("BISA_PHONE_VERIFICATION_MODE") != "development_bypass"
        or env.get("BISA_PUSH_REQUIRED", "").lower() != "false"
    ):
        fail("environment_template_version_or_seed_invalid")
    legacy_namespace = "KHADA" + "MATI_"
    if legacy_namespace in env_text.upper():
        fail("foreign_namespace_in_environment_template")
    if re.search(
        r"(?im)^(?:BISA_.*(?:TOKEN|SECRET|PASSWORD|KEY|PEPPER)|.*ACCESS_TOKEN)=[ \t]*[^ \t\r\n]",
        env_text,
    ):
        fail("environment_template_contains_secret")

    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8", errors="replace")
    if "Pillow==12.3.0" not in requirements or "pywebpush==2.3.0" not in requirements:
        fail("python_dependency_lock_incomplete")

    render = (ROOT / "render.yaml").read_text(encoding="utf-8", errors="replace")
    if (
        "autoDeployTrigger: off" not in render
        or "generation: off" not in render
        or "plan: free" not in render
        or "preDeployCommand: python scripts/check_production_readiness.py" not in render
        or "healthCheckPath: /readyz" not in render
        or "value: production" not in render
        or "key: BISA_SEED_SAMPLE_DATA" not in render
        or "key: BISA_PHONE_VERIFICATION_MODE" not in render
        or "value: invite_only" not in render
        or not re.search(r"- key: BISA_PUSH_REQUIRED\s+value: false", render)
        or "value: false" not in render
        or legacy_namespace in render.upper()
    ):
        fail("render_blueprint_safety_invalid")
    for secret_name in ("BISA_AUTH_PEPPER", "BISA_MEDIA_SIGNING_KEY"):
        block = re.search(rf"- key: {secret_name}\s+([^\n]+)", render)
        if not block or "generateValue: true" not in block.group(0):
            fail("render_generated_secret_missing", variable=secret_name)

    backup_tool = (ROOT / "scripts/backup_sqlite.py").read_text(encoding="utf-8", errors="replace")
    restore_tool = (ROOT / "scripts/restore_sqlite_backup.py").read_text(encoding="utf-8", errors="replace")
    audit_tool = (ROOT / "scripts/audit_database.py").read_text(encoding="utf-8", errors="replace")
    if (
        'FORMAT = "bisa-backup-v1"' not in backup_tool
        or 'APP_ID = "om.bisa.marketplace"' not in backup_tool
        or "RESTORE_BISA_TO_EMPTY_TARGET" not in restore_tool
        or 'APP_ID = "om.bisa.marketplace"' not in audit_tool
    ):
        fail("bisa_operations_tool_identity_invalid")
    leaflet_files = (ROOT / "vendor/leaflet.js", ROOT / "vendor/leaflet.css")
    if any(path.exists() for path in leaflet_files):
        license_path = ROOT / "vendor/leaflet.LICENSE.txt"
        notices_path = ROOT / "THIRD_PARTY_NOTICES.md"
        if (
            not all(path.is_file() for path in leaflet_files)
            or not license_path.is_file()
            or not notices_path.is_file()
            or "Leaflet 1.9.4" not in leaflet_files[0].read_text(encoding="utf-8", errors="replace")[:300]
            or "BSD 2-Clause License" not in license_path.read_text(encoding="utf-8", errors="replace")
        ):
            fail("leaflet_identity_or_license_invalid")

    quality = (ROOT / ".github/workflows/quality.yml").read_text(encoding="utf-8", errors="replace")
    for token in (
        "python -m pip install -r requirements.txt",
        'python -m unittest discover -s tests -p "test_bisa*.py" -v',
        "python scripts/verify_bisa.py",
        "npm audit --audit-level=high",
        "npm run test:map",
        "npm run test:ui",
        "npm run test:performance",
    ):
        if token not in quality:
            fail("quality_workflow_gate_missing", gate=token)
    if legacy_namespace in quality.upper():
        fail("foreign_namespace_in_quality_workflow")

    pages = (ROOT / ".github/workflows/pages.yml").read_text(encoding="utf-8", errors="replace")
    if (
        re.search(r"(?m)^\s*push\s*:", pages)
        or "workflow_dispatch:" not in pages
        or "DEPLOY_BISA_PREVIEW" not in pages
        or "github.actor == github.repository_owner" not in pages
        or 'python -m unittest discover -s tests -p "test_bisa*.py" -v' not in pages
        or "npm test" not in pages
    ):
        fail("pages_workflow_requires_explicit_confirmation")
    release = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8", errors="replace")
    if "git archive" not in release or "zip -r" in release or legacy_namespace in release.upper():
        fail("source_archive_workflow_invalid")

    active_surface = (
        "index.html",
        "manifest.webmanifest",
        "service-worker.js",
        "assets/scripts/bisa-app.js",
        "assets/styles/bisa.css",
    )
    legacy_identity = "KHADA" + "MATI"
    for relative in active_surface:
        if legacy_identity in (ROOT / relative).read_text(encoding="utf-8", errors="ignore").upper():
            fail("foreign_identity_in_active_surface", file=relative)

    try:
        tracked = tracked_files()
    except (RuntimeError, subprocess.SubprocessError, UnicodeDecodeError) as exc:
        fail("git_inventory_failed", error=type(exc).__name__)
        tracked = []
    tracked_set = {relative.replace("\\", "/") for relative in tracked}
    release_surface = set(REQUIRED)
    release_surface.update(TOP_LEVEL_MIRRORS)
    release_surface.update(f"public/{relative}" for relative in TOP_LEVEL_MIRRORS)
    for relative in (
        "assets/brand/bisa-logo.svg",
        "assets/brand/bisa-mark.svg",
        "assets/styles/bisa.css",
        "assets/scripts/bisa-app.js",
        "assets/scripts/bisa-map.js",
        "assets/images/bisa-hero-v1.webp",
        "vendor/leaflet.js",
        "vendor/leaflet.css",
        "vendor/leaflet.LICENSE.txt",
    ):
        release_surface.add(relative)
        release_surface.add(f"public/{relative}")
    for relative in sorted(release_surface - tracked_set):
        fail("required_release_file_not_tracked", file=relative)
    for relative in tracked:
        normalized = relative.replace("\\", "/")
        allowed = normalized == ".env.example"
        if not allowed and any(pattern.search(normalized) for pattern in PROHIBITED_TRACKED):
            fail("prohibited_tracked_file", file=normalized)
            continue
        path = ROOT / relative
        if not path.is_file() or path.stat().st_size > 5_000_000:
            continue
        raw = path.read_bytes()
        if any(pattern.search(raw) for pattern in SECRET_PATTERNS):
            fail("possible_secret", file=normalized)
        if path.suffix.lower() in {".py", ".js", ".html", ".css", ".webmanifest", ".bat"}:
            if legacy_identity.encode("ascii") in raw.upper():
                fail("foreign_product_source_tracked", file=normalized)

    if not errors and version.endswith("-dev"):
        warnings.append({
            "type": "development_release_not_for_production",
            "version": version,
        })

    result = {
        "ok": not errors,
        "app": "BISA",
        "version": version,
        "mirrorsChecked": mirrored_count,
        "requiredFiles": len(REQUIRED),
        "trackedFilesInspected": len(tracked),
        "errors": errors,
        "warnings": warnings,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
