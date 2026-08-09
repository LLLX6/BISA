"""Fast deterministic repository verifier for the public BISA foundation."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIRRORS = [
    ("index.html", "public/index.html"),
    ("manifest.webmanifest", "public/manifest.webmanifest"),
    ("service-worker.js", "public/service-worker.js"),
    ("assets/brand/bisa-logo.svg", "public/assets/brand/bisa-logo.svg"),
    ("assets/brand/bisa-mark.svg", "public/assets/brand/bisa-mark.svg"),
    ("assets/styles/bisa.css", "public/assets/styles/bisa.css"),
    ("assets/scripts/bisa-app.js", "public/assets/scripts/bisa-app.js"),
]
REQUIRED = [
    ".env.example", "README.md", "LICENSE", "SECURITY.md", "bisa_config.py",
    "bisa_domain.py", "bisa_server.py", "package.json", "package-lock.json",
    "tests/test_bisa_domain.py", "tests/smoke-bisa-ui.js",
]


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    errors = []
    for relative in REQUIRED:
        if not (ROOT / relative).is_file():
            errors.append(f"missing:{relative}")
    for left, right in MIRRORS:
        if not (ROOT / left).is_file() or not (ROOT / right).is_file():
            errors.append(f"mirror_missing:{left}:{right}")
        elif digest(ROOT / left) != digest(ROOT / right):
            errors.append(f"mirror_mismatch:{left}:{right}")

    manifest = json.loads((ROOT / "manifest.webmanifest").read_text(encoding="utf-8"))
    if manifest.get("name") != "BISA | بيسا" or manifest.get("scope") != "./" or not str(manifest.get("id", "")).startswith("./"):
        errors.append("manifest_identity")
    worker = (ROOT / "service-worker.js").read_text(encoding="utf-8")
    if "bisa-pwa-v0.1.1-demo-catalog" not in worker or "api/" not in worker or "self.registration.scope" not in worker:
        errors.append("service_worker_policy")
    config = (ROOT / "bisa_config.py").read_text(encoding="utf-8")
    for token in ("om.bisa.marketplace", "PRODUCT_MIN_BAISA = 100", "PRODUCT_MAX_BAISA = 2000"):
        if token not in config:
            errors.append(f"config_missing:{token}")
    env = (ROOT / ".env.example").read_text(encoding="utf-8")
    if re.search(r"(?im)^(?:BISA_.*(?:TOKEN|SECRET|PASSWORD|KEY)|.*ACCESS_TOKEN)=[ \t]*[^ \t\r\n]", env):
        errors.append("example_contains_secret")
    active = [ROOT / p for p in ["index.html", "manifest.webmanifest", "service-worker.js", "assets/scripts/bisa-app.js", "assets/styles/bisa.css"]]
    legacy_marker = "KHADA" + "MATI"
    if any(legacy_marker in path.read_text(encoding="utf-8", errors="ignore").upper() for path in active):
        errors.append("legacy_identity_in_active_surface")
    if errors:
        print(json.dumps({"ok": False, "errors": errors}, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps({"ok": True, "mirrors": len(MIRRORS), "required": len(REQUIRED), "app": "BISA"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
