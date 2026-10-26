"""Командная строка приложения."""
from __future__ import annotations

import argparse
import getpass
import json
import sys
import webbrowser
from datetime import datetime, timedelta, timezone

from .config import APP_DIR, get_source, load_sources, sources_path
from .credentials import get_max_credentials, remove_max_credentials, save_max_credentials
from .corrections import create_correction_draft, mark_correction_evidence_checked
from .db import connect
from .draft_generator import create_daily_draft
from .fetcher import fetch_enabled_sources, ingest_manual
from .logs import recent_events, record_event
from .publisher import publish as publish_draft, publish_scheduled, resolve_attempt
from .publication_validator import validate_publication_url
from .schedule import (MOSCOW_WINDOWS_TIMEZONE, install_schedule, local_timezone_id,
                       TRIGGER_END_BOUNDARY, notify_editor, remove_schedule, schedule_status)
from .relevance import list_relevance, decide_relevance, relevance_report
from .review_queue import (claim_link_check_is_current, draft_claims, draft_groups,
                           edit_draft, exclude_claim, get_draft,
                           list_drafts, list_batches, batch_parts, select_batch_part,
                           close_batch_part, defer_batch_part, mark_claim_link_checked, review_candidate_group,
                           review_claim_event_date, revision_alerts, review_source_revision,
                           set_decision)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="Сборщик информации", description="Сбор материалов и редакторская очередь для MAX")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="создать базу и проверить путь к sources.yaml")
    schedule = commands.add_parser("schedule", help="управление ежедневными задачами Планировщика Windows")
    schedule_commands = schedule.add_subparsers(dest="schedule_command", required=True)
    schedule_commands.add_parser("install", help="зарегистрировать сбор на 12:00 и публикацию на 14:00 МСК")
    schedule_commands.add_parser("status", help="показать состояние задач и время следующего запуска")
    schedule_commands.add_parser("remove", help="удалить зарегистрированные приложением задачи")
    credentials = commands.add_parser("credentials", help="безопасная настройка учётных данных MAX")
    credential_commands = credentials.add_subparsers(dest="credentials_command", required=True)
    credential_commands.add_parser("status", help="показать, настроены ли учётные данные, не раскрывая их")
    credential_commands.add_parser("set-max", help="ввести токен и ID канала в Диспетчер учётных данных Windows")
    credential_commands.add_parser("remove-max", help="удалить сохранённые в Windows учётные данные MAX")
    commands.add_parser("fetch", help="получить новые материалы из включённых RSS/API-источников")
    run = commands.add_parser("run", help="собрать материалы и создать черновик без публикации")
    run.add_argument("--hours", type=int, default=24, help="окно публикаций, по умолчанию 24 часа; максимум 96")
    run.add_argument("--notify", action="store_true", help="уведомить редактора после планового сбора")
    ingest = commands.add_parser("ingest", help="добавить материал, проверенный вручную")
    ingest.add_argument("--source", required=True, help="id источника из sources.yaml")
    ingest.add_argument("--title", required=True)
    ingest.add_argument("--url", required=True)
    ingest.add_argument("--published-at", required=True, help="дата публикации ISO-8601 с часовым поясом")
    ingest.add_argument("--summary", default="", help="краткий пересказ своими словами")
    draft = commands.add_parser("draft", help="создать черновик из новых материалов")
    draft.add_argument("--hours", type=int, default=24, help="окно публикаций, по умолчанию 24 часа; максимум 96")
    review = commands.add_parser("review", help="очередь редакторской проверки")
    review_commands = review.add_subparsers(dest="review_command", required=True)
    listing = review_commands.add_parser("list", help="показать черновики")
    listing.add_argument("--status", choices=["pending", "approved", "rejected", "published"])
    review_commands.add_parser("batches", help="показать партии сводок и выбранные части")
    batch_show = review_commands.add_parser("batch-show", help="показать части партии и даты материалов")
    batch_show.add_argument("batch_id")
    select_part = review_commands.add_parser("select-part", help="выбрать ровно одну часть партии для дневной публикации")
    select_part.add_argument("batch_id")
    select_part.add_argument("draft_id", type=int)
    close_part = review_commands.add_parser("close-part", help="закрыть невыбранную часть партии")
    close_part.add_argument("batch_id")
    close_part.add_argument("draft_id", type=int)
    close_part.add_argument("--reason", required=True)
    defer_part = review_commands.add_parser("defer-part", help="отложить часть до даты в пределах срока актуальности")
    defer_part.add_argument("batch_id")
    defer_part.add_argument("draft_id", type=int)
    defer_part.add_argument("--until", required=True, help="дата ГГГГ-ММ-ДД")
    defer_part.add_argument("--note", required=True, help="основание переноса")
    relevance_list = review_commands.add_parser(
        "relevance", help="просмотреть тематические совпадения и спорные материалы RSS/API"
    )
    relevance_list.add_argument("--status", choices=["review", "excluded", "included", "all"], default="review")
    relevance_list.add_argument("--limit", type=int, default=100)
    relevance_check = review_commands.add_parser(
        "relevance-check", help="принять или отклонить кандидата релевантности и сохранить причину"
    )
    relevance_check.add_argument("id", type=int, help="ID оценки из review relevance")
    relevance_choice = relevance_check.add_mutually_exclusive_group(required=True)
    relevance_choice.add_argument("--include", action="store_true", help="добавить в материалы для черновиков")
    relevance_choice.add_argument("--exclude", action="store_true", help="оставить в истории, исключить из черновиков")
    relevance_check.add_argument("--note", required=True, help="почему материал принят или отклонён")
    filter_report = review_commands.add_parser(
        "filter-report", help="сводка охвата сборщика и редакторских исправлений фильтра"
    )
    filter_report.add_argument("--days", type=int, default=7)
    show = review_commands.add_parser("show", help="показать полный текст черновика")
    show.add_argument("id", type=int)
    revision_check = review_commands.add_parser(
        "revision-check", help="проверить новую версию источника и сохранить решение редактора"
    )
    revision_check.add_argument("id", type=int, help="id черновика")
    revision_check.add_argument("claim_id", type=int, help="id утверждения из review show")
    revision_mode = revision_check.add_mutually_exclusive_group(required=True)
    revision_mode.add_argument("--keep-current", action="store_true",
                               help="новая версия проверена; прежняя формулировка черновика остаётся верной")
    revision_mode.add_argument("--refresh", action="store_true",
                               help="обновить утверждение до последней версии и повторить проверки")
    revision_check.add_argument("--note", required=True, help="что проверил редактор и почему выбрано это решение")
    claim_check = review_commands.add_parser(
        "claim-check", help="отметить проверку ссылки и её связи с конкретным утверждением"
    )
    claim_check.add_argument("id", type=int, help="id черновика")
    claim_check.add_argument("claim_id", type=int, help="id утверждения из review show")
    claim_check.add_argument("--note", required=True,
                             help="кратко указать, что на странице подтверждает утверждение")
    correction_check = review_commands.add_parser(
        "correction-check", help="проверить ссылку и основание отдельного черновика исправления"
    )
    correction_check.add_argument("id", type=int, help="id черновика исправления")
    correction_check.add_argument("--note", required=True, help="что именно перепроверено")
    event_date = review_commands.add_parser(
        "event-date", help="указать дату события или редакторски подтвердить её статус"
    )
    event_date.add_argument("id", type=int, help="id черновика")
    event_date.add_argument("claim_id", type=int, help="id утверждения из review show")
    event_date_choice = event_date.add_mutually_exclusive_group(required=True)
    event_date_choice.add_argument("--date", dest="event_date", help="дата события в формате ГГГГ-ММ-ДД")
    event_date_choice.add_argument("--same-day", action="store_true",
                                   help="подтвердить, что событие произошло в день публикации материала")
    event_date_choice.add_argument("--unknown", action="store_true",
                                   help="зафиксировать, что дату установить не удалось")
    event_date.add_argument("--note", required=True, help="основание решения о дате события")
    exclude = review_commands.add_parser(
        "claim-exclude", help="исключить утверждение из поста, сохранив запись и причину в базе"
    )
    exclude.add_argument("id", type=int, help="id черновика")
    exclude.add_argument("claim_id", type=int, help="id утверждения из review show")
    exclude.add_argument("--reason", required=True, help="почему пункт исключается")
    group_check = review_commands.add_parser(
        "group-check", help="зафиксировать редакторскую проверку группы-кандидата"
    )
    group_check.add_argument("id", type=int, help="id черновика")
    group_check.add_argument("group_no", type=int, help="номер группы из review show")
    event_choice = group_check.add_mutually_exclusive_group(required=True)
    event_choice.add_argument("--same-event", dest="same_event", action="store_true",
                              help="материалы описывают одно событие")
    event_choice.add_argument("--different-events", dest="same_event", action="store_false",
                              help="материалы описывают разные события")
    independence_choice = group_check.add_mutually_exclusive_group(required=True)
    independence_choice.add_argument("--independent", dest="independent", action="store_true",
                                     help="основания действительно независимы")
    independence_choice.add_argument("--not-independent", dest="independent", action="store_false",
                                     help="источники повторяют одно основание или связь не установлена")
    group_check.add_argument("--note", required=True, help="краткое основание решения редактора")
    edit = review_commands.add_parser("edit", help="исправить pending-черновик")
    edit.add_argument("id", type=int)
    edit.add_argument("--text", required=True)
    for decision in ("approve", "reject"):
        sub = review_commands.add_parser(decision)
        sub.add_argument("id", type=int)
        if decision == "approve":
            sub.add_argument("--evidence-checked", action="store_true", required=True,
                             help="подтвердить проверку содержания и ссылок-оснований")
            sub.add_argument("--independence-checked", action="store_true", required=True,
                             help="подтвердить, что независимые подтверждения не пересказывают одно основание")
            sub.add_argument("--groups-checked", action="store_true",
                             help="подтвердить ручную проверку повторов, если модель группировки недоступна")
    publish = commands.add_parser("publish", help="опубликовать только одобренный черновик")
    publish.add_argument("id", type=int)
    publish.add_argument("--dry-run", action="store_true", help="показать текст без обращения к MAX")
    scheduled_publish = commands.add_parser(
        "publish-scheduled", help="опубликовать один одобренный основной черновик за текущий день по Москве"
    )
    scheduled_publish.add_argument("--dry-run", action="store_true", help="показать подходящий текст без отправки в MAX")
    resolve = commands.add_parser("publish-resolve", help="разрешить неопределённую попытку после проверки канала")
    resolve.add_argument("id", type=int)
    resolution = resolve.add_mutually_exclusive_group(required=True)
    resolution.add_argument("--not-posted", action="store_true", help="подтвердить проверкой, что MAX не опубликовал пост")
    resolution.add_argument("--posted", action="store_true", help="подтвердить, что пост опубликован")
    resolve.add_argument("--url", default="", help="ссылка на опубликованный пост при --posted")
    correction = commands.add_parser("correct", help="подготовить черновик заметного исправления опубликованного поста")
    correction.add_argument("id", type=int, help="id исходного опубликованного черновика")
    correction.add_argument("--reason", required=True, help="что было неверно")
    correction.add_argument("--corrected", required=True, help="исправленная формулировка или факт")
    correction.add_argument("--source-url", required=True, help="ссылка на основание исправления")
    logs = commands.add_parser("logs", help="показать последние события журнала")
    logs.add_argument("--limit", type=int, default=50)
    return parser


def confirm_claim_link_review(url: str) -> str:
    """Open the cited page and capture a short editor-entered evidence excerpt."""
    print(f"Открываю ссылку утверждения в браузере: {url}")
    try:
        opened = webbrowser.open(url, new=2)
    except Exception:
        opened = False
    if not opened:
        print("Автоматически открыть браузер не удалось. Откройте ссылку вручную перед подтверждением.")
    try:
        confirmation = input("После чтения страницы и сверки с утверждением введите ПРОВЕРЕНО: ")
    except EOFError as exc:
        raise ValueError("Проверка требует интерактивного редакторского подтверждения в терминале") from exc
    if confirmation.strip().casefold() != "проверено":
        raise ValueError("Редакторская проверка не подтверждена; отметка не сохранена")
    try:
        evidence = input(
            "Кратко укажите фрагмент или содержание страницы, которое поддерживает утверждение "
            "(не более 500 символов): "
        ).strip()
    except EOFError as exc:
        raise ValueError("Нужно сохранить краткое основание проверки") from exc
    if len(evidence) < 8:
        raise ValueError("Основание слишком короткое; отметка не сохранена")
    if len(evidence) > 500:
        raise ValueError("Основание ограничено 500 символами; отметка не сохранена")
    return evidence


def _draft_batch_ids(db, first_id: int) -> list[int]:
    event = db.execute(
        """SELECT details FROM events WHERE kind='draft_created' AND entity_type='draft'
           AND entity_id=? ORDER BY id DESC LIMIT 1""", (str(first_id),)
    ).fetchone()
    if not event:
        return [first_id]
    try:
        ids = json.loads(event["details"]).get("part_ids")
        return [int(value) for value in ids] if ids else [first_id]
    except (TypeError, ValueError, json.JSONDecodeError):
        return [first_id]


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    db = None
    try:
        config = None
        if args.command not in {"schedule", "credentials"}:
            config = load_sources()
            db = connect()
        if args.command == "schedule":
            if args.schedule_command == "install":
                tasks = install_schedule(sys.executable)
                print(f"Расписание установлено с Python: {sys.executable}")
                for task in tasks:
                    task_name = {
                        "hourly": "Сборщик информации - Сбор каждый час",
                        "12": "Сборщик информации - Сбор 12-00",
                    }.get(str(task.get("key")), "Задача Планировщика")
                    print(f"{task_name}: зарегистрирована; следующий запуск {task.get('nextRunTime', 'не определён')}")
                print("Задачи работают только при входе этого пользователя Windows в систему; токен MAX должен быть задан для него.")
            elif args.schedule_command == "status":
                tasks = schedule_status()
                timezone_id = local_timezone_id()
                if timezone_id != MOSCOW_WINDOWS_TIMEZONE:
                    detected = (
                        f" Сейчас Windows сообщает: {timezone_id}."
                        if timezone_id else " Идентификатор зоны прочитать не удалось."
                    )
                    print("ВНИМАНИЕ: часовой пояс Windows не определён как московский "
                          f"(Russian Standard Time); задачи могут запускаться не по МСК.{detected}")
                else:
                    print("Часовой пояс Windows: Москва (Russian Standard Time).")
                for task in tasks:
                    task_name = {
                        "hourly": "Сборщик информации - Сбор каждый час",
                        "12": "Сборщик информации - Сбор 12-00",
                    }.get(str(task.get("key")), "Задача Планировщика")
                    if not task.get("registered"):
                        print(f"{task_name}: НЕ УСТАНОВЛЕНА")
                        continue
                    next_run = task.get("nextRunTime") or "не назначен"
                    cadence = "каждый час" if task.get("key") == "hourly" else "ежедневно"
                    print(f"{task_name}: {task.get('state', 'неизвестно')}; следующий запуск {next_run}; "
                          f"периодичность {cadence}; "
                          f"последний код {task.get('lastTaskResult', '—')}; "
                          f"пробуждение {'включено' if task.get('wakeToRun') else 'выключено'}; "
                          f"запуск после пропуска {'включён' if task.get('startWhenAvailable') else 'выключен'}; "
                          f"настройки {'совпадают' if task.get('actionMatches') is True else 'нужно переустановить'}")
            else:
                removed = remove_schedule()
                print("Удалены задачи: " + (", ".join(removed) if removed else "зарегистрированных задач не было"))
        elif args.command == "credentials":
            if args.credentials_command == "status":
                token, channel_id = get_max_credentials()
                print(f"MAX bot token: {'настроен' if token else 'НЕ НАСТРОЕН'}")
                print(f"MAX channel ID: {'настроен' if channel_id else 'НЕ НАСТРОЕН'}")
                print("Значения не показываются.")
            elif args.credentials_command == "set-max":
                channel_id = input("ID или адрес канала MAX: ").strip()
                token = getpass.getpass("Токен бота MAX (ввод скрыт): ")
                confirmation = getpass.getpass("Повторите токен для проверки (ввод скрыт): ")
                if token != confirmation:
                    raise ValueError("Токены не совпадают; сохранение отменено")
                save_max_credentials(token, channel_id)
                print("Учётные данные MAX сохранены в Диспетчере учётных данных Windows для текущего пользователя.")
            else:
                removed = remove_max_credentials()
                print("Учётные данные MAX удалены." if removed else "Сохранённых учётных данных MAX не найдено.")
        elif args.command == "init":
            print(f"База создана/готова. Источники: {sources_path()}")
        elif args.command == "fetch":
            for result in fetch_enabled_sources(db, config["sources"]):
                print(result)
        elif args.command == "run":
            if args.hours < 1 or args.hours > 96:
                raise ValueError("--hours должен быть в диапазоне от 1 до 96")
            for result in fetch_enabled_sources(db, config["sources"]):
                print(result)
            try:
                draft_id = create_daily_draft(db, args.hours)
                batch_ids = _draft_batch_ids(db, draft_id)
                draft_label = ", ".join(f"#{value}" for value in batch_ids)
                print(f"Созданы черновики: {draft_label}; публикация не выполнялась. Одобрите не более одной части для дневной публикации.")
                if args.notify:
                    reason = f"Созданы черновики: {draft_label}. Проверьте даты событий, ссылки и группировки до 13:45; для дневной публикации одобрите не более одной части."
                    notified, detail = notify_editor("Сборщик информации: черновик готов", reason)
                    record_event(db, "editor_notification", "draft", draft_id, {
                        "status": "requested" if notified else "failed", "reason": reason, "detail": detail,
                    })
            except ValueError as exc:
                if "Нет материалов с датой публикации" not in str(exc):
                    if "Нет материалов, принятых для черновика" not in str(exc):
                        raise
                print(f"{exc}; публикация не выполнялась.")
                if args.notify:
                    reason = str(exc)
                    record_event(db, "scheduled_draft_skipped", details={"reason": reason})
                    notified, detail = notify_editor("Сборщик информации: черновик не создан", reason)
                    record_event(db, "editor_notification", details={
                        "status": "requested" if notified else "failed", "reason": reason, "detail": detail,
                    })
        elif args.command == "ingest":
            source = get_source(config, args.source)
            if source.get("retrieval", {}).get("method") != "manual":
                print(f"Примечание: источник {args.source!r} добавляется вручную; его автоматический сбор не изменён.")
            result = ingest_manual(db, source, args.title, args.url, args.published_at, args.summary)
            print(f"Материал: {result}")
        elif args.command == "draft":
            if args.hours < 1 or args.hours > 96:
                raise ValueError("--hours должен быть в диапазоне от 1 до 96")
            draft_id = create_daily_draft(db, args.hours)
            print("Созданы черновики: " + ", ".join(
                f"#{value}" for value in _draft_batch_ids(db, draft_id)
            ))
        elif args.command == "review":
            if args.review_command == "list":
                for row in list_drafts(db, args.status):
                    print(f"#{row['id']} [{row['status']}] {row['created_at']} — {row['title']}")
            elif args.review_command == "batches":
                for row in list_batches(db):
                    print(f"{row['id']} · {row['batch_date']} · частей: {row['part_count']} · "
                          f"выбрано: {row['selected_count'] or 0}")
            elif args.review_command == "batch-show":
                for part in batch_parts(db, args.batch_id):
                    print(f"Часть {part['part_no']} · черновик #{part['draft_id']} [{part['status']}] "
                          f"· решение: {part['disposition']}" +
                          (f" · отложено до {part['deferred_until']}" if part['deferred_until'] else ""))
                    if part["oldest_published_at"]:
                        print(f"  Публикации материалов: {part['oldest_published_at']} — {part['newest_published_at']}")
                    if part["note"]:
                        print(f"  Примечание: {part['note']}")
                    for claim in db.execute(
                        "SELECT source_name,published_at,source_url FROM draft_claims WHERE draft_id=? AND included=1 ORDER BY published_at",
                        (part["draft_id"],),
                    ):
                        published_at = datetime.fromisoformat(claim["published_at"])
                        expires_at = published_at + timedelta(hours=96)
                        print(f"  Публикация: {claim['published_at']} · актуально до "
                              f"{expires_at.astimezone(timezone(timedelta(hours=3))).strftime('%d.%m.%Y %H:%M МСК')} · "
                              f"{claim['source_name']} · {claim['source_url']}")
            elif args.review_command == "select-part":
                select_batch_part(db, args.batch_id, args.draft_id)
                print(f"Черновик #{args.draft_id} выбран для плановой публикации из партии {args.batch_id}.")
            elif args.review_command == "close-part":
                close_batch_part(db, args.batch_id, args.draft_id, args.reason)
                print(f"Черновик #{args.draft_id} закрыт; решение записано в журнал.")
            elif args.review_command == "defer-part":
                defer_batch_part(db, args.batch_id, args.draft_id, args.until, args.note)
                print(f"Черновик #{args.draft_id} отложен до {args.until}, если материалы останутся в пределах 96 часов.")
            elif args.review_command == "relevance":
                for row in list_relevance(db, args.status, args.limit):
                    terms = ", ".join(json.loads(row["matched_terms"] or "[]")) or "совпадений нет"
                    print(f"#{row['id']} [{row['status']}/{row['confidence']}] {row['source_name']} · "
                          f"{row['published_at'] or 'дата не указана'} · {row['title']}")
                    print(f"  {row['reason']}; термины: {terms}")
                    print(f"  {row['url']}")
                    if row["confidence"] == "low" and not row["reviewed_at"]:
                        print("  Автоматически исключено фильтром; для аудита используйте review relevance --status excluded")
                    if row["editor_note"]:
                        print(f"  Решение редактора: {row['editor_note']}")
            elif args.review_command == "relevance-check":
                decide_relevance(db, args.id, args.include, args.note)
                decision = "включён" if args.include else "исключён"
                print(f"Кандидат релевантности #{args.id} {decision}; решение записано в журнал.")
            elif args.review_command == "filter-report":
                for row in relevance_report(db, args.days):
                    print(f"{row['source_id']}: запусков {row['runs']}, записей в лентах {row['feed_items']}, "
                          f"уверенность высокая/средняя/низкая {row['high_confidence']}/{row['medium_confidence']}/{row['low_confidence']}, "
                          f"ожидают проверки {row['needs_review']}, "
                          f"дублей {row['duplicates']}, ошибок обработки {row['processing_errors']}, "
                          f"решений редактора: включено {row['editor_included']}, исключено {row['editor_excluded']}, "
                          f"вероятные пропуски {row['potential_misses']}")
            elif args.review_command == "show":
                draft = get_draft(db, args.id)
                print(f"#{draft['id']} [{draft['status']}] {draft['title']}\n\n{draft['body']}")
                claims = draft_claims(db, args.id)
                print("\nСТРУКТУРИРОВАННЫЕ УТВЕРЖДЕНИЯ И СВЯЗИ С ИСТОЧНИКАМИ")
                for claim in claims:
                    checked = "проверка актуальна" if claim_link_check_is_current(claim) else "НЕ ПРОВЕРЕНО / УСТАРЕЛО"
                    included = "включено" if claim["included"] else f"ИСКЛЮЧЕНО: {claim['exclusion_reason']}"
                    print(f"\nУтверждение #{claim['id']} · группа-кандидат #{claim['group_no']} · {included} · {checked}")
                    print(f"  Текст: {claim['claim_text']}\n  Источник: {claim['source_name']} ({claim['source_id']})")
                    if claim["version_no"] is not None:
                        print(f"  Версия материала: v{claim['version_no']}")
                    print(f"  Опубликовано: {claim['published_at']}\n  Ссылка: {claim['source_url']}")
                    print(f"  Текущая HTTP-проверка: {claim['publication_check_status'] or 'нет'} "
                          f"({claim['publication_checked_at'] or 'время не записано'})")
                    if claim["link_check_note"]:
                        print(f"  Пояснение проверки редактора: {claim['link_check_note']}")
                    if claim["same_url_other_source"]:
                        print("  Повторная ссылка: та же страница поступила через другой источник; "
                              "не считать независимым подтверждением.")
                    event_date_label = claim["event_date"] or "не установлена"
                    print(f"  Дата события: {event_date_label} [{claim['event_date_status']}]")
                    if claim["event_date_note"]:
                        print(f"  Основание даты: {claim['event_date_note']}")
                for group in draft_groups(db, args.id):
                    if int(group["claim_count"]) > 1:
                        same_event = "не проверено" if group["review_same_event"] is None else ("да" if group["review_same_event"] else "нет")
                        independent = "не проверено" if group["sources_independent"] is None else ("да" if group["sources_independent"] else "нет")
                        print(f"\nГруппа-кандидат #{group['group_no']}: одно событие — {same_event}; независимые источники — {independent}")
                        if group["review_note"]:
                            print(f"  Основание решения: {group['review_note']}")
                for alert in revision_alerts(db, args.id):
                    fields = ", ".join(json.loads(alert["changed_fields"] or "[]"))
                    state = alert["resolution"] or "ТРЕБУЕТ ПРОВЕРКИ"
                    print(f"\nНовая версия источника для утверждения #{alert['claim_id']}: "
                          f"v{alert['previous_version']} → v{alert['current_version']} [{state}]"
                          f"; изменено: {fields or 'содержимое'}")
                    print(f"  Новый заголовок: {alert['current_title']}")
                    if alert["current_summary"]:
                        print(f"  Новое описание: {alert['current_summary']}")
                    print(f"  Новая дата публикации: {alert['current_published_at'] or 'не указана'}")
                    if alert["note"]:
                        print(f"  Решение редактора: {alert['note']}")
            elif args.review_command == "revision-check":
                mode = "refresh" if args.refresh else "keep"
                review_source_revision(db, args.id, args.claim_id, mode, args.note)
                print(f"Проверка новой версии для утверждения #{args.claim_id} в черновике #{args.id} сохранена.")
            elif args.review_command == "claim-check":
                claim = db.execute(
                    "SELECT source_id,source_url FROM draft_claims WHERE id=? AND draft_id=? AND included=1",
                    (args.claim_id, args.id),
                ).fetchone()
                if claim is None:
                    raise ValueError(f"Активное утверждение #{args.claim_id} не найдено в черновике #{args.id}")
                source = get_source(load_sources(), claim["source_id"])
                validate_publication_url(source, claim["source_url"])
                evidence = confirm_claim_link_review(claim["source_url"])
                editor_note = f"{args.note.strip()} Основание редактора: {evidence}"
                result = mark_claim_link_checked(db, args.id, args.claim_id, True, editor_note)
                http_status = result.get("http_status", "не получен")
                print(f"Ссылка утверждения #{args.claim_id} перепроверена: {result['status']} "
                      f"(HTTP {http_status}). Сохранено пояснение редактора; HTTP-проверка не подтверждает истинность факта.")
            elif args.review_command == "correction-check":
                correction = db.execute(
                    "SELECT correction_source_id,correction_source_url FROM drafts WHERE id=? AND correction_of_id IS NOT NULL",
                    (args.id,),
                ).fetchone()
                if correction is None:
                    raise ValueError(f"Черновик #{args.id} не является исправлением")
                source = get_source(load_sources(), correction["correction_source_id"])
                validate_publication_url(source, correction["correction_source_url"])
                evidence = confirm_claim_link_review(correction["correction_source_url"])
                result = mark_correction_evidence_checked(db, args.id, args.note, evidence)
                http_status = result.get("http_status", "не получен")
                print(f"Основание исправления перепроверено ({result['status']}, HTTP {http_status}); "
                      "решение сохранено. HTTP-проверка не доказывает истинность основания.")
            elif args.review_command == "event-date":
                mode = "explicit" if args.event_date else ("same_day" if args.same_day else "unknown")
                review_claim_event_date(db, args.id, args.claim_id, mode, args.event_date, args.note)
                print(f"Дата события утверждения #{args.claim_id} в черновике #{args.id} обновлена.")
            elif args.review_command == "claim-exclude":
                exclude_claim(db, args.id, args.claim_id, args.reason)
                print(f"Утверждение #{args.claim_id} исключено из текста; запись и причина сохранены.")
            elif args.review_command == "group-check":
                review_candidate_group(db, args.id, args.group_no, args.same_event, args.independent, args.note)
                print(f"Проверка группы-кандидата #{args.group_no} в черновике #{args.id} записана.")
            elif args.review_command == "edit":
                edit_draft(db, args.id, args.text)
                print(f"Черновик #{args.id} исправлен; изменение записано в журнал.")
            else:
                decision = "approved" if args.review_command == "approve" else "rejected"
                checks = None
                if decision == "approved":
                    checks = {
                        "evidence_checked": args.evidence_checked,
                        "independence_checked": args.independence_checked,
                        "groups_checked": args.groups_checked,
                    }
                set_decision(db, args.id, decision, checks)
                print(f"Черновик #{args.id}: {decision}")
        elif args.command == "publish":
            result = publish_draft(db, args.id, dry_run=args.dry_run)
            print(result["text"] if result["status"] == "dry_run" else result)
        elif args.command == "publish-scheduled":
            result = publish_scheduled(db, dry_run=args.dry_run)
            print(result["text"] if result["status"] == "dry_run" else result)
            if not args.dry_run and result.get("status") in {"skipped", "needs_review"}:
                reason = str(result.get("reason", "Плановая публикация не выполнена."))
                notified, detail = notify_editor("Сборщик информации: пост не опубликован", reason)
                record_event(db, "editor_notification", details={
                    "status": "requested" if notified else "failed", "reason": reason, "detail": detail,
                })
                if not notified:
                    print(f"Уведомление Windows не показано: {detail}", file=sys.stderr)
        elif args.command == "publish-resolve":
            resolve_attempt(db, args.id, posted=args.posted, url=args.url)
            print(f"Попытка публикации для черновика #{args.id} разрешена по результату вашей проверки канала.")
        elif args.command == "correct":
            correction_id = create_correction_draft(db, args.id, args.reason, args.corrected, args.source_url)
            print(f"Создан черновик исправления #{correction_id}; проверьте и одобрите его обычным способом.")
        elif args.command == "logs":
            for row in recent_events(db, max(1, min(args.limit, 500))):
                print(f"{row['created_at']} {row['kind']} {row['entity_type']}:{row['entity_id']} {row['details']}")
        return 0
    except Exception as exc:
        if db is not None:
            try:
                record_event(db, "application_error", details=f"{type(exc).__name__}: {exc}")
            except Exception:
                pass
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    finally:
        if db is not None:
            db.close()
