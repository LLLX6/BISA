"""Validate and restore a BISA backup only into new, empty target paths."""

from __future__ import annotations

import argparse
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import stat
import tempfile
import zipfile

try:
    from scripts.backup_sqlite import (
        APP_ID,
        FORMAT,
        MAX_ENTRY_BYTES,
        MAX_FILES,
        MAX_TOTAL_BYTES,
        sha256_file,
    )
except ModuleNotFoundError:
    # Support: python scripts/restore_sqlite_backup.py ...
    from backup_sqlite import (  # type: ignore
        APP_ID,
        FORMAT,
        MAX_ENTRY_BYTES,
        MAX_FILES,
        MAX_TOTAL_BYTES,
        sha256_file,
    )


SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def safe_archive_name(name: str) -> bool:
    path = PurePosixPath(name)
    return (
        bool(name)
        and "\\" not in name
        and not path.is_absolute()
        and ".." not in path.parts
        and (
            name in {"database.sqlite3", "manifest.json", "uploads/"}
            or (len(path.parts) > 1 and path.parts[0] == "uploads" and not name.endswith("/"))
        )
    )


def _archive_checksum(archive_path: Path, checksum_path: Path | None) -> bool:
    selected = checksum_path
    if selected is None:
        candidate = archive_path.with_suffix(archive_path.suffix + ".sha256")
        selected = candidate if candidate.is_file() else None
    if selected is None:
        return False
    if selected.is_symlink():
        raise ValueError("checksum_symlink_not_allowed")
    if not selected.is_file():
        raise FileNotFoundError(selected)
    expected = selected.read_text(encoding="ascii").split()[0].lower()
    if not SHA256.fullmatch(expected) or sha256_file(archive_path) != expected:
        raise ValueError("archive_checksum_mismatch")
    return True


def _extract_checked(
    archive: zipfile.ZipFile,
    name: str,
    target: Path,
    expected: dict[str, object],
) -> None:
    expected_bytes = int(expected.get("bytes", -1))
    expected_hash = str(expected.get("sha256", "")).lower()
    if expected_bytes < 0 or expected_bytes > MAX_ENTRY_BYTES or not SHA256.fullmatch(expected_hash):
        raise ValueError("backup_manifest_entry_invalid")
    target.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    written = 0
    with archive.open(name, "r") as source, target.open("xb") as destination:
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            written += len(chunk)
            if written > expected_bytes or written > MAX_ENTRY_BYTES:
                raise ValueError("backup_entry_size_mismatch")
            digest.update(chunk)
            destination.write(chunk)
    if written != expected_bytes or digest.hexdigest() != expected_hash:
        raise ValueError("backup_entry_checksum_mismatch")


def restore_backup(
    archive_path: Path,
    database_target: Path,
    uploads_target: Path | None = None,
    checksum_path: Path | None = None,
) -> dict[str, object]:
    if (
        archive_path.is_symlink()
        or database_target.is_symlink()
        or (uploads_target is not None and uploads_target.is_symlink())
        or (checksum_path is not None and checksum_path.is_symlink())
    ):
        raise ValueError("restore_symlink_path_not_allowed")
    archive_path = archive_path.resolve()
    database_target = database_target.resolve()
    uploads_target = uploads_target.resolve() if uploads_target is not None else None
    checksum_path = checksum_path.resolve() if checksum_path is not None else None
    if uploads_target is not None and _inside(database_target, uploads_target):
        raise ValueError("database_target_must_not_be_inside_uploads_target")
    if not archive_path.is_file():
        raise FileNotFoundError(archive_path)
    if database_target.exists():
        raise FileExistsError(database_target)
    if uploads_target is not None and uploads_target.exists():
        raise FileExistsError(uploads_target)
    database_target.parent.mkdir(parents=True, exist_ok=True)
    if uploads_target is not None:
        uploads_target.parent.mkdir(parents=True, exist_ok=True)
    checksum_verified = _archive_checksum(archive_path, checksum_path)

    with zipfile.ZipFile(archive_path) as archive:
        infos = archive.infolist()
        names = [info.filename for info in infos]
        if len(names) != len(set(names)):
            raise ValueError("duplicate_archive_entry")
        if len(infos) > MAX_FILES + 2:
            raise ValueError("backup_file_count_exceeded")
        if any(not safe_archive_name(name) for name in names):
            raise ValueError("unsafe_archive_path")
        for info in infos:
            file_type = (info.external_attr >> 16) & 0o170000
            if file_type == stat.S_IFLNK:
                raise ValueError("archive_symlink_not_allowed")
        by_name = {info.filename: info for info in infos}
        if "manifest.json" not in by_name or "database.sqlite3" not in by_name:
            raise ValueError("backup_required_entry_missing")
        if by_name["manifest.json"].file_size > 2 * 1024 * 1024:
            raise ValueError("backup_manifest_too_large")
        manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
        if manifest.get("format") != FORMAT or manifest.get("applicationId") != APP_ID:
            raise ValueError("unsupported_backup_identity")
        entries = manifest.get("entries")
        if not isinstance(entries, dict):
            raise ValueError("backup_manifest_invalid")
        payload_names = {name for name in names if name != "manifest.json" and not name.endswith("/")}
        if payload_names != set(entries):
            raise ValueError("backup_manifest_entries_mismatch")
        if any(name.startswith("uploads/") for name in payload_names) and uploads_target is None:
            raise ValueError("uploads_target_required")

        total_bytes = 0
        for name, expected in entries.items():
            if not isinstance(expected, dict):
                raise ValueError("backup_manifest_entry_invalid")
            info = by_name.get(name)
            declared = int(expected.get("bytes", -1))
            if info is None or declared != info.file_size or declared < 0 or declared > MAX_ENTRY_BYTES:
                raise ValueError("backup_manifest_entry_invalid")
            total_bytes += declared
        if total_bytes > MAX_TOTAL_BYTES:
            raise ValueError("backup_total_too_large")

        with ExitStack() as stack:
            db_temp = Path(stack.enter_context(tempfile.TemporaryDirectory(
                prefix="bisa-restore-db-", dir=database_target.parent
            )))
            staged_database = db_temp / "database.sqlite3"
            _extract_checked(
                archive, "database.sqlite3", staged_database, entries["database.sqlite3"]
            )

            staged_uploads: Path | None = None
            upload_count = 0
            if uploads_target is not None:
                upload_temp = Path(stack.enter_context(tempfile.TemporaryDirectory(
                    prefix="bisa-restore-uploads-", dir=uploads_target.parent
                )))
                staged_uploads = upload_temp / "uploads"
                staged_uploads.mkdir()
                for name in sorted(payload_names):
                    if not name.startswith("uploads/"):
                        continue
                    relative = PurePosixPath(name).relative_to("uploads")
                    target = staged_uploads.joinpath(*relative.parts)
                    _extract_checked(archive, name, target, entries[name])
                    upload_count += 1

            check = sqlite3.connect(
                f"file:{staged_database.as_posix()}?mode=ro", uri=True
            )
            try:
                integrity = str(check.execute("PRAGMA integrity_check").fetchone()[0])
                foreign_key_violations = sum(
                    1 for _ in check.execute("PRAGMA foreign_key_check")
                )
            finally:
                check.close()
            if integrity != "ok":
                raise ValueError("restored_database_integrity_failed")
            if foreign_key_violations:
                raise ValueError("restored_database_foreign_key_check_failed")

            claimed_database = False
            claimed_uploads = False
            database_installed = False
            try:
                descriptor = os.open(
                    database_target,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o600,
                )
                os.close(descriptor)
                claimed_database = True
                if staged_uploads is not None:
                    uploads_target.mkdir()
                    claimed_uploads = True
                    for child in staged_uploads.iterdir():
                        os.replace(child, uploads_target / child.name)
                os.replace(staged_database, database_target)
                database_installed = True
            except Exception:
                if claimed_uploads and uploads_target is not None and uploads_target.is_dir():
                    shutil.rmtree(uploads_target)
                if claimed_database and not database_installed and database_target.is_file():
                    database_target.unlink()
                raise

    return {
        "format": FORMAT,
        "applicationId": APP_ID,
        "database": database_target.name,
        "uploads": upload_count,
        "integrity": integrity,
        "foreignKeyViolations": foreign_key_violations,
        "archiveChecksumVerified": checksum_verified,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Restore a verified BISA backup without overwriting any target."
    )
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--checksum", type=Path)
    parser.add_argument("--database-target", required=True, type=Path)
    parser.add_argument("--uploads-target", type=Path)
    parser.add_argument(
        "--confirm",
        required=True,
        choices=["RESTORE_BISA_TO_EMPTY_TARGET"],
        help="Restoration never overwrites an existing database or uploads directory.",
    )
    args = parser.parse_args()
    result = restore_backup(
        args.archive, args.database_target, args.uploads_target, args.checksum
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
