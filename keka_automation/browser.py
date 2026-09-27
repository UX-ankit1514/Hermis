from __future__ import annotations

import fcntl
import json
import os
import re
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError, sync_playwright

from .actions import AttendanceState, BrowserAutomationError


REQUIRED_SELECTORS = ("authenticated_marker", "clock_in")
KEKA_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sept", "Oct", "Nov", "Dec")


def keka_day_label(day: date) -> str:
    return f"{day.strftime('%a')}, {day.day:02d} {KEKA_MONTHS[day.month - 1]}"


def wait_for_meaningful_body(page: Page, timeout_ms: int = 20000) -> bool:
    deadline = time.monotonic() + timeout_ms / 1000
    stable_since = None
    last_signature = None
    while time.monotonic() < deadline:
        try:
            text = page.locator("body").inner_text(timeout=1000).strip()
            controls = page.locator("button, a, [role], input, nav, main").count()
            signature = (page.url, page.title(), len(text), controls)
            if text and controls:
                if signature != last_signature:
                    last_signature = signature
                    stable_since = time.monotonic()
                elif stable_since is not None and time.monotonic() - stable_since >= 1.25:
                    return True
            else:
                last_signature = None
                stable_since = None
        except Exception:
            last_signature = None
            stable_since = None
        page.wait_for_timeout(250)
    return False


def locator_from_spec(page: Page, spec: dict[str, Any]):
    strategy = spec.get("strategy")
    if strategy == "role":
        return page.get_by_role(
            spec["role"],
            name=spec.get("name"),
            exact=bool(spec.get("exact", True)),
        )
    if strategy == "label":
        return page.get_by_label(spec["name"], exact=bool(spec.get("exact", True)))
    if strategy == "testid":
        return page.get_by_test_id(spec["name"])
    if strategy == "text":
        return page.get_by_text(spec["name"], exact=bool(spec.get("exact", True)))
    if strategy == "css" and spec.get("selector"):
        return page.locator(spec["selector"])
    raise BrowserAutomationError("Unsupported or missing locator specification", "SETUP_REQUIRED")


def page_requires_authentication(page: Page, fail_closed: bool = False) -> bool:
    """Return True only on explicit auth evidence.

    fail_closed=True treats an inspection error as "auth may be required"
    (used by the login flow, which must never report success on doubt).
    """
    try:
        auth_inputs = page.locator(
            'input[type="password"], input[autocomplete="one-time-code"], '
            'input[name*="otp" i], input[id*="otp" i]'
        )
        if any(auth_inputs.nth(index).is_visible() for index in range(auth_inputs.count())):
            return True
        exact_auth_name = re.compile(r"^(sign in|log in|verify otp)$", re.IGNORECASE)
        for role in ("button", "link"):
            controls = page.get_by_role(role, name=exact_auth_name)
            if any(controls.nth(index).is_visible() for index in range(controls.count())):
                return True
    except Exception:
        return fail_closed
    return False


def acquire_profile_lock(profile: Path):
    profile = Path(profile)
    profile.mkdir(parents=True, exist_ok=True)
    lock_path = profile.parent / "browser.lock"
    lock_path.touch(mode=0o600, exist_ok=True)
    os.chmod(lock_path, 0o600)
    handle = lock_path.open("r+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.close()
        raise BrowserAutomationError(
            "Persistent browser profile is already in use", "BUSY"
        ) from exc
    return handle


def release_profile_lock(handle) -> None:
    if handle is None:
        return
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


class PageAttendanceAdapter:
    def __init__(
        self,
        page: Page,
        selectors: dict[str, Any],
        timeout_ms: int = 15000,
        date_provider=None,
    ):
        self.page = page
        self.selectors = selectors or {}
        self.timeout_ms = timeout_ms
        self.date_provider = date_provider or (lambda: datetime.now(ZoneInfo("Asia/Kolkata")).date())

    def inspect_state(self) -> AttendanceState:
        if any(not self.selectors.get(name) for name in REQUIRED_SELECTORS):
            raise BrowserAutomationError("Attendance selectors have not been configured", "SETUP_REQUIRED")
        if not self._visible("authenticated_marker"):
            return AttendanceState.AUTH_REQUIRED if self._authentication_required() else AttendanceState.UNKNOWN
        if self.selectors.get("day_complete") and self._visible("day_complete"):
            return AttendanceState.DAY_COMPLETE
        if self.selectors.get("clock_out") and self._visible("clock_out"):
            return AttendanceState.READY_TO_CLOCK_OUT
        if self._visible("clock_in"):
            row_text = self._today_row_text()
            if row_text and "No Time Entries Logged" not in row_text and re.search(r"\b\d+h\s+\d+m\b", row_text):
                return AttendanceState.DAY_COMPLETE
            return AttendanceState.READY_TO_CLOCK_IN
        return AttendanceState.UNKNOWN

    def perform_and_verify(self, action: str) -> AttendanceState:
        selector_name = "clock_in" if action == "CLOCK_IN" else "clock_out"
        control = locator_from_spec(self.page, self.selectors[selector_name])
        if control.count() != 1 or not control.is_visible():
            raise BrowserAutomationError("Expected attendance control is unavailable", "STATE_DETECTION")
        control.click(timeout=self.timeout_ms)

        if action == "CLOCK_OUT" and self.selectors.get("clock_out_confirm"):
            confirm = locator_from_spec(self.page, self.selectors["clock_out_confirm"])
            try:
                confirm.wait_for(state="visible", timeout=self.timeout_ms)
            except PlaywrightTimeoutError as exc:
                raise BrowserAutomationError("Clock Out confirmation did not appear", "VERIFICATION") from exc
            if confirm.count() != 1:
                raise BrowserAutomationError("Clock Out confirmation is not unique", "STATE_DETECTION")
            confirm.click(timeout=self.timeout_ms)

        deadline = time.monotonic() + (self.timeout_ms / 1000)
        latest = AttendanceState.UNKNOWN
        while time.monotonic() < deadline:
            latest = self.inspect_state()
            expected = (
                action == "CLOCK_IN" and latest == AttendanceState.READY_TO_CLOCK_OUT
            ) or (
                action == "CLOCK_OUT"
                and latest in {AttendanceState.DAY_COMPLETE, AttendanceState.READY_TO_CLOCK_IN}
            )
            if expected:
                return latest
            self.page.wait_for_timeout(250)
        return latest

    def accessibility_snapshot(self) -> str:
        snapshot = self.page.locator("body").aria_snapshot(timeout=self.timeout_ms)
        safe_lines = []
        for line in snapshot.splitlines():
            lowered = line.lower()
            if any(word in lowered for word in ("password", "textbox", "otp", "one-time")):
                continue
            safe_lines.append(line[:500])
        return "\n".join(safe_lines)[:30000]

    def _visible(self, name: str) -> bool:
        spec = self.selectors.get(name)
        if not spec:
            return False
        locator = locator_from_spec(self.page, spec)
        return locator.count() == 1 and locator.is_visible()

    def _authentication_required(self) -> bool:
        return page_requires_authentication(self.page)

    def _today_row_text(self) -> str | None:
        label = keka_day_label(self.date_provider())
        matches = self.page.get_by_text(label, exact=True)
        for index in range(matches.count()):
            text = matches.nth(index).evaluate(
                r"""(element, label) => {
                  let node = element;
                  for (let level = 0; level < 7 && node; level++, node = node.parentElement) {
                    const text = (node.innerText || '').trim().replace(/\s+/g, ' ');
                    if (text.includes(label) && text.length < 1000 &&
                        (text.includes('No Time Entries Logged') || /\b\d+h\s+\d+m\b/.test(text))) return text;
                  }
                  return null;
                }""",
                label,
            )
            if text:
                return text
        return None


def login_page_is_authenticated(
    page: Page,
    current_host: str | None,
    target_host: str | None,
    adapter: PageAttendanceAdapter,
) -> bool:
    return (
        current_host == target_host
        and adapter._visible("authenticated_marker")
        and not page_requires_authentication(page, fail_closed=True)
    )


class KekaBrowser:
    def __init__(self, config: dict[str, Any], headless: bool = True):
        self.config = config
        self.headless = headless
        self.playwright = None
        self.context = None
        self.page = None
        self.adapter = None
        self.profile_lock = None
        self._open()

    def _open(self) -> None:
        profile = Path(self.config["browser_profile"])
        profile.mkdir(parents=True, exist_ok=True)
        os.chmod(profile, 0o700)
        self.profile_lock = acquire_profile_lock(profile)
        try:
            self.playwright = sync_playwright().start()
            self.context = self.playwright.chromium.launch_persistent_context(
                user_data_dir=str(profile),
                headless=self.headless,
                viewport={"width": 1440, "height": 1000},
                locale="en-IN",
                timezone_id="Asia/Kolkata",
            )
            self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
            self.adapter = PageAttendanceAdapter(self.page, self.config.get("selectors") or {})
            self._navigate()
        except Exception:
            self.close()
            raise

    def _navigate(self) -> None:
        url = self.config.get("attendance_url") or self.config["base_url"]
        attempts = 1 + int(self.config.get("network_retries", 2))
        delays = (5, 15, 30)
        state_timeout_ms = int(self.config.get("attendance_state_timeout_ms", 12000))
        last_error = None
        for attempt in range(attempts):
            try:
                self.page.goto(url, wait_until="domcontentloaded", timeout=30000)
                if not wait_for_meaningful_body(self.page):
                    raise BrowserAutomationError("Keka page did not finish rendering", "BROWSER")
                state = self._wait_for_resolved_state(state_timeout_ms)
                if state == AttendanceState.UNKNOWN:
                    raise BrowserAutomationError("Keka attendance route did not become ready", "BROWSER")
                return
            except BrowserAutomationError as exc:
                last_error = exc
            except PlaywrightTimeoutError as exc:
                last_error = exc
            except Exception as exc:
                last_error = exc
            if attempt < attempts - 1:
                time.sleep(delays[min(attempt, len(delays) - 1)])
        category = last_error.category if isinstance(last_error, BrowserAutomationError) else "NETWORK"
        raise BrowserAutomationError(f"Keka navigation failed: {type(last_error).__name__}", category)

    def _wait_for_resolved_state(self, timeout_ms: int) -> AttendanceState:
        deadline = time.monotonic() + max(0, timeout_ms) / 1000
        while True:
            state = self.adapter.inspect_state()
            if state != AttendanceState.UNKNOWN or time.monotonic() >= deadline:
                return state
            self.page.wait_for_timeout(250)

    def inspect_state(self) -> AttendanceState:
        return self.adapter.inspect_state()

    def perform_and_verify(self, action: str) -> AttendanceState:
        return self.adapter.perform_and_verify(action)

    def sanitized_probe(self) -> dict[str, Any]:
        return {
            "url": self.page.url,
            "title": self.page.title()[:200],
            "state": self.inspect_state().value,
        }

    def close(self) -> None:
        if self.context is not None:
            try:
                self.context.close()
            except Exception:
                pass
            self.context = None
        if self.playwright is not None:
            try:
                self.playwright.stop()
            except Exception:
                pass
            self.playwright = None
        if self.profile_lock is not None:
            release_profile_lock(self.profile_lock)
            self.profile_lock = None


def interactive_login(config: dict[str, Any], observation_path: Path, timeout_seconds: int = 900) -> bool:
    browser = KekaBrowser(config, headless=False)
    target_host = urlparse(config["base_url"]).hostname
    adapter = PageAttendanceAdapter(browser.page, config.get("selectors") or {})
    stable_since = None
    try:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if browser.page.is_closed():
                return False
            current_host = urlparse(browser.page.url).hostname
            candidate = login_page_is_authenticated(
                browser.page, current_host, target_host, adapter
            )
            if candidate:
                stable_since = stable_since or time.monotonic()
                if time.monotonic() - stable_since >= 8:
                    data = {
                        "url": browser.page.url,
                        "title": browser.page.title()[:200],
                        "accessibility": adapter.accessibility_snapshot(),
                    }
                    observation_path.parent.mkdir(parents=True, exist_ok=True)
                    observation_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
                    os.chmod(observation_path, 0o600)
                    return True
            else:
                stable_since = None
            browser.page.wait_for_timeout(1000)
        return False
    finally:
        browser.close()
