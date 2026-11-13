"""Consistent, verified SQLite snapshots; restoration never replaces a file."""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

MANIFEST_VERSION = 1


def readonly_connection(path: Path) -> sqlite3.Connection:
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError("База данных не найдена")
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
    connection.execute("PRAGMA query_only=ON")
    return connection


def schema_metadata(db: sqlite3.Connection) -> dict[str, Any]:
    rows = db.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' "
        "ORDER BY type,name"
    ).fetchall()
    encoded = json.dumps([tuple(row) for row in rows], ensure_ascii=False, separators=(",", ":"))
    return {
        "schema_sha256": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
        "user_version": int(db.execute("PRAGMA user_version").fetchone()[0]),
        "tables": sorted(row[1] for row in rows if row[0] == "table"),
    }


def check_database(db: sqlite3.Connection) -> dict[str, Any]:
    integrity = [str(row[0]) for row in db.execute("PRAGMA integrity_check")]
    violations = db.execute("PRAGMA foreign_key_check").fetchall()
    return {
        "integrity_ok": integrity == ["ok"],
        "foreign_keys_ok": not violations,
        "foreign_key_violations": len(violations),
        **schema_metadata(db),
    }


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _manifest_path(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".manifest.json")


def create_backup(source: Path | sqlite3.Connection, destination: Path | None = None) -> dict[str, Any]:
    """Use SQLite online backup, including committed WAL data, then verify it.

    Callers supplying a connection must commit or roll back first. Destination
    and manifest are both exclusive new files. No source migration is run.
    """
    own_connection = not isinstance(source, sqlite3.Connection)
    if own_connection:
        source_path = Path(source).expanduser().resolve()
        db = readonly_connection(source_path)
    else:
        db = source
        if db.in_transaction:
            raise ValueError("Перед резервным копированием завершите текущую транзакцию")
        database = next((row[2] for row in db.execute("PRAGMA database_list") if row[1] == "main"), "")
        source_path = Path(database).resolve() if database else None
    try:
        if destination is None:
            if source_path is None:
                raise ValueError("Для базы в памяти укажите путь резервной копии")
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            destination = source_path.parent / "backups" / f"{source_path.stem}-{stamp}-{uuid4().hex[:8]}.sqlite3"
        target = Path(destination).expanduser().resolve()
        manifest_path = _manifest_path(target)
        if target == source_path or target.exists() or manifest_path.exists():
            raise FileExistsError("Путь резервной копии уже существует или совпадает с исходной базой")
        target.parent.mkdir(parents=True, exist_ok=True)
        created = False
        manifest_created = False
        try:
            with target.open("xb"):
                pass
            created = True
            snapshot = sqlite3.connect(target)
            try:
                deadline = time.monotonic() + 30
                def progress(_status: int, _remaining: int, _total: int) -> None:
                    if time.monotonic() > deadline:
                        raise TimeoutError("Резервное копирование превысило 30 секунд; повторите после завершения записи")
                db.backup(snapshot, pages=256, progress=progress, sleep=0.01)
                # A portable snapshot is one complete file, with no WAL/SHM
                # dependency carried over from the source database.
                snapshot.execute("PRAGMA journal_mode=DELETE")
                checks = check_database(snapshot)
            finally:
                snapshot.close()
            if not checks["integrity_ok"] or not checks["foreign_keys_ok"]:
                raise ValueError("Резервная копия не прошла integrity_check/foreign_key_check")
            manifest = {
                "format_version": MANIFEST_VERSION,
                "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "database_file": target.name,
                "source_file": source_path.name if source_path else ":memory:",
                "sqlite_version": sqlite3.sqlite_version,
                "size_bytes": target.stat().st_size,
                "sha256": _hash(target),
                **checks,
            }
            with manifest_path.open("x", encoding="utf-8", newline="\n") as stream:
                manifest_created = True
                json.dump(manifest, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
            return {"status": "ok", "backup_path": str(target), "manifest_path": str(manifest_path), **manifest}
        except BaseException:
            if manifest_created:
                manifest_path.unlink(missing_ok=True)
            if created:
                target.unlink(missing_ok=True)
            raise
    finally:
        if own_connection:
            db.close()


def verify_backup(backup_path: Path) -> dict[str, Any]:
    path = Path(backup_path).expanduser().resolve()
    manifest_path = _manifest_path(path)
    if not path.is_file() or not manifest_path.is_file():
        raise FileNotFoundError("Для проверки нужны файл резервной копии и его manifest.json")
    if manifest_path.stat().st_size > 1024 * 1024:
        raise ValueError("Манифест резервной копии слишком велик")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("format_version") != MANIFEST_VERSION:
        raise ValueError("Неподдерживаемый формат манифеста")
    if manifest.get("sha256") != _hash(path) or manifest.get("size_bytes") != path.stat().st_size:
        raise ValueError("Контрольная сумма или размер резервной копии не совпадает с манифестом")
    with closing(readonly_connection(path)) as db:
        checks = check_database(db)
    if not checks["integrity_ok"] or not checks["foreign_keys_ok"]:
        raise ValueError("Резервная копия не прошла integrity_check/foreign_key_check")
    if any(checks[key] != manifest.get(key) for key in ("schema_sha256", "user_version", "tables")):
        raise ValueError("Схема резервной копии не совпадает с манифестом")
    return {"status": "ok", "backup_path": str(path), "manifest_path": str(manifest_path), **checks,
            "sha256": manifest["sha256"], "size_bytes": manifest["size_bytes"]}


def restore_backup(backup_path: Path, destination: Path) -> dict[str, Any]:
    """Restore to an exclusive new path; preserve the current runtime database."""
    from .config import DB_PATH
    path = Path(backup_path).expanduser().resolve()
    target = Path(destination).expanduser().resolve()
    if target in {path, DB_PATH.resolve()} or target.exists():
        raise FileExistsError("Восстановление разрешено только в новый путь, отличный от рабочей базы")
    checked = verify_backup(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    created = False
    try:
        with path.open("rb") as source, target.open("xb") as output:
            created = True
            shutil.copyfileobj(source, output)
        if _hash(target) != checked["sha256"]:
            raise ValueError("Контрольная сумма восстановленной базы не совпадает")
        with closing(readonly_connection(target)) as db:
            checks = check_database(db)
        if not checks["integrity_ok"] or not checks["foreign_keys_ok"]:
            raise ValueError("Восстановленная база не прошла проверку целостности")
        return {"status": "ok", "restored_path": str(target), **checks, "sha256": checked["sha256"]}
    except BaseException:
        if created:
            target.unlink(missing_ok=True)
        raise


def backup_before_change(path: Path, reason: str = "before_change") -> dict[str, Any] | None:
    """Snapshot an existing database before a migration or another change."""
    path = Path(path)
    if not path.exists():
        return None
    result = create_backup(path)
    return {**result, "reason": reason}
