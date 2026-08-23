import json
from pathlib import Path
import unittest
from unittest.mock import patch

from reading_manager.lark_base import (
    LarkAuthorizationError,
    LarkBaseClient,
    LarkBaseError,
    extract_attachment_file_token,
    extract_ignored_fields,
    extract_record_id,
    link_value,
    make_book_fields,
    make_daily_stats_fields,
    make_highlight_fields,
    make_progress_fields,
    make_sync_fields,
    make_tag_fields,
    progress_ratio,
)
from reading_manager.tagging import BookTag
from reading_manager.weread import BookMetadata, WeReadHighlight, WeReadProgress


class LarkBaseTests(unittest.TestCase):
    @patch.dict(
        "os.environ",
        {
            "LARK_BASE_TOKEN": "base_example",
            "LARK_TABLE_BOOKS": "table_books",
            "LARK_TABLE_READING_RECORDS": "table_reading_records",
            "LARK_TABLE_NOTES": "table_notes",
            "LARK_TABLE_DAILY_STATS": "table_daily_stats",
            "LARK_TABLE_TAGS": "table_tags",
            "LARK_TABLE_SYNC_RUNS": "table_sync_runs",
        },
        clear=True,
    )
    def test_client_loads_runtime_resource_configuration(self):
        client = LarkBaseClient(identity="user", runner=lambda *args, **kwargs: None)

        self.assertEqual(client.base_token, "base_example")
        self.assertEqual(client.table_id("书籍"), "table_books")
        self.assertEqual(client.table_id("同步记录"), "table_sync_runs")

    def test_extracts_record_id_from_upsert_response(self):
        payload = {"data": {"record": {"record_id": "rec123"}}}

        self.assertEqual(extract_record_id(payload), "rec123")

    def test_extracts_ignored_fields_from_record_response(self):
        payload = {"data": {"record": {"ignored_fields": [{"name": "封面图片"}]}}}

        self.assertEqual(extract_ignored_fields(payload), [{"name": "封面图片"}])

    def test_link_value_uses_record_id_objects(self):
        self.assertEqual(link_value("rec123"), [{"id": "rec123"}])

    def test_book_fields_map_to_lark_schema(self):
        fields = make_book_fields(
            BookMetadata(
                book_id="book-1",
                title="示例书籍甲",
                author="示例作者",
                translator="译者",
                publisher="示例出版社",
                isbn="9780000000000",
                category="示例分类",
                cover="https://example.test/cover.jpg",
                intro="简介",
            ),
            now="2030-01-02 17:00:00",
            last_read_time="2030-01-01 17:45:33",
            reading_status="读完",
            reading_progress=99,
        )

        self.assertEqual(fields["书名"], "示例书籍甲")
        self.assertEqual(fields["微信读书 Book ID"], "book-1")
        self.assertEqual(fields["阅读状态"], "读完")
        self.assertEqual(fields["阅读进度"], 0.99)
        self.assertEqual(fields["数据来源"], "微信读书")

    def test_progress_ratio_matches_lark_percentage_value(self):
        self.assertEqual(progress_ratio(99), 0.99)
        self.assertEqual(progress_ratio(100), 1.0)
        self.assertEqual(progress_ratio(150), 1.0)
        self.assertEqual(progress_ratio(-1), 0.0)

    def test_finished_book_without_progress_displays_complete(self):
        fields = make_book_fields(
            BookMetadata(book_id="book-1", title="示例书籍己"),
            now="2030-01-02 17:00:00",
            last_read_time=None,
            reading_status="读完",
            reading_progress=0,
        )

        self.assertEqual(fields["阅读进度"], 1.0)

    def test_progress_fields_include_links_and_minutes(self):
        fields = make_progress_fields(
            title="示例书籍甲",
            book_id="book-1",
            book_record_id="rec_book",
            sync_record_id="rec_sync",
            progress=WeReadProgress(progress=99, reading_minutes=61, last_read_time=1700000000),
            now="2030-01-02 17:00:00",
        )

        self.assertEqual(fields["书籍"], [{"id": "rec_book"}])
        self.assertEqual(fields["同步批次"], [{"id": "rec_sync"}])
        self.assertEqual(fields["阅读进度"], 0.99)
        self.assertEqual(fields["阅读时长"], 61)

    def test_progress_fields_include_daily_key_when_given(self):
        fields = make_progress_fields(
            title="示例书籍甲",
            book_id="book-1",
            book_record_id="rec_book",
            sync_record_id="rec_sync",
            progress=WeReadProgress(progress=37),
            now="2030-01-02 23:30:00",
            record_date="2030-01-02",
            daily_key="daily:book-1:2030-01-02",
        )

        self.assertEqual(fields["记录日期"], "2030-01-02")
        self.assertEqual(fields["日级记录键"], "daily:book-1:2030-01-02")
        self.assertIn("2030-01-02", fields["记录标题"])

    def test_daily_stats_fields_use_heatmap_minutes(self):
        fields = make_daily_stats_fields(
            record_date="2030-01-08",
            stats_key="daily-stats:2030-01-08",
            duration_seconds=1500,
            duration_minutes=25.0,
            effective_reading_day=1,
            status="官方统计",
            now="2030-01-08 20:30:00",
            sync_record_id="rec_sync",
            note="官方统计说明",
        )

        self.assertEqual(fields["统计标题"], "2030-01-08 阅读统计")
        self.assertEqual(fields["当天阅读时长（分钟）"], 25.0)
        self.assertEqual(fields["当天阅读时长（秒）"], 1500)
        self.assertEqual(fields["有效阅读日（1/0）"], 1)
        self.assertEqual(fields["同步批次"], [{"id": "rec_sync"}])
        self.assertEqual(fields["数据来源"], "微信读书阅读统计")

    def test_highlight_fields_include_full_text_and_hash(self):
        fields = make_highlight_fields(
            title="示例书籍甲",
            highlight=WeReadHighlight(
                book_id="book-1",
                note_id="note-1",
                chapter="第一章",
                position="1-2",
                highlight_text="完整划线原文",
                create_time=1700000000,
            ),
            book_record_id="rec_book",
            sync_record_id="rec_sync",
            now="2030-01-02 17:00:00",
        )

        self.assertEqual(fields["划线原文"], "完整划线原文")
        self.assertEqual(fields["书籍"], [{"id": "rec_book"}])
        self.assertTrue(fields["内容哈希"])
        self.assertNotIn("AI 确认状态", json.loads(json.dumps(fields)))

    def test_review_note_fields_use_personal_note_text(self):
        fields = make_highlight_fields(
            title="示例书籍丙",
            highlight=WeReadHighlight(
                book_id="book-1",
                note_id="review:review-1",
                chapter="第一章",
                position="1-2",
                highlight_text="关联的原文",
                personal_note="我的想法",
                note_type="想法",
                create_time=1700000000,
            ),
            book_record_id="rec_book",
            sync_record_id="rec_sync",
            now="2030-01-08 18:00:00",
        )

        self.assertEqual(fields["类型"], "想法")
        self.assertEqual(fields["划线原文"], "关联的原文")
        self.assertEqual(fields["个人笔记"], "我的想法")
        self.assertTrue(fields["内容哈希"])

    def test_sync_fields_capture_counts(self):
        fields = make_sync_fields(
            batch_id="batch-1",
            status="成功",
            scope="手动选择 2 本书",
            selected_books=["示例书籍甲", "示例书籍乙"],
            started_at="2030-01-02 17:00:00",
            ended_at="2030-01-02 17:01:00",
            books_count=2,
            reading_records_count=2,
            highlights_count=6,
            tags_count=0,
            error_summary="",
        )

        self.assertEqual(fields["同步批次 ID"], "batch-1")
        self.assertEqual(fields["选择书籍"], "示例书籍甲\n示例书籍乙")
        self.assertEqual(fields["写入笔记划线数"], 6)

    def test_tag_fields_use_normalized_name(self):
        fields = make_tag_fields(
            BookTag(name=" 主题 乙 ", description="主题说明", confidence=0.8),
            now="2030-01-02 17:30:00",
        )

        self.assertEqual(fields["标签名"], "主题 乙")
        self.assertEqual(fields["规范化名称"], "主题乙")
        self.assertEqual(fields["标签来源"], "AI")
        self.assertNotIn("确认状态", fields)
        self.assertEqual(fields["适用层级"], "书籍")

    def test_client_upsert_record_returns_record_id(self):
        calls = []

        def runner(command, **kwargs):
            calls.append(command)
            return _Result(
                stdout=json.dumps({"ok": True, "data": {"record": {"record_id": "rec123"}}}),
                stderr="",
                returncode=0,
            )

        client = LarkBaseClient(base_token="base", identity="user", runner=runner)

        record_id = client.upsert_record("tbl123", {"书名": "示例书籍甲"})

        self.assertEqual(record_id, "rec123")
        self.assertIn("+record-upsert", calls[0])
        self.assertIn("--json", calls[0])
        self.assertIn("--as", calls[0])
        self.assertIn("user", calls[0])

    def test_client_find_record_id_returns_first_search_result(self):
        def runner(command, **kwargs):
            return _Result(
                stdout=json.dumps({"ok": True, "data": {"record_id_list": ["rec_first"]}}),
                stderr="",
                returncode=0,
            )

        client = LarkBaseClient(base_token="base", identity="user", runner=runner)

        self.assertEqual(
            client.find_record_id("tbl123", search_field="微信读书 Book ID", keyword="book-1"),
            "rec_first",
        )

    def test_client_find_record_id_propagates_search_failures(self):
        def runner(command, **kwargs):
            raise LarkBaseError("search failed")

        client = LarkBaseClient(base_token="base", identity="user", runner=runner)

        with self.assertRaisesRegex(LarkBaseError, "search failed"):
            client.find_record_id("tbl123", search_field="规范化名称", keyword="x")

    def test_client_list_records_maps_rows_to_field_names(self):
        def runner(command, **kwargs):
            return _Result(
                stdout=json.dumps(
                    {
                        "ok": True,
                        "data": {
                            "fields": ["标题", "内容"],
                            "data": [["t1", "c1"]],
                            "record_id_list": ["rec1"],
                        },
                    }
                ),
                stderr="",
                returncode=0,
            )

        client = LarkBaseClient(base_token="base", identity="user", runner=runner)

        records = client.list_records("tbl123", ["标题", "内容"])

        self.assertEqual(records, [{"record_id": "rec1", "标题": "t1", "内容": "c1"}])

    def test_client_list_records_paginates_until_short_page(self):
        offsets = []

        def runner(command, **kwargs):
            offset = int(command[command.index("--offset") + 1])
            offsets.append(offset)
            if offset == 0:
                rows = [[f"title-{index}"] for index in range(200)]
                record_ids = [f"rec{index}" for index in range(200)]
            else:
                rows = [["title-200"]]
                record_ids = ["rec200"]
            return _Result(
                stdout=json.dumps(
                    {
                        "ok": True,
                        "data": {
                            "fields": ["标题"],
                            "data": rows,
                            "record_id_list": record_ids,
                        },
                    }
                ),
                stderr="",
                returncode=0,
            )

        client = LarkBaseClient(base_token="base", identity="user", runner=runner)

        records = client.list_records("tbl123", ["标题"])

        self.assertEqual(offsets, [0, 200])
        self.assertEqual(len(records), 201)
        self.assertEqual(records[-1], {"record_id": "rec200", "标题": "title-200"})

    def test_client_upload_attachment_uses_attachment_shortcut(self):
        calls = []

        def runner(command, **kwargs):
            calls.append(command)
            return _Result(
                stdout="Uploading attachment...\n" + json.dumps({"ok": True, "data": {"file_token": "file123"}}),
                stderr="",
                returncode=0,
            )

        client = LarkBaseClient(base_token="base", identity="user", runner=runner)

        payload = client.upload_attachment(
            "tbl123",
            "rec123",
            "封面图片",
            Path("/tmp/cover.jpg"),
        )

        self.assertEqual(payload["data"]["file_token"], "file123")
        self.assertIn("+record-upload-attachment", calls[0])
        self.assertIn("--record-id", calls[0])
        self.assertIn("rec123", calls[0])
        self.assertIn("--field-id", calls[0])
        self.assertIn("封面图片", calls[0])
        self.assertIn("--file", calls[0])
        self.assertIn("cover.jpg", calls[0])
        self.assertNotIn("--name", calls[0])
        identity_index = calls[0].index("--as")
        self.assertEqual(calls[0][identity_index : identity_index + 2], ["--as", "user"])

    def test_client_upload_attachment_rejects_ignored_field_response(self):
        def runner(command, **kwargs):
            return _Result(
                stdout=json.dumps(
                    {
                        "ok": True,
                        "data": {
                            "record": {
                                "ignored_fields": [
                                    {
                                        "name": "封面图片",
                                        "reason": "MOBILE_ONLY: attachment field input is limited to mobile upload.",
                                    }
                                ],
                                "update": {},
                            }
                        },
                    }
                ),
                stderr="",
                returncode=0,
            )

        client = LarkBaseClient(base_token="base", identity="user", runner=runner)

        with self.assertRaisesRegex(LarkBaseError, "封面图片"):
            client.upload_attachment("tbl123", "rec123", "封面图片", Path("/tmp/cover.jpg"))

    def test_extract_attachment_file_token_from_upload_payload(self):
        payload = {"ok": True, "data": {"file_token": "file123"}}

        self.assertEqual(extract_attachment_file_token(payload, "封面图片", "cover.jpg"), "file123")

    def test_extract_attachment_file_token_from_record_fields(self):
        payload = {
            "ok": True,
            "data": {
                "record": {
                    "fields": {
                        "封面图片": [
                            {"file_token": "old", "name": "old.bin"},
                            {"file_token": "new", "name": "cover.jpg"},
                        ]
                    }
                }
            },
        }

        self.assertEqual(extract_attachment_file_token(payload, "封面图片", "cover.jpg"), "new")

    def test_client_replace_attachment_field_uses_bitable_record_update(self):
        calls = []

        def runner(command, **kwargs):
            calls.append(command)
            return _Result(stdout=json.dumps({"code": 0, "msg": "success"}), stderr="", returncode=0)

        client = LarkBaseClient(base_token="base", identity="user", runner=runner)

        client.replace_attachment_field("tbl123", "rec123", "封面图片", ["file123"])

        self.assertEqual(calls[0][0:3], ["lark-cli", "api", "PUT"])
        self.assertIn("/open-apis/bitable/v1/apps/base/tables/tbl123/records/rec123", calls[0])
        self.assertIn("--data", calls[0])
        self.assertIn('"file_token": "file123"', calls[0][calls[0].index("--data") + 1])
        self.assertEqual(calls[0][-2:], ["--as", "user"])

    def test_client_upsert_returns_existing_record_id_when_supplied(self):
        def runner(command, **kwargs):
            return _Result(
                stdout=json.dumps({"ok": True, "data": {"record": {"update": {"书名": "x"}}}}),
                stderr="",
                returncode=0,
            )

        client = LarkBaseClient(base_token="base", identity="user", runner=runner)

        self.assertEqual(client.upsert_record("tbl123", {"书名": "x"}, record_id="rec_existing"), "rec_existing")

    def test_client_reports_user_reauthorization_without_upstream_payload(self):
        errors = [
            {
                "type": "authorization",
                "subtype": "missing_scope",
                "message": "permission missing",
                "hint": "token=must-not-leak",
            },
            {
                "type": "api_error",
                "message": "no access token available for user",
                "hint": "token=must-not-leak",
            },
        ]
        for error in errors:
            with self.subTest(error=error):
                def runner(command, **kwargs):
                    return _Result(
                        stdout="",
                        stderr=json.dumps(
                            {
                                "ok": False,
                                "identity": "user",
                                "error": error,
                            }
                        ),
                        returncode=1,
                    )

                client = LarkBaseClient(base_token="base", identity="user", runner=runner)

                with self.assertRaisesRegex(LarkAuthorizationError, "重新授权") as raised:
                    client.list_records("tbl123", ["标题"])

                self.assertNotIn("must-not-leak", str(raised.exception))

    def test_find_record_id_does_not_swallow_user_authorization_error(self):
        def runner(command, **kwargs):
            return _Result(
                stdout="",
                stderr=json.dumps(
                    {
                        "ok": False,
                        "identity": "user",
                        "error": {
                            "type": "authorization",
                            "subtype": "missing_scope",
                            "message": "permission missing",
                        },
                    }
                ),
                returncode=1,
            )

        client = LarkBaseClient(base_token="base", identity="user", runner=runner)

        with self.assertRaises(LarkAuthorizationError):
            client.find_record_id("tbl123", "标题", "x")


class _Result:
    def __init__(self, stdout: str, stderr: str, returncode: int):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


if __name__ == "__main__":
    unittest.main()
