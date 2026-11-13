"""Нормализация дат, текста, ссылок и карточек материалов."""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from typing import Any


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    parser = _TextExtractor()
    try:
        parser.feed(value)
        plain = " ".join(parser.parts)
    except Exception:
        plain = value
    plain = unescape(plain)
    return re.sub(r"\s+", " ", plain).strip()


def normalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    if parts.scheme.lower() not in {"http", "https"} or not parts.netloc:
        raise ValueError(f"Ожидалась ссылка http(s): {url!r}")
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
             if not key.lower().startswith("utm_") and key.lower() not in {"fbclid", "gclid"}]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, urlencode(query), ""))


def normalize_datetime(value: str | datetime | None) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            parsed = parsedate_to_datetime(str(value))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


def normalize_item(raw: dict[str, Any]) -> dict[str, Any]:
    title = clean_text(str(raw.get("title", "")))
    summary = clean_text(str(raw.get("summary", raw.get("text", ""))))
    if not title:
        raise ValueError("У материала должен быть заголовок")
    url = normalize_url(str(raw.get("url", "")))
    published_at = normalize_datetime(raw.get("published_at"))
    fetched_at = normalize_datetime(raw.get("fetched_at") or datetime.now(timezone.utc))
    fingerprint_text = re.sub(r"\W+", " ", f"{title} {summary}".casefold()).strip()
    # A publisher may reuse the same headline and summary for different dated
    # reports. Include publication identity so the legacy UNIQUE(source_id,
    # content_hash) constraint does not collapse those distinct URLs. Existing
    # publications remain addressable by their canonical source_id + URL.
    hash_input = f"{raw.get('source_id', '')}\n{url}\n{fingerprint_text}"
    content_hash = hashlib.sha256(hash_input.encode("utf-8")).hexdigest()
    return {**raw, "title": title, "summary": summary, "url": url,
            "published_at": published_at, "fetched_at": fetched_at,
            "content_hash": content_hash}
