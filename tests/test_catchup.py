import unittest
from datetime import date

from reading_manager.cli import _catchup_record_dates, _date_from_lark_value


class CatchupTests(unittest.TestCase):
    def test_catchup_includes_missing_dates_and_today(self):
        dates = _catchup_record_dates(
            {
                date(2030, 1, 2),
                date(2030, 1, 3),
                date(2030, 1, 5),
            },
            through_date_value=date(2030, 1, 7),
            lookback_days=30,
        )

        self.assertEqual(
            dates,
            [
                date(2030, 1, 4),
                date(2030, 1, 6),
                date(2030, 1, 7),
            ],
        )

    def test_catchup_runs_today_when_today_already_has_snapshot(self):
        dates = _catchup_record_dates(
            {date(2030, 1, 6), date(2030, 1, 7)},
            through_date_value=date(2030, 1, 7),
            lookback_days=30,
        )

        self.assertEqual(dates, [date(2030, 1, 7)])

    def test_catchup_starts_at_today_without_existing_records(self):
        dates = _catchup_record_dates(
            set(),
            through_date_value=date(2030, 1, 7),
            lookback_days=30,
        )

        self.assertEqual(dates, [date(2030, 1, 7)])

    def test_date_from_lark_value_accepts_date_prefix(self):
        self.assertEqual(
            _date_from_lark_value("2030-01-07 23:30:00"),
            date(2030, 1, 7),
        )
        self.assertIsNone(_date_from_lark_value("not a date"))


if __name__ == "__main__":
    unittest.main()
