"""Проверка происхождения и доступности публикационных ссылок без загрузки текста."""
from __future__ import annotations

import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlsplit

from .logs import now_iso

USER_AGENT = "SborshchikInformatsii/0.2 (link availability check)"


def _allowed_domains(source: dict[str, Any]) -> list[str]:
    domains = [str(domain).lower().lstrip(".") for domain in source.get("allowed_domains", [])]
    if domains:
        return domains
    host = (urlsplit(source["url"]).hostname or "").lower().removeprefix("www.")
    return [host] if host else []


def _host_allowed(url: str, domains: list[str]) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return any(host == domain or host.endswith("." + domain) for domain in domains)


def check_publication_link(source: dict[str, Any], url: str, origin: str) -> dict[str, str]:
    domains = _allowed_domains(source)
    if not _host_allowed(url, domains):
        raise ValueError(f"Домен ссылки не входит в разрешённые домены источника {source['id']!r}")
    feed_record = origin == "rss"
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT}, method="HEAD")
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            final_url = response.geturl()
            if not _host_allowed(final_url, domains):
                raise ValueError("Ссылка перенаправляет за пределы разрешённого домена источника")
            return {"status": "http_ok_feed" if feed_record else "http_ok", "checked_at": now_iso(),
                    "http_status": str(response.status), "final_url": final_url,
                    "note": "Запись получена из ленты; ссылка отвечает. Текст страницы не загружался." if feed_record
                    else "Ссылка отвечает; текст страницы не загружался."}
    except urllib.error.HTTPError as exc:
        final_url = exc.geturl()
        if final_url and not _host_allowed(final_url, domains):
            raise ValueError("Ссылка перенаправляет за пределы разрешённого домена источника") from exc
        if exc.code in {404, 410}:
            raise ValueError(f"Публикация по ссылке не найдена: HTTP {exc.code}") from exc
        if exc.code in {403, 405, 429}:
            return {"status": "manual_check_required", "checked_at": now_iso(),
                    "http_status": str(exc.code),
                    "note": ("Запись есть в RSS/Atom-ленте, но сайт ограничил HEAD-запрос; редактору нужно открыть ссылку вручную."
                             if feed_record else "Сайт ограничил проверку HEAD-запросом; редактору нужно открыть ссылку вручную.")}
        return {"status": "manual_check_required", "checked_at": now_iso(),
                "http_status": str(exc.code),
                "note": "Не удалось подтвердить доступность публикации автоматически."}
    except (urllib.error.URLError, TimeoutError) as exc:
        return {"status": "manual_check_required", "checked_at": now_iso(),
                "note": f"Сетевая проверка не завершилась ({type(exc).__name__}); редактору нужно проверить ссылку."}
