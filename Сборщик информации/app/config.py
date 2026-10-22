"""Загрузка конфигурации источников и путей приложения."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

APP_DIR = Path(__file__).resolve().parents[1]
DEFAULT_SOURCES_FILE = APP_DIR.parent / "sources.yaml"
DATA_DIR = APP_DIR / "data"
DB_PATH = DATA_DIR / "app.sqlite3"


def sources_path() -> Path:
    configured = os.environ.get("SOURCES_FILE")
    if configured:
        path = Path(configured).expanduser()
        return (Path.cwd() / path).resolve() if not path.is_absolute() else path
    return DEFAULT_SOURCES_FILE


def load_sources(path: Path | None = None) -> dict[str, Any]:
    file_path = path or sources_path()
    if not file_path.is_file():
        raise FileNotFoundError(f"Файл источников не найден: {file_path}")
    with file_path.open("r", encoding="utf-8-sig") as stream:
        config = yaml.safe_load(stream) or {}
    if not isinstance(config.get("sources"), list):
        raise ValueError("В sources.yaml должен быть список sources")
    ids: set[str] = set()
    for source in config["sources"]:
        source_id = source.get("id")
        if not source_id or source_id in ids:
            raise ValueError(f"Пустой или повторяющийся id источника: {source_id!r}")
        ids.add(source_id)
    return config


def get_source(config: dict[str, Any], source_id: str) -> dict[str, Any]:
    for source in config["sources"]:
        if source.get("id") == source_id:
            return source
    raise ValueError(f"Источник с id {source_id!r} не найден в sources.yaml")
