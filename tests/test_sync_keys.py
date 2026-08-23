import unittest

from reading_manager.sync_keys import (
    daily_record_key,
    daily_stats_key,
    normalize_label_name,
    note_dedupe_key,
    stable_text_hash,
)


class SyncKeyTests(unittest.TestCase):
    def test_normalize_label_name(self):
        self.assertEqual(normalize_label_name("  AI  打标 "), "ai打标")

    def test_stable_text_hash_is_short_and_stable(self):
        first = stable_text_hash("hello")
        second = stable_text_hash("hello")

        self.assertEqual(first, second)
        self.assertEqual(len(first), 16)

    def test_note_dedupe_key_prefers_note_id(self):
        self.assertEqual(
            note_dedupe_key(book_id="book", note_id="note123", position="p1", text="abc"),
            "note:note123",
        )

    def test_note_dedupe_key_falls_back_to_composite_hash(self):
        key = note_dedupe_key(book_id="book", note_id="", position="p1", text="abc")

        self.assertTrue(key.startswith("note_fallback:book:p1:"))

    def test_daily_record_key_uses_book_and_date(self):
        self.assertEqual(daily_record_key("book-1", "2030-01-02"), "daily:book-1:2030-01-02")

    def test_daily_stats_key_uses_date(self):
        self.assertEqual(daily_stats_key("2030-01-02"), "daily-stats:2030-01-02")


if __name__ == "__main__":
    unittest.main()
