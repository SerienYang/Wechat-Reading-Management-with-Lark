import argparse
import getpass
import json
import os
import tempfile
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from .cover_images import CoverImageError, download_cover_image, has_cover_attachment
from .config import ConfigurationError, load_lark_resource_config
from .launchagent import (
    build_launchagent_plist,
    default_launchagent_log_path,
    default_launchagent_path,
    default_launchagent_runtime_path,
    sync_runtime_project,
)
from .lark_base import (
    LarkBaseError,
    LarkBaseClient,
    epoch_to_lark_datetime,
    extract_attachment_file_token,
    make_book_fields,
    make_daily_stats_fields,
    make_highlight_fields,
    make_progress_fields,
    make_sync_fields,
    make_tag_fields,
)
from .lark_im import LarkImClient, LarkImError
from .llm import LLMConfig, LLMError, OpenAICompatibleClient
from .security import load_secret_from_keychain, redact_secret, save_secret_to_keychain
from .schema import core_table_names
from .sync_keys import daily_record_key, daily_stats_key, normalize_label_name
from .tagging import (
    TaggingNote,
    TaggingParseError,
    TaggingServiceError,
    build_tagging_prompt,
    run_api_tagging,
)
from .weread import (
    BookMetadata,
    WeReadApiError,
    WeReadClient,
    WeReadGatewayClient,
    WeReadProgress,
    WeReadShelfItem,
    cookie_header,
    cookies_from_playwright_state,
    count_highlights,
    count_reviews,
    match_books_by_titles,
    parse_book_metadata,
    parse_bookmarks_payload,
    parse_progress_payload,
    parse_reviews_payload,
    progress_percent,
)


WEREAD_KEYCHAIN_SERVICE = "reading-manager.weread.cookies"
WEREAD_API_KEYCHAIN_SERVICE = "reading-manager.weread.api_key"
LLM_API_KEYCHAIN_SERVICE = "reading-manager.llm.api_key"
WEREAD_KEYCHAIN_ACCOUNT = "default"
WEREAD_API_KEY_ENV = "WEREAD_API_KEY"
LLM_API_KEY_ENV = "LLM_API_KEY"
DEFAULT_CATCHUP_LOOKBACK_DAYS = 30


@dataclass
class TaggingStats:
    candidate_notes: int = 0
    tagged_notes: int = 0
    failed_notes: int = 0
    tags_count: int = 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="reading-manager")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("doctor", help="Run offline project checks.")

    auth_parser = subparsers.add_parser(
        "auth-weread",
        help="Save WeRead cookies from a Playwright storage state file into macOS Keychain.",
    )
    auth_parser.add_argument("--state-file", required=True, help="Path to Playwright storage state JSON.")

    api_key_parser = subparsers.add_parser(
        "auth-weread-api-key",
        help="Save the official WeRead Agent Gateway API key into macOS Keychain.",
    )
    api_key_parser.add_argument(
        "--env",
        default=WEREAD_API_KEY_ENV,
        help=f"Read the API key from this environment variable before prompting, default {WEREAD_API_KEY_ENV}.",
    )

    llm_api_key_parser = subparsers.add_parser(
        "auth-llm-api-key",
        help="Save the model API key into macOS Keychain for local scheduled runs.",
    )
    llm_api_key_parser.add_argument(
        "--env",
        default=LLM_API_KEY_ENV,
        help=f"Read the API key from this environment variable before prompting, default {LLM_API_KEY_ENV}.",
    )

    list_parser = subparsers.add_parser("list-books", help="List WeRead notebook books.")
    list_parser.add_argument(
        "--title",
        action="append",
        default=[],
        help="Requested book title to match. Can be provided multiple times.",
    )
    list_parser.add_argument("--limit", type=int, default=20, help="Max books to print when no titles are given.")

    preview_parser = subparsers.add_parser(
        "preview-sync",
        help="Preview selected WeRead books without writing data to Lark Base.",
    )
    preview_parser.add_argument(
        "--title",
        action="append",
        required=True,
        help="Requested book title to preview. Can be provided multiple times.",
    )

    sync_parser = subparsers.add_parser(
        "sync-weread",
        help="Write selected WeRead books, progress, and highlights into Lark Base.",
    )
    sync_parser.add_argument(
        "--title",
        action="append",
        required=True,
        help="Requested book title to sync. Can be provided multiple times.",
    )

    tag_parser = subparsers.add_parser(
        "tag-weread",
        help="Use the configured model API to tag pending WeRead highlights in Lark Base.",
    )
    tag_parser.add_argument("--limit", type=int, default=20, help="Max pending highlights to tag.")

    daily_parser = subparsers.add_parser(
        "sync-daily",
        help="Run daily incremental WeRead sync and tag new or changed highlights.",
    )
    daily_parser.add_argument("--limit", type=int, default=20, help="Max active books to sync.")
    daily_parser.add_argument(
        "--date",
        default=None,
        help="Record date for the daily snapshot, defaults to today.",
    )

    daily_stats_parser = subparsers.add_parser(
        "sync-daily-stats",
        help="Recompute day-level reading duration stats from reading snapshots.",
    )
    daily_stats_parser.add_argument(
        "--date",
        default=None,
        help="Stats date to recompute, defaults to today.",
    )
    daily_stats_parser.add_argument(
        "--all",
        action="store_true",
        help="Recompute stats for every date present in reading snapshots.",
    )

    catchup_parser = subparsers.add_parser(
        "sync-catchup",
        help="Backfill missing daily snapshots in a recent window, then update today incrementally.",
    )
    catchup_parser.add_argument("--limit", type=int, default=20, help="Max active books to sync per day.")
    catchup_parser.add_argument(
        "--date",
        default=None,
        help="Last date to include, defaults to today.",
    )
    catchup_parser.add_argument(
        "--lookback-days",
        type=int,
        default=DEFAULT_CATCHUP_LOOKBACK_DAYS,
        help=f"Recent window to inspect for missing snapshots, default {DEFAULT_CATCHUP_LOOKBACK_DAYS} days.",
    )

    finished_parser = subparsers.add_parser(
        "sync-finished-books",
        help="Sync finished WeRead shelf books into the Lark Books table without importing the full shelf.",
    )
    finished_parser.add_argument("--limit", type=int, default=0, help="Max finished books to sync, 0 means all.")

    retry_parser = subparsers.add_parser(
        "retry-tagging",
        help="Retry AI tagging for untagged highlights.",
    )
    retry_parser.add_argument("--limit", type=int, default=20, help="Max untagged highlights to retry.")

    covers_parser = subparsers.add_parser(
        "sync-covers",
        help="Upload missing WeRead book covers as Lark Base attachments.",
    )
    covers_parser.add_argument("--limit", type=int, default=0, help="Max book covers to upload, 0 means all.")
    backfill_parser = subparsers.add_parser(
        "backfill-recent",
        help="Add missing books and last-read reading records for recently read shelf books.",
    )
    backfill_parser.add_argument("--since", required=True, help="Inclusive ISO date, e.g. 2026-10-01.")
    backfill_parser.add_argument("--dry-run", action="store_true", help="Print planned writes without changing Lark Base.")
    backfill_parser.add_argument(
        "--untrusted-last-read-on",
        help="If the live shelf last-read falls on this ISO date, prefer an earlier Bitable or known last-read date.",
    )

    subparsers.add_parser(
        "run-daily",
        help="Run the complete scheduled sync: finished books, catch-up, and covers.",
    )

    launchagent_parser = subparsers.add_parser(
        "install-launchagent",
        help="Write the local macOS LaunchAgent plist for daily sync.",
    )
    launchagent_parser.add_argument(
        "--install",
        action="store_true",
        help="Actually write the plist. Without this flag, only prints the target path and preview.",
    )
    args = parser.parse_args()

    try:
        return _dispatch_command(args)
    except ConfigurationError as exc:
        print(f"Configuration error: {exc}")
        return 2


def _dispatch_command(args) -> int:
    if args.command == "doctor":
        print("Reading Manager doctor")
        try:
            resources = load_lark_resource_config()
        except ConfigurationError:
            print("Lark Base: not configured")
        else:
            if resources.base_url:
                print(f"Lark Base: {resources.base_url}")
            else:
                print("Lark Base: configured")
        print("Core tables: " + ", ".join(core_table_names()))
        return 0
    if args.command == "auth-weread":
        return _auth_weread(Path(args.state_file))
    if args.command == "auth-weread-api-key":
        return _auth_weread_api_key(args.env)
    if args.command == "auth-llm-api-key":
        return _auth_llm_api_key(args.env)
    if args.command == "list-books":
        return _list_books(args.title, args.limit)
    if args.command == "preview-sync":
        return _preview_sync(args.title)
    if args.command == "sync-weread":
        return _sync_weread(args.title)
    if args.command == "tag-weread":
        return _tag_weread(args.limit)
    if args.command == "sync-daily":
        return _sync_daily(args.limit, args.date)
    if args.command == "sync-daily-stats":
        return _sync_daily_stats(args.date, args.all)
    if args.command == "sync-catchup":
        return _sync_catchup(args.limit, args.date, args.lookback_days)
    if args.command == "sync-finished-books":
        return _sync_finished_books(args.limit)
    if args.command == "retry-tagging":
        return _retry_tagging(args.limit)
    if args.command == "sync-covers":
        return _sync_covers(args.limit)
    if args.command == "backfill-recent":
        return _backfill_recent(args.since, args.dry_run, getattr(args, "untrusted_last_read_on", None))
    if args.command == "run-daily":
        return _run_daily_job()
    if args.command == "install-launchagent":
        return _install_launchagent(args.install)

    return 1


def _run_daily_job() -> int:
    stages = [
        ("sync-finished-books", lambda: _sync_finished_books(0)),
        ("sync-catchup", lambda: _sync_catchup(20)),
        ("sync-covers", lambda: _sync_covers(0)),
    ]
    failures = []
    for stage_name, run_stage in stages:
        try:
            result = run_stage()
        except Exception as exc:
            print(f"Daily job stage failed unexpectedly: {stage_name} ({type(exc).__name__})")
            failures.append(stage_name)
            continue
        if result != 0:
            print(f"Daily job stage failed: {stage_name} (exit={result})")
            failures.append(stage_name)

    if failures:
        print("Daily job failed stages: " + ", ".join(failures))
        return 9
    print("Daily job completed.")
    return 0


def _auth_weread(state_file: Path) -> int:
    raw_state = state_file.read_text(encoding="utf-8")
    cookies = cookies_from_playwright_state(raw_state)
    if not cookies:
        print("No WeRead cookies found in the browser state file.")
        return 2

    save_secret_to_keychain(
        WEREAD_KEYCHAIN_SERVICE,
        WEREAD_KEYCHAIN_ACCOUNT,
        json.dumps(cookies, ensure_ascii=False, sort_keys=True),
    )
    print(f"Saved WeRead login state to macOS Keychain ({len(cookies)} cookies).")
    print(f"Cookie header ready: {len(cookie_header(cookies))} characters.")
    return 0


def _auth_weread_api_key(env_name: str = WEREAD_API_KEY_ENV) -> int:
    api_key = os.environ.get(env_name, "").strip()
    if not api_key:
        api_key = getpass.getpass("WeRead official API key: ").strip()
    if not api_key:
        print("No WeRead API key provided.")
        return 2
    if not api_key.startswith("wrk-"):
        print("The WeRead API key format looks invalid; expected a value starting with wrk-.")
        return 2

    save_secret_to_keychain(WEREAD_API_KEYCHAIN_SERVICE, WEREAD_KEYCHAIN_ACCOUNT, api_key)
    print(f"Saved official WeRead API key to macOS Keychain ({redact_secret(api_key)}).")
    print("Future syncs will prefer the official WeRead Agent Gateway.")
    return 0


def _auth_llm_api_key(env_name: str = LLM_API_KEY_ENV) -> int:
    api_key = os.environ.get(env_name, "").strip()
    if not api_key:
        api_key = getpass.getpass("Model API key: ").strip()
    if not api_key:
        print("No model API key provided.")
        return 2

    save_secret_to_keychain(LLM_API_KEYCHAIN_SERVICE, WEREAD_KEYCHAIN_ACCOUNT, api_key)
    print(f"Saved model API key to macOS Keychain ({redact_secret(api_key)}).")
    return 0


def _save_weread_cookies(cookies: dict, changed_names: list) -> None:
    save_secret_to_keychain(
        WEREAD_KEYCHAIN_SERVICE,
        WEREAD_KEYCHAIN_ACCOUNT,
        json.dumps(cookies, ensure_ascii=False, sort_keys=True),
    )
    print("Refreshed WeRead login cookies: " + ", ".join(changed_names))


def _list_books(requested_titles: list, limit: int) -> int:
    try:
        client = _weread_client_from_keychain()
        books = client.get_notebooks()
    except WeReadApiError as exc:
        print(str(exc))
        return 3

    if requested_titles:
        result = match_books_by_titles(books, requested_titles)
        if result.matched:
            print("Matched WeRead books:")
            for book in result.matched:
                print(_format_book(book))
        if result.missing_titles:
            print("Missing requested titles: " + ", ".join(result.missing_titles))
            return 4
        return 0

    print(f"WeRead notebook books: {len(books)} total")
    for book in books[: max(limit, 0)]:
        print(_format_book(book))
    return 0


def _preview_sync(requested_titles: list) -> int:
    try:
        client = _weread_client_from_keychain()
        books = client.get_notebooks()
    except WeReadApiError as exc:
        print(str(exc))
        return 3

    result = match_books_by_titles(books, requested_titles)
    if result.missing_titles:
        print("Missing requested titles: " + ", ".join(result.missing_titles))
        return 4

    print("WeRead sync preview, no Lark writes performed.")
    for book in result.matched:
        try:
            progress = client.get_progress(book.book_id)
            highlights = client.get_bookmarks(book.book_id)
            reviews = client.get_reviews(book.book_id)
        except WeReadApiError as exc:
            print(f"- {book.title}: preview failed: {exc}")
            return 5
        progress_text = "unknown"
        progress_value = progress_percent(progress)
        if progress_value is not None:
            progress_text = f"{progress_value}%"
        print(
            f"- {book.title}: highlights={count_highlights(highlights)}, "
            f"reviews={count_reviews(reviews)}, progress={progress_text}"
        )
    return 0


def _sync_weread(requested_titles: list) -> int:
    started_at = _now_lark_datetime()
    batch_id = "weread-" + datetime.now().strftime("%Y%m%d-%H%M%S")
    lark = LarkBaseClient()

    try:
        client = _weread_client_from_keychain()
        books = client.get_notebooks()
    except WeReadApiError as exc:
        print(str(exc))
        return 3

    result = match_books_by_titles(books, requested_titles)
    if result.missing_titles:
        print("Missing requested titles: " + ", ".join(result.missing_titles))
        return 4

    sync_record_id = lark.upsert_record(
        lark.table_id("同步记录"),
        make_sync_fields(
            batch_id=batch_id,
            status="运行中",
            scope=f"手动选择 {len(result.matched)} 本书",
            selected_books=[book.title for book in result.matched],
            started_at=started_at,
            ended_at=None,
            books_count=0,
            reading_records_count=0,
            highlights_count=0,
            tags_count=0,
            error_summary="",
        ),
    )
    if not sync_record_id:
        sync_record_id = lark.find_record_id(
            lark.table_id("同步记录"),
            search_field="同步批次 ID",
            keyword=batch_id,
        )
    if not sync_record_id:
        print("Failed to locate the sync record after creating it.")
        return 5

    counts = {"books": 0, "reading": 0, "highlights": 0}
    book_index = _load_book_index(lark)
    for book in result.matched:
        book_payload = client.get_book(book.book_id)
        progress_payload = client.get_progress(book.book_id)
        bookmarks_payload = client.get_bookmarks(book.book_id)
        reviews_payload = client.get_reviews(book.book_id)
        metadata = parse_book_metadata(book_payload if isinstance(book_payload, dict) else {})
        if not metadata.title:
            metadata = parse_book_metadata(
                {
                    "bookId": book.book_id,
                    "title": book.title,
                    "author": book.author,
                    "cover": book.cover,
                }
            )
        progress = parse_progress_payload(progress_payload)
        now = _now_lark_datetime()
        book_record_id = _upsert_book(lark, metadata, progress, now, book_index=book_index)
        counts["books"] += 1

        lark.upsert_record(
            lark.table_id("阅读记录"),
            make_progress_fields(
                title=metadata.title,
                book_id=metadata.book_id,
                book_record_id=book_record_id,
                sync_record_id=sync_record_id,
                progress=progress,
                now=now,
            ),
        )
        counts["reading"] += 1

        for highlight in _parse_note_payloads(bookmarks_payload, reviews_payload):
            fields = make_highlight_fields(
                title=metadata.title,
                highlight=highlight,
                book_record_id=book_record_id,
                sync_record_id=sync_record_id,
                now=now,
            )
            existing_highlight_record_id = lark.find_record_id(
                lark.table_id("笔记划线"),
                search_field="内容哈希",
                keyword=str(fields["内容哈希"]),
            )
            lark.upsert_record(
                lark.table_id("笔记划线"),
                fields,
                record_id=existing_highlight_record_id or None,
            )
            counts["highlights"] += 1

    ended_at = _now_lark_datetime()
    lark.upsert_record(
        lark.table_id("同步记录"),
        make_sync_fields(
            batch_id=batch_id,
            status="成功",
            scope=f"手动选择 {len(result.matched)} 本书",
            selected_books=[book.title for book in result.matched],
            started_at=started_at,
            ended_at=ended_at,
            books_count=counts["books"],
            reading_records_count=counts["reading"],
            highlights_count=counts["highlights"],
            tags_count=0,
            error_summary="",
        ),
        record_id=sync_record_id,
    )

    print(
        "Lark Base sync completed: "
        f"books={counts['books']}, reading_records={counts['reading']}, "
        f"highlights={counts['highlights']}, batch_id={batch_id}"
    )
    return 0


def _sync_daily(limit: int, record_date: str = None) -> int:
    started_at = _now_lark_datetime()
    record_date = record_date or date.today().isoformat()
    batch_id = "weread-daily-" + datetime.now().strftime("%Y%m%d-%H%M%S")
    lark = LarkBaseClient()
    sync_table_id = lark.table_id("同步记录")
    note_table_id = lark.table_id("笔记划线")
    sync_record_id = ""
    selected_books = []
    counts = {"books": 0, "reading": 0, "highlights": 0, "tags": 0}
    errors = []

    try:
        client = _weread_client_from_keychain()
        target_date = _parse_iso_date(record_date)
        is_today = target_date == date.today()
        # Capture last-read from shelf/notebooks before later book API calls, which can refresh readUpdateTime.
        notebooks = client.get_notebooks()
        shelf_items = client.get_shelf()
        books = _select_daily_sync_books(
            notebooks,
            shelf_items,
            as_of=target_date,
            limit=limit,
            recent_days=DEFAULT_CATCHUP_LOOKBACK_DAYS,
        )
        selected_books = [book.title for book in books]
        print(_reading_record_scope_message())
        sync_record_id = lark.upsert_record(
            sync_table_id,
            make_sync_fields(
                batch_id=batch_id,
                status="运行中",
                scope=f"日更最近活跃 {len(books)} 本书",
                selected_books=selected_books,
                started_at=started_at,
                ended_at=None,
                books_count=0,
                reading_records_count=0,
                highlights_count=0,
                tags_count=0,
                error_summary="",
            ),
        )
        if not sync_record_id:
            sync_record_id = _find_record_id_with_retry(
                lark,
                sync_table_id,
                "同步批次 ID",
                batch_id,
            )
        if not sync_record_id:
            print("Failed to locate the daily sync record after creating it.")
            return 5

        if is_today:
            untagged_records = _taggable_note_records(_list_note_records(lark), limit=limit)
            retry_stats = _tag_note_records(lark, untagged_records)
            counts["tags"] += retry_stats.tags_count
            if retry_stats.failed_notes:
                errors.append(f"历史未打标笔记重试仍失败 {retry_stats.failed_notes} 条")

        note_index = _load_note_index(lark) if is_today else {"by_note_id": {}, "by_hash": {}}
        book_index = _load_book_index(lark)
        new_or_changed_records = []
        for book in books:
            now = _now_lark_datetime()
            metadata = book.metadata
            # Evidence is taken from the pre-captured last-read/finish times, not from later API calls.
            captured_progress = book.progress
            should_write_reading = _has_reading_evidence_on(captured_progress, target_date)
            should_fetch_notes = is_today and book.from_notebook
            progress = _effective_progress(captured_progress, book.finish_reading)
            if is_today and should_fetch_notes:
                book_payload = client.get_book(book.book_id)
                progress_payload = client.get_progress(book.book_id)
                bookmarks_payload = client.get_bookmarks(book.book_id)
                reviews_payload = client.get_reviews(book.book_id)
                fetched_metadata = parse_book_metadata(book_payload if isinstance(book_payload, dict) else {})
                if fetched_metadata.title:
                    metadata = fetched_metadata
                fetched_progress = parse_progress_payload(progress_payload)
                # Keep captured last-read evidence; only enrich progress percent / finish_time when useful.
                progress = WeReadProgress(
                    progress=fetched_progress.progress or progress.progress,
                    reading_minutes=fetched_progress.reading_minutes or progress.reading_minutes,
                    last_read_time=captured_progress.last_read_time or fetched_progress.last_read_time,
                    finish_time=fetched_progress.finish_time or captured_progress.finish_time,
                )
                progress = _effective_progress(progress, book.finish_reading)
            elif not metadata.title:
                continue

            book_record_id = _upsert_book(
                lark,
                metadata,
                progress,
                now,
                reading_status=_reading_status(progress, book.finish_reading),
                book_index=book_index,
            )
            counts["books"] += 1

            if should_write_reading:
                written = _upsert_reading_record_if_missing(
                    lark,
                    metadata=metadata,
                    progress=progress,
                    book_record_id=book_record_id,
                    sync_record_id=sync_record_id,
                    record_date=record_date,
                    finish_reading=book.finish_reading,
                    now=now,
                )
                if written == "created":
                    counts["reading"] += 1
            elif is_today:
                print(
                    f"Skip 阅读记录 for {metadata.title} on {record_date}: "
                    "no last-read/finish evidence for this date."
                )

            if not should_fetch_notes:
                continue

            for highlight in _parse_note_payloads(bookmarks_payload, reviews_payload):
                fields = make_highlight_fields(
                    title=metadata.title,
                    highlight=highlight,
                    book_record_id=book_record_id,
                    sync_record_id=sync_record_id,
                    now=now,
                )
                existing = _find_existing_note(note_index, highlight.note_id, str(fields["内容哈希"]))
                if existing and str(existing.get("内容哈希") or "") == str(fields["内容哈希"]):
                    continue
                record_id = lark.upsert_record(
                    note_table_id,
                    fields,
                    record_id=str(existing.get("record_id")) if existing else None,
                )
                if not record_id and existing:
                    record_id = str(existing.get("record_id") or "")
                if not record_id:
                    record_id = lark.find_record_id(
                        note_table_id,
                        "内容哈希",
                        str(fields["内容哈希"]),
                    )
                if record_id:
                    indexed_record = {
                        "record_id": record_id,
                        "笔记标题": fields.get("笔记标题"),
                        "书籍": fields.get("书籍"),
                        "微信读书 Note ID": highlight.note_id,
                        "类型": fields.get("类型"),
                        "划线原文": fields.get("划线原文"),
                        "个人笔记": fields.get("个人笔记"),
                        "内容哈希": fields.get("内容哈希"),
                    }
                    new_or_changed_records.append(indexed_record)
                    note_index = _index_note_record(note_index, indexed_record, highlight.note_id)
                counts["highlights"] += 1

        tag_stats = _tag_note_records(lark, new_or_changed_records)
        counts["tags"] += tag_stats.tags_count
        if tag_stats.failed_notes:
            errors.append(f"本批次新增/变更笔记打标失败 {tag_stats.failed_notes} 条")

        daily_stats = {}
        try:
            daily_stats = _upsert_daily_reading_stats(lark, record_date, sync_record_id, client=client)
        except LarkBaseError as exc:
            errors.append(f"每日阅读统计写入失败：{exc}")

        status = "部分成功" if errors else "成功"
        _finish_sync_record(
            lark,
            sync_record_id,
            batch_id=batch_id,
            status=status,
            scope=f"日更最近活跃 {len(books)} 本书",
            selected_books=selected_books,
            started_at=started_at,
            counts=counts,
            error_summary="；".join(errors),
        )
        if errors:
            _notify_sync_failure(batch_id, "\n".join(errors))
        print(
            "Daily WeRead sync completed: "
            f"books={counts['books']}, reading_records={counts['reading']}, "
            f"new_or_changed_highlights={counts['highlights']}, tags={counts['tags']}, "
            f"daily_reading_minutes={daily_stats.get('当天阅读时长（分钟）', 0)}, "
            f"status={status}, batch_id={batch_id}"
        )
        return 0 if not errors else 8
    except (WeReadApiError, LarkBaseError) as exc:
        error_summary = str(exc)
        if sync_record_id:
            _finish_sync_record(
                lark,
                sync_record_id,
                batch_id=batch_id,
                status="失败",
                scope="日更最近活跃书籍",
                selected_books=selected_books,
                started_at=started_at,
                counts=counts,
                error_summary=error_summary,
            )
        _notify_sync_failure(batch_id, error_summary)
        print(f"Daily WeRead sync failed: {error_summary}")
        return 9


def _sync_catchup(limit: int, through_date: str = None, lookback_days: int = DEFAULT_CATCHUP_LOOKBACK_DAYS) -> int:
    through = _parse_iso_date(through_date) if through_date else date.today()
    lark = LarkBaseClient()
    existing_dates = _existing_daily_record_dates(lark)
    dates_to_run = _catchup_record_dates(existing_dates, through, lookback_days)
    if not dates_to_run:
        print("No daily snapshots need catch-up.")
        return 0

    print("Daily catch-up dates: " + ", ".join(d.isoformat() for d in dates_to_run))
    failed_dates = []
    for record_date in dates_to_run:
        result = _sync_daily(limit, record_date.isoformat())
        if result != 0:
            failed_dates.append(record_date.isoformat())

    if failed_dates:
        print("Daily catch-up failed for: " + ", ".join(failed_dates))
        return 9
    print("Daily catch-up completed.")
    return 0


def _sync_daily_stats(record_date: str = None, all_dates: bool = False) -> int:
    lark = LarkBaseClient()
    client = _weread_client_from_keychain()
    if all_dates:
        through = _parse_iso_date(record_date) if record_date else date.today()
        dates = _year_to_date_dates(through)
    else:
        dates = [_parse_iso_date(record_date) if record_date else date.today()]

    if not dates:
        print("No daily reading stats dates found.")
        return 0

    daily_seconds_by_date = _official_daily_read_seconds(client, dates)
    stats_index = _load_daily_stats_index(lark)

    failed_dates = []
    for stats_date in dates:
        try:
            fields = _upsert_daily_reading_stats(
                lark,
                stats_date.isoformat(),
                client=client,
                daily_seconds_by_date=daily_seconds_by_date,
                stats_index=stats_index,
            )
        except LarkBaseError as exc:
            print(f"Daily reading stats failed for {stats_date.isoformat()}: {exc}")
            failed_dates.append(stats_date.isoformat())
            continue
        print(
            "Daily reading stats updated: "
            f"date={stats_date.isoformat()}, minutes={fields.get('当天阅读时长（分钟）', 0)}, "
            f"status={fields.get('统计状态', '')}"
        )

    if failed_dates:
        print("Daily reading stats failed for: " + ", ".join(failed_dates))
        return 9
    return 0


def _sync_finished_books(limit: int = 0) -> int:
    started_at = _now_lark_datetime()
    batch_id = "weread-finished-" + datetime.now().strftime("%Y%m%d-%H%M%S")
    lark = LarkBaseClient()
    sync_table_id = lark.table_id("同步记录")
    sync_record_id = ""
    counts = {"books": 0, "reading": 0, "highlights": 0, "tags": 0}

    try:
        client = _weread_client_from_keychain()
        finished_items = _finished_shelf_items(client.get_shelf(), limit)
        selected_books = [item.metadata.title for item in finished_items]
        sync_record_id = lark.upsert_record(
            sync_table_id,
            make_sync_fields(
                batch_id=batch_id,
                status="运行中",
                scope=f"同步读完书籍 {len(finished_items)} 本",
                selected_books=selected_books,
                started_at=started_at,
                ended_at=None,
                books_count=0,
                reading_records_count=0,
                highlights_count=0,
                tags_count=0,
                error_summary="",
            ),
        )
        if not sync_record_id:
            sync_record_id = _find_record_id_with_retry(
                lark,
                sync_table_id,
                "同步批次 ID",
                batch_id,
            )
        if not sync_record_id:
            print("Failed to locate the finished-books sync record after creating it.")
            return 5

        book_index = _load_book_index(lark)
        print(_reading_record_scope_message())
        for item in finished_items:
            now = _now_lark_datetime()
            progress = _effective_progress(item.progress, True)
            book_record_id = _upsert_book(
                lark,
                item.metadata,
                progress,
                now,
                reading_status="读完",
                book_index=book_index,
            )
            counts["books"] += 1
            finish_date = _epoch_to_date(progress.finish_time) or _epoch_to_date(progress.last_read_time)
            if finish_date:
                written = _upsert_reading_record_if_missing(
                    lark,
                    metadata=item.metadata,
                    progress=progress,
                    book_record_id=book_record_id,
                    sync_record_id=sync_record_id,
                    record_date=finish_date.isoformat(),
                    finish_reading=True,
                    now=now,
                )
                if written == "created":
                    counts["reading"] += 1
            else:
                print(f"Skip 阅读记录 for finished book {item.metadata.title}: no finish or last-read date.")

        _finish_sync_record(
            lark,
            sync_record_id,
            batch_id=batch_id,
            status="成功",
            scope=f"同步读完书籍 {len(finished_items)} 本",
            selected_books=selected_books,
            started_at=started_at,
            counts=counts,
            error_summary="",
        )
        print(f"Finished WeRead books sync completed: books={counts['books']}, batch_id={batch_id}")
        return 0
    except (WeReadApiError, LarkBaseError) as exc:
        error_summary = str(exc)
        if sync_record_id:
            _finish_sync_record(
                lark,
                sync_record_id,
                batch_id=batch_id,
                status="失败",
                scope="同步读完书籍",
                selected_books=[],
                started_at=started_at,
                counts=counts,
                error_summary=error_summary,
            )
        _notify_sync_failure(batch_id, error_summary)
        print(f"Finished WeRead books sync failed: {error_summary}")
        return 9


def _backfill_recent(since: str, dry_run: bool = False, untrusted_last_read_on: str = None, known_last_reads: dict = None) -> int:
    """Add missing 书籍 and last-read 阅读记录 for books read since the given date.

    Existing 阅读记录 rows are never overwritten. Live shelf last-read timestamps that fall
    on untrusted_last_read_on are replaced with an earlier Bitable or known last-read date
    when available, and skipped (with a warning) when not.
    """
    since_date = _parse_iso_date(since)
    untrusted = _parse_iso_date(untrusted_last_read_on) if untrusted_last_read_on else None
    known_last_reads = known_last_reads or {}
    started_at = _now_lark_datetime()
    batch_id = "weread-backfill-" + datetime.now().strftime("%Y%m%d-%H%M%S")
    lark = LarkBaseClient()
    client = _weread_client_from_keychain()
    # Match the verified backfill order: touch notebooks before reading the shelf.
    client.get_notebooks()
    shelf_items = client.get_shelf()
    print(_reading_record_scope_message())

    book_index = _load_book_index(lark)
    book_records = {
        str(record.get("微信读书 Book ID") or ""): record
        for record in lark.list_records(
            lark.table_id("书籍"),
            ["书名", "微信读书 Book ID", "最近阅读时间"],
        )
        if record.get("微信读书 Book ID")
    }

    planned = []
    skipped = []
    for item in shelf_items:
        if not isinstance(item, WeReadShelfItem) or not item.metadata.book_id:
            continue
        existing = book_records.get(item.metadata.book_id)
        preferred = None
        title = item.metadata.title
        for key, value in known_last_reads.items():
            if key == title or key in title or title in key:
                preferred = value if isinstance(value, date) else _parse_iso_date(str(value))
                break
        trusted = _trusted_last_read_date(
            item.progress,
            existing_book_record=existing,
            preferred_date=preferred,
            untrusted_on=untrusted,
        )
        finish_date = _epoch_to_date(item.progress.finish_time)
        evidence_dates = []
        if trusted and trusted >= since_date:
            evidence_dates.append(trusted)
        if finish_date and finish_date >= since_date and finish_date not in evidence_dates:
            evidence_dates.append(finish_date)
        if not evidence_dates:
            live = _epoch_to_date(item.progress.last_read_time)
            if live and untrusted is not None and live == untrusted and not trusted:
                skipped.append(
                    {
                        "title": title,
                        "reason": f"live last-read is {live.isoformat()} which is untrusted and no earlier date is available",
                    }
                )
            continue
        planned.append(
            {
                "title": title,
                "book_id": item.metadata.book_id,
                "item": item,
                "trusted": trusted,
                "evidence_dates": evidence_dates,
                "book_exists": bool(existing or book_index.get(item.metadata.book_id)),
                "finish_reading": item.finish_reading,
            }
        )

    print(
        f"Backfill since {since_date.isoformat()}: {len(planned)} books with evidence, "
        f"{len(skipped)} skipped for untrusted last-read."
    )
    for row in skipped:
        print(f"  skip {row['title']}: {row['reason']}")

    if dry_run:
        for row in planned:
            book_action = "keep 书籍" if row["book_exists"] else "create 书籍"
            print(
                f"  {row['title']}: {book_action}; 阅读记录 dates="
                + ", ".join(d.isoformat() for d in row["evidence_dates"])
                + f"; finished={row['finish_reading']}"
            )
        return 0

    sync_table_id = lark.table_id("同步记录")
    sync_record_id = lark.upsert_record(
        sync_table_id,
        make_sync_fields(
            batch_id=batch_id,
            status="运行中",
            scope=f"补齐 {since_date.isoformat()} 起阅读书籍与末次阅读记录",
            selected_books=[row["title"] for row in planned],
            started_at=started_at,
            ended_at=None,
            books_count=0,
            reading_records_count=0,
            highlights_count=0,
            tags_count=0,
            error_summary="",
        ),
    )
    if not sync_record_id:
        sync_record_id = _find_record_id_with_retry(
            lark,
            sync_table_id,
            "同步批次 ID",
            batch_id,
        )
    if not sync_record_id:
        print("Failed to locate the backfill sync record after creating it.")
        return 5
    counts = {"books": 0, "reading": 0, "highlights": 0, "tags": 0}
    errors = []
    now = _now_lark_datetime()
    for row in planned:
        item = row["item"]
        try:
            progress = item.progress
            if row["trusted"] is not None:
                progress = _progress_with_trusted_last_read(progress, item.finish_reading, row["trusted"])
            progress = _effective_progress(progress, item.finish_reading)
            existed = bool(book_index.get(item.metadata.book_id))
            book_record_id = _upsert_book(
                lark,
                item.metadata,
                progress,
                now,
                reading_status=_reading_status(progress, item.finish_reading),
                book_index=book_index,
            )
            if not existed:
                counts["books"] += 1
            for evidence_date in row["evidence_dates"]:
                written = _upsert_reading_record_if_missing(
                    lark,
                    metadata=item.metadata,
                    progress=progress,
                    book_record_id=book_record_id,
                    sync_record_id=sync_record_id or "",
                    record_date=evidence_date.isoformat(),
                    finish_reading=item.finish_reading,
                    now=now,
                )
                if written == "created":
                    counts["reading"] += 1
                    print(f"  created 阅读记录 {item.metadata.title} / {evidence_date.isoformat()}")
                else:
                    print(f"  kept existing 阅读记录 {item.metadata.title} / {evidence_date.isoformat()}")
        except (WeReadApiError, LarkBaseError) as exc:
            errors.append(f"{item.metadata.title}: {exc}")
            print(f"  failed {item.metadata.title}: {type(exc).__name__}")

    status = "部分成功" if errors else "成功"
    if sync_record_id:
        _finish_sync_record(
            lark,
            sync_record_id,
            batch_id=batch_id,
            status=status,
            scope=f"补齐 {since_date.isoformat()} 起阅读书籍与末次阅读记录",
            selected_books=[row["title"] for row in planned],
            started_at=started_at,
            counts=counts,
            error_summary="；".join(str(item) for item in errors)[:500],
        )
    print(
        f"Backfill completed: new_books={counts['books']}, new_reading_records={counts['reading']}, "
        f"status={status}, batch_id={batch_id}"
    )
    return 0 if not errors else 8


def _existing_daily_record_dates(lark: LarkBaseClient) -> set:
    records = lark.list_records(lark.table_id("阅读记录"), ["记录日期"])
    dates = set()
    for record in records:
        record_date = _date_from_lark_value(record.get("记录日期"))
        if record_date:
            dates.add(record_date)
    return dates


def _upsert_daily_reading_stats(
    lark: LarkBaseClient,
    record_date: str,
    sync_record_id: str = "",
    client=None,
    daily_seconds_by_date: dict = None,
    stats_index: dict = None,
) -> dict:
    if daily_seconds_by_date is None:
        client = client or _weread_client_from_keychain()
        daily_seconds_by_date = _official_daily_read_seconds(client, [_parse_iso_date(record_date)])
    stats = _compute_daily_reading_stats_from_seconds(
        daily_seconds_by_date.get(record_date, 0)
    )
    stats_key = daily_stats_key(record_date)
    fields = make_daily_stats_fields(
        record_date=record_date,
        stats_key=stats_key,
        duration_seconds=stats["duration_seconds"],
        duration_minutes=stats["duration_minutes"],
        effective_reading_day=stats["effective_reading_day"],
        status=stats["status"],
        now=_now_lark_datetime(),
        sync_record_id=sync_record_id,
        note=stats["note"],
    )
    stats_table_id = lark.table_id("每日阅读统计")
    existing_record_id = ""
    if stats_index is not None:
        existing_record_id = stats_index.get(stats_key, "")
    if not existing_record_id:
        existing_record_id = lark.find_record_id(
            stats_table_id,
            search_field="日级统计键",
            keyword=stats_key,
        )
    lark.upsert_record(
        stats_table_id,
        fields,
        record_id=existing_record_id or None,
    )
    return fields


def _load_daily_stats_index(lark: LarkBaseClient) -> dict:
    stats_table_id = lark.table_id("每日阅读统计")
    records = lark.list_records(stats_table_id, ["日级统计键"])
    return {
        str(record.get("日级统计键") or ""): str(record.get("record_id") or "")
        for record in records
        if record.get("日级统计键") and record.get("record_id")
    }


def _official_daily_read_seconds(client, dates: list) -> dict:
    requested_dates = sorted(set(dates))
    result = {day.isoformat(): 0 for day in requested_dates}
    months = sorted({(day.year, day.month) for day in requested_dates})
    for year, month in months:
        month_base_time = int(datetime(year, month, 1).timestamp())
        read_data = client.get_read_data_detail(mode="monthly", base_time=month_base_time)
        result.update(
            {
                day: seconds
                for day, seconds in read_data.daily_read_seconds.items()
                if day in result
            }
        )
    return result


def _compute_daily_reading_stats_from_seconds(duration_seconds: int) -> dict:
    duration_seconds = max(int(duration_seconds or 0), 0)
    duration_minutes = round(duration_seconds / 60, 2)
    effective_reading_day = 1 if duration_seconds >= 60 else 0
    status = "官方统计" if duration_seconds > 0 else "无阅读"
    note = (
        "来自微信读书 /readdata/detail 的 readTimes；原始单位为秒，分钟字段由秒数除以 60 后保留两位。"
    )
    return {
        "duration_seconds": duration_seconds,
        "duration_minutes": duration_minutes,
        "effective_reading_day": effective_reading_day,
        "status": status,
        "note": note,
    }


def _year_to_date_dates(through_date_value: date) -> list:
    current = date(through_date_value.year, 1, 1)
    dates = []
    while current <= through_date_value:
        dates.append(current)
        current += timedelta(days=1)
    return dates


def _catchup_record_dates(existing_dates: set, through_date_value: date, lookback_days: int) -> list:
    if lookback_days <= 0:
        lookback_days = DEFAULT_CATCHUP_LOOKBACK_DAYS
    window_start = through_date_value - timedelta(days=lookback_days - 1)
    if existing_dates:
        earliest_existing = min(existing_dates)
        window_start = max(window_start, earliest_existing)
    else:
        window_start = through_date_value

    dates = []
    current = window_start
    while current <= through_date_value:
        if current not in existing_dates or current == through_date_value:
            dates.append(current)
        current += timedelta(days=1)
    return dates


def _date_from_lark_value(value):
    if isinstance(value, str) and len(value) >= 10:
        try:
            return _parse_iso_date(value[:10])
        except ValueError:
            return None
    return None


def _parse_iso_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def _tag_weread(limit: int) -> int:
    lark = LarkBaseClient()
    note_table_id = lark.table_id("笔记划线")
    note_records = lark.list_records(
        note_table_id,
        ["笔记标题", "书籍", "划线原文", "个人笔记", "标签主题", "内容哈希"],
    )
    candidates = _taggable_note_records(note_records, limit=limit)
    if not candidates:
        print("No untagged highlights need AI tagging.")
        return 0

    stats = _tag_note_records(lark, candidates)
    _update_latest_sync_tag_count(lark, stats.tags_count)
    print(
        "AI tagging completed: "
        f"candidate_notes={stats.candidate_notes}, tagged_notes={stats.tagged_notes}, "
        f"failed_notes={stats.failed_notes}, tags={stats.tags_count}"
    )
    return 0 if stats.failed_notes == 0 else 7


def _retry_tagging(limit: int) -> int:
    lark = LarkBaseClient()
    note_table_id = lark.table_id("笔记划线")
    note_records = lark.list_records(
        note_table_id,
        ["笔记标题", "书籍", "划线原文", "个人笔记", "标签主题", "内容哈希"],
    )
    candidates = _taggable_note_records(note_records, limit=limit)
    if not candidates:
        print("No untagged highlights need retry.")
        return 0

    stats = _tag_note_records(lark, candidates)
    print(
        "AI tagging retry completed: "
        f"candidate_notes={stats.candidate_notes}, tagged_notes={stats.tagged_notes}, "
        f"failed_notes={stats.failed_notes}, tags={stats.tags_count}"
    )
    return 0 if stats.failed_notes == 0 else 7


def _tag_note_records(lark: LarkBaseClient, records: list) -> TaggingStats:
    total = TaggingStats(candidate_notes=len(records))
    if not records:
        return total
    try:
        stats, failed_record_ids, tag_ids = _tag_note_records_once(lark, records)
    except (LLMError, TaggingServiceError, TaggingParseError):
        total.failed_notes = len(records)
        return total

    total.tagged_notes = stats.tagged_notes
    total.failed_notes = len(failed_record_ids)
    total.tags_count = len(tag_ids)
    return total


def _tag_note_records_once(lark: LarkBaseClient, note_records: list):
    note_table_id = lark.table_id("笔记划线")
    tag_table_id = lark.table_id("标签主题")
    now = _now_lark_datetime()
    notes = [
        TaggingNote(
            note_key=str(record["record_id"]),
            title=str(record.get("笔记标题") or ""),
            text=_note_text_for_tagging(record),
        )
        for record in note_records
    ]
    note_book_record_ids = {
        str(record["record_id"]): _link_record_ids(record.get("书籍")) for record in note_records
    }
    existing_tag_records = lark.list_records(
        tag_table_id,
        ["标签名", "规范化名称"],
    )
    existing_tags = [str(record.get("标签名") or "") for record in existing_tag_records if record.get("标签名")]
    tag_record_ids = {
        normalize_label_name(str(record.get("规范化名称") or "")): str(record.get("record_id") or "")
        for record in existing_tag_records
        if record.get("规范化名称") and record.get("record_id")
    }

    prompt = build_tagging_prompt(notes=notes, existing_tags=existing_tags)
    llm_client = OpenAICompatibleClient(
        LLMConfig.from_env(api_key=_load_llm_api_key())
    )
    result = run_api_tagging(
        prompt,
        valid_note_keys={note.note_key for note in notes},
        client=llm_client,
    )

    current_tag_record_ids = set()
    for tag in result.book_tags:
        normalized = normalize_label_name(tag.name)
        if not normalized:
            continue
        record_id = tag_record_ids.get(normalized)
        if not record_id:
            record_id = lark.find_record_id(
                tag_table_id,
                "规范化名称",
                normalized,
            )
        if not record_id:
            record_id = lark.upsert_record(
                tag_table_id,
                make_tag_fields(tag, now=now),
            ) or lark.find_record_id(
                tag_table_id,
                "规范化名称",
                normalized,
            )
        if record_id:
            tag_record_ids[normalized] = record_id
            current_tag_record_ids.add(record_id)

    tagged_notes = 0
    tagged_note_keys = set()
    book_tag_record_ids = {}
    for note_tag in result.note_tags:
        linked_tag_ids = []
        for tag_name in note_tag.tags:
            normalized = normalize_label_name(tag_name)
            record_id = tag_record_ids.get(normalized)
            if record_id:
                current_tag_record_ids.add(record_id)
                linked_tag_ids.append({"id": record_id})
        if not linked_tag_ids:
            continue
        for book_record_id in note_book_record_ids.get(note_tag.note_key, []):
            book_tag_record_ids.setdefault(book_record_id, set()).update(
                tag_link["id"] for tag_link in linked_tag_ids
            )
        lark.upsert_record(
            note_table_id,
            {"标签主题": linked_tag_ids},
            record_id=note_tag.note_key,
        )
        tagged_notes += 1
        tagged_note_keys.add(note_tag.note_key)

    for book_record_id, tag_ids in book_tag_record_ids.items():
        lark.upsert_record(
            lark.table_id("书籍"),
            {"书籍级标签": [{"id": tag_id} for tag_id in sorted(tag_ids)]},
            record_id=book_record_id,
        )

    failed_record_ids = {
        str(record["record_id"]) for record in note_records if str(record["record_id"]) not in tagged_note_keys
    }
    return (
        TaggingStats(candidate_notes=len(note_records), tagged_notes=tagged_notes),
        failed_record_ids,
        current_tag_record_ids,
    )


@dataclass
class DailySyncBook:
    """A book considered for daily/catchup sync, with last-read captured before later API calls."""

    book_id: str
    title: str
    metadata: BookMetadata
    progress: WeReadProgress
    finish_reading: bool = False
    from_notebook: bool = False
    notebook: object = None


def _epoch_to_date(value):
    if value is None:
        return None
    try:
        timestamp = int(value)
    except (TypeError, ValueError):
        return None
    if timestamp <= 0:
        return None
    if timestamp > 10 ** 12:
        timestamp = int(timestamp / 1000)
    try:
        return datetime.fromtimestamp(timestamp).date()
    except (OSError, OverflowError, ValueError):
        return None


def _has_reading_evidence_on(progress, record_date: date, per_book_days=None) -> bool:
    """Return True if we have evidence the book was read on record_date.

    The official WeRead gateway does not currently expose per-book daily history,
    so the default evidence is the last-read date and, if present, the finish date.
    """
    if per_book_days is not None:
        return record_date in per_book_days or record_date.isoformat() in per_book_days
    last_read = _epoch_to_date(progress.last_read_time)
    finish = _epoch_to_date(progress.finish_time)
    return last_read == record_date or finish == record_date


def _is_recently_read_or_in_progress(progress, finish_reading: bool, as_of: date, recent_days: int) -> bool:
    if recent_days <= 0:
        recent_days = DEFAULT_CATCHUP_LOOKBACK_DAYS
    window_start = as_of - timedelta(days=recent_days - 1)
    for event in (_epoch_to_date(progress.last_read_time), _epoch_to_date(progress.finish_time)):
        if event and window_start <= event <= as_of:
            return True
    return False


def _effective_progress(progress, finish_reading: bool = False):
    if not finish_reading:
        return progress
    if progress.progress >= 95 or progress.finish_time:
        return progress
    return WeReadProgress(
        progress=100,
        reading_minutes=progress.reading_minutes,
        last_read_time=progress.last_read_time,
        finish_time=progress.finish_time or progress.last_read_time,
    )


def _progress_with_trusted_last_read(progress, finish_reading: bool, trusted_last_read: date):
    trusted_epoch = int(
        datetime(trusted_last_read.year, trusted_last_read.month, trusted_last_read.day, 12, 0, 0).timestamp()
    )
    adjusted = WeReadProgress(
        progress=progress.progress,
        reading_minutes=progress.reading_minutes,
        last_read_time=trusted_epoch,
        finish_time=progress.finish_time or (trusted_epoch if finish_reading else None),
    )
    return _effective_progress(adjusted, finish_reading)


def _select_daily_sync_books(notebooks: list, shelf_items: list, as_of: date, limit: int, recent_days: int = DEFAULT_CATCHUP_LOOKBACK_DAYS) -> list:
    """Notebooks plus shelf books that were recently read or are still in progress."""
    selected = {}
    for item in shelf_items or []:
        if not isinstance(item, WeReadShelfItem):
            continue
        book_id = item.metadata.book_id
        if not book_id:
            continue
        if not _is_recently_read_or_in_progress(item.progress, item.finish_reading, as_of, recent_days):
            continue
        selected[book_id] = DailySyncBook(
            book_id=book_id,
            title=item.metadata.title,
            metadata=item.metadata,
            progress=item.progress,
            finish_reading=item.finish_reading,
            from_notebook=False,
        )

    for book in notebooks or []:
        book_id = getattr(book, "book_id", "")
        if not book_id:
            continue
        if book_id in selected:
            selected[book_id].from_notebook = True
            selected[book_id].notebook = book
            continue
        notebook_progress = WeReadProgress(last_read_time=getattr(book, "last_read_time", 0) or None)
        selected[book_id] = DailySyncBook(
            book_id=book_id,
            title=book.title,
            metadata=BookMetadata(
                book_id=book.book_id,
                title=book.title,
                author=getattr(book, "author", ""),
                cover=getattr(book, "cover", ""),
            ),
            progress=notebook_progress,
            finish_reading=False,
            from_notebook=True,
            notebook=book,
        )

    recent_ids = [
        book.book_id
        for book in selected.values()
        if _is_recently_read_or_in_progress(book.progress, book.finish_reading, as_of, recent_days)
    ]
    older_notebook_ids = [
        book_id
        for book_id, book in selected.items()
        if book.from_notebook and book_id not in set(recent_ids)
    ]
    older_notebook_ids = sorted(
        older_notebook_ids,
        key=lambda book_id: selected[book_id].progress.last_read_time or 0,
        reverse=True,
    )
    if limit > 0:
        # Never drop recently read/in-progress shelf books; cap only older notebooks.
        kept_old = older_notebook_ids[: max(0, limit - len(recent_ids))]
        keep = set(recent_ids) | set(kept_old)
        selected = {book_id: book for book_id, book in selected.items() if book_id in keep}

    return sorted(
        selected.values(),
        key=lambda book: book.progress.last_read_time or 0,
        reverse=True,
    )


def _reading_record_scope_message() -> str:
    return (
        "Reading-record evidence uses last-read/finish dates only; "
        "WeRead gateway did not provide per-book daily history."
    )


def _trusted_last_read_date(progress, existing_book_record=None, preferred_date=None, untrusted_on=None):
    """Prefer a previously stored or audited last-read date when the live shelf timestamp is untrusted."""
    live = _epoch_to_date(progress.last_read_time)
    stored = None
    if existing_book_record:
        stored = _date_from_lark_value(existing_book_record.get("最近阅读时间"))
    if preferred_date is not None and live == untrusted_on:
        return preferred_date
    if stored is not None and untrusted_on is not None and live == untrusted_on:
        return stored
    if preferred_date is not None and (live is None or (untrusted_on is not None and live == untrusted_on)):
        return preferred_date
    return live


def _upsert_reading_record_if_missing(
    lark,
    *,
    metadata,
    progress,
    book_record_id: str,
    sync_record_id: str,
    record_date: str,
    finish_reading: bool = False,
    now: str = None,
) -> str:
    """Create a daily 阅读记录 when none exists. Never overwrites an existing row."""
    now = now or _now_lark_datetime()
    effective = _effective_progress(progress, finish_reading)
    progress_key = daily_record_key(metadata.book_id, record_date)
    table_id = lark.table_id("阅读记录")
    existing = lark.find_record_id(table_id, search_field="日级记录键", keyword=progress_key)
    if existing:
        return "exists"
    lark.upsert_record(
        table_id,
        make_progress_fields(
            title=metadata.title,
            book_id=metadata.book_id,
            book_record_id=book_record_id,
            sync_record_id=sync_record_id,
            progress=effective,
            now=now,
            record_date=record_date,
            daily_key=progress_key,
        ),
    )
    return "created"


def _active_books(books: list, limit: int) -> list:
    sorted_books = sorted(books, key=lambda book: book.last_read_time or 0, reverse=True)
    if limit <= 0:
        return sorted_books
    return sorted_books[:limit]


def _finished_shelf_items(items: list, limit: int = 0) -> list:
    finished = [
        item
        for item in items
        if isinstance(item, WeReadShelfItem) and (item.finish_reading or item.progress.progress >= 95)
    ]
    finished = sorted(finished, key=lambda item: item.progress.last_read_time or 0, reverse=True)
    if limit <= 0:
        return finished
    return finished[:limit]


def _parse_note_payloads(bookmarks_payload, reviews_payload) -> list:
    return parse_bookmarks_payload(bookmarks_payload) + parse_reviews_payload(reviews_payload)


def _load_book_index(lark: LarkBaseClient) -> dict:
    records = lark.list_records(lark.table_id("书籍"), ["微信读书 Book ID"])
    result = {}
    for record in records:
        book_id = str(record.get("微信读书 Book ID") or "")
        record_id = str(record.get("record_id") or "")
        if book_id and record_id and book_id not in result:
            result[book_id] = record_id
    return result


def _find_book_record_id(lark: LarkBaseClient, book_id: str, book_index: dict = None) -> str:
    if not book_id:
        return ""
    if book_index is not None and book_id in book_index:
        return book_index[book_id]
    record_id = lark.find_record_id(lark.table_id("书籍"), "微信读书 Book ID", book_id)
    if record_id:
        return record_id
    return _load_book_index(lark).get(book_id, "")


def _find_record_id_with_retry(
    lark: LarkBaseClient,
    table_id: str,
    search_field: str,
    keyword: str,
    attempts: int = 3,
    delay_seconds: float = 1,
    sleeper=time.sleep,
) -> str:
    total_attempts = max(attempts, 1)
    for attempt in range(total_attempts):
        record_id = lark.find_record_id(table_id, search_field, keyword)
        if record_id:
            return record_id
        if attempt + 1 < total_attempts:
            sleeper(delay_seconds)
    return ""


def _upsert_book(
    lark: LarkBaseClient,
    metadata,
    progress,
    now: str,
    reading_status: str = None,
    book_index: dict = None,
) -> str:
    book_table_id = lark.table_id("书籍")
    existing_book_record_id = _find_book_record_id(lark, metadata.book_id, book_index)
    book_record_id = lark.upsert_record(
        book_table_id,
        make_book_fields(
            metadata,
            now=now,
            last_read_time=epoch_to_lark_datetime(progress.last_read_time),
            reading_status=reading_status or _reading_status(progress),
            reading_progress=progress.progress,
        ),
        record_id=existing_book_record_id or None,
    )
    if book_record_id and book_index is not None:
        book_index[metadata.book_id] = book_record_id
    if not book_record_id:
        book_record_id = _find_book_record_id(lark, metadata.book_id, book_index)
    if not book_record_id:
        raise LarkBaseError(f"failed to locate Lark book record for {metadata.title}")
    return book_record_id


def _sync_covers(limit: int = 0) -> int:
    lark = LarkBaseClient()
    records = _list_book_cover_records(lark)
    uploaded = 0
    skipped_existing = 0
    skipped_missing_url = 0
    failed = 0
    processed = 0

    for record in records:
        if limit > 0 and processed >= limit:
            break
        processed += 1
        metadata = _metadata_from_book_record(record)
        if has_cover_attachment(record.get("封面图片")):
            skipped_existing += 1
            continue
        if not metadata.cover:
            skipped_missing_url += 1
            continue
        if _upload_missing_book_cover(lark, str(record.get("record_id") or ""), metadata, record):
            uploaded += 1
        else:
            failed += 1

    print(
        "Book cover attachment sync completed: "
        f"uploaded={uploaded}, skipped_existing={skipped_existing}, "
        f"skipped_missing_url={skipped_missing_url}, failed={failed}"
    )
    return 0 if failed == 0 else 7


def _list_book_cover_records(lark: LarkBaseClient) -> list:
    return lark.list_records(
        lark.table_id("书籍"),
        ["书名", "微信读书 Book ID", "作者", "封面", "封面图片"],
    )


def _metadata_from_book_record(record: dict) -> BookMetadata:
    return BookMetadata(
        book_id=str(record.get("微信读书 Book ID") or ""),
        title=str(record.get("书名") or ""),
        author=str(record.get("作者") or ""),
        cover=str(record.get("封面") or ""),
    )


def _upload_missing_book_cover(
    lark: LarkBaseClient,
    book_record_id: str,
    metadata: BookMetadata,
    current_record: dict = None,
) -> bool:
    if not book_record_id or not metadata.cover:
        return False
    if current_record and has_cover_attachment(current_record.get("封面图片")):
        return False

    with tempfile.TemporaryDirectory(prefix="reading-manager-covers-") as temp_dir:
        try:
            cover_path = download_cover_image(
                metadata.cover,
                Path(temp_dir),
                title=metadata.title,
                book_id=metadata.book_id,
            )
            payload = lark.upload_attachment(
                lark.table_id("书籍"),
                book_record_id,
                "封面图片",
                cover_path,
            )
            file_token = extract_attachment_file_token(payload, "封面图片", cover_path.name)
            if file_token:
                lark.replace_attachment_field(
                    lark.table_id("书籍"),
                    book_record_id,
                    "封面图片",
                    [file_token],
                )
            return True
        except (CoverImageError, LarkBaseError) as exc:
            print(f"Book cover attachment skipped for {metadata.title or metadata.book_id}: {exc}")
            return False


def _finish_sync_record(
    lark: LarkBaseClient,
    sync_record_id: str,
    batch_id: str,
    status: str,
    scope: str,
    selected_books: list,
    started_at: str,
    counts: dict,
    error_summary: str,
) -> None:
    lark.upsert_record(
        lark.table_id("同步记录"),
        make_sync_fields(
            batch_id=batch_id,
            status=status,
            scope=scope,
            selected_books=selected_books,
            started_at=started_at,
            ended_at=_now_lark_datetime(),
            books_count=counts.get("books", 0),
            reading_records_count=counts.get("reading", 0),
            highlights_count=counts.get("highlights", 0),
            tags_count=counts.get("tags", 0),
            error_summary=error_summary,
        ),
        record_id=sync_record_id,
    )


def _list_note_records(lark: LarkBaseClient) -> list:
    return lark.list_records(
        lark.table_id("笔记划线"),
        ["笔记标题", "书籍", "微信读书 Note ID", "类型", "划线原文", "个人笔记", "标签主题", "内容哈希"],
    )


def _load_note_index(lark: LarkBaseClient) -> dict:
    index = {"by_note_id": {}, "by_hash": {}}
    for record in _list_note_records(lark):
        index = _index_note_record(index, record, str(record.get("微信读书 Note ID") or ""))
    return index


def _index_note_record(index: dict, record: dict, note_id: str) -> dict:
    record_id = str(record.get("record_id") or "")
    if note_id:
        index["by_note_id"][note_id] = record
    content_hash = str(record.get("内容哈希") or "")
    if content_hash:
        index["by_hash"][content_hash] = record
    if record_id:
        record["record_id"] = record_id
    return index


def _find_existing_note(index: dict, note_id: str, content_hash: str) -> dict:
    if note_id and note_id in index["by_note_id"]:
        return index["by_note_id"][note_id]
    if content_hash and content_hash in index["by_hash"]:
        return index["by_hash"][content_hash]
    return {}


def _taggable_note_records(records: list, limit: int) -> list:
    result = []
    for record in records:
        if _link_record_ids(record.get("标签主题")):
            continue
        text = _note_text_for_tagging(record)
        if not text:
            continue
        result.append(record)
    if limit <= 0:
        return result
    return result[:limit]


def _note_text_for_tagging(record: dict) -> str:
    parts = [record.get("划线原文"), record.get("个人笔记")]
    return "\n".join(str(part).strip() for part in parts if str(part or "").strip())


def _notify_sync_failure(batch_id: str, detail: str) -> None:
    user_id = os.environ.get("READING_MANAGER_LARK_NOTIFY_USER_ID", "").strip()
    chat_id = os.environ.get("READING_MANAGER_LARK_NOTIFY_CHAT_ID", "").strip()
    if not user_id and not chat_id:
        print("Lark notification skipped: no READING_MANAGER_LARK_NOTIFY_USER_ID or CHAT_ID configured.")
        return
    text = (
        "读书管理同步需要处理\n"
        f"批次：{batch_id}\n"
        f"问题：{detail}\n"
        "下次日更会自动重试未打标笔记；也可以在本机运行 retry-tagging。"
    )
    try:
        LarkImClient().send_text(
            text=text,
            user_id=user_id or None,
            chat_id=chat_id or None,
            idempotency_key=f"reading-manager-{batch_id}",
        )
    except LarkImError as exc:
        print(f"Lark notification failed: {exc}")


def _install_launchagent(install: bool) -> int:
    project_root = Path(__file__).resolve().parents[2]
    runtime_path = default_launchagent_runtime_path(Path.home())
    log_path = default_launchagent_log_path(Path.home())
    target_path = default_launchagent_path(Path.home())
    plist_bytes = build_launchagent_plist(project_root, runtime_root=runtime_path, log_path=log_path)
    if not install:
        print(f"LaunchAgent target: {target_path}")
        print(f"Runtime copy target: {runtime_path}")
        print(f"Log target: {log_path}")
        print(plist_bytes.decode("utf-8"))
        print("Run with --install to write the plist.")
        return 0
    sync_runtime_project(project_root, runtime_path)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_bytes(plist_bytes)
    print(f"LaunchAgent plist written: {target_path}")
    print(f"Runtime copy written: {runtime_path}")
    print(f"LaunchAgent log target: {log_path}")
    print("Use launchctl load or log out and back in to let macOS schedule it.")
    return 0


def _weread_client_from_keychain():
    api_key = _load_weread_api_key()
    if api_key:
        return WeReadGatewayClient(api_key)

    raw_secret = load_secret_from_keychain(WEREAD_KEYCHAIN_SERVICE, WEREAD_KEYCHAIN_ACCOUNT)
    if raw_secret is None:
        raise WeReadApiError(
            "No WeRead credential found. Run auth-weread-api-key for the official gateway, "
            "or auth-weread after browser login as a temporary fallback."
        )
    return WeReadClient(json.loads(raw_secret), on_cookies_refreshed=_save_weread_cookies)


def _load_weread_api_key() -> str:
    api_key = os.environ.get(WEREAD_API_KEY_ENV, "").strip()
    if api_key:
        return api_key
    stored_api_key = load_secret_from_keychain(WEREAD_API_KEYCHAIN_SERVICE, WEREAD_KEYCHAIN_ACCOUNT)
    return stored_api_key.strip() if stored_api_key else ""


def _load_llm_api_key() -> str:
    api_key = os.environ.get(LLM_API_KEY_ENV, "").strip()
    if api_key:
        return api_key
    stored_api_key = load_secret_from_keychain(LLM_API_KEYCHAIN_SERVICE, WEREAD_KEYCHAIN_ACCOUNT)
    return stored_api_key.strip() if stored_api_key else ""


def _format_book(book) -> str:
    counts = f"notes={book.note_count}, reviews={book.review_count}"
    author = f" / {book.author}" if book.author else ""
    return f"- {book.title}{author} ({counts}, book_id={book.book_id})"


def _now_lark_datetime() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _reading_status(progress, finish_reading: bool = False) -> str:
    if finish_reading or progress.finish_time or progress.progress >= 95:
        return "读完"
    if progress.progress > 0 or progress.last_read_time:
        return "在读"
    return "未知"


def _select_text(value) -> str:
    if isinstance(value, list) and value:
        return str(value[0])
    return str(value or "")


def _link_record_ids(value) -> list:
    if not isinstance(value, list):
        return []
    record_ids = []
    for item in value:
        if isinstance(item, dict) and item.get("id"):
            record_ids.append(str(item["id"]))
    return record_ids


def _update_latest_sync_tag_count(lark: LarkBaseClient, tags_count: int) -> None:
    sync_table_id = lark.table_id("同步记录")
    records = lark.list_records(
        sync_table_id,
        ["同步批次 ID", "同步状态"],
    )
    success_records = [record for record in records if _select_text(record.get("同步状态")) == "成功"]
    if not success_records:
        return
    lark.upsert_record(
        sync_table_id,
        {"写入标签数": tags_count},
        record_id=str(success_records[-1]["record_id"]),
    )


if __name__ == "__main__":
    raise SystemExit(main())
