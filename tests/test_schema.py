import unittest

from reading_manager.schema import BASE_SCHEMA, core_table_names, table_by_name


class SchemaTests(unittest.TestCase):
    def test_core_schema_has_expected_tables(self):
        self.assertEqual(
            core_table_names(),
            ["书籍", "阅读记录", "笔记划线", "每日阅读统计", "标签主题", "同步记录"],
        )

    def test_book_table_contains_required_fields(self):
        books = table_by_name("书籍")

        field_names = [field.name for field in books.fields]

        for name in [
            "书名",
            "微信读书 Book ID",
            "作者",
            "阅读状态",
            "阅读进度",
            "封面",
            "封面图片",
            "书籍级标签",
        ]:
            self.assertIn(name, field_names)

    def test_reading_table_contains_daily_snapshot_fields(self):
        reading = table_by_name("阅读记录")

        field_names = [field.name for field in reading.fields]

        self.assertIn("记录日期", field_names)
        self.assertIn("日级记录键", field_names)

    def test_daily_stats_table_contains_heatmap_fields(self):
        stats = table_by_name("每日阅读统计")

        field_names = [field.name for field in stats.fields]

        for name in [
            "统计日期",
            "日级统计键",
            "当天阅读时长（分钟）",
            "当天阅读时长（秒）",
            "有效阅读日（1/0）",
            "统计状态",
        ]:
            self.assertIn(name, field_names)

    def test_schema_excludes_default_lark_table(self):
        self.assertNotIn("默认表（待确认删除）", [table.name for table in BASE_SCHEMA.tables])


if __name__ == "__main__":
    unittest.main()
