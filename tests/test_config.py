import json
import tempfile
import unittest
from pathlib import Path

from keka_automation.config import ConfigManager
from keka_automation.core import ConfigError
from keka_automation.launchd import build_scheduled_plist, build_telegram_plist, calendar_intervals


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "config.json"
        self.config = ConfigManager(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_default_configuration_is_disabled_and_weekdays_only(self):
        data = self.config.ensure_defaults()
        self.assertFalse(data["enabled"])
        self.assertFalse(data["production_approved"])
        self.assertEqual(data["in_time"], "10:00")
        self.assertEqual(data["out_time"], "19:00")
        self.assertEqual(data["clock_in_grace_minutes"], 30)
        self.assertEqual(data["network_retries"], 3)
        self.assertEqual(data["working_days"], [1, 2, 3, 4, 5])
        self.assertEqual(data["wake_schedule"], {"enabled_after_approval": False, "time": "09:55"})
        self.assertNotIn("telegram_bot_token", json.dumps(data))

    def test_schedule_update_persists_valid_time(self):
        self.config.ensure_defaults()
        updated = self.config.update_schedule("in", "09:45")
        self.assertEqual(updated["in_time"], "09:45")
        self.assertEqual(self.config.load()["in_time"], "09:45")

    def test_schedule_update_rejects_invalid_time(self):
        self.config.ensure_defaults()
        with self.assertRaises(ConfigError):
            self.config.update_schedule("out", "25:00")

    def test_clock_in_grace_update_persists_minutes(self):
        self.config.ensure_defaults()
        updated = self.config.update_clock_in_grace(30)
        self.assertEqual(updated["clock_in_grace_minutes"], 30)
        self.assertEqual(self.config.load()["clock_in_grace_minutes"], 30)

    def test_clock_in_grace_rejects_unsafe_range(self):
        self.config.ensure_defaults()
        for value in (-1, 181):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                self.config.update_clock_in_grace(value)

    def test_working_days_reject_duplicates_and_out_of_range(self):
        self.config.ensure_defaults()
        with self.assertRaises(ConfigError):
            self.config.update_working_days([1, 1, 8])


class LaunchdTests(unittest.TestCase):
    def test_calendar_intervals_use_launchd_weekday_mapping(self):
        values = calendar_intervals("10:05", [1, 5, 7])
        self.assertEqual(
            values,
            [
                {"Weekday": 1, "Hour": 10, "Minute": 5},
                {"Weekday": 5, "Hour": 10, "Minute": 5},
                {"Weekday": 0, "Hour": 10, "Minute": 5},
            ],
        )

    def test_clock_in_plist_retries_each_minute_through_grace_window(self):
        plist = build_scheduled_plist(
            Path("/tmp/keka"), "CLOCK_IN", "10:00", [1, 2, 3, 4, 5], grace_minutes=30
        )
        self.assertEqual(plist["Label"], "com.ankit.keka-attendance.clockin")
        self.assertEqual(plist["ProgramArguments"][-1], "clockin-dispatch")
        self.assertTrue(plist["RunAtLoad"])
        self.assertEqual(len(plist["StartCalendarInterval"]), 155)
        self.assertIn({"Weekday": 1, "Hour": 10, "Minute": 0}, plist["StartCalendarInterval"])
        self.assertIn({"Weekday": 1, "Hour": 10, "Minute": 30}, plist["StartCalendarInterval"])
        self.assertNotIn("token", json.dumps(plist).lower())

    def test_clock_out_plist_retries_each_minute_through_grace_window(self):
        plist = build_scheduled_plist(
            Path("/tmp/keka"), "CLOCK_OUT", "19:00", [1, 2, 3, 4, 5], grace_minutes=90
        )
        self.assertEqual(plist["Label"], "com.ankit.keka-attendance.clockout")
        self.assertEqual(plist["ProgramArguments"][-2:], ["scheduled", "CLOCK_OUT"])
        self.assertFalse(plist["RunAtLoad"])
        self.assertEqual(len(plist["StartCalendarInterval"]), 455)
        self.assertIn({"Weekday": 5, "Hour": 20, "Minute": 30}, plist["StartCalendarInterval"])
        self.assertNotIn("token", json.dumps(plist).lower())

    def test_retry_calendar_never_crosses_into_next_business_date(self):
        plist = build_scheduled_plist(
            Path("/tmp/keka"), "CLOCK_IN", "23:50", [5], grace_minutes=30
        )
        self.assertIn({"Weekday": 5, "Hour": 23, "Minute": 50}, plist["StartCalendarInterval"])
        self.assertIn({"Weekday": 5, "Hour": 23, "Minute": 59}, plist["StartCalendarInterval"])
        self.assertNotIn({"Weekday": 6, "Hour": 0, "Minute": 0}, plist["StartCalendarInterval"])
        self.assertEqual(len(plist["StartCalendarInterval"]), 10)

    def test_telegram_plist_is_keepalive_without_bot_token(self):
        plist = build_telegram_plist(Path("/tmp/keka"))
        self.assertTrue(plist["KeepAlive"])
        self.assertEqual(plist["ProgramArguments"][-1], "telegram")
        self.assertNotIn("token", json.dumps(plist).lower())


if __name__ == "__main__":
    unittest.main()
