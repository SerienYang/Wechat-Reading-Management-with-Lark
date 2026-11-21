import io
import os
import unittest
from datetime import date, datetime
from unittest.mock import patch

from reading_manager.cli import (
    LLM_API_KEYCHAIN_SERVICE,
    WEREAD_API_KEYCHAIN_SERVICE,
    WEREAD_KEYCHAIN_ACCOUNT,
    _active_books,
    _auth_llm_api_key,
    _auth_weread_api_key,
    _compute_daily_reading_stats_from_seconds,
    _finished_shelf_items,
    _has_reading_evidence_on,
    _is_recently_read_or_in_progress,
    _find_record_id_with_retry,
    _load_llm_api_key,
    _load_weread_api_key,
    _official_daily_read_seconds,
    _run_daily_job,
    _select_daily_sync_books,
    _trusted_last_read_date,
    _upsert_reading_record_if_missing,
    _tag_note_records,
    _taggable_note_records,
    _upsert_book,
    _upload_missing_book_cover,
    _year_to_date_dates,
    main,
)
from reading_manager.config import ConfigurationError, load_lark_identity
from reading_manager.llm import LLMFatalError
from reading_manager.tagging import TaggingServiceError
from reading_manager.weread import BookMetadata, WeReadBook, WeReadProgress, WeReadReadData, WeReadShelfItem


class FakeLark:
    def __init__(self, existing_keys=None):
        self.upsert_record_id = None
        self.find_called = False
        self.upserts = []
        self.existing_keys = existing_keys or {}

    def table_id(self, table_name):
        return f"table-{table_name}"

    def upsert_record(self, table_id, fields, record_id=None):
        self.upsert_record_id = record_id
        self.upserts.append((table_id, fields, record_id))
        return record_id or "rec-new"

    def find_record_id(self, table_id=None, search_field=None, keyword=None, *args, **kwargs):
        self.find_called = True
        if keyword in self.existing_keys:
            return self.existing_keys[keyword]
        # positional compatibility: find_record_id(table, field, keyword)
        if args and args[-1] in self.existing_keys:
            return self.existing_keys[args[-1]]
        if search_field is None and args:
            pass
        return ""


class CliHelperTests(unittest.TestCase):
    def test_active_books_prefers_recent_read_time(self):
        books = [
            WeReadBook(book_id="old", title="旧书", last_read_time=1),
            WeReadBook(book_id="new", title="新书", last_read_time=3),
            WeReadBook(book_id="mid", title="中间", last_read_time=2),
        ]

        result = _active_books(books, limit=2)

        self.assertEqual([book.book_id for book in result], ["new", "mid"])

    def test_finished_shelf_items_use_finish_flag_or_nearly_complete_progress(self):
        items = [
            WeReadShelfItem(
                metadata=BookMetadata(book_id="flag", title="标记读完"),
                progress=WeReadProgress(progress=0, last_read_time=2),
                finish_reading=True,
            ),
            WeReadShelfItem(
                metadata=BookMetadata(book_id="progress", title="进度读完"),
                progress=WeReadProgress(progress=99, last_read_time=3),
                finish_reading=False,
            ),
            WeReadShelfItem(
                metadata=BookMetadata(book_id="reading", title="在读"),
                progress=WeReadProgress(progress=60, last_read_time=4),
                finish_reading=False,
            ),
        ]

        result = _finished_shelf_items(items, limit=0)

        self.assertEqual([item.metadata.book_id for item in result], ["progress", "flag"])

    def test_upsert_book_uses_exact_book_index(self):
        lark = FakeLark()
        book_index = {"book-1": "rec-existing"}

        record_id = _upsert_book(
            lark,
            BookMetadata(book_id="book-1", title="已有书"),
            WeReadProgress(progress=100),
            now="2030-01-07 12:00:00",
            book_index=book_index,
        )

        self.assertEqual(record_id, "rec-existing")
        self.assertEqual(lark.upsert_record_id, "rec-existing")
        self.assertFalse(lark.find_called)

    @patch("reading_manager.cli.download_cover_image")
    def test_upload_missing_book_cover_uses_local_basename_without_name_flag(
        self,
        download_cover,
    ):
        class AttachmentLark:
            def __init__(self):
                self.uploaded_path = None
                self.replaced_tokens = None

            def table_id(self, table_name):
                return f"table-{table_name}"

            def upload_attachment(self, table_id, record_id, field_id, file_path):
                self.uploaded_path = file_path
                return {"ok": True, "data": {"file_token": "file-1"}}

            def replace_attachment_field(
                self,
                table_id,
                record_id,
                field_id,
                file_tokens,
            ):
                self.replaced_tokens = file_tokens

        download_cover.side_effect = (
            lambda url, output_dir, title, book_id: output_dir / "cover.jpg"
        )
        lark = AttachmentLark()

        uploaded = _upload_missing_book_cover(
            lark,
            "rec-book",
            BookMetadata(
                book_id="book-1",
                title="测试书",
                cover="https://example.test/cover.jpg",
            ),
        )

        self.assertTrue(uploaded)
        self.assertEqual(lark.uploaded_path.name, "cover.jpg")
        self.assertEqual(lark.replaced_tokens, ["file-1"])

    def test_find_record_id_with_retry_waits_for_new_record_visibility(self):
        class EventuallyVisibleLark:
            def __init__(self):
                self.results = ["", "", "rec-visible"]
                self.calls = 0

            def find_record_id(self, *args, **kwargs):
                self.calls += 1
                return self.results.pop(0)

        lark = EventuallyVisibleLark()
        sleeps = []

        record_id = _find_record_id_with_retry(
            lark,
            "tbl123",
            "同步批次 ID",
            "batch-1",
            attempts=3,
            delay_seconds=1,
            sleeper=sleeps.append,
        )

        self.assertEqual(record_id, "rec-visible")
        self.assertEqual(lark.calls, 3)
        self.assertEqual(sleeps, [1, 1])

    def test_taggable_note_records_selects_untagged_notes_with_text(self):
        records = [
            {"record_id": "rec1", "划线原文": "x", "标签主题": []},
            {"record_id": "rec2", "划线原文": "x", "标签主题": [{"id": "tag-1"}]},
            {"record_id": "rec3", "划线原文": ""},
            {"record_id": "rec4", "个人笔记": "我的想法", "标签主题": []},
        ]

        self.assertEqual([record["record_id"] for record in _taggable_note_records(records, limit=10)], ["rec1", "rec4"])

    @patch(
        "reading_manager.cli._tag_note_records_once",
        side_effect=TaggingServiceError("model unavailable"),
    )
    def test_tag_note_records_does_not_add_an_outer_retry_loop(self, tag_once):
        records = [{"record_id": "rec1", "划线原文": "x"}]

        stats = _tag_note_records(FakeLark(), records)

        self.assertEqual(tag_once.call_count, 1)
        self.assertEqual(stats.candidate_notes, 1)
        self.assertEqual(stats.failed_notes, 1)

    @patch(
        "reading_manager.cli._tag_note_records_once",
        side_effect=LLMFatalError("LLM_API_KEY is required"),
    )
    def test_tag_note_records_converts_configuration_failure_to_failed_notes(self, tag_once):
        records = [{"record_id": "rec1", "划线原文": "x"}]

        stats = _tag_note_records(FakeLark(), records)

        self.assertEqual(tag_once.call_count, 1)
        self.assertEqual(stats.candidate_notes, 1)
        self.assertEqual(stats.failed_notes, 1)

    def test_run_daily_job_runs_all_stages_in_order(self):
        calls = []

        with patch(
                "reading_manager.cli._sync_finished_books",
                side_effect=lambda limit: calls.append(("finished", limit)) or 0,
            ), patch(
                "reading_manager.cli._sync_catchup",
                side_effect=lambda limit: calls.append(("catchup", limit)) or 0,
            ), patch(
                "reading_manager.cli._sync_covers",
                side_effect=lambda limit: calls.append(("covers", limit)) or 0,
            ), patch(
                "sys.stdout", new_callable=io.StringIO
            ):
            result = _run_daily_job()

        self.assertEqual(
            calls,
            [("finished", 0), ("catchup", 20), ("covers", 0)],
        )
        self.assertEqual(result, 0)

    def test_run_daily_job_continues_and_returns_failure(self):
        calls = []

        def fail_finished(limit):
            calls.append("finished")
            return 7

        def fail_catchup(limit):
            calls.append("catchup")
            raise RuntimeError("private note must not be printed")

        def pass_covers(limit):
            calls.append("covers")
            return 0

        with patch(
            "reading_manager.cli._sync_finished_books", side_effect=fail_finished
        ), patch(
            "reading_manager.cli._sync_catchup", side_effect=fail_catchup
        ), patch(
            "reading_manager.cli._sync_covers", side_effect=pass_covers
        ), patch(
            "sys.stdout", new_callable=io.StringIO
        ) as stdout:
            result = _run_daily_job()

        self.assertEqual(calls, ["finished", "catchup", "covers"])
        self.assertEqual(result, 9)
        self.assertIn("sync-finished-books", stdout.getvalue())
        self.assertIn("sync-catchup", stdout.getvalue())
        self.assertNotIn("private note must not be printed", stdout.getvalue())

    @patch("reading_manager.cli._run_daily_job", return_value=0)
    def test_main_dispatches_run_daily(self, run_daily):
        with patch("sys.argv", ["reading-manager", "run-daily"]):
            result = main()

        self.assertEqual(result, 0)
        run_daily.assert_called_once_with()

    @patch.dict("os.environ", {}, clear=True)
    def test_doctor_reports_missing_lark_configuration_without_failing(self):
        with patch("sys.argv", ["reading-manager", "doctor"]), patch(
            "sys.stdout", new_callable=io.StringIO
        ) as stdout:
            result = main()

        self.assertEqual(result, 0)
        self.assertIn("Lark Base: not configured", stdout.getvalue())

    @patch.dict(
        "os.environ",
        {
            "LARK_BASE_TOKEN": "base_example",
            "LARK_BASE_URL": "https://example.feishu.cn/base/base_example",
            "LARK_TABLE_BOOKS": "table_books",
            "LARK_TABLE_READING_RECORDS": "table_reading_records",
            "LARK_TABLE_NOTES": "table_notes",
            "LARK_TABLE_DAILY_STATS": "table_daily_stats",
            "LARK_TABLE_TAGS": "table_tags",
            "LARK_TABLE_SYNC_RUNS": "table_sync_runs",
        },
        clear=True,
    )
    def test_doctor_prints_explicit_base_url_without_resource_ids(self):
        with patch("sys.argv", ["reading-manager", "doctor"]), patch(
            "sys.stdout", new_callable=io.StringIO
        ) as stdout:
            result = main()

        output = stdout.getvalue()
        self.assertEqual(result, 0)
        self.assertIn("https://example.feishu.cn/base/base_example", output)
        self.assertNotIn("table_books", output)

    @patch("reading_manager.cli._sync_covers")
    def test_main_reports_configuration_errors_without_traceback(self, sync_covers):
        sync_covers.side_effect = ConfigurationError(
            "Missing required Lark configuration: LARK_BASE_TOKEN"
        )

        with patch("sys.argv", ["reading-manager", "sync-covers"]), patch(
            "sys.stdout", new_callable=io.StringIO
        ) as stdout:
            result = main()

        self.assertEqual(result, 2)
        self.assertEqual(
            stdout.getvalue().strip(),
            "Configuration error: Missing required Lark configuration: "
            "LARK_BASE_TOKEN",
        )

    def test_compute_daily_reading_stats_uses_official_seconds(self):
        stats = _compute_daily_reading_stats_from_seconds(1500)

        self.assertEqual(stats["duration_seconds"], 1500)
        self.assertEqual(stats["duration_minutes"], 25.0)
        self.assertEqual(stats["effective_reading_day"], 1)
        self.assertEqual(stats["status"], "官方统计")

    def test_compute_daily_reading_stats_marks_zero_reading(self):
        stats = _compute_daily_reading_stats_from_seconds(0)

        self.assertEqual(stats["duration_seconds"], 0)
        self.assertEqual(stats["duration_minutes"], 0)
        self.assertEqual(stats["effective_reading_day"], 0)
        self.assertEqual(stats["status"], "无阅读")

    def test_official_daily_read_seconds_queries_monthly_buckets(self):
        class FakeClient:
            def get_read_data_detail(self, mode, base_time):
                self.call = (mode, base_time)
                return WeReadReadData(
                    daily_read_seconds={
                        "2030-01-02": 1500,
                        "2030-01-03": 0,
                    }
                )

        client = FakeClient()
        seconds = _official_daily_read_seconds(
            client,
            [_date("2030-01-02"), _date("2030-01-03")],
        )

        self.assertEqual(client.call[0], "monthly")
        self.assertEqual(seconds["2030-01-02"], 1500)
        self.assertEqual(seconds["2030-01-03"], 0)

    def test_year_to_date_dates_includes_every_day(self):
        self.assertEqual(
            [day.isoformat() for day in _year_to_date_dates(_date("2030-01-03"))],
            ["2030-01-01", "2030-01-02", "2030-01-03"],
        )

    @patch.dict("os.environ", {"WEREAD_API_KEY": "wrk-env-key"}, clear=False)
    @patch("reading_manager.cli.load_secret_from_keychain")
    def test_load_weread_api_key_prefers_environment(self, load_secret):
        self.assertEqual(_load_weread_api_key(), "wrk-env-key")
        load_secret.assert_not_called()

    @patch.dict("os.environ", {}, clear=True)
    @patch("reading_manager.cli.load_secret_from_keychain", return_value="wrk-stored-key\n")
    def test_load_weread_api_key_uses_keychain(self, load_secret):
        self.assertEqual(_load_weread_api_key(), "wrk-stored-key")
        load_secret.assert_called_once_with(WEREAD_API_KEYCHAIN_SERVICE, WEREAD_KEYCHAIN_ACCOUNT)

    @patch.dict("os.environ", {"LLM_API_KEY": "sk-env-key"}, clear=False)
    @patch("reading_manager.cli.load_secret_from_keychain")
    def test_load_llm_api_key_prefers_environment(self, load_secret):
        self.assertEqual(_load_llm_api_key(), "sk-env-key")
        load_secret.assert_not_called()

    @patch.dict("os.environ", {}, clear=True)
    @patch("reading_manager.cli.load_secret_from_keychain", return_value="sk-stored-key\n")
    def test_load_llm_api_key_uses_keychain(self, load_secret):
        self.assertEqual(_load_llm_api_key(), "sk-stored-key")
        load_secret.assert_called_once_with(LLM_API_KEYCHAIN_SERVICE, WEREAD_KEYCHAIN_ACCOUNT)

    @patch.dict("os.environ", {"LLM_API_KEY": "sk-test-key"}, clear=False)
    @patch("reading_manager.cli.save_secret_to_keychain")
    def test_auth_llm_api_key_saves_to_keychain(self, save_secret):
        with patch("sys.stdout", new_callable=io.StringIO):
            result = _auth_llm_api_key()

        self.assertEqual(result, 0)
        save_secret.assert_called_once_with(
            LLM_API_KEYCHAIN_SERVICE,
            WEREAD_KEYCHAIN_ACCOUNT,
            "sk-test-key",
        )

    def test_load_lark_identity_accepts_user_or_bot(self):
        for identity in ("user", "bot"):
            with self.subTest(identity=identity):
                with patch.dict(os.environ, {"LARK_IDENTITY": identity}, clear=True):
                    self.assertEqual(load_lark_identity(), identity)

    def test_load_lark_identity_rejects_missing_or_invalid_value(self):
        for environment in ({}, {"LARK_IDENTITY": "auto"}):
            with self.subTest(environment=environment):
                with patch.dict(os.environ, environment, clear=True):
                    with self.assertRaises(ConfigurationError):
                        load_lark_identity()

    @patch.dict("os.environ", {"WEREAD_API_KEY": "wrk-test-key"}, clear=False)
    @patch("reading_manager.cli.save_secret_to_keychain")
    def test_auth_weread_api_key_saves_to_keychain(self, save_secret):
        with patch("sys.stdout", new_callable=io.StringIO):
            result = _auth_weread_api_key()

        self.assertEqual(result, 0)
        save_secret.assert_called_once_with(
            WEREAD_API_KEYCHAIN_SERVICE,
            WEREAD_KEYCHAIN_ACCOUNT,
            "wrk-test-key",
        )

    def test_select_daily_sync_books_includes_recent_shelf_and_notebooks(self):
        as_of = date(2026, 11, 21)
        recent_epoch = int(datetime(2026, 11, 16, 12, 0, 0).timestamp())
        old_epoch = int(datetime(2022, 3, 23, 12, 0, 0).timestamp())
        notebooks = [
            WeReadBook(book_id="nb-old", title="旧笔记书", last_read_time=old_epoch),
            WeReadBook(book_id="nb-recent", title="新笔记书", last_read_time=recent_epoch),
        ]
        shelf = [
            WeReadShelfItem(
                metadata=BookMetadata(book_id="shelf-recent", title="示例近期书架书"),
                progress=WeReadProgress(progress=10, last_read_time=int(datetime(2026, 11, 13, 12, 0, 0).timestamp())),
                finish_reading=False,
            ),
            WeReadShelfItem(
                metadata=BookMetadata(book_id="shelf-old", title="很久没读"),
                progress=WeReadProgress(progress=20, last_read_time=int(datetime(2025, 1, 1, 12, 0, 0).timestamp())),
                finish_reading=False,
            ),
            WeReadShelfItem(
                metadata=BookMetadata(book_id="nb-recent", title="新笔记书"),
                progress=WeReadProgress(progress=12, last_read_time=recent_epoch),
                finish_reading=False,
            ),
        ]

        result = _select_daily_sync_books(notebooks, shelf, as_of=as_of, limit=20, recent_days=30)
        titles = [book.title for book in result]
        self.assertIn("示例近期书架书", titles)
        self.assertIn("新笔记书", titles)
        self.assertIn("旧笔记书", titles)
        self.assertNotIn("很久没读", titles)
        merged = [book for book in result if book.book_id == "nb-recent"][0]
        self.assertTrue(merged.from_notebook)
        self.assertEqual(merged.progress.last_read_time, recent_epoch)

    def test_select_daily_sync_books_never_drops_recent_shelf_when_limit_is_small(self):
        as_of = date(2026, 11, 21)
        notebooks = [
            WeReadBook(book_id=f"nb-{index}", title=f"旧{index}", last_read_time=index)
            for index in range(1, 8)
        ]
        shelf = [
            WeReadShelfItem(
                metadata=BookMetadata(book_id="qin", title="示例读完书架书"),
                progress=WeReadProgress(progress=100, last_read_time=int(datetime(2026, 11, 12, 12, 0, 0).timestamp())),
                finish_reading=True,
            )
        ]
        result = _select_daily_sync_books(notebooks, shelf, as_of=as_of, limit=3, recent_days=30)
        ids = [book.book_id for book in result]
        self.assertIn("qin", ids)
        self.assertEqual(len(result), 3)

    def test_has_reading_evidence_on_uses_last_read_or_finish_date(self):
        progress = WeReadProgress(
            last_read_time=int(datetime(2026, 11, 12, 18, 0, 0).timestamp()),
            finish_time=int(datetime(2026, 11, 12, 18, 0, 0).timestamp()),
        )
        self.assertTrue(_has_reading_evidence_on(progress, date(2026, 11, 12)))
        self.assertFalse(_has_reading_evidence_on(progress, date(2026, 11, 11)))
        self.assertFalse(_is_recently_read_or_in_progress(progress, True, date(2026, 10, 1), 30))
        self.assertTrue(_is_recently_read_or_in_progress(progress, True, date(2026, 11, 21), 30))

    def test_trusted_last_read_prefers_stored_date_when_live_date_is_untrusted(self):
        live = WeReadProgress(last_read_time=int(datetime(2026, 11, 21, 14, 28, 47).timestamp()))
        trusted = _trusted_last_read_date(
            live,
            existing_book_record={"最近阅读时间": "2026-11-12 00:00:00"},
            preferred_date=date(2026, 11, 16),
            untrusted_on=date(2026, 11, 21),
        )
        # preferred_date wins when live is untrusted
        self.assertEqual(trusted, date(2026, 11, 16))
        trusted_stored = _trusted_last_read_date(
            live,
            existing_book_record={"最近阅读时间": "2026-11-12 00:00:00"},
            preferred_date=None,
            untrusted_on=date(2026, 11, 21),
        )
        self.assertEqual(trusted_stored, date(2026, 11, 12))

    def test_upsert_reading_record_if_missing_does_not_overwrite(self):
        metadata = BookMetadata(book_id="qin", title="示例读完书")
        progress = WeReadProgress(progress=100, last_read_time=int(datetime(2026, 11, 12, 12, 0, 0).timestamp()))
        existing = FakeLark(existing_keys={"daily:qin:2026-11-12": "rec-old"})
        result = _upsert_reading_record_if_missing(
            existing,
            metadata=metadata,
            progress=progress,
            book_record_id="rec-book",
            sync_record_id="rec-sync",
            record_date="2026-11-12",
            finish_reading=True,
        )
        self.assertEqual(result, "exists")
        self.assertEqual(existing.upserts, [])

        fresh = FakeLark()
        result = _upsert_reading_record_if_missing(
            fresh,
            metadata=metadata,
            progress=progress,
            book_record_id="rec-book",
            sync_record_id="rec-sync",
            record_date="2026-11-12",
            finish_reading=True,
        )
        self.assertEqual(result, "created")
        self.assertEqual(len(fresh.upserts), 1)
        self.assertEqual(fresh.upserts[0][1]["日级记录键"], "daily:qin:2026-11-12")

def _date(value: str):
    return datetime.strptime(value, "%Y-%m-%d").date()


if __name__ == "__main__":
    unittest.main()
