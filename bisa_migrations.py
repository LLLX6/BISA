"""Additive, transactional SQLite migrations for the independent BISA domain."""

from __future__ import annotations

import sqlite3
import hashlib
from pathlib import Path


ROOT = Path(__file__).resolve().parent
MIGRATIONS_DIR = ROOT / "migrations"


def _statements(source: str):
    """Yield complete SQLite statements without using executescript's implicit commit."""
    buffer: list[str] = []
    for line in source.splitlines():
        if not buffer and (not line.strip() or line.lstrip().startswith("--")):
            continue
        buffer.append(line)
        candidate = "\n".join(buffer).strip()
        if candidate and sqlite3.complete_statement(candidate):
            yield candidate
            buffer.clear()
    if any(line.strip() for line in buffer):
        raise RuntimeError("incomplete_migration_statement")


def apply_migrations(con: sqlite3.Connection, applied_at: str) -> list[str]:
    """Apply every new migration in its own savepoint and return applied versions."""
    applied: list[str] = []
    for path in sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.sql")):
        version = path.name.split("_", 1)[0]
        source = path.read_text(encoding="utf-8")
        checksum = hashlib.sha256(source.encode("utf-8")).hexdigest()
        columns = {row["name"] for row in con.execute("PRAGMA table_info(schema_migrations)")}
        existing = con.execute("SELECT * FROM schema_migrations WHERE version=?", (version,)).fetchone()
        if existing:
            if "checksum" in columns and existing["checksum"] and existing["checksum"] != checksum:
                raise RuntimeError(f"migration_checksum_mismatch:{version}")
            continue
        savepoint = f"migration_{version}"
        con.execute(f"SAVEPOINT {savepoint}")
        try:
            for statement in _statements(source):
                con.execute(statement)
            con.execute(
                "INSERT INTO schema_migrations(version,description,applied_at,checksum) VALUES(?,?,?,?)",
                (version, path.stem.split("_", 1)[1].replace("_", " "), applied_at, checksum),
            )
            con.execute(f"RELEASE SAVEPOINT {savepoint}")
        except Exception:
            con.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            con.execute(f"RELEASE SAVEPOINT {savepoint}")
            raise
        applied.append(version)
    return applied
