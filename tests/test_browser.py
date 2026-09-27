import unittest
import tempfile
from datetime import date
from pathlib import Path
from unittest.mock import patch

from playwright.sync_api import sync_playwright

from keka_automation.actions import AttendanceState, BrowserAutomationError
from keka_automation.browser import (
    KekaBrowser,
    PageAttendanceAdapter,
    acquire_profile_lock,
    keka_day_label,
    login_page_is_authenticated,
    release_profile_lock,
    wait_for_meaningful_body,
)


SELECTORS = {
    "authenticated_marker": {"strategy": "role", "role": "main", "name": "Attendance", "exact": True},
    "clock_in": {"strategy": "role", "role": "button", "name": "Clock In", "exact": True},
    "clock_out": {"strategy": "role", "role": "button", "name": "Clock Out", "exact": True},
    "clock_out_confirm": {"strategy": "role", "role": "button", "name": "Clock-out", "exact": True},
    "day_complete": {"strategy": "role", "role": "status", "name": "Day complete", "exact": True},
    "success_marker": {"strategy": "role", "role": "status", "name": "Attendance updated", "exact": True},
}


class BrowserAdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def page(self, body):
        page = self.browser.new_page()
        page.set_content(body)
        self.addCleanup(page.close)
        return page

    def test_missing_selectors_refuses_state_detection(self):
        page = self.page("<main>Attendance</main>")
        with self.assertRaises(BrowserAutomationError) as raised:
            PageAttendanceAdapter(page, {}).inspect_state()
        self.assertEqual(raised.exception.category, "SETUP_REQUIRED")

    def test_visible_clock_in_control_means_ready_to_clock_in(self):
        page = self.page('<main aria-label="Attendance"><button aria-label="Clock In">In</button></main>')
        self.assertEqual(PageAttendanceAdapter(page, SELECTORS).inspect_state(), AttendanceState.READY_TO_CLOCK_IN)

    def test_clock_in_control_with_completed_today_row_means_day_complete(self):
        page = self.page('<main aria-label="Attendance"><button aria-label="Clock In">In</button><div><div>8h 50m <span>Tue, 01 Sept</span></div></div></main>')
        adapter = PageAttendanceAdapter(page, SELECTORS, date_provider=lambda: date(2026, 9, 1))
        self.assertEqual(adapter.inspect_state(), AttendanceState.DAY_COMPLETE)

    def test_keka_day_label_matches_observed_september_format(self):
        self.assertEqual(keka_day_label(date(2026, 9, 1)), "Tue, 01 Sept")

    def test_visible_clock_out_control_means_ready_to_clock_out(self):
        page = self.page('<main aria-label="Attendance"><button aria-label="Clock Out">Out</button></main>')
        self.assertEqual(PageAttendanceAdapter(page, SELECTORS).inspect_state(), AttendanceState.READY_TO_CLOCK_OUT)

    def test_missing_authenticated_marker_means_authentication_required(self):
        page = self.page('<form><input type="password"><button>Sign in</button></form>')
        self.assertEqual(PageAttendanceAdapter(page, SELECTORS).inspect_state(), AttendanceState.AUTH_REQUIRED)

    def test_authenticated_dashboard_without_attendance_marker_is_not_logged_out(self):
        page = self.page('''
            <nav><a href="#/me/attendance">Me</a></nav>
            <main><h1>Welcome</h1><button>Web Clock-In</button></main>
        ''')
        self.assertEqual(PageAttendanceAdapter(page, SELECTORS).inspect_state(), AttendanceState.UNKNOWN)

    def test_interactive_login_requires_authenticated_marker_not_just_public_controls(self):
        page = self.page(
            '<main><a href="/one">One</a><a href="/two">Two</a>'
            '<button>Menu</button></main>'
        )
        adapter = PageAttendanceAdapter(page, SELECTORS)
        self.assertFalse(
            login_page_is_authenticated(
                page, "tenant.keka.com", "tenant.keka.com", adapter
            )
        )

    def test_interactive_login_accepts_exact_authenticated_marker(self):
        page = self.page('<main aria-label="Attendance"><button>Menu</button></main>')
        adapter = PageAttendanceAdapter(page, SELECTORS)
        self.assertTrue(
            login_page_is_authenticated(
                page, "tenant.keka.com", "tenant.keka.com", adapter
            )
        )

    def test_interactive_login_fails_closed_when_auth_probe_errors(self):
        class BrokenPage:
            def locator(self, *args, **kwargs):
                raise RuntimeError("page detached")

            def get_by_role(self, *args, **kwargs):
                raise RuntimeError("page detached")

        class MarkerVisible:
            def _visible(self, name):
                return True

        self.assertFalse(
            login_page_is_authenticated(
                BrokenPage(), "tenant.keka.com", "tenant.keka.com", MarkerVisible()
            )
        )

    def test_authenticated_shell_with_sign_in_history_text_is_not_logged_out(self):
        page = self.page('''
            <nav><a href="#/me/attendance">Me</a></nav>
            <main><h1>Welcome</h1><p>Last sign in: today at 09:42</p></main>
        ''')
        self.assertEqual(PageAttendanceAdapter(page, SELECTORS).inspect_state(), AttendanceState.UNKNOWN)

    def test_exact_visible_sign_in_control_is_authentication_required(self):
        page = self.page('<main><h1>Welcome back</h1><button>Sign in</button></main>')
        self.assertEqual(PageAttendanceAdapter(page, SELECTORS).inspect_state(), AttendanceState.AUTH_REQUIRED)

    def test_generic_continue_button_is_not_treated_as_authentication(self):
        page = self.page('<main><h1>Welcome</h1><button>Continue</button></main>')
        self.assertEqual(PageAttendanceAdapter(page, SELECTORS).inspect_state(), AttendanceState.UNKNOWN)

    def test_clock_in_clicks_once_and_verifies_resulting_state(self):
        page = self.page('''
            <main aria-label="Attendance">
              <button aria-label="Clock In" onclick="this.remove(); const b=document.createElement('button'); b.setAttribute('aria-label','Clock Out'); b.textContent='Out'; document.querySelector('main').appendChild(b); const s=document.createElement('div'); s.setAttribute('role','status'); s.setAttribute('aria-label','Attendance updated'); document.querySelector('main').appendChild(s)">In</button>
            </main>
        ''')
        state = PageAttendanceAdapter(page, SELECTORS).perform_and_verify("CLOCK_IN")
        self.assertEqual(state, AttendanceState.READY_TO_CLOCK_OUT)

    def test_clock_out_confirms_once_and_verifies_resulting_state(self):
        page = self.page('''
            <main aria-label="Attendance" data-trigger-clicks="0" data-confirm-clicks="0">
              <button aria-label="Clock Out" id="clock-out-trigger">Out</button>
            </main>
            <script>
              document.querySelector('#clock-out-trigger').addEventListener('click', () => {
                const main=document.querySelector('main');
                main.dataset.triggerClicks=String(Number(main.dataset.triggerClicks)+1);
                const confirm=document.createElement('button');
                confirm.setAttribute('aria-label','Clock-out');
                confirm.textContent='Clock-out';
                confirm.addEventListener('click', () => {
                  main.dataset.confirmClicks=String(Number(main.dataset.confirmClicks)+1);
                  main.replaceChildren();
                  const status=document.createElement('div');
                  status.setAttribute('role','status');
                  status.setAttribute('aria-label','Day complete');
                  status.textContent='Done';
                  main.appendChild(status);
                });
                main.appendChild(confirm);
              });
            </script>
        ''')
        adapter = PageAttendanceAdapter(page, SELECTORS, timeout_ms=500)
        state = adapter.perform_and_verify("CLOCK_OUT")
        self.assertEqual(state, AttendanceState.DAY_COMPLETE)
        self.assertEqual(page.locator("main").get_attribute("data-trigger-clicks"), "1")
        self.assertEqual(page.locator("main").get_attribute("data-confirm-clicks"), "1")

    def test_accessibility_snapshot_contains_roles_but_not_html(self):
        page = self.page('<main aria-label="Attendance"><button aria-label="Clock In">In</button></main>')
        snapshot = PageAttendanceAdapter(page, SELECTORS).accessibility_snapshot()
        self.assertIn("Clock In", snapshot)
        self.assertNotIn("<button", snapshot)

    def test_waits_for_spa_to_render_meaningful_body(self):
        page = self.page("<body><script>setTimeout(() => document.body.innerHTML='<button>Ready</button>', 100)</script></body>")
        self.assertTrue(wait_for_meaningful_body(page, timeout_ms=3000))

    def test_waits_past_initial_spa_shell_until_content_stabilizes(self):
        page = self.page("<body><button>Shell</button><script>setTimeout(() => document.body.innerHTML='<button>Final attendance content</button>', 500)</script></body>")
        self.assertTrue(wait_for_meaningful_body(page, timeout_ms=3000))
        self.assertIn("Final attendance content", page.locator("body").inner_text())


class BrowserNavigationTests(unittest.TestCase):
    def test_retries_attendance_route_when_first_page_is_authenticated_dashboard(self):
        class FakePage:
            def __init__(self):
                self.goto_urls = []

            def goto(self, url, **kwargs):
                self.goto_urls.append(url)

            def wait_for_timeout(self, milliseconds):
                pass

        class RouteAwareAdapter:
            def __init__(self, page):
                self.page = page

            def inspect_state(self):
                return (
                    AttendanceState.UNKNOWN
                    if len(self.page.goto_urls) == 1
                    else AttendanceState.READY_TO_CLOCK_IN
                )

        browser = object.__new__(KekaBrowser)
        browser.config = {
            "attendance_url": "https://tenant.keka.com/#/me/attendance",
            "base_url": "https://tenant.keka.com",
            "network_retries": 1,
            "attendance_state_timeout_ms": 0,
        }
        browser.page = FakePage()
        browser.adapter = RouteAwareAdapter(browser.page)

        with patch("keka_automation.browser.wait_for_meaningful_body", return_value=True), patch(
            "keka_automation.browser.time.sleep"
        ):
            browser._navigate()

        self.assertEqual(browser.page.goto_urls, [browser.config["attendance_url"]] * 2)

    def test_cold_network_retries_use_bounded_increasing_wake_delays(self):
        class RecoveringPage:
            def __init__(self):
                self.goto_calls = 0

            def goto(self, url, **kwargs):
                self.goto_calls += 1
                if self.goto_calls < 4:
                    raise OSError("network not ready")

            def wait_for_timeout(self, milliseconds):
                pass

        class ReadyAdapter:
            def inspect_state(self):
                return AttendanceState.READY_TO_CLOCK_IN

        browser = object.__new__(KekaBrowser)
        browser.config = {
            "attendance_url": "https://tenant.keka.com/#/me/attendance",
            "base_url": "https://tenant.keka.com",
            "network_retries": 3,
            "attendance_state_timeout_ms": 0,
        }
        browser.page = RecoveringPage()
        browser.adapter = ReadyAdapter()
        delays = []

        with patch("keka_automation.browser.wait_for_meaningful_body", return_value=True), patch(
            "keka_automation.browser.time.sleep", side_effect=delays.append
        ):
            browser._navigate()

        self.assertEqual(browser.page.goto_calls, 4)
        self.assertEqual(delays, [5, 15, 30])


class ProfileLockTests(unittest.TestCase):
    def test_all_persistent_browser_users_share_one_profile_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp) / "browser-profile"
            first = acquire_profile_lock(profile)
            try:
                with self.assertRaises(BrowserAutomationError) as raised:
                    acquire_profile_lock(profile)
                self.assertEqual(raised.exception.category, "BUSY")
            finally:
                release_profile_lock(first)

            second = acquire_profile_lock(profile)
            release_profile_lock(second)


if __name__ == "__main__":
    unittest.main()
