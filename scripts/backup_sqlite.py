"""Create a consistent, checksummed backup of BISA SQLite data and uploads."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import zipfile


FORMAT = "bisa-backup-v1"
APP_ID = "om.bisa.marketplace"


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _bounded_env(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"invalid_integer_environment:{name}") from exc
    return max(minimum, min(maximum, value))


MAX_FILES = _bounded_env("BISA_BACKUP_MAX_FILES", 100_000, 1, 1_000_000)
MAX_ENTRY_BYTES = _bounded_env(
    "BISA_BACKUP_MAX_ENTRY_BYTES", 2 * 1024**3, 1024, 20 * 1024**3
)
MAX_TOTAL_BYTES = _bounded_env(
    "BISA_BACKUP_MAX_TOTAL_BYTES", 20 * 1024**3, 1024, 100 * 1024**3
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_pending_archive(path: Path, entries: dict[str, dict[str, object]]) -> None:
    """Catch files that changed between manifest hashing and ZIP streaming."""
    with zipfile.ZipFile(path) as archive:
        for name, expected in entries.items():
            digest = hashlib.sha256()
            size = 0
            with archive.open(name) as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    size += len(chunk)
                    digest.update(chunk)
            if size != int(expected["bytes"]) or digest.hexdigest() != expected["sha256"]:
                raise RuntimeError("backup_source_changed_during_archive")


def upload_files(root: Path | None) -> list[tuple[Path, str]]:
    if root is None:
        return []
    if not root.exists():
        raise FileNotFoundError(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("uploads_path_is_not_a_real_directory")
    result: list[tuple[Path, str]] = []
    total = 0
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError("uploads_symlink_not_allowed")
        if not path.is_file():
            continue
        size = path.stat().st_size
        if size > MAX_ENTRY_BYTES:
            raise ValueError("backup_entry_too_large")
        total += size
        if total > MAX_TOTAL_BYTES:
            raise ValueError("backup_total_too_large")
        relative = path.relative_to(root).as_posix()
        result.append((path, f"uploads/{relative}"))
        if len(result) > MAX_FILES:
            raise ValueError("backup_file_count_exceeded")
    return result


def _migration_snapshot(database: Path) -> list[dict[str, str]]:
    con = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        exists = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
        ).fetchone()
        if not exists:
            return []
        columns = {
            str(row["name"])
            for row in con.execute("PRAGMA table_info(schema_migrations)")
        }
        checksum = "checksum" if "checksum" in columns else "'' AS checksum"
        return [
            {"version": str(row["version"]), "checksum": str(row["checksum"] or "")}
            for row in con.execute(
                f"SELECT version,{checksum} FROM schema_migrations ORDER BY version"
            )
        ]
    finally:
        con.close()


def create_backup(
    database: Path,
    output: Path,
    uploads: Path | None = None,
) -> dict[str, object]:
    if database.is_symlink() or output.is_symlink() or (uploads is not None and uploads.is_symlink()):
        raise ValueError("backup_symlink_path_not_allowed")
    database = database.resolve()
    output = output.resolve()
    uploads = uploads.resolve() if uploads is not None else None
    if uploads is not None and _inside(output, uploads):
        raise ValueError("backup_output_must_not_be_inside_uploads")
    if not database.is_file():
        raise FileNotFoundError(database)
    if output.exists():
        raise FileExistsError(output)
    checksum = output.with_suffix(output.suffix + ".sha256")
    if checksum.exists():
        raise FileExistsError(checksum)
    output.parent.mkdir(parents=True, exist_ok=True)

    files = upload_files(uploads)
    with tempfile.TemporaryDirectory(prefix="bisa-backup-", dir=output.parent) as temp:
        temp_root = Path(temp)
        snapshot = temp_root / "database.sqlite3"
        source = sqlite3.connect(
            f"file:{database.as_posix()}?mode=ro", uri=True, timeout=30
        )
        destination = sqlite3.connect(snapshot)
        try:
            source.backup(destination)
        finally:
            destination.close()
            source.close()

        verify = sqlite3.connect(f"file:{snapshot.as_posix()}?mode=ro", uri=True)
        try:
            integrity = str(verify.execute("PRAGMA integrity_check").fetchone()[0])
            foreign_key_violations = sum(
                1 for _ in verify.execute("PRAGMA foreign_key_check")
            )
        finally:
            verify.close()
        if integrity != "ok":
            raise RuntimeError("backup_integrity_check_failed")
        if foreign_key_violations:
            raise RuntimeError("backup_foreign_key_check_failed")
        if snapshot.stat().st_size > MAX_ENTRY_BYTES:
            raise ValueError("backup_database_too_large")

        entries: dict[str, dict[str, object]] = {
            "database.sqlite3": {
                "bytes": snapshot.stat().st_size,
                "sha256": sha256_file(snapshot),
            }
        }
        for path, archive_name in files:
            entries[archive_name] = {
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        total_bytes = sum(int(entry["bytes"]) for entry in entries.values())
        if total_bytes > MAX_TOTAL_BYTES:
            raise ValueError("backup_total_too_large")
        manifest = {
            "format": FORMAT,
            "applicationId": APP_ID,
            "release": os.environ.get("BISA_RELEASE", "unknown")[:80],
            "createdAt": datetime.now(UTC).isoformat(),
            "databaseEngine": "sqlite",
            "encrypted": False,
            "migrations": _migration_snapshot(snapshot),
            "entries": entries,
        }

        pending = temp_root / "backup.zip"
        with zipfile.ZipFile(
            pending, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
        ) as archive:
            archive.write(snapshot, "database.sqlite3")
            for path, archive_name in files:
                archive.write(path, archive_name)
            archive.writestr(
                "manifest.json",
                json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
            )
        _verify_pending_archive(pending, entries)
        archive_hash = sha256_file(pending)
        pending_checksum = temp_root / "backup.zip.sha256"
        pending_checksum.write_text(
            f"{archive_hash}  {output.name}\n", encoding="ascii"
        )
        os.replace(pending, output)
        try:
            os.replace(pending_checksum, checksum)
        except Exception:
            output.unlink(missing_ok=True)
            raise

    if os.name != "nt":
        os.chmod(output, 0o600)
        os.chmod(checksum, 0o600)
    return {
        "format": FORMAT,
        "applicationId": APP_ID,
        "archive": output.name,
        "bytes": output.stat().st_size,
        "sha256": archive_hash,
        "uploads": len(files),
        "integrity": integrity,
        "foreignKeyViolations": foreign_key_violations,
        "encrypted": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create a BISA-only backup. Store the resulting unencrypted archive securely."
    )
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--uploads", type=Path)
    args = parser.parse_args()
    result = create_backup(args.database, args.output, args.uploads)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
