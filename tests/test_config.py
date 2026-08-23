import unittest
from dataclasses import FrozenInstanceError

from reading_manager.config import (
    ConfigurationError,
    LarkResourceConfig,
    load_lark_resource_config,
)


LARK_ENV = {
    "LARK_BASE_TOKEN": "base_example",
    "LARK_BASE_URL": "https://example.feishu.cn/base/base_example",
    "LARK_TABLE_BOOKS": "table_books",
    "LARK_TABLE_READING_RECORDS": "table_reading_records",
    "LARK_TABLE_NOTES": "table_notes",
    "LARK_TABLE_DAILY_STATS": "table_daily_stats",
    "LARK_TABLE_TAGS": "table_tags",
    "LARK_TABLE_SYNC_RUNS": "table_sync_runs",
}


class LarkResourceConfigTests(unittest.TestCase):
    def test_loads_complete_lark_resource_configuration(self):
        config = load_lark_resource_config(LARK_ENV)

        self.assertEqual(config.base_token, "base_example")
        self.assertEqual(
            config.base_url,
            "https://example.feishu.cn/base/base_example",
        )
        self.assertEqual(config.table_id("书籍"), "table_books")
        self.assertEqual(config.table_id("同步记录"), "table_sync_runs")

    def test_base_url_is_optional(self):
        environment = dict(LARK_ENV)
        environment.pop("LARK_BASE_URL")

        config = load_lark_resource_config(environment)

        self.assertEqual(config.base_url, "")

    def test_rejects_missing_base_token_without_exposing_other_values(self):
        environment = dict(LARK_ENV)
        environment.pop("LARK_BASE_TOKEN")
        environment["LARK_TABLE_BOOKS"] = "private_table_value"

        with self.assertRaises(ConfigurationError) as raised:
            load_lark_resource_config(environment)

        message = str(raised.exception)
        self.assertIn("LARK_BASE_TOKEN", message)
        self.assertNotIn("private_table_value", message)

    def test_rejects_each_missing_table_id(self):
        table_variables = [
            "LARK_TABLE_BOOKS",
            "LARK_TABLE_READING_RECORDS",
            "LARK_TABLE_NOTES",
            "LARK_TABLE_DAILY_STATS",
            "LARK_TABLE_TAGS",
            "LARK_TABLE_SYNC_RUNS",
        ]

        for variable in table_variables:
            with self.subTest(variable=variable):
                environment = dict(LARK_ENV)
                environment.pop(variable)

                with self.assertRaisesRegex(ConfigurationError, variable):
                    load_lark_resource_config(environment)

    def test_configuration_is_immutable(self):
        config = load_lark_resource_config(LARK_ENV)

        with self.assertRaises(FrozenInstanceError):
            config.base_token = "changed"
        self.assertIsInstance(config.table_ids, tuple)

    def test_rejects_unknown_table_name(self):
        config = load_lark_resource_config(LARK_ENV)

        with self.assertRaisesRegex(ConfigurationError, "Unknown Lark table"):
            config.table_id("missing")

    def test_direct_configuration_supports_table_lookup(self):
        config = LarkResourceConfig(
            base_token="base_example",
            base_url="",
            table_ids=(("书籍", "table_books"),),
        )

        self.assertEqual(config.table_id("书籍"), "table_books")


if __name__ == "__main__":
    unittest.main()
