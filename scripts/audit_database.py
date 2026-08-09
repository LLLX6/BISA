"""Read-only BISA SQLite integrity and business-invariant audit.

Only aggregate counts are emitted; account, merchant, order and address data are
never printed.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3


APP_ID = "om.bisa.marketplace"
REQUIRED_TABLES = {
    "accounts",
    "account_roles",
    "merchants",
    "store_branches",
    "product_categories",
    "products",
    "product_branch_inventory",
    "bundles",
    "bundle_items",
    "carts",
    "cart_items",
    "orders",
    "order_items",
    "inventory_reservations",
    "schema_migrations",
}
BUSINESS_CHECKS = {
    "productsOutsidePriceRule": """
        SELECT COUNT(*) FROM products WHERE price_baisa < 100 OR price_baisa > 2000
    """,
    "bundleComponentsOutsidePriceRule": """
        SELECT COUNT(*) FROM bundle_items x
        JOIN products p ON p.id=x.product_id
        WHERE p.price_baisa < 100 OR p.price_baisa > 2000 OR x.quantity < 1
    """,
    "negativeInventory": """
        SELECT COUNT(*) FROM product_branch_inventory WHERE quantity < 0
    """,
    "publicBranchesWithoutApproval": """
        SELECT COUNT(*) FROM store_branches
        WHERE active=1 AND public_visible=1 AND status!='approved'
    """,
    "invalidOrderTotals": """
        SELECT COUNT(*) FROM orders
        WHERE subtotal_baisa < 0 OR delivery_fee_baisa < 0
           OR total_baisa != subtotal_baisa + delivery_fee_baisa
    """,
    "activeReservationsWithoutLiveOrder": """
        SELECT COUNT(*) FROM inventory_reservations r
        LEFT JOIN orders o ON o.id=r.order_id
        WHERE r.status='active'
          AND (o.id IS NULL OR o.status IN ('rejected','cancelled','expired','completed'))
    """,
}


def scalar(con: sqlite3.Connection, query: str) -> int:
    return int(con.execute(query).fetchone()[0])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("database", type=Path)
    parser.add_argument("--allow-sample-data", action="store_true")
    args = parser.parse_args()
    database = args.database.resolve()
    if not database.is_file():
        parser.error(f"Database not found: {database}")

    con = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        integrity = str(con.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_key_violations = sum(
            1 for _ in con.execute("PRAGMA foreign_key_check")
        )
        tables = {
            str(row["name"])
            for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        missing_tables = sorted(REQUIRED_TABLES - tables)
        violations = (
            {name: scalar(con, query) for name, query in BUSINESS_CHECKS.items()}
            if not missing_tables
            else {}
        )
        demo_records = scalar(con, "SELECT COUNT(*) FROM demo_records") if "demo_records" in tables else 0
        blank_migration_checksums = 0
        migration_versions: list[str] = []
        if "schema_migrations" in tables:
            columns = {
                str(row["name"])
                for row in con.execute("PRAGMA table_info(schema_migrations)")
            }
            migration_versions = [
                str(row["version"])
                for row in con.execute("SELECT version FROM schema_migrations ORDER BY version")
            ]
            if "checksum" in columns:
                blank_migration_checksums = scalar(
                    con, "SELECT COUNT(*) FROM schema_migrations WHERE checksum='' AND version!='001'"
                )
    finally:
        con.close()

    errors: list[str] = []
    if integrity != "ok":
        errors.append("integrity_check_failed")
    if foreign_key_violations:
        errors.append("foreign_key_violations")
    if missing_tables:
        errors.append("required_tables_missing")
    if any(violations.values()):
        errors.append("business_invariant_violation")
    if blank_migration_checksums:
        errors.append("migration_checksum_missing")
    if demo_records and not args.allow_sample_data:
        errors.append("sample_data_present")

    result = {
        "ok": not errors,
        "applicationId": APP_ID,
        "database": database.name,
        "bytes": database.stat().st_size,
        "integrity": integrity,
        "foreignKeyViolations": foreign_key_violations,
        "tableCount": len(tables),
        "missingTables": missing_tables,
        "migrations": migration_versions,
        "blankMigrationChecksums": blank_migration_checksums,
        "businessViolations": violations,
        "sampleRecords": demo_records,
        "errors": errors,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
