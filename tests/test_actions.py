import tempfile
import unittest
import plistlib
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from keka_automation.actions import (
    AttendanceController,
    AttendanceState,
    BrowserAutomationError,
    clock_in_dispatch_trigger,
    macos_full_wake,
)
from keka_automation.store import AttendanceStore


IST = ZoneInfo("Asia/Kolkata")


class FakeBrowser:
    def __init__(self, state, after=None):
        self.state = state
        self.after = after
        self.performed = []

    def inspect_state(self):
        return self.state

    def perform_and_verify(self, action):
        self.performed.append(action)
        return self.after

    def close(self):
        pass


class DispatchTests(unittest.TestCase):
    def test_dispatch_uses_startup_trigger_only_when_booted_after_target(self):
        config = {"in_time": "10:00"}
        now = datetime(2026, 9, 1, 10, 17, tzinfo=IST)
        self.assertEqual(
            clock_in_dispatch_trigger(config, now, datetime(2026, 9, 1, 10, 5, tzinfo=IST)),
            "startup_catchup",
        )
        self.assertEqual(
            clock_in_dispatch_trigger(config, now, datetime(2026, 9, 1, 9, 55, tzinfo=IST)),
            "scheduled",
        )
        self.assertEqual(clock_in_dispatch_trigger(config, now, None), "scheduled")


class PowerStateTests(unittest.TestCase):
    def test_macos_full_wake_reads_iopm_user_triggered_full_wake(self):
        payload = plistlib.dumps([{"IOPMUserTriggeredFullWake": False}])
        completed = SimpleNamespace(returncode=0, stdout=payload)
        with patch("keka_automation.actions.subprocess.run", return_value=completed):
            self.assertFalse(macos_full_wake())

    def test_macos_full_wake_rejects_stale_true_flag_without_live_display(self):
        ioreg = SimpleNamespace(
            returncode=0,
            stdout=plistlib.dumps([{"IOPMUserTriggeredFullWake": True}]),
        )
        pmset = SimpleNamespace(
            returncode=0,
            stdout=b"Assertion status system-wide: PreventUserIdleSystemSleep 0",
        )
        with patch(
            "keka_automation.actions.subprocess.run", side_effect=[ioreg, pmset]
        ):
            self.assertFalse(macos_full_wake())

    def test_macos_full_wake_accepts_live_display_on_when_wake_flag_is_stale(self):
        ioreg = SimpleNamespace(
            returncode=0,
            stdout=plistlib.dumps([{"IOPMUserTriggeredFullWake": False}]),
        )
        pmset = SimpleNamespace(
            returncode=0,
            stdout=(
                b'pid 339(powerd): PreventUserIdleSystemSleep named: '
                b'"Powerd - Prevent sleep while display is on"'
            ),
        )
        with patch(
            "keka_automation.actions.subprocess.run", side_effect=[ioreg, pmset]
        ):
            self.assertTrue(macos_full_wake())

    def test_macos_full_wake_fails_open_when_power_state_is_unavailable(self):
        completed = SimpleNamespace(returncode=1, stdout=b"")
        with patch("keka_automation.actions.subprocess.run", return_value=completed):
            self.assertIsNone(macos_full_wake())


class ActionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = AttendanceStore(Path(self.tmp.name))
        self.config = {
            "enabled": True,
            "working_days": [1, 2, 3, 4, 5],
            "in_time": "10:00",
            "out_time": "19:00",
            "schedule_grace_minutes": 90,
            "clock_in_grace_minutes": 30,
        }

    def tearDown(self):
        self.tmp.cleanup()

    def controller(self, browser):
        return AttendanceController(
            self.config,
            self.store,
            lambda: browser,
            boot_time_provider=lambda timezone: datetime(2026, 8, 1, tzinfo=timezone),
        )

    def test_scheduled_action_does_not_run_while_disabled(self):
        self.config["enabled"] = False
        browser = FakeBrowser(AttendanceState.READY_TO_CLOCK_IN)
        result = self.controller(browser).run("CLOCK_IN", "scheduled", datetime(2026, 8, 31, 10, 0, tzinfo=IST))
        self.assertEqual(result.result, "DISABLED")
        self.assertEqual(browser.performed, [])

    def test_dark_wake_defers_scheduled_action_before_opening_browser(self):
        def refuse_browser():
            raise AssertionError("browser must not open during a dark wake")

        controller = AttendanceController(
            self.config,
            self.store,
            refuse_browser,
            boot_time_provider=lambda timezone: datetime(2026, 8, 1, tzinfo=timezone),
            full_wake_provider=lambda: False,
        )
        result = controller.run(
            "CLOCK_IN", "scheduled", datetime(2026, 9, 24, 10, 0, tzinfo=IST)
        )
        self.assertEqual(result.result, "DEFERRED_ASLEEP")
        self.assertEqual(result.verification, "NOT_ATTEMPTED")
        self.assertEqual(result.error_category, "POWER_STATE")

    def test_manual_action_is_not_blocked_by_full_wake_preflight(self):
        browser = FakeBrowser(AttendanceState.READY_TO_CLOCK_IN, AttendanceState.READY_TO_CLOCK_OUT)
        controller = AttendanceController(
            self.config,
            self.store,
            lambda: browser,
            boot_time_provider=lambda timezone: datetime(2026, 8, 1, tzinfo=timezone),
            full_wake_provider=lambda: False,
        )
        result = controller.run(
            "CLOCK_IN", "manual", datetime(2026, 9, 24, 10, 17, tzinfo=IST)
        )
        self.assertEqual(result.result, "SUCCESS")

    def test_profile_lock_collision_is_in_progress_not_failed(self):
        def busy_browser():
            raise BrowserAutomationError("profile is in use", "BUSY")

        controller = AttendanceController(
            self.config,
            self.store,
            busy_browser,
            boot_time_provider=lambda timezone: datetime(2026, 8, 1, tzinfo=timezone),
            full_wake_provider=lambda: True,
        )
        result = controller.run(
            "CLOCK_IN", "scheduled", datetime(2026, 9, 24, 10, 17, tzinfo=IST)
        )
        self.assertEqual(result.result, "IN_PROGRESS")
        self.assertEqual(result.verification, "NOT_ATTEMPTED")

    def test_scheduled_action_skips_non_working_day(self):
        browser = FakeBrowser(AttendanceState.READY_TO_CLOCK_IN)
        result = self.controller(browser).run("CLOCK_IN", "scheduled", datetime(2026, 9, 5, 10, 0, tzinfo=IST))
        self.assertEqual(result.result, "NON_WORKING_DAY")
        self.assertEqual(browser.performed, [])

    def test_expired_schedule_window_never_backfills(self):
        browser = FakeBrowser(AttendanceState.READY_TO_CLOCK_IN)
        result = self.controller(browser).run("CLOCK_IN", "scheduled", datetime(2026, 8, 31, 12, 0, tzinfo=IST))
        self.assertEqual(result.result, "MISSED_WINDOW")
        self.assertEqual(browser.performed, [])

    def test_scheduled_clock_in_uses_configured_grace_window(self):
        inside = FakeBrowser(AttendanceState.READY_TO_CLOCK_IN, AttendanceState.READY_TO_CLOCK_OUT)
        inside_result = self.controller(inside).run(
            "CLOCK_IN", "scheduled", datetime(2026, 9, 1, 10, 30, tzinfo=IST)
        )
        self.assertEqual(inside_result.result, "SUCCESS")
        self.assertEqual(inside.performed, ["CLOCK_IN"])

        outside = FakeBrowser(AttendanceState.READY_TO_CLOCK_IN, AttendanceState.READY_TO_CLOCK_OUT)
        outside_result = self.controller(outside).run(
            "CLOCK_IN", "scheduled", datetime(2026, 9, 2, 10, 31, tzinfo=IST)
        )
        self.assertEqual(outside_result.result, "MISSED_WINDOW")
        self.assertEqual(outside.performed, [])

    def test_reboot_after_schedule_never_backfills_within_grace_window(self):
        browser = FakeBrowser(AttendanceState.READY_TO_CLOCK_IN, AttendanceState.READY_TO_CLOCK_OUT)
        controller = AttendanceController(
            self.config,
            self.store,
            lambda: browser,
            boot_time_provider=lambda timezone: datetime(2026, 8, 31, 10, 15, tzinfo=timezone),
        )
        result = controller.run("CLOCK_IN", "scheduled", datetime(2026, 8, 31, 10, 30, tzinfo=IST))
        self.assertEqual(result.result, "MISSED_WINDOW")
        self.assertEqual(browser.performed, [])

    def test_startup_catchup_allows_one_clock_in_inside_grace_and_logs_real_trigger(self):
        browser = FakeBrowser(AttendanceState.READY_TO_CLOCK_IN, AttendanceState.READY_TO_CLOCK_OUT)
        controller = AttendanceController(
            self.config,
            self.store,
            lambda: browser,
            boot_time_provider=lambda timezone: datetime(2026, 9, 1, 10, 5, tzinfo=timezone),
        )
        result = controller.run(
            "CLOCK_IN", "startup_catchup", datetime(2026, 9, 1, 10, 17, tzinfo=IST)
        )
        self.assertEqual(result.result, "SUCCESS")
        self.assertEqual(browser.performed, ["CLOCK_IN"])
        self.assertEqual(self.store.recent(1)[0]["trigger"], "startup_catchup")
        self.assertEqual(
            self.store.recent(1)[0]["execution_timestamp"], "2026-09-01T10:17:00+05:30"
        )

    def test_startup_catchup_after_grace_never_opens_browser(self):
        def refuse_browser():
            raise AssertionError("browser must not open outside grace")

        controller = AttendanceController(
            self.config,
            self.store,
            refuse_browser,
            boot_time_provider=lambda timezone: datetime(2026, 9, 1, 10, 5, tzinfo=timezone),
        )
        result = controller.run(
            "CLOCK_IN", "startup_catchup", datetime(2026, 9, 1, 10, 31, tzinfo=IST)
        )
        self.assertEqual(result.result, "MISSED_WINDOW")
        self.assertEqual(result.verification, "NOT_ATTEMPTED")

    def test_startup_catchup_checks_remote_state_before_clicking(self):
        browser = FakeBrowser(AttendanceState.READY_TO_CLOCK_OUT)
        controller = AttendanceController(
            self.config,
            self.store,
            lambda: browser,
            boot_time_provider=lambda timezone: datetime(2026, 9, 1, 10, 5, tzinfo=timezone),
        )
        result = controller.run(
            "CLOCK_IN", "startup_catchup", datetime(2026, 9, 1, 10, 17, tzinfo=IST)
        )
        self.assertEqual(result.result, "ALREADY_DONE")
        self.assertEqual(result.verification, "UI_CONFIRMED")
        self.assertEqual(browser.performed, [])

    def test_startup_catchup_checks_enabled_and_working_day_before_browser(self):
        def refuse_browser():
            raise AssertionError("browser must not open when catch-up is gated")

        self.config["enabled"] = False
        disabled = AttendanceController(
            self.config,
            self.store,
            refuse_browser,
            boot_time_provider=lambda timezone: datetime(2026, 9, 1, 10, 5, tzinfo=timezone),
        ).run("CLOCK_IN", "startup_catchup", datetime(2026, 9, 1, 10, 17, tzinfo=IST))
        self.assertEqual(disabled.result, "DISABLED")

        self.config["enabled"] = True
        non_working = AttendanceController(
            self.config,
            self.store,
            refuse_browser,
            boot_time_provider=lambda timezone: datetime(2026, 9, 5, 10, 5, tzinfo=timezone),
        ).run("CLOCK_IN", "startup_catchup", datetime(2026, 9, 5, 10, 17, tzinfo=IST))
        self.assertEqual(non_working.result, "NON_WORKING_DAY")

    def test_clock_in_detects_already_clocked_in(self):
        browser = FakeBrowser(AttendanceState.READY_TO_CLOCK_OUT)
        result = self.controller(browser).run("CLOCK_IN", "manual", datetime(2026, 8, 31, 10, 0, tzinfo=IST))
        self.assertEqual(result.result, "ALREADY_DONE")
        self.assertEqual(browser.performed, [])

    def test_clock_out_detects_completed_day(self):
        browser = FakeBrowser(AttendanceState.DAY_COMPLETE)
        result = self.controller(browser).run("CLOCK_OUT", "manual", datetime(2026, 8, 31, 19, 0, tzinfo=IST))
        self.assertEqual(result.result, "ALREADY_DONE")
        self.assertEqual(browser.performed, [])

    def test_authentication_expiry_stops_action(self):
        browser = FakeBrowser(AttendanceState.AUTH_REQUIRED)
        result = self.controller(browser).run("CLOCK_IN", "manual", datetime(2026, 8, 31, 10, 0, tzinfo=IST))
        self.assertEqual(result.result, "AUTHENTICATION_REQUIRED")
        self.assertEqual(browser.performed, [])

    def test_clock_in_clicks_once_and_requires_verified_state(self):
        browser = FakeBrowser(AttendanceState.READY_TO_CLOCK_IN, AttendanceState.READY_TO_CLOCK_OUT)
        result = self.controller(browser).run("CLOCK_IN", "manual", datetime(2026, 8, 31, 10, 0, tzinfo=IST))
        self.assertEqual(result.result, "SUCCESS")
        self.assertEqual(browser.performed, ["CLOCK_IN"])

    def test_failed_verification_is_not_success(self):
        browser = FakeBrowser(AttendanceState.READY_TO_CLOCK_IN, AttendanceState.UNKNOWN)
        result = self.controller(browser).run("CLOCK_IN", "manual", datetime(2026, 8, 31, 10, 0, tzinfo=IST))
        self.assertEqual(result.result, "FAILED")
        self.assertEqual(result.error_category, "VERIFICATION")


if __name__ == "__main__":
    unittest.main()
