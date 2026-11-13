"""Загрузка конфигурации источников и путей приложения."""
from __future__ import annotations

import os
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

APP_DIR = Path(__file__).resolve().parents[1]
DEFAULT_SOURCES_FILE = APP_DIR.parent / "sources.yaml"
def runtime_data_dir() -> Path:
    """Resolve the optional runtime directory without creating or modifying it."""
    configured = os.environ.get("APP_DATA_DIR", "").strip()
    return Path(configured).expanduser().resolve() if configured else APP_DIR / "data"


DATA_DIR = runtime_data_dir()
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
    validate_sources(config)
    return config


def _text(value: Any, location: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{location}: требуется непустая строка")


def _url(value: Any, location: str) -> None:
    _text(value, location)
    parsed = urlsplit(value)
    try:
        valid = parsed.scheme in {"http", "https"} and parsed.hostname and parsed.port != 0
    except ValueError:
        valid = False
    if not valid or parsed.username or parsed.password or any(char.isspace() for char in value):
        raise ValueError(f"{location}: требуется HTTP(S)-адрес без учётных данных")


def _strings(value: Any, location: str, *, nonempty: bool = False) -> None:
    if not isinstance(value, list) or (nonempty and not value):
        raise ValueError(f"{location}: требуется список строк")
    for item in value:
        _text(item, location)


def _fields(value: dict[str, Any], allowed: set[str], location: str) -> None:
    if any(not isinstance(key, str) or key not in allowed for key in value):
        raise ValueError(f"{location}: неизвестное поле конфигурации (проверьте написание и версию схемы)")


def _date(value: Any, location: str) -> None:
    if isinstance(value, (date, datetime)):
        return
    if not isinstance(value, str):
        raise ValueError(f"{location}: требуется дата ISO-8601")
    try:
        datetime.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{location}: требуется дата ISO-8601") from None


def validate_sources(config: Any) -> None:
    """Validate every consumed source field before collection or DB changes.

    Version 1 rejects unknown fields and validates nested operational and
    editorial metadata. Disabled sources are validated before enabling them.
    """
    if not isinstance(config, dict) or not isinstance(config.get("sources"), list):
        raise ValueError("В sources.yaml должен быть объект со списком sources")
    _fields(config, {"version", "updated_at", "status", "source_policy", "sources"}, "sources.yaml")
    if "version" in config and (type(config["version"]) is not int or config["version"] != 1):
        raise ValueError("version: поддерживается версия схемы 1")
    if "status" in config:
        _text(config["status"], "status")
    if "updated_at" in config:
        _date(config["updated_at"], "updated_at")
    if "source_policy" in config:
        policy = config["source_policy"]
        if not isinstance(policy, dict):
            raise ValueError("source_policy: требуется объект")
        _fields(policy, {"allow", "exclude", "note"}, "source_policy")
        for field in ("allow", "exclude"):
            if field in policy:
                _strings(policy[field], f"source_policy.{field}")
        if "note" in policy:
            _text(policy["note"], "source_policy.note")
    ids: set[str] = set()
    for number, source in enumerate(config["sources"], 1):
        location = f"sources[{number}]"
        if not isinstance(source, dict):
            raise ValueError(f"{location}: требуется объект источника")
        _fields(source, {"id", "name", "url", "allowed_domains", "type", "level", "retrieval", "reuse",
                         "topics", "limitations", "verification_rule", "enabled", "relevance_filter", "include_keywords"}, location)
        source_id = source.get("id")
        if not isinstance(source_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", source_id):
            raise ValueError(f"{location}.id: требуется буквенно-цифровой идентификатор")
        if source_id in ids:
            raise ValueError(f"{location}.id: повторяющийся идентификатор")
        ids.add(source_id)
        location = f"sources.{source_id}"
        for field in ("name", "type", "level"):
            _text(source.get(field), f"{location}.{field}")
        if source["level"] not in {"primary", "independent_confirmation", "analysis_osint"}:
            raise ValueError(f"{location}.level: неизвестный уровень источника")
        _url(source.get("url"), f"{location}.url")
        if type(source.get("enabled")) is not bool:
            raise ValueError(f"{location}.enabled: требуется true или false")
        _strings(source.get("allowed_domains"), f"{location}.allowed_domains", nonempty=True)
        for domain in source["allowed_domains"]:
            if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", domain) or ".." in domain:
                raise ValueError(f"{location}.allowed_domains: требуется доменное имя без пути")
        retrieval = source.get("retrieval")
        if not isinstance(retrieval, dict) or retrieval.get("method") not in {"manual", "rss", "reliefweb_api"}:
            raise ValueError(f"{location}.retrieval.method: поддерживаются manual, rss, reliefweb_api")
        _fields(retrieval, {"method", "endpoint", "verified", "format", "checked_at", "notes", "appname_env", "query"},
                f"{location}.retrieval")
        if "verified" in retrieval and type(retrieval["verified"]) is not bool:
            raise ValueError(f"{location}.retrieval.verified: требуется true или false")
        if retrieval.get("method") != "manual":
            _url(retrieval.get("endpoint"), f"{location}.retrieval.endpoint")
        elif retrieval.get("endpoint") is not None:
            _url(retrieval["endpoint"], f"{location}.retrieval.endpoint")
        if source["enabled"] and retrieval["method"] != "manual" and not retrieval.get("verified", False):
            raise ValueError(f"{location}: автоматический источник должен иметь retrieval.verified: true")
        for field in ("notes", "format", "appname_env"):
            if field in retrieval:
                _text(retrieval[field], f"{location}.retrieval.{field}")
        if "appname_env" in retrieval and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", retrieval["appname_env"]):
            raise ValueError(f"{location}.retrieval.appname_env: требуется имя переменной окружения")
        if "checked_at" in retrieval:
            _date(retrieval["checked_at"], f"{location}.retrieval.checked_at")
        if "query" in retrieval:
            _validate_query(retrieval["query"], f"{location}.retrieval.query")
        for field in ("include_keywords", "topics", "limitations"):
            if field in source:
                _strings(source[field], f"{location}.{field}")
        if "verification_rule" in source:
            _text(source["verification_rule"], f"{location}.verification_rule")
        if "relevance_filter" in source:
            relevance = source["relevance_filter"]
            if not isinstance(relevance, dict):
                raise ValueError(f"{location}.relevance_filter: требуется объект")
            _fields(relevance, {"version", "matching", "policy", "high_confidence_terms", "geography_terms",
                               "conflict_terms", "frontline_action_terms", "context_terms"}, f"{location}.relevance_filter")
            for field in ("high_confidence_terms", "geography_terms", "conflict_terms", "frontline_action_terms", "context_terms"):
                if field in relevance:
                    _strings(relevance[field], f"{location}.relevance_filter.{field}")
            if "version" in relevance and (type(relevance["version"]) is not int or relevance["version"] < 1):
                raise ValueError(f"{location}.relevance_filter.version: требуется положительное целое")
            for field in ("matching", "policy"):
                if field in relevance:
                    _text(relevance[field], f"{location}.relevance_filter.{field}")
        if "reuse" in source:
            if not isinstance(source["reuse"], dict):
                raise ValueError(f"{location}.reuse: требуется объект")
            _fields(source["reuse"], {"license", "url", "attribution", "exceptions"}, f"{location}.reuse")
            for field, value in source["reuse"].items():
                (_url if field == "url" else _text)(value, f"{location}.reuse.{field}")


def _validate_query(query: Any, location: str) -> None:
    if not isinstance(query, dict):
        raise ValueError(f"{location}: требуется объект")
    _fields(query, {"preset", "limit", "fields", "filter"}, location)
    if "limit" in query and (type(query["limit"]) is not int or not 1 <= query["limit"] <= 1000):
        raise ValueError(f"{location}.limit: требуется целое от 1 до 1000")
    if "preset" in query:
        _text(query["preset"], f"{location}.preset")
    if "fields" in query:
        if not isinstance(query["fields"], dict):
            raise ValueError(f"{location}.fields: требуется объект")
        _fields(query["fields"], {"include", "exclude"}, f"{location}.fields")
        for field, value in query["fields"].items():
            _strings(value, f"{location}.fields.{field}")
    if "filter" in query:
        def check_filter(value: Any, here: str, depth: int = 0) -> None:
            if depth > 10 or not isinstance(value, dict):
                raise ValueError(f"{here}: требуется фильтр глубиной до 10")
            _fields(value, {"operator", "conditions", "field", "value", "negate"}, here)
            if "negate" in value and type(value["negate"]) is not bool:
                raise ValueError(f"{here}.negate: требуется true или false")
            if "conditions" in value:
                if value.get("operator") not in {"AND", "OR"} or not isinstance(value["conditions"], list):
                    raise ValueError(f"{here}: нужны operator AND/OR и список conditions")
                for condition in value["conditions"]:
                    check_filter(condition, f"{here}.conditions", depth + 1)
            else:
                _text(value.get("field"), f"{here}.field")
                if "value" not in value or not isinstance(value["value"], (str, int, float, list, dict)):
                    raise ValueError(f"{here}.value: требуется значение фильтра")
        check_filter(query["filter"], f"{location}.filter")


def get_source(config: dict[str, Any], source_id: str) -> dict[str, Any]:
    for source in config["sources"]:
        if source.get("id") == source_id:
            return source
    raise ValueError(f"Источник с id {source_id!r} не найден в sources.yaml")
