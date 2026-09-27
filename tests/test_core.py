import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from keka_automation.core import (
    ConfigError,
    is_authorized_update,
    is_working_day,
    parse_hhmm,
)
from keka_automation.store import AttendanceStore


class CoreTests(unittest.TestCase):
    def test_parse_hhmm_accepts_valid_24_hour_time(self):
        self.assertEqual(parse_hhmm("10:05"), (10, 5))

    def test_parse_hhmm_rejects_invalid_time(self):
        with self.assertRaises(ConfigError):
            parse_hhmm("24:00")

    def test_working_days_use_iso_weekday_numbers(self):
        self.assertTrue(is_working_day(date(2026, 8, 31), [1, 2, 3, 4, 5]))
        self.assertFalse(is_working_day(date(2026, 9, 5), [1, 2, 3, 4, 5]))

    def test_telegram_requires_exact_private_user_and_chat(self):
        allowed = {"from": {"id": 123}, "chat": {"id": 456, "type": "private"}}
        group = {"from": {"id": 123}, "chat": {"id": 456, "type": "group"}}
        wrong_user = {"from": {"id": 999}, "chat": {"id": 456, "type": "private"}}
        self.assertTrue(is_authorized_update(allowed, 123, 456))
        self.assertFalse(is_authorized_update(group, 123, 456))
        self.assertFalse(is_authorized_update(wrong_user, 123, 456))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = AttendanceStore(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_success_is_idempotent_per_day_and_action(self):
        self.assertFalse(self.store.was_successful("2026-08-31", "CLOCK_IN"))
        self.store.record(
            date="2026-08-31",
            execution_timestamp="2026-08-31T10:00:03+05:30",
            action="CLOCK_IN",
            trigger="scheduled",
            result="SUCCESS",
            verification="UI_CONFIRMED",
            error_category=None,
        )
        self.assertTrue(self.store.was_successful("2026-08-31", "CLOCK_IN"))
        self.assertFalse(self.store.was_successful("2026-08-31", "CLOCK_OUT"))

    def test_later_non_success_row_does_not_erase_verified_success(self):
        common = {
            "date": "2026-08-31",
            "action": "CLOCK_IN",
            "trigger": "scheduled",
            "error_category": None,
        }
        self.store.record(
            **common,
            execution_timestamp="2026-08-31T10:00:03+05:30",
            result="SUCCESS",
            verification="UI_CONFIRMED",
        )
        self.store.record(
            **common,
            execution_timestamp="2026-08-31T10:01:03+05:30",
            result="IN_PROGRESS",
            verification="NOT_ATTEMPTED",
        )
        self.assertTrue(self.store.was_successful("2026-08-31", "CLOCK_IN"))

    def test_unverified_success_like_rows_never_suppress_future_attempts(self):
        for result in ("SUCCESS", "ALREADY_DONE"):
            self.store.record(
                date="2026-09-24",
                execution_timestamp="2026-09-24T10:00:00+05:30",
                action="CLOCK_IN",
                trigger="scheduled",
                result=result,
                verification="NOT_VERIFIED",
                error_category="VERIFICATION",
            )
        self.assertFalse(self.store.was_successful("2026-09-24", "CLOCK_IN"))

    def test_record_and_read_hold_file_locks(self):
        import fcntl
        from unittest.mock import patch

        calls = []
        real_flock = fcntl.flock

        def tracking_flock(fd, op):
            calls.append(op)
            return real_flock(fd, op)

        with patch("keka_automation.store.fcntl.flock", side_effect=tracking_flock):
            self.store.record(
                date="2026-09-24",
                execution_timestamp="2026-09-24T10:00:00+05:30",
                action="CLOCK_IN",
                trigger="scheduled",
                result="FAILED",
                verification="NOT_VERIFIED",
                error_category="BROWSER",
            )
            self.store.recent(5)
        self.assertIn(fcntl.LOCK_EX, calls)
        self.assertIn(fcntl.LOCK_SH, calls)

    def test_log_contains_only_allowlisted_fields(self):
        self.store.record(
            date="2026-08-31",
            execution_timestamp="2026-08-31T10:00:03+05:30",
            action="CLOCK_IN",
            trigger="manual",
            result="FAILED",
            verification="NOT_VERIFIED",
            error_category="NETWORK",
            ignored_secret="must-not-be-written",
        )
        row = json.loads((Path(self.tmp.name) / "logs" / "attendance.jsonl").read_text().strip())
        self.assertEqual(
            set(row),
            {"date", "execution_timestamp", "action", "trigger", "result", "verification", "error_category"},
        )
        self.assertNotIn("must-not-be-written", json.dumps(row))


if __name__ == "__main__":
    unittest.main()
