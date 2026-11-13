"""Offline diagnostics: no requests, model imports, migrations or secret output."""
from __future__ import annotations

import importlib.metadata
import math
import os
import sqlite3
import tempfile
from contextlib import closing
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

from .backup import check_database, readonly_connection
from .config import DB_PATH, load_sources, sources_path


@lru_cache(maxsize=1)
def _expected_schema() -> tuple[dict[str, set[str]], set[str]]:
    from .db import SCHEMA, WORKFLOW_COLUMNS, _install_workflow_guards
    with closing(sqlite3.connect(":memory:")) as db:
        db.executescript(SCHEMA)
        for table, definitions in WORKFLOW_COLUMNS.items():
            existing = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
            for column, definition in definitions.items():
                if column not in existing:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        _install_workflow_guards(db)
        tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        columns = {table: {row[1] for row in db.execute(f'PRAGMA table_info("{table}")')} for table in tables}
        guards = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'workflow_%'")}
        return columns, guards


def _expected_columns() -> dict[str, set[str]]:
    return _expected_schema()[0]


def _model_cache_present(model: str) -> bool:
    def complete(folder: Path) -> bool:
        return (folder / "config.json").is_file() and (folder / "modules.json").is_file() and any(
            (folder / name).is_file() for name in ("model.safetensors", "pytorch_model.bin")
        )
    local = Path(model).expanduser()
    if local.is_dir():
        return complete(local)
    cache = os.environ.get("HF_HUB_CACHE") or os.environ.get("HUGGINGFACE_HUB_CACHE")
    root = Path(cache).expanduser() if cache else Path(
        os.environ.get("HF_HOME", str(Path.home() / ".cache" / "huggingface"))
    ).expanduser() / "hub"
    snapshots = root / ("models--" + model.replace("/", "--")) / "snapshots"
    return snapshots.is_dir() and any(complete(folder) for folder in snapshots.iterdir() if folder.is_dir())


def run_doctor(db_path: Path | None = None, config_path: Path | None = None) -> dict[str, Any]:
    """Return JSON-serializable verdicts without changing the working database."""
    database = Path(db_path or DB_PATH).expanduser().resolve()
    config_file = Path(config_path or sources_path()).expanduser().resolve()
    checks: list[dict[str, Any]] = []

    def add(name: str, status: str, detail: str, **metadata: Any) -> None:
        checks.append({"name": name, "status": status, "detail": detail, **metadata})

    try:
        config = load_sources(config_file)
        add("config", "ok", "Конфигурация источников прошла проверку",
            source_count=len(config["sources"]), enabled_count=sum(source["enabled"] for source in config["sources"]))
    except ValueError as exc:
        add("config", "error", str(exc))
    except Exception as exc:
        add("config", "error", "Не удалось прочитать конфигурацию источников", error_type=type(exc).__name__)

    runtime = database.parent
    probe_dir = runtime
    while not probe_dir.exists() and probe_dir != probe_dir.parent:
        probe_dir = probe_dir.parent
    try:
        with tempfile.TemporaryFile(dir=probe_dir) as probe:
            probe.write(b"runtime-write-check")
            probe.flush()
        add("runtime_writable", "ok", "Каталог runtime доступен для записи" if runtime.exists()
            else "Родитель runtime доступен для создания каталога", runtime_exists=runtime.exists())
    except OSError:
        add("runtime_writable", "error", "Каталог runtime недоступен для записи")

    if not database.exists():
        add("database", "warning", "База ещё не создана; doctor не запускает её создание или миграции")
        add("schema", "warning", "Проверка схемы доступна после создания базы")
        add("unresolved_attempts", "warning", "Нет базы для проверки незавершённых отправок", count=0)
    else:
        try:
            with closing(readonly_connection(database)) as db:
                integrity = check_database(db)
                add("database", "ok" if integrity["integrity_ok"] and integrity["foreign_keys_ok"] else "error",
                    "Проверены integrity_check и foreign_key_check", **integrity)
                tables = set(integrity["tables"])
                missing: dict[str, list[str]] = {}
                for table, expected in _expected_columns().items():
                    actual = {row[1] for row in db.execute(f'PRAGMA table_info("{table}")')} if table in tables else set()
                    if expected - actual:
                        missing[table] = sorted(expected - actual)
                from .db import SCHEMA_VERSION
                version_matches = integrity["user_version"] == SCHEMA_VERSION
                actual_guards = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
                missing_guards = sorted(_expected_schema()[1] - actual_guards)
                outdated = bool(missing or missing_guards or not version_matches)
                add("schema", "error" if outdated else "ok", "Требуется обновление или восстановление схемы" if outdated
                    else "Обязательные таблицы и столбцы присутствуют", missing_columns=missing,
                    missing_workflow_guards=missing_guards, schema_sha256=integrity["schema_sha256"],
                    user_version=integrity["user_version"], expected_version=SCHEMA_VERSION)
                if "publish_attempts" in tables and "publish_attempts" not in missing:
                    attempts = [{"id": row[0], "draft_id": row[1], "state": row[2]} for row in db.execute(
                        "SELECT id,draft_id,state FROM publish_attempts WHERE state IN ('started','unknown') ORDER BY id"
                    )]
                    add("unresolved_attempts", "warning" if attempts else "ok",
                        "Проверьте канал и разрешите незавершённые отправки через publish-resolve" if attempts
                        else "Незавершённых отправок нет", count=len(attempts), attempts=attempts)
                else:
                    add("unresolved_attempts", "error", "В схеме нет полноценного журнала попыток публикации")
        except (sqlite3.Error, OSError, ValueError) as exc:
            if not any(row["name"] == "database" for row in checks):
                add("database", "error", "Не удалось проверить базу без изменений", error_type=type(exc).__name__)
            if not any(row["name"] == "schema" for row in checks):
                add("schema", "error", "Схема недоступна для проверки")
            if not any(row["name"] == "unresolved_attempts" for row in checks):
                add("unresolved_attempts", "error", "Попытки публикации недоступны для проверки")

    for package, optional in (("PyYAML", False), ("sentence-transformers", True)):
        try:
            add("dependency_" + package, "ok", "Пакет установлен; импорт и загрузка модели не выполнялись",
                version=importlib.metadata.version(package))
        except importlib.metadata.PackageNotFoundError:
            add("dependency_" + package, "warning" if optional else "error", "Пакет не установлен", optional=optional)

    from .draft_generator import DEFAULT_MODEL
    model = os.environ.get("EVENT_MATCH_MODEL", DEFAULT_MODEL)
    try:
        cached = _model_cache_present(model)
        threshold = float(os.environ.get("EVENT_MATCH_THRESHOLD", "0.70"))
        if not math.isfinite(threshold) or not 0 <= threshold <= 1:
            raise ValueError("threshold")
        add("model", "ok" if cached else "warning", "Найдены локальные файлы модели; загрузка и качество не проверялись"
            if cached else "Локальные файлы модели не найдены; автоматическая загрузка не выполнялась", cache_found=cached)
    except (OSError, ValueError):
        add("model", "error", "Проверьте путь к модели и EVENT_MATCH_THRESHOLD (число от 0 до 1)")

    try:
        from .credentials import get_max_credentials
        token, channel = get_max_credentials()
        add("max_credentials", "ok" if token and channel else "warning", "Учётные данные присутствуют; доступ к MAX не проверялся"
            if token and channel else "Учётные данные MAX заданы не полностью", token_present=bool(token), channel_present=bool(channel))
    except Exception:
        add("max_credentials", "error", "Не удалось проверить локальное хранилище учётных данных MAX")
    status = "error" if any(row["status"] == "error" for row in checks) else (
        "warning" if any(row["status"] == "warning" for row in checks) else "ok")
    return {"status": status, "offline": True, "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "checks": checks}
