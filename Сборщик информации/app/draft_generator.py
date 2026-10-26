"""Формирование черновиков с локальной семантической группировкой событий."""
from __future__ import annotations

import os
import json
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

from .logs import now_iso, record_event

MOSCOW = timezone(timedelta(hours=3))
DEFAULT_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
SOURCE_SUMMARY_LIMIT = 520
SOURCE_SUMMARY_SENTENCE_LIMIT = 2
SENSITIVE_SUMMARY_RE = re.compile(
    r"\b(?:gps|mgrs|coordinates?|latitude|longitude|grid reference|exact location)\b"
    r"|(?:координат\w*|широт\w*|долгот\w*|точн\w+ местоположен\w*|квадрат\w+ сет\w*)"
    r"|[+-]?\d{1,2}\.\d{4,}\s*[,;/]\s*[+-]?\d{1,3}\.\d{4,}"
    r"|\b(?:unit|troop|force|battalion|brigade|platoon)\s+"
    r"(?:positions?|deployment|location|routes?|movements?)\b"
    r"|(?:позици\w*|расположен\w*|дислокац\w*|маршрут\w*|перемещен\w*)"
    r".{0,45}(?:подразделен\w*|войск\w*|батальон\w*|бригад\w*|военн\w*)"
    r"|(?:подразделен\w*|войск\w*|батальон\w*|бригад\w*|военн\w*)"
    r".{0,45}(?:позици\w*|расположен\w*|дислокац\w*|маршрут\w*|перемещен\w*)"
    r"|\b(?:route|movement) of (?:the )?(?:troops|forces|units)\b",
    re.IGNORECASE | re.UNICODE,
)
MODEL_FALLBACK_NOTICE = (
    "Семантическая группировка не выполнена: локальная модель недоступна. "
    "Материалы оставлены отдельными; редактору нужно проверить возможные повторы вручную."
)
LOW_SIMILARITY_MARGIN = 0.02
LOCATION_PATTERNS = {
    "donetsk": r"donetsk\w*|донецк\w*",
    "donbas": r"donbas\w*|donbass\w*|донбас\w*|донбасс\w*",
    "luhansk": r"luhansk\w*|lugansk\w*|луганск\w*",
    "crimea": r"crimea\w*|крым\w*",
    "avdiivka": r"avdiivka\w*|avdeevka\w*|авдеевк\w*",
    "bakhmut": r"bakhmut\w*|artemivsk\w*|артемовск\w*|артёмовск\w*|бахмут\w*",
    "pokrovsk": r"pokrovsk\w*|покровск\w*",
    "kupiansk": r"kupiansk\w*|kupyansk\w*|купянск\w*",
    "kharkiv": r"kharkiv\w*|kharkov\w*|харьков\w*",
    "zaporizhzhia": r"zaporizhzhia\w*|zaporozhye\w*|запорожь\w*",
    "kherson": r"kherson\w*|херсон\w*",
    "kursk": r"kursk\w*|курск\w*",
    "belgorod": r"belgorod\w*|белгород\w*",
    "mariupol": r"mariupol\w*|мариупол\w*",
    "odesa": r"odesa\w*|odessa\w*|одесс\w*",
    "vuhledar": r"vuhledar\w*|ugledar\w*|угледар\w*",
}
COMPILED_LOCATION_PATTERNS = {
    key: re.compile(rf"(?<!\w)(?:{pattern})(?!\w)", re.IGNORECASE | re.UNICODE)
    for key, pattern in LOCATION_PATTERNS.items()
}


def _location_keys(row: sqlite3.Row) -> set[str]:
    searchable = f"{row['title']} {row['summary']}"
    return {key for key, pattern in COMPILED_LOCATION_PATTERNS.items() if pattern.search(searchable)}


def _cluster_rows(rows: list[sqlite3.Row], similarity, threshold: float) -> list[list[sqlite3.Row]]:
    """Группирует при сходстве каждой пары, согласованной известной географии и датам."""
    groups: list[list[int]] = []
    locations = [_location_keys(row) for row in rows]
    for index, row in enumerate(rows):
        match = None
        for group in groups:
            compatible = True
            for member_index in group:
                member = rows[member_index]
                if row["source_level"] == "analysis_osint" or member["source_level"] == "analysis_osint":
                    compatible = False
                    break
                left_date = datetime.fromisoformat(row["published_at"])
                right_date = datetime.fromisoformat(member["published_at"])
                if abs((left_date - right_date).total_seconds()) > 96 * 3600:
                    compatible = False
                    break
                left_locations, right_locations = locations[index], locations[member_index]
                # A pair of explicit, different settlements is a hard event
                # boundary even when the embedding sees similar wording.
                if left_locations and right_locations and not left_locations.intersection(right_locations):
                    compatible = False
                    break
                score = similarity(index, member_index)
                if score < threshold:
                    # Recover borderline multilingual paraphrases only when
                    # both texts identify the same known settlement. Without
                    # this anchor, leave the reports separate for the editor.
                    if (score < threshold - LOW_SIMILARITY_MARGIN
                            or not left_locations.intersection(right_locations)):
                        compatible = False
                        break
            if compatible:
                match = group
                break
        if match is None:
            groups.append([index])
        else:
            match.append(index)
    return [[rows[index] for index in group] for group in groups]


def _event_groups(rows: list[sqlite3.Row]) -> list[list[sqlite3.Row]]:
    return _event_groups_with_status(rows)[0]


def _event_groups_with_status(
    rows: list[sqlite3.Row],
) -> tuple[list[list[sqlite3.Row]], str | None]:
    if len(rows) < 2:
        return [[row] for row in rows], None
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError as exc:
        return [[row] for row in rows], f"sentence-transformers не установлен: {exc}"
    model_name = os.environ.get("EVENT_MATCH_MODEL", DEFAULT_MODEL)
    try:
        model = SentenceTransformer(model_name, device="cpu")
        texts = [f"{row['title']}\n{row['summary'][:1200]}" for row in rows]
        vectors = model.encode(texts, normalize_embeddings=True, convert_to_numpy=True, show_progress_bar=False)
    except Exception as exc:
        return [[row] for row in rows], f"не удалось загрузить {model_name!r}: {type(exc).__name__}: {exc}"

    try:
        threshold = float(os.environ.get("EVENT_MATCH_THRESHOLD", "0.70"))
        if not 0.0 <= threshold <= 1.0:
            raise ValueError("порог должен находиться в диапазоне от 0 до 1")
    except ValueError as exc:
        return [[row] for row in rows], f"некорректный EVENT_MATCH_THRESHOLD: {exc}"
    def cosine_similarity(left: int, right: int) -> float:
        return float(vectors[left] @ vectors[right])

    return _cluster_rows(rows, cosine_similarity, threshold), None


def _published_label(value: str) -> str:
    parsed = datetime.fromisoformat(value)
    return parsed.astimezone(MOSCOW).strftime("%d.%m.%Y, %H:%M МСК")


def _level(row: sqlite3.Row) -> str:
    level = row["source_level"]
    if level:
        return level
    if row["source_type"] in {"official", "official_foreign", "official_uk"}:
        return "primary"
    if row["source_type"] == "analytical_project":
        return "analysis_osint"
    return "independent_confirmation"


def _source_summary_excerpt(row: sqlite3.Row) -> str:
    summary = " ".join(str(row["summary"] or "").split())
    title = " ".join(str(row["title"] or "").split())
    if not summary or summary.rstrip(". ").casefold() == title.rstrip(". ").casefold():
        return ""
    # Do not place obvious tactical coordinates or unit/route details in a
    # publication candidate. The editor still gets a warning to inspect the
    # omitted source description manually.
    if SENSITIVE_SUMMARY_RE.search(summary):
        return ""
    title_prefix = re.compile(rf"^{re.escape(title)}\s*[:—–-]\s*", re.IGNORECASE)
    summary = title_prefix.sub("", summary, count=1)
    sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+", summary) if part.strip()]
    selected: list[str] = []
    for sentence in sentences:
        if sentence.rstrip(". ").casefold() == title.rstrip(". ").casefold():
            continue
        proposed = " ".join([*selected, sentence])
        if len(proposed) <= SOURCE_SUMMARY_LIMIT:
            selected.append(sentence)
            if len(selected) >= SOURCE_SUMMARY_SENTENCE_LIMIT:
                break
        elif not selected:
            boundary = sentence.rfind(" ", 0, SOURCE_SUMMARY_LIMIT)
            selected.append(sentence[:boundary if boundary > 0 else SOURCE_SUMMARY_LIMIT].rstrip() + "…")
            break
        else:
            break
    return " ".join(selected)


def _summary_has_sensitive_detail(row: sqlite3.Row) -> bool:
    return bool(SENSITIVE_SUMMARY_RE.search(str(row["summary"] or "")))


def _render_group(group: list[sqlite3.Row]) -> str:
    ordered = sorted(group, key=lambda row: {"primary": 0, "independent_confirmation": 1,
                                              "analysis_osint": 2}.get(_level(row), 3))
    lines: list[str] = []
    for row in ordered:
        level = _level(row)
        status = "ОЦЕНКА АНАЛИТИКА" if level == "analysis_osint" else "ЗАЯВЛЕНИЕ ИСТОЧНИКА"
        attribution = f"{row['source_name']} оценивает" if level == "analysis_osint" else f"{row['source_name']} сообщает"
        try:
            payload = json.loads(row["payload"] or "{}")
            check = payload.get("publication_check", {})
        except (TypeError, json.JSONDecodeError):
            check = {}
        if check.get("status") == "http_ok_feed":
            validation = f"запись есть в RSS/Atom, ссылка отвечает HTTP {check.get('http_status', '2xx')}"
        elif check.get("status") == "http_ok":
            validation = f"ссылка отвечает HTTP {check.get('http_status', '2xx')}"
        elif check.get("status") == "manual_check_required":
            validation = "ссылку нужно проверить вручную"
        else:
            validation = "проверка ссылки не сохранена"
        claim = row["title"].rstrip(". ")
        summary_excerpt = _source_summary_excerpt(row)
        published = _published_label(row["published_at"])
        # Render every source assertion as its own post bullet. Candidate-group
        # membership is kept in structured records for editor review, never
        # rendered as a shared factual claim with pooled citations.
        lines.append(f"• **{status}** — {attribution}: {claim}.")
        if summary_excerpt:
            lines.append(f"— Выжимка из описания {row['source_name']} (до двух предложений): {summary_excerpt}")
        elif _summary_has_sensitive_detail(row):
            lines.append("— Описание источника скрыто: проверьте его вручную и не переносите координаты или оперативные детали.")
        if row["same_url_other_source"]:
            lines.append("— Эта же ссылка поступила через другой источник; она не считается независимым подтверждением.")
        lines.append("— Дата события не проверена редактором; требуется уточнить до одобрения.")
        lines.append(f"— {row['source_name']}, опубликовано {published}; {validation}: {row['url']}")
    return "\n".join(lines)


MAX_POST_CHARS = 4000
OLDER_NOTE = "Перенесённые материалы и публикации старше суток — отдельный блок. Дата публикации не равна дате события: укажите дату события или исключите пункт."


def _draft_header(title: str, window_label: str) -> str:
    return "\n\n".join((title, f"Окно отбора: публикации за последние {window_label}. Это дата публикации, а не обязательно дата события. Если событие произошло раньше, редактор должен указать его дату или исключить пункт."))


def _draft_footer() -> str:
    return "Статусы и группировка — автоматические подсказки. Перед одобрением редактор обязан проверить, что содержание страницы соответствует заголовку, проверить основания и независимость подтверждений. HEAD-проверка подтверждает только ответ сервера, а не достоверность фактов. Неподтверждённые сообщения не публиковать."


def _pack_groups(groups: list[list[sqlite3.Row]], title: str, window_label: str,
                 extra_note: str = "") -> list[list[list[sqlite3.Row]]]:
    """Pack whole event groups into MAX-sized drafts; split oversized groups by claim."""
    # Reserve room for the longest part marker and the optional older-items note.
    header = _draft_header(f"{title} — часть 999/999", window_label)
    footer = _draft_footer()
    note_reserve = len(extra_note) + (2 if extra_note else 0)
    available = MAX_POST_CHARS - len(header) - len(footer) - len(OLDER_NOTE) - note_reserve - 6
    if available <= 0:
        raise ValueError("Служебный текст сводки сам превышает лимит MAX")

    blocks: list[list[sqlite3.Row]] = []
    for group in groups:
        if len(_render_group(group)) <= available:
            blocks.append(group)
        else:
            # Keep each claim and its citation together. If a candidate group
            # itself is too large, place its claims in separate drafts/groups.
            for row in group:
                claim = [row]
                if len(_render_group(claim)) > available:
                    raise ValueError(
                        f"Одно утверждение слишком велико для лимита MAX: item #{row['id']}. "
                        "Сократите или исключите его при редактуре."
                    )
                blocks.append(claim)

    parts: list[list[list[sqlite3.Row]]] = []
    current: list[list[sqlite3.Row]] = []
    # Reserve the older-material note even for parts that may not need it.
    current_length = len(header) + len(footer) + len(OLDER_NOTE) + note_reserve + 6
    for block in blocks:
        block_length = len(_render_group(block)) + 2
        if current and current_length + block_length > MAX_POST_CHARS:
            parts.append(current)
            current = []
            current_length = len(header) + len(footer) + len(OLDER_NOTE) + note_reserve + 6
        current.append(block)
        current_length += block_length
    if current:
        parts.append(current)
    return parts


def create_daily_draft(db: sqlite3.Connection, hours: int = 24) -> int:
    if hours < 1 or hours > 96:
        raise ValueError("Период черновика должен быть от 1 до 96 часов")
    now = datetime.now(timezone.utc)
    local_now = now.astimezone(MOSCOW)
    cutoff = (now - timedelta(hours=hours)).isoformat(timespec="seconds")
    current = now.isoformat(timespec="seconds")
    rows = list(db.execute(
        """SELECT i.id,i.source_id,i.source_name,i.source_type,i.source_level,
                  v.title,v.summary,v.url,v.published_at,v.fetched_at,v.content_hash,
                  v.payload,v.id AS version_id,v.version_no,
                  EXISTS(SELECT 1 FROM items other WHERE other.url=v.url
                         AND other.source_id<>i.source_id) AS same_url_other_source,
                  EXISTS(SELECT 1 FROM draft_claims oldc JOIN drafts deferred_d ON deferred_d.id=oldc.draft_id
                         JOIN digest_batch_parts bp ON bp.draft_id=oldc.draft_id WHERE oldc.version_id=v.id
                         AND bp.disposition='deferred' AND bp.deferred_until<=?
                         AND NOT EXISTS (SELECT 1 FROM draft_claims laterc JOIN drafts laterd
                                         ON laterd.id=laterc.draft_id WHERE laterc.version_id=v.id
                                         AND laterd.id>deferred_d.id)) AS deferred
           FROM items i JOIN item_versions v ON v.item_id=i.id
           WHERE v.version_no=(SELECT MAX(v2.version_no) FROM item_versions v2 WHERE v2.item_id=i.id)
             AND v.published_at IS NOT NULL AND v.published_at >= ? AND v.published_at <= ?
             AND NOT EXISTS (SELECT 1 FROM relevance_assessments ra WHERE ra.version_id=v.id
                             AND ra.status IN ('review','excluded'))
             AND (
               NOT EXISTS (SELECT 1 FROM draft_claims c JOIN drafts d ON d.id=c.draft_id
                           WHERE c.version_id=v.id AND c.included=1
                             AND d.status IN ('pending','approved','published','rejected'))
               OR EXISTS (
                   SELECT 1 FROM draft_claims oldc
                   JOIN drafts deferred_d ON deferred_d.id=oldc.draft_id
                   JOIN digest_batch_parts bp ON bp.draft_id=oldc.draft_id
                   WHERE oldc.version_id=v.id AND bp.disposition='deferred'
                     AND bp.deferred_until<=?
                     AND NOT EXISTS (SELECT 1 FROM draft_claims laterc JOIN drafts laterd
                                     ON laterd.id=laterc.draft_id WHERE laterc.version_id=v.id
                                     AND laterd.id>deferred_d.id)
               )
             )
             AND (v.published_at >= ? OR EXISTS (
                   SELECT 1 FROM draft_claims oldc
                   JOIN drafts deferred_d ON deferred_d.id=oldc.draft_id
                   JOIN digest_batch_parts bp ON bp.draft_id=oldc.draft_id
                   WHERE oldc.version_id=v.id AND bp.disposition='deferred'
                     AND bp.deferred_until<=?
                     AND NOT EXISTS (SELECT 1 FROM draft_claims laterc JOIN drafts laterd
                                     ON laterd.id=laterc.draft_id WHERE laterc.version_id=v.id
                                     AND laterd.id>deferred_d.id)
             ))
             AND v.published_at >= ?
             AND NOT EXISTS (SELECT 1 FROM revision_alerts a JOIN drafts d ON d.id=a.draft_id
                             WHERE a.current_version_id=v.id AND a.resolution IS NULL
                               AND d.status IN ('pending','approved'))
             AND NOT EXISTS (SELECT 1 FROM revision_alerts a
                             WHERE a.current_version_id=v.id AND a.resolution='kept')
           ORDER BY v.published_at""", (local_now.date().isoformat(),
                                            (now - timedelta(hours=96)).isoformat(timespec="seconds"),
                                            current, local_now.date().isoformat(), cutoff,
                                            local_now.date().isoformat(),
                                            (now - timedelta(hours=96)).isoformat(timespec="seconds"))))
    if not rows:
        held = int(db.execute(
            """SELECT COUNT(*) FROM relevance_assessments a
               JOIN item_versions v ON v.id=a.version_id
               WHERE a.status='review' AND v.published_at>=? AND v.published_at<=?""",
            (cutoff, current),
        ).fetchone()[0])
        if held:
            raise ValueError(
                f"Нет материалов, принятых для черновика; {held} кандидат(ов) ожидают "
                "редакторской проверки командой review relevance"
            )
        raise ValueError("Нет материалов с датой публикации в выбранном периоде")
    window_label = "24 часа" if hours == 24 else ("4 дня" if hours == 96 else f"{hours} ч")
    title = f"Сводка за последние {window_label} — проверено {local_now.strftime('%d.%m.%Y в %H:%M МСК')}"
    groups, grouping_warning = _event_groups_with_status(rows)
    older_cutoff = now - timedelta(hours=24)
    current_groups: list[list[sqlite3.Row]] = []
    older_groups: list[list[sqlite3.Row]] = []
    for group in groups:
        group_dates = [datetime.fromisoformat(row["published_at"]).astimezone(timezone.utc) for row in group]
        if any(row["deferred"] for row in group) or (
                hours > 24 and all(published < older_cutoff for published in group_dates)):
            older_groups.append(group)
        else:
            current_groups.append(group)
    display_groups: list[list[sqlite3.Row]] = list(current_groups)
    if older_groups:
        display_groups.extend(older_groups)
    extra_note = MODEL_FALLBACK_NOTICE if grouping_warning else ""
    parts = _pack_groups(display_groups, title, window_label, extra_note)
    created = now_iso()
    batch_id = uuid.uuid4().hex
    batch_date = local_now.date().isoformat()
    db.execute("INSERT INTO digest_batches(id,created_at,batch_date) VALUES(?,?,?)",
               (batch_id, created, batch_date))
    draft_ids: list[int] = []
    for part_no, part_groups in enumerate(parts, start=1):
        part_title = title if len(parts) == 1 else f"{title} — часть {part_no}/{len(parts)}"
        body_parts = [_draft_header(part_title, window_label)]
        if extra_note:
            body_parts.append(extra_note)
        older_heading_added = False
        for group in part_groups:
            group_is_older = any(row["deferred"] for row in group) or all(
                datetime.fromisoformat(row["published_at"]).astimezone(timezone.utc) < older_cutoff
                for row in group
            )
            if group_is_older and not older_heading_added:
                body_parts.append(OLDER_NOTE)
                older_heading_added = True
            body_parts.append(_render_group(group))
        body_parts.append(_draft_footer())
        body = "\n\n".join(body_parts)
        if len(body) > MAX_POST_CHARS:
            raise ValueError(f"Внутренняя ошибка разбиения: часть {part_no} превышает лимит MAX ({len(body)} символов)")
        part_rows = [row for group in part_groups for row in group]
        requires_group_review = int(bool(grouping_warning) or any(len(group) > 1 for group in part_groups))
        cursor = db.execute(
            """INSERT INTO drafts(title,body,status,created_at,updated_at,requires_group_review)
               VALUES(?,?,'pending',?,?,?)""",
            (part_title, body, created, created, requires_group_review),
        )
        draft_id = int(cursor.lastrowid)
        draft_ids.append(draft_id)
        db.execute(
            """INSERT INTO digest_batch_parts(draft_id,batch_id,part_no,updated_at)
               VALUES(?,?,?,?)""", (draft_id, batch_id, part_no, created)
        )
        db.executemany("INSERT INTO draft_items(draft_id,item_id) VALUES(?,?)",
                       [(draft_id, int(row["id"])) for row in part_rows])
        for group_no, group in enumerate(part_groups, start=1):
            group_cursor = db.execute(
                "INSERT INTO draft_groups(draft_id,group_no) VALUES(?,?)", (draft_id, group_no)
            )
            group_id = int(group_cursor.lastrowid)
            for row in group:
                try:
                    payload = json.loads(row["payload"] or "{}")
                    publication_check = payload.get("publication_check", {})
                except (TypeError, json.JSONDecodeError):
                    publication_check = {}
                db.execute(
                    """INSERT INTO draft_claims(
                        draft_id,group_id,item_id,version_id,claim_text,source_id,source_name,source_level,
                        published_at,source_url,same_url_other_source,publication_check_status,publication_checked_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (draft_id, group_id, int(row["id"]), int(row["version_id"]), row["title"].rstrip(". "), row["source_id"],
                     row["source_name"], _level(row), row["published_at"], row["url"],
                     int(row["same_url_other_source"]),
                     publication_check.get("status", ""), publication_check.get("checked_at")),
                )
    db.commit()
    for part_index, (draft_id, part_groups) in enumerate(zip(draft_ids, parts), start=1):
        record_event(db, "draft_created", "draft", draft_id,
                     {"items": sum(len(group) for group in part_groups), "events": len(part_groups),
                      "hours": hours, "semantic_model": os.environ.get("EVENT_MATCH_MODEL", DEFAULT_MODEL),
                       "grouping_mode": ("not_needed" if len(rows) < 2 else
                                         ("individual_fallback" if grouping_warning else "semantic_model")),
                       "grouping_warning": grouping_warning,
                      "part": part_index, "parts": len(draft_ids), "part_ids": draft_ids,
                      "batch_id": batch_id})
    return draft_ids[0]
