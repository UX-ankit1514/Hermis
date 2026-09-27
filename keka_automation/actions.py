from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
import plistlib
import re
import subprocess
from typing import Any, Callable

from .core import is_working_day, parse_hhmm
from .store import AttendanceStore


class AttendanceState(str, Enum):
    AUTH_REQUIRED = "AUTH_REQUIRED"
    READY_TO_CLOCK_IN = "READY_TO_CLOCK_IN"
    READY_TO_CLOCK_OUT = "READY_TO_CLOCK_OUT"
    DAY_COMPLETE = "DAY_COMPLETE"
    UNKNOWN = "UNKNOWN"


class BrowserAutomationError(RuntimeError):
    def __init__(self, message: str, category: str = "BROWSER"):
        super().__init__(message)
        self.category = category


def macos_boot_time(timezone) -> datetime | None:
    try:
        completed = subprocess.run(
            ["/usr/sbin/sysctl", "-n", "kern.boottime"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"sec\s*=\s*(\d+),\s*usec\s*=\s*(\d+)", completed.stdout)
    if completed.returncode != 0 or match is None:
        return None
    seconds = int(match.group(1)) + int(match.group(2)) / 1_000_000
    return datetime.fromtimestamp(seconds, timezone)


def macos_full_wake() -> bool | None:
    try:
        completed = subprocess.run(
            ["/usr/sbin/ioreg", "-r", "-c", "IOPMrootDomain", "-d", "1", "-a"],
            capture_output=True,
            timeout=2,
            check=False,
        )
        payload = plistlib.loads(completed.stdout)
    except (OSError, subprocess.SubprocessError, plistlib.InvalidFileException):
        return None
    if completed.returncode != 0 or not isinstance(payload, list) or not payload:
        return None
    value = payload[0].get("IOPMUserTriggeredFullWake")
    if not isinstance(value, bool):
        return None

    # IOPMUserTriggeredFullWake is historical state and can remain stale in
    # either direction. powerd's display-on assertion is the live signal: it
    # is present during an interactive full wake and absent during DarkWake.
    try:
        assertions = subprocess.run(
            ["/usr/bin/pmset", "-g", "assertions"],
            capture_output=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if assertions.returncode != 0:
        return None
    output = assertions.stdout
    if isinstance(output, bytes):
        output = output.decode("utf-8", errors="replace")
    return '"Powerd - Prevent sleep while display is on"' in output


def clock_in_dispatch_trigger(
    config: dict[str, Any], now: datetime, boot_time: datetime | None
) -> str:
    hour, minute = parse_hhmm(config["in_time"])
    scheduled = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if boot_time is not None and boot_time > scheduled:
        return "startup_catchup"
    return "scheduled"


@dataclass(frozen=True)
class ActionResult:
    action: str
    result: str
    verification: str
    error_category: str | None = None


class AttendanceController:
    def __init__(
        self,
        config: dict[str, Any],
        store: AttendanceStore,
        browser_factory: Callable[[], Any],
        boot_time_provider: Callable[[Any], datetime | None] | None = None,
        full_wake_provider: Callable[[], bool | None] | None = None,
    ):
        self.config = config
        self.store = store
        self.browser_factory = browser_factory
        self.boot_time_provider = boot_time_provider or macos_boot_time
        self.full_wake_provider = full_wake_provider or macos_full_wake

    def run(self, action: str, trigger: str, now: datetime) -> ActionResult:
        if action not in {"CLOCK_IN", "CLOCK_OUT"}:
            raise ValueError("Unsupported attendance action")
        if trigger not in {"scheduled", "startup_catchup", "manual"}:
            raise ValueError("Unsupported trigger")
        if trigger == "startup_catchup" and action != "CLOCK_IN":
            raise ValueError("Startup catch-up supports only Clock In")

        if trigger == "scheduled":
            gate = self._scheduled_gate(action, now)
            if gate:
                return self._record(now, action, trigger, gate, "NOT_ATTEMPTED", None)
        elif trigger == "startup_catchup":
            gate = self._startup_catchup_gate(now)
            if gate:
                return self._record(now, action, trigger, gate, "NOT_ATTEMPTED", None)

        if trigger != "manual" and self.full_wake_provider() is False:
            return self._record(
                now,
                action,
                trigger,
                "DEFERRED_ASLEEP",
                "NOT_ATTEMPTED",
                "POWER_STATE",
            )

        browser = None
        try:
            browser = self.browser_factory()
            state = browser.inspect_state()

            if state == AttendanceState.AUTH_REQUIRED:
                return self._record(
                    now, action, trigger, "AUTHENTICATION_REQUIRED", "NOT_VERIFIED", "AUTHENTICATION"
                )

            already = (
                action == "CLOCK_IN" and state in {AttendanceState.READY_TO_CLOCK_OUT, AttendanceState.DAY_COMPLETE}
            ) or (action == "CLOCK_OUT" and state == AttendanceState.DAY_COMPLETE)
            if already:
                return self._record(now, action, trigger, "ALREADY_DONE", "UI_CONFIRMED", None)

            day = now.date().isoformat()
            if self.store.was_successful(day, action):
                return self._record(now, action, trigger, "ALREADY_DONE", "LOCAL_IDEMPOTENCY", None)

            expected_start = (
                AttendanceState.READY_TO_CLOCK_IN if action == "CLOCK_IN" else AttendanceState.READY_TO_CLOCK_OUT
            )
            if state != expected_start:
                return self._record(now, action, trigger, "FAILED", "NOT_VERIFIED", "STATE_DETECTION")

            verified_state = browser.perform_and_verify(action)
            verified = (
                action == "CLOCK_IN" and verified_state == AttendanceState.READY_TO_CLOCK_OUT
            ) or (
                action == "CLOCK_OUT"
                and verified_state in {AttendanceState.DAY_COMPLETE, AttendanceState.READY_TO_CLOCK_IN}
            )
            if not verified:
                return self._record(now, action, trigger, "FAILED", "NOT_VERIFIED", "VERIFICATION")
            return self._record(now, action, trigger, "SUCCESS", "UI_CONFIRMED", None)
        except BrowserAutomationError as exc:
            if exc.category == "BUSY":
                return self._record(
                    now, action, trigger, "IN_PROGRESS", "NOT_ATTEMPTED", "BUSY"
                )
            return self._record(now, action, trigger, "FAILED", "NOT_VERIFIED", exc.category)
        except Exception:
            return self._record(now, action, trigger, "FAILED", "NOT_VERIFIED", "UNEXPECTED")
        finally:
            if browser is not None:
                browser.close()

    def _scheduled_gate(self, action: str, now: datetime) -> str | None:
        if not self.config["enabled"]:
            return "DISABLED"
        if not is_working_day(now.date(), self.config["working_days"]):
            return "NON_WORKING_DAY"
        key = "in_time" if action == "CLOCK_IN" else "out_time"
        hour, minute = parse_hhmm(self.config[key])
        scheduled = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        delta = now - scheduled
        grace_minutes = (
            self.config["clock_in_grace_minutes"]
            if action == "CLOCK_IN"
            else self.config["schedule_grace_minutes"]
        )
        if delta < timedelta(0) or delta > timedelta(minutes=grace_minutes):
            return "MISSED_WINDOW"
        boot_time = self.boot_time_provider(now.tzinfo)
        if boot_time is None or boot_time > scheduled:
            return "MISSED_WINDOW"
        return None

    def _startup_catchup_gate(self, now: datetime) -> str | None:
        if not self.config["enabled"]:
            return "DISABLED"
        if not is_working_day(now.date(), self.config["working_days"]):
            return "NON_WORKING_DAY"
        hour, minute = parse_hhmm(self.config["in_time"])
        scheduled = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        boot_time = self.boot_time_provider(now.tzinfo)
        if boot_time is None or boot_time <= scheduled:
            return "NOT_APPLICABLE"
        delta = now - scheduled
        if delta < timedelta(0):
            return "NOT_DUE"
        if delta > timedelta(minutes=self.config["clock_in_grace_minutes"]):
            return "MISSED_WINDOW"
        return None

    def _record(
        self,
        now: datetime,
        action: str,
        trigger: str,
        result: str,
        verification: str,
        error_category: str | None,
    ) -> ActionResult:
        self.store.record(
            date=now.date().isoformat(),
            execution_timestamp=now.isoformat(timespec="seconds"),
            action=action,
            trigger=trigger,
            result=result,
            verification=verification,
            error_category=error_category,
        )
        return ActionResult(action, result, verification, error_category)
