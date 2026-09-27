from __future__ import annotations

import fcntl
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .actions import ActionResult, AttendanceController, AttendanceState, BrowserAutomationError
from .browser import KekaBrowser
from .config import ConfigManager
from .core import parse_hhmm
from .store import AttendanceStore


WEEKDAY_NAMES = {1: "Mon", 2: "Tue", 3: "Wed", 4: "Thu", 5: "Fri", 6: "Sat", 7: "Sun"}


def format_action_result(result: ActionResult) -> str:
    if result.result == "AUTHENTICATION_REQUIRED":
        return "Authentication required"
    if result.result == "SUCCESS":
        return "Clock In successful" if result.action == "CLOCK_IN" else "Clock Out successful"
    if result.result == "ALREADY_DONE":
        return "Already clocked in" if result.action == "CLOCK_IN" else "Already clocked out"
    if result.result == "IN_PROGRESS":
        return "Attendance action already in progress — run /test shortly"
    return "Attendance action failed"


def format_schedule(config: dict[str, Any]) -> str:
    state = "Enabled" if config["enabled"] else "Disabled"
    days = ", ".join(WEEKDAY_NAMES[day] for day in config["working_days"])
    return f"{state} | IN {config['in_time']} | OUT {config['out_time']} | {days} | Asia/Kolkata"


def scheduled_notification(result: ActionResult) -> str | None:
    if result.result in {
        "DISABLED",
        "NON_WORKING_DAY",
        "MISSED_WINDOW",
        "DEFERRED_ASLEEP",
        "IN_PROGRESS",
        "RETRY_LIMIT",
    }:
        return None
    if result.result == "ALREADY_DONE" and result.verification == "LOCAL_IDEMPOTENCY":
        return None
    if result.result == "AUTHENTICATION_REQUIRED":
        return "Keka session expired — send /reauth"
    if result.result in {"SUCCESS", "ALREADY_DONE"}:
        return format_action_result(result)
    action = "Clock In" if result.action == "CLOCK_IN" else "Clock Out"
    if result.error_category == "NETWORK":
        return f"{action} failed — Keka unreachable"
    return f"{action} failed"


def startup_catchup_notification(
    result: ActionResult, in_time: str, grace_minutes: int
) -> str | None:
    if result.result in {"DISABLED", "NON_WORKING_DAY", "NOT_DUE", "NOT_APPLICABLE"}:
        return None
    if result.result == "MISSED_WINDOW":
        hour, minute = parse_hhmm(in_time)
        end = datetime(2000, 1, 1, hour, minute) + timedelta(minutes=grace_minutes)
        return (
            "Clock In missed — Mac became available outside the "
            f"{in_time}–{end.strftime('%H:%M')} grace window."
        )
    return scheduled_notification(result)


def format_recent_logs(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "No attendance execution logs yet"
    lines = []
    for row in rows[-10:]:
        error = f" [{row.get('error_category')}]" if row.get("error_category") else ""
        lines.append(
            f"{row.get('execution_timestamp')} {row.get('action')} {row.get('trigger')} "
            f"{row.get('result')} {row.get('verification')}{error}"
        )
    return "\n".join(lines)


class Operations:
    def __init__(
        self,
        root: Path,
        launchd_manager: Any | None = None,
        browser_factory: Callable[[dict[str, Any]], Any] | None = None,
    ):
        self.root = Path(root)
        self.config_manager = ConfigManager(self.root / "config.json")
        self.store = AttendanceStore(self.root)
        self.launchd_manager = launchd_manager
        self.browser_factory = browser_factory or (lambda config: KekaBrowser(config, headless=True))

    @property
    def production_approved(self) -> bool:
        return bool(self.config_manager.load().get("production_approved"))

    def status(self) -> str:
        config = self.config_manager.load()
        return f"{format_schedule(config)}\nToday's attendance: {self.test().removeprefix('Test: ')}"

    def run_action(self, action: str, trigger: str = "manual") -> str:
        result = self.run_action_result(action, trigger)
        return format_action_result(result)

    def run_action_result(self, action: str, trigger: str) -> ActionResult:
        config = self.config_manager.load()
        now = datetime.now(ZoneInfo(config["timezone"]))
        early = self._scheduled_short_circuit(now, action, trigger)
        if early is not None:
            return early
        lock_path = self.root / "run.lock"
        lock_path.touch(mode=0o600, exist_ok=True)
        with lock_path.open("r+") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                self.store.record(
                    date=now.date().isoformat(),
                    execution_timestamp=now.isoformat(timespec="seconds"),
                    action=action,
                    trigger=trigger,
                    result="IN_PROGRESS",
                    verification="NOT_ATTEMPTED",
                    error_category="BUSY",
                )
                return ActionResult(action, "IN_PROGRESS", "NOT_ATTEMPTED", "BUSY")
            # Re-check under the lock: another worker may have finished
            # (verified success or terminal failure) while we were waiting.
            early = self._scheduled_short_circuit(now, action, trigger)
            if early is not None:
                return early
            controller = AttendanceController(
                config,
                self.store,
                lambda: self.browser_factory(config),
            )
            return controller.run(action, trigger, now)

    def _scheduled_short_circuit(
        self, now: datetime, action: str, trigger: str
    ) -> ActionResult | None:
        if trigger == "manual":
            return None
        day = now.date().isoformat()
        if self.store.was_successful(day, action):
            return ActionResult(action, "ALREADY_DONE", "LOCAL_IDEMPOTENCY")
        if self._scheduled_retry_exhausted(day, action):
            return ActionResult(action, "RETRY_LIMIT", "NOT_ATTEMPTED")
        return None

    def _scheduled_retry_exhausted(self, day: str, action: str) -> bool:
        rows = [
            row
            for row in self.store.recent(500)
            if row["date"] == day
            and row["action"] == action
            and row["trigger"] in {"scheduled", "startup_catchup"}
        ]
        if any(row["result"] == "AUTHENTICATION_REQUIRED" for row in rows):
            return True
        failed_workers = sum(row["result"] == "FAILED" for row in rows)
        return failed_workers >= 3

    def enable(self) -> str:
        config = self.config_manager.load()
        if not config.get("production_approved"):
            return "Production scheduling is not approved yet"
        try:
            self.config_manager.set_enabled(True)
            if self.launchd_manager:
                self.launchd_manager.enable_schedules(self.config_manager.load())
        except Exception:
            self.config_manager.set_enabled(False)
            return "Automation enable failed"
        return "Automation enabled"

    def disable(self) -> str:
        self.config_manager.set_enabled(False)
        try:
            if self.launchd_manager:
                self.launchd_manager.disable_schedules()
        except Exception:
            return "Automation disabled; launchd cleanup needs attention"
        return "Automation disabled"

    def schedule(self) -> str:
        return format_schedule(self.config_manager.load())

    def grace(self) -> str:
        config = self.config_manager.load()
        hour, minute = parse_hhmm(config["in_time"])
        end = datetime(2000, 1, 1, hour, minute) + timedelta(
            minutes=config["clock_in_grace_minutes"]
        )
        return (
            f"Clock In grace: {config['clock_in_grace_minutes']} minutes "
            f"({config['in_time']}–{end.strftime('%H:%M')} Asia/Kolkata)"
        )

    def set_grace(self, minutes: int) -> str:
        config = self.config_manager.update_clock_in_grace(minutes)
        return f"Clock In grace updated: {config['clock_in_grace_minutes']} minutes"

    def set_schedule(self, action: str, value: str) -> str:
        config = self.config_manager.update_schedule(action, value)
        if self.launchd_manager:
            self.launchd_manager.sync_schedules(config)
        return f"Schedule updated: {action} {value}\n{format_schedule(config)}"

    def working_days(self) -> str:
        days = ", ".join(WEEKDAY_NAMES[day] for day in self.config_manager.load()["working_days"])
        return f"Working days: {days}"

    def set_working_days(self, days: list[int]) -> str:
        config = self.config_manager.update_working_days(days)
        if self.launchd_manager:
            self.launchd_manager.sync_schedules(config)
        return self.working_days()

    def logs(self) -> str:
        return format_recent_logs(self.store.recent(10))

    def login(self, reauth: bool = False) -> str:
        subprocess.Popen(
            [str(self.root / "bin" / "keka-attendance"), "login-worker"],
            cwd=str(self.root),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return "Keka re-authentication window opened on Mac" if reauth else "Keka login window opened on Mac"

    def setcredentials(self) -> str:
        subprocess.Popen(
            ["/usr/bin/open", str(self.root / "bin" / "secure-keychain-setup.command")],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return "Secure credential entry opened on Mac"

    def test(self) -> str:
        lock_path = self.root / "run.lock"
        lock_path.touch(mode=0o600, exist_ok=True)
        with lock_path.open("r+") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return "Test: attendance action in progress"
            return self._test_with_profile()

    def _test_with_profile(self) -> str:
        browser = None
        try:
            config = self.config_manager.load()
            browser = self.browser_factory(config)
            state = browser.inspect_state()
            messages = {
                AttendanceState.AUTH_REQUIRED: "authentication required",
                AttendanceState.READY_TO_CLOCK_IN: "session valid; ready to Clock In",
                AttendanceState.READY_TO_CLOCK_OUT: "session valid; currently clocked in",
                AttendanceState.DAY_COMPLETE: "session valid; Clock Out complete",
                AttendanceState.UNKNOWN: "session valid; attendance state unknown",
            }
            return "Test: " + messages[state]
        except BrowserAutomationError as exc:
            if exc.category == "SETUP_REQUIRED":
                return "Test: login/UI inspection required"
            if exc.category == "NETWORK":
                return "Test: Keka unreachable"
            return "Test: browser failure"
        except Exception:
            return "Test: browser failure"
        finally:
            if browser is not None:
                browser.close()
