import base64
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import zipfile

from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from py_vapid import Vapid02

from scripts.backup_sqlite import APP_ID, FORMAT, create_backup
from scripts.check_production_readiness import check_environment
from scripts.restore_sqlite_backup import restore_backup


ROOT = Path(__file__).resolve().parents[1]


class BisaProductionReadinessTests(unittest.TestCase):
    def valid_environment(self, root: Path) -> dict[str, str]:
        data = root / "bisa-data"
        uploads = data / "uploads"
        backups = data / "backups"
        uploads.mkdir(parents=True)
        backups.mkdir()
        return {
            "BISA_ENV": "production",
            "BISA_RELEASE": "1.0.0",
            "BISA_SEED_SAMPLE_DATA": "false",
            "BISA_PHONE_VERIFICATION_MODE": "invite_only",
            "BISA_PUBLIC_URL": "https://bisa.example/app/",
            "BISA_ALLOWED_ORIGINS": "https://bisa.example",
            "BISA_DB_PATH": str(data / "bisa.sqlite3"),
            "BISA_UPLOAD_DIR": str(uploads),
            "BISA_BACKUP_DIR": str(backups),
            "BISA_AUTH_PEPPER": "a" * 40,
            "BISA_MEDIA_SIGNING_KEY": "m" * 40,
            "BISA_PAYMENT_GATEWAY": "unconfigured",
        }

    def test_valid_environment_is_secret_free(self):
        with tempfile.TemporaryDirectory(prefix="bisa-preflight-") as temp:
            environment = self.valid_environment(Path(temp))
            result = check_environment(environment)
        self.assertTrue(result["ok"], result)
        self.assertFalse(result["valuesExposed"])
        rendered = json.dumps(result)
        self.assertNotIn(environment["BISA_AUTH_PEPPER"], rendered)
        self.assertNotIn(environment["BISA_MEDIA_SIGNING_KEY"], rendered)

    def test_development_prerelease_and_ephemeral_paths_are_rejected(self):
        result = check_environment(
            {
                "BISA_ENV": "development",
                "BISA_RELEASE": "0.2.0-dev",
                "BISA_SEED_SAMPLE_DATA": "true",
                "BISA_PUBLIC_URL": "http://localhost:8080",
                "BISA_ALLOWED_ORIGINS": "*",
                "BISA_AUTH_PEPPER": "short",
                "BISA_MEDIA_SIGNING_KEY": "short",
            },
            check_paths=False,
        )
        codes = {item["code"] for item in result["errors"]}
        self.assertFalse(result["ok"])
        self.assertIn("environment_not_production", codes)
        self.assertIn("stable_release_required", codes)
        self.assertIn("sample_seed_must_be_disabled", codes)
        self.assertIn("phone_verification_flow_required", codes)
        self.assertIn("persistent_path_required", codes)

    def test_overlapping_data_paths_are_rejected(self):
        with tempfile.TemporaryDirectory(prefix="bisa-preflight-overlap-") as temp:
            environment = self.valid_environment(Path(temp))
            environment["BISA_BACKUP_DIR"] = environment["BISA_UPLOAD_DIR"]
            result = check_environment(environment)
        codes = {item["code"] for item in result["errors"]}
        self.assertIn("upload_and_backup_paths_must_be_separate", codes)

    def test_credentials_cannot_make_unimplemented_adapter_ready(self):
        with tempfile.TemporaryDirectory(prefix="bisa-preflight-adapter-") as temp:
            environment = self.valid_environment(Path(temp))
            environment.update({
                "BISA_WHATSAPP_PHONE_NUMBER_ID": "configured-id",
                "BISA_WHATSAPP_ACCESS_TOKEN": "configured-token",
            })
            result = check_environment(environment)
        codes = {item["code"] for item in result["errors"]}
        self.assertFalse(result["ok"])
        self.assertIn("external_adapter_implementation_required", codes)
        self.assertEqual(
            "credentials_present_but_adapter_unavailable",
            result["integrations"]["whatsapp"],
        )

    def test_push_requires_a_complete_matching_vapid_pair_when_enabled(self):
        vapid = Vapid02()
        vapid.generate_keys()
        public_key = base64.urlsafe_b64encode(
            vapid.public_key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
        ).decode().rstrip("=")
        with tempfile.TemporaryDirectory(prefix="bisa-preflight-push-") as temp:
            environment = self.valid_environment(Path(temp))
            environment.update({
                "BISA_VAPID_PUBLIC_KEY": public_key,
                "BISA_VAPID_PRIVATE_KEY": vapid.private_pem().decode(),
                "BISA_VAPID_SUBJECT": "mailto:push-operator@example.com",
                "BISA_PUSH_REQUIRED": "true",
            })
            valid = check_environment(environment)
            self.assertTrue(valid["ok"], valid)
            self.assertEqual("configured", valid["integrations"]["push"])
            environment["BISA_VAPID_PUBLIC_KEY"] = public_key[:-1] + (
                "A" if public_key[-1] != "A" else "B"
            )
            mismatch = check_environment(environment)
        self.assertFalse(mismatch["ok"])
        codes = {item["code"] for item in mismatch["errors"]}
        self.assertIn("push_vapid_key_mismatch", codes)
        self.assertIn("push_required_but_unavailable", codes)

    def test_private_or_ambiguous_production_urls_are_rejected(self):
        with tempfile.TemporaryDirectory(prefix="bisa-preflight-url-") as temp:
            environment = self.valid_environment(Path(temp))
            environment["BISA_PUBLIC_URL"] = "https://10.0.0.5/app?unsafe=1"
            environment["BISA_ALLOWED_ORIGINS"] = "https://10.0.0.5"
            result = check_environment(environment)
        codes = {item["code"] for item in result["errors"]}
        self.assertIn("public_url_must_use_public_https", codes)
        self.assertIn("production_origin_invalid", codes)

    def test_runtime_rejects_prerelease_in_production(self):
        environment = {
            **os.environ,
            "BISA_ENV": "production",
            "BISA_RELEASE": "0.2.0-dev",
            "BISA_SEED_SAMPLE_DATA": "false",
            "BISA_PHONE_VERIFICATION_MODE": "invite_only",
            "BISA_DB_PATH": "/var/data/bisa.sqlite3",
            "BISA_UPLOAD_DIR": "/var/data/uploads",
            "BISA_BACKUP_DIR": "/var/data/backups",
            "BISA_PUBLIC_URL": "https://bisa.example",
            "BISA_ALLOWED_ORIGINS": "https://bisa.example",
        }
        command = [
            sys.executable,
            "-c",
            "import json; from bisa_config import production_readiness; "
            "print(json.dumps(production_readiness()))",
        ]
        result = subprocess.run(
            command,
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=True,
        )
        readiness = json.loads(result.stdout)
        self.assertFalse(readiness["ready"])
        self.assertIn("stable_release_required", readiness["errors"])

        environment["BISA_RELEASE"] = "1.2.3"
        stable = subprocess.run(
            command,
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=True,
        )
        stable_readiness = json.loads(stable.stdout)
        self.assertTrue(stable_readiness["ready"], stable_readiness)
        self.assertNotIn("stable_release_required", stable_readiness["errors"])


class BisaBackupRestoreTests(unittest.TestCase):
    def make_source(self, root: Path) -> tuple[Path, Path]:
        database = root / "source.sqlite3"
        uploads = root / "uploads"
        uploads.mkdir()
        (uploads / "catalog" / "image.txt").parent.mkdir()
        (uploads / "catalog" / "image.txt").write_text("bisa-test", encoding="utf-8")
        con = sqlite3.connect(database)
        try:
            con.execute("PRAGMA foreign_keys=ON")
            con.execute("CREATE TABLE sample(id INTEGER PRIMARY KEY,value TEXT NOT NULL)")
            con.execute("INSERT INTO sample(value) VALUES(?)", ("safe",))
            con.commit()
        finally:
            con.close()
        return database, uploads

    def test_bisa_backup_round_trip_and_checksum(self):
        with tempfile.TemporaryDirectory(prefix="bisa-backup-test-") as temp:
            root = Path(temp)
            source, uploads = self.make_source(root)
            archive = root / "backup.zip"
            created = create_backup(source, archive, uploads)
            self.assertEqual(FORMAT, created["format"])
            self.assertEqual(APP_ID, created["applicationId"])
            self.assertFalse(created["encrypted"])
            with zipfile.ZipFile(archive) as bundle:
                manifest = json.loads(bundle.read("manifest.json"))
            self.assertEqual(FORMAT, manifest["format"])
            self.assertEqual(APP_ID, manifest["applicationId"])

            target_database = root / "restored.sqlite3"
            target_uploads = root / "restored-uploads"
            restored = restore_backup(archive, target_database, target_uploads)
            self.assertTrue(restored["archiveChecksumVerified"])
            self.assertEqual(0, restored["foreignKeyViolations"])
            con = sqlite3.connect(target_database)
            try:
                self.assertEqual("safe", con.execute("SELECT value FROM sample").fetchone()[0])
            finally:
                con.close()
            self.assertEqual(
                "bisa-test",
                (target_uploads / "catalog" / "image.txt").read_text(encoding="utf-8"),
            )

    def test_restore_refuses_existing_target_and_bad_checksum(self):
        with tempfile.TemporaryDirectory(prefix="bisa-restore-guard-") as temp:
            root = Path(temp)
            source, _ = self.make_source(root)
            archive = root / "backup.zip"
            create_backup(source, archive)
            existing = root / "existing.sqlite3"
            existing.write_bytes(b"do-not-overwrite")
            with self.assertRaises(FileExistsError):
                restore_backup(archive, existing)
            self.assertEqual(b"do-not-overwrite", existing.read_bytes())

            bad_checksum = root / "bad.sha256"
            bad_checksum.write_text(f"{'0' * 64}  {archive.name}\n", encoding="ascii")
            with self.assertRaisesRegex(ValueError, "archive_checksum_mismatch"):
                restore_backup(archive, root / "other.sqlite3", checksum_path=bad_checksum)

    def test_restore_cli_requires_bisa_confirmation_phrase(self):
        result = subprocess.run(
            [sys.executable, "scripts/restore_sqlite_backup.py", "--help"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("RESTORE_BISA_TO_EMPTY_TARGET", result.stdout)

    def test_backup_and_restore_reject_overlapping_upload_paths(self):
        with tempfile.TemporaryDirectory(prefix="bisa-backup-paths-") as temp:
            root = Path(temp)
            source, uploads = self.make_source(root)
            with self.assertRaisesRegex(ValueError, "backup_output_must_not_be_inside_uploads"):
                create_backup(source, uploads / "unsafe-backup.zip", uploads)

            archive = root / "safe-backup.zip"
            create_backup(source, archive, uploads)
            with self.assertRaisesRegex(
                ValueError, "database_target_must_not_be_inside_uploads_target"
            ):
                restore_backup(
                    archive,
                    root / "restored" / "database.sqlite3",
                    root / "restored",
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)
