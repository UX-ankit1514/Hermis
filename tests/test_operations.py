import fcntl
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from keka_automation.actions import ActionResult
from keka_automation.operations import (
    Operations,
    format_action_result,
    format_recent_logs,
    format_schedule,
    scheduled_notification,
    startup_catchup_notification,
)


class FormattingTests(unittest.TestCase):
    def test_manual_action_messages_match_required_responses(self):
        cases = [
            (ActionResult("CLOCK_IN", "SUCCESS", "UI_CONFIRMED"), "Clock In successful"),
            (ActionResult("CLOCK_IN", "ALREADY_DONE", "UI_CONFIRMED"), "Already clocked in"),
            (ActionResult("CLOCK_OUT", "SUCCESS", "UI_CONFIRMED"), "Clock Out successful"),
            (ActionResult("CLOCK_OUT", "ALREADY_DONE", "UI_CONFIRMED"), "Already clocked out"),
            (ActionResult("CLOCK_IN", "AUTHENTICATION_REQUIRED", "NOT_VERIFIED", "AUTHENTICATION"), "Authentication required"),
            (ActionResult("CLOCK_OUT", "FAILED", "NOT_VERIFIED", "NETWORK"), "Attendance action failed"),
            (ActionResult("CLOCK_IN", "IN_PROGRESS", "NOT_ATTEMPTED", "BUSY"), "Attendance action already in progress — run /test shortly"),
        ]
        for result, expected in cases:
            with self.subTest(result=result):
                self.assertEqual(format_action_result(result), expected)

    def test_schedule_is_human_readable(self):
        config = {"enabled": False, "in_time": "10:00", "out_time": "19:00", "working_days": [1, 2, 3, 4, 5]}
        text = format_schedule(config)
        self.assertIn("Disabled", text)
        self.assertIn("10:00", text)
        self.assertIn("Mon, Tue, Wed, Thu, Fri", text)

    def test_recent_logs_include_only_sanitized_columns(self):
        rows = [{
            "date": "2026-08-31",
            "execution_timestamp": "2026-08-31T10:00:00+05:30",
            "action": "CLOCK_IN",
            "trigger": "manual",
            "result": "SUCCESS",
            "verification": "UI_CONFIRMED",
            "error_category": None,
            "secret": "never",
        }]
        text = format_recent_logs(rows)
        self.assertIn("CLOCK_IN", text)
        self.assertNotIn("never", text)
        self.assertNotIn("secret", text)

    def test_authentication_failure_notification_requests_reauth(self):
        result = ActionResult("CLOCK_IN", "AUTHENTICATION_REQUIRED", "NOT_VERIFIED", "AUTHENTICATION")
        self.assertEqual(scheduled_notification(result), "Keka session expired — send /reauth")

    def test_disabled_schedule_does_not_notify(self):
        result = ActionResult("CLOCK_IN", "DISABLED", "NOT_ATTEMPTED")
        self.assertIsNone(scheduled_notification(result))

    def test_dark_wake_and_local_retry_noops_do_not_notify(self):
        asleep = ActionResult("CLOCK_IN", "DEFERRED_ASLEEP", "NOT_ATTEMPTED")
        local_noop = ActionResult("CLOCK_IN", "ALREADY_DONE", "LOCAL_IDEMPOTENCY")
        retry_limit = ActionResult("CLOCK_IN", "RETRY_LIMIT", "NOT_ATTEMPTED")
        self.assertIsNone(scheduled_notification(asleep))
        self.assertIsNone(scheduled_notification(local_noop))
        self.assertIsNone(scheduled_notification(retry_limit))

    def test_startup_catchup_outside_grace_sends_exact_missed_notification(self):
        result = ActionResult("CLOCK_IN", "MISSED_WINDOW", "NOT_ATTEMPTED")
        self.assertEqual(
            startup_catchup_notification(result, "10:00", 30),
            "Clock In missed — Mac became available outside the 10:00–10:30 grace window.",
        )


class OperationExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.ops = Operations(
            self.root,
            browser_factory=lambda config: (_ for _ in ()).throw(
                AssertionError("browser must not open")
            ),
        )
        config = self.ops.config_manager.ensure_defaults()
        config["enabled"] = True
        self.ops.config_manager.save(config)

    def tearDown(self):
        self.tmp.cleanup()

    def test_scheduled_retry_skips_browser_after_verified_local_success(self):
        now = datetime.now(ZoneInfo("Asia/Kolkata"))
        self.ops.store.record(
            date=now.date().isoformat(),
            execution_timestamp=now.isoformat(timespec="seconds"),
            action="CLOCK_IN",
            trigger="scheduled",
            result="SUCCESS",
            verification="UI_CONFIRMED",
            error_category=None,
        )
        before = len(self.ops.store.recent(100))
        result = self.ops.run_action_result("CLOCK_IN", "scheduled")
        self.assertEqual(result.result, "ALREADY_DONE")
        self.assertEqual(result.verification, "LOCAL_IDEMPOTENCY")
        self.assertEqual(len(self.ops.store.recent(100)), before)

    def test_overlapping_manual_action_is_reported_as_in_progress_not_failed(self):
        lock_path = self.root / "run.lock"
        lock_path.touch(mode=0o600)
        with lock_path.open("r+") as held:
            fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.ops.run_action_result("CLOCK_IN", "manual")
        self.assertEqual(result.result, "IN_PROGRESS")
        self.assertEqual(result.verification, "NOT_ATTEMPTED")

    def test_status_probe_never_opens_persistent_profile_during_action(self):
        lock_path = self.root / "run.lock"
        lock_path.touch(mode=0o600)
        with lock_path.open("r+") as held:
            fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.ops.test()
        self.assertEqual(result, "Test: attendance action in progress")

    def test_scheduled_browser_retries_stop_after_three_failed_workers(self):
        now = datetime.now(ZoneInfo("Asia/Kolkata"))
        for minute in range(3):
            self.ops.store.record(
                date=now.date().isoformat(),
                execution_timestamp=now.replace(minute=minute).isoformat(timespec="seconds"),
                action="CLOCK_IN",
                trigger="scheduled",
                result="FAILED",
                verification="NOT_VERIFIED",
                error_category="BROWSER",
            )
        before = len(self.ops.store.recent(100))
        result = self.ops.run_action_result("CLOCK_IN", "scheduled")
        self.assertEqual(result.result, "RETRY_LIMIT")
        self.assertEqual(result.verification, "NOT_ATTEMPTED")
        self.assertEqual(len(self.ops.store.recent(100)), before)

    def test_scheduled_authentication_failure_is_not_retried(self):
        now = datetime.now(ZoneInfo("Asia/Kolkata"))
        self.ops.store.record(
            date=now.date().isoformat(),
            execution_timestamp=now.isoformat(timespec="seconds"),
            action="CLOCK_IN",
            trigger="scheduled",
            result="AUTHENTICATION_REQUIRED",
            verification="NOT_VERIFIED",
            error_category="AUTHENTICATION",
        )
        result = self.ops.run_action_result("CLOCK_IN", "scheduled")
        self.assertEqual(result.result, "RETRY_LIMIT")

    def test_terminal_state_written_while_waiting_for_lock_is_rechecked(self):
        # Simulates a racing worker: the pre-lock check sees a clean day, then
        # another worker records AUTHENTICATION_REQUIRED before this one gets
        # run.lock. The browser must still never open.
        now = datetime.now(ZoneInfo("Asia/Kolkata"))
        real_flock = fcntl.flock
        store = self.ops.store

        def racing_flock(fd, op):
            if op & fcntl.LOCK_EX and op & fcntl.LOCK_NB:
                store.record(
                    date=now.date().isoformat(),
                    execution_timestamp=now.isoformat(timespec="seconds"),
                    action="CLOCK_IN",
                    trigger="scheduled",
                    result="AUTHENTICATION_REQUIRED",
                    verification="NOT_VERIFIED",
                    error_category="AUTHENTICATION",
                )
            return real_flock(fd, op)

        with patch("keka_automation.operations.fcntl.flock", side_effect=racing_flock):
            result = self.ops.run_action_result("CLOCK_IN", "scheduled")
        self.assertEqual(result.result, "RETRY_LIMIT")

    def test_verified_success_written_while_waiting_for_lock_is_rechecked(self):
        now = datetime.now(ZoneInfo("Asia/Kolkata"))
        real_flock = fcntl.flock
        store = self.ops.store

        def racing_flock(fd, op):
            if op & fcntl.LOCK_EX and op & fcntl.LOCK_NB:
                store.record(
                    date=now.date().isoformat(),
                    execution_timestamp=now.isoformat(timespec="seconds"),
                    action="CLOCK_IN",
                    trigger="scheduled",
                    result="SUCCESS",
                    verification="UI_CONFIRMED",
                    error_category=None,
                )
            return real_flock(fd, op)

        with patch("keka_automation.operations.fcntl.flock", side_effect=racing_flock):
            result = self.ops.run_action_result("CLOCK_IN", "scheduled")
        self.assertEqual(result.result, "ALREADY_DONE")
        self.assertEqual(result.verification, "LOCAL_IDEMPOTENCY")


if __name__ == "__main__":
    unittest.main()
