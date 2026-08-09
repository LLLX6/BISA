import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bisa_migrations


class BisaMigrationTests(unittest.TestCase):
    def connection(self):
        con = sqlite3.connect(":memory:", isolation_level=None)
        con.row_factory = sqlite3.Row
        con.execute("BEGIN")
        con.execute("""CREATE TABLE schema_migrations(
            version TEXT PRIMARY KEY, description TEXT NOT NULL,
            applied_at TEXT NOT NULL, checksum TEXT NOT NULL DEFAULT '')""")
        return con

    def test_migration_is_applied_once_and_records_checksum(self):
        with tempfile.TemporaryDirectory(prefix="bisa-migrations-") as directory:
            root = Path(directory)
            (root / "010_add_example.sql").write_text(
                "CREATE TABLE example(id TEXT PRIMARY KEY);\n", encoding="utf-8"
            )
            con = self.connection()
            with patch.object(bisa_migrations, "MIGRATIONS_DIR", root):
                self.assertEqual(bisa_migrations.apply_migrations(con, "2026-08-09T00:00:00Z"), ["010"])
                self.assertEqual(bisa_migrations.apply_migrations(con, "2026-08-09T00:00:01Z"), [])
            row = con.execute("SELECT * FROM schema_migrations WHERE version='010'").fetchone()
            self.assertEqual(len(row["checksum"]), 64)
            self.assertEqual(con.execute("SELECT COUNT(*) n FROM example").fetchone()["n"], 0)

    def test_failed_migration_rolls_back_its_partial_schema(self):
        with tempfile.TemporaryDirectory(prefix="bisa-migrations-") as directory:
            root = Path(directory)
            (root / "011_broken.sql").write_text(
                "CREATE TABLE should_rollback(id TEXT);\n"
                "INSERT INTO table_that_does_not_exist(id) VALUES('x');\n",
                encoding="utf-8",
            )
            con = self.connection()
            with patch.object(bisa_migrations, "MIGRATIONS_DIR", root):
                with self.assertRaises(sqlite3.OperationalError):
                    bisa_migrations.apply_migrations(con, "2026-08-09T00:00:00Z")
            self.assertIsNone(con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='should_rollback'"
            ).fetchone())
            self.assertEqual(con.execute("SELECT COUNT(*) n FROM schema_migrations").fetchone()["n"], 0)

    def test_changed_applied_migration_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix="bisa-migrations-") as directory:
            root = Path(directory)
            migration = root / "012_immutable.sql"
            migration.write_text("CREATE TABLE immutable(id TEXT);\n", encoding="utf-8")
            con = self.connection()
            with patch.object(bisa_migrations, "MIGRATIONS_DIR", root):
                bisa_migrations.apply_migrations(con, "2026-08-09T00:00:00Z")
                migration.write_text("CREATE TABLE changed(id TEXT);\n", encoding="utf-8")
                with self.assertRaisesRegex(RuntimeError, "migration_checksum_mismatch:012"):
                    bisa_migrations.apply_migrations(con, "2026-08-09T00:00:01Z")

    def test_supplier_notification_migration_moves_legacy_account_target(self):
        con = self.connection()
        con.execute(
            """CREATE TABLE supplier_campaigns(
                id TEXT PRIMARY KEY,supplier_id TEXT NOT NULL)"""
        )
        con.execute(
            """CREATE TABLE notifications(
                id TEXT PRIMARY KEY,target_kind TEXT NOT NULL,target_id TEXT NOT NULL,
                route TEXT NOT NULL)"""
        )
        con.execute(
            "INSERT INTO supplier_campaigns(id,supplier_id) VALUES('campaign_one','supplier_one')"
        )
        con.executemany(
            "INSERT INTO notifications(id,target_kind,target_id,route) VALUES(?,?,?,?)",
            [
                ("supplier_notice", "account", "shared_account", "supplier:campaign:campaign_one"),
                ("shopper_notice", "account", "shared_account", "shopper:order:order_one"),
                ("unknown_supplier_notice", "account", "shared_account", "supplier:campaign:missing"),
            ],
        )
        migration = Path(bisa_migrations.MIGRATIONS_DIR) / "005_supplier_notification_isolation.sql"
        with tempfile.TemporaryDirectory(prefix="bisa-migrations-") as directory:
            copied = Path(directory) / migration.name
            copied.write_text(migration.read_text(encoding="utf-8"), encoding="utf-8")
            with patch.object(bisa_migrations, "MIGRATIONS_DIR", Path(directory)):
                self.assertEqual(
                    bisa_migrations.apply_migrations(con, "2026-08-09T00:00:00Z"), ["005"],
                )
        rows = {
            row["id"]: (row["target_kind"], row["target_id"])
            for row in con.execute("SELECT * FROM notifications")
        }
        self.assertEqual(("supplier", "supplier_one"), rows["supplier_notice"])
        self.assertEqual(("account", "shared_account"), rows["shopper_notice"])
        self.assertEqual(("account", "shared_account"), rows["unknown_supplier_notice"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
