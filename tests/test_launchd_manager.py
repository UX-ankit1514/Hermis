import plistlib
import tempfile
import unittest
from pathlib import Path

from keka_automation.launchd_manager import LaunchdManager


class FakeRunner:
    def __init__(self):
        self.calls = []

    def __call__(self, args):
        self.calls.append(args)
        return 0


class LaunchdManagerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "app"
        self.agents = Path(self.tmp.name) / "LaunchAgents"
        self.root.mkdir()
        self.runner = FakeRunner()
        self.manager = LaunchdManager(self.root, self.agents, uid=501, runner=self.runner)
        self.config = {
            "in_time": "10:00",
            "out_time": "19:00",
            "working_days": [1, 2, 3, 4, 5],
            "clock_in_grace_minutes": 30,
            "schedule_grace_minutes": 90,
        }

    def tearDown(self):
        self.tmp.cleanup()

    def test_install_scheduled_plists_does_not_enable_them(self):
        paths = self.manager.install_scheduled_plists(self.config)
        self.assertEqual(len(paths), 2)
        self.assertTrue(all(path.exists() for path in paths))
        self.assertEqual(self.runner.calls, [])

    def test_enable_bootstraps_both_independent_jobs(self):
        self.manager.install_scheduled_plists(self.config)
        self.manager.enable_schedules(self.config)
        bootstrap = [call for call in self.runner.calls if call[1] == "bootstrap"]
        self.assertEqual(len(bootstrap), 2)

    def test_schedule_sync_rewrites_calendar_time(self):
        paths = self.manager.install_scheduled_plists(self.config)
        self.config["in_time"] = "09:30"
        self.manager.sync_schedules(self.config)
        with paths[0].open("rb") as handle:
            data = plistlib.load(handle)
        self.assertIn(
            {"Weekday": 1, "Hour": 9, "Minute": 30},
            data["StartCalendarInterval"],
        )
        self.assertIn(
            {"Weekday": 1, "Hour": 10, "Minute": 0},
            data["StartCalendarInterval"],
        )

    def test_telegram_install_bootstraps_only_telegram_service(self):
        path = self.manager.install_telegram_service()
        self.assertTrue(path.exists())
        bootstrap = [call for call in self.runner.calls if call[1] == "bootstrap"]
        self.assertEqual(len(bootstrap), 1)
        self.assertIn("telegram", str(bootstrap[0][-1]))

    def test_bootstrap_retries_once_after_launchd_transition_race(self):
        calls = []
        sleeps = []

        def flaky_runner(args):
            calls.append(args)
            bootstrap_count = sum(1 for call in calls if call[1] == "bootstrap")
            return 5 if args[1] == "bootstrap" and bootstrap_count == 1 else 0

        manager = LaunchdManager(self.root, self.agents, uid=501, runner=flaky_runner, sleeper=sleeps.append)
        manager.install_telegram_service()
        self.assertEqual(sum(1 for call in calls if call[1] == "bootstrap"), 2)
        self.assertEqual(sleeps, [0.5])


if __name__ == "__main__":
    unittest.main()
