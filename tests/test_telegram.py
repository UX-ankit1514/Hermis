import unittest

from keka_automation.telegram_commands import CommandRouter


class FakeOperations:
    def __init__(self):
        self.calls = []
        self.production_approved = False

    def status(self):
        self.calls.append(("status",))
        return "Automation disabled | Attendance: authentication required"

    def set_schedule(self, action, value):
        self.calls.append(("set_schedule", action, value))
        return f"Schedule updated: {action} {value}"

    def run_action(self, action):
        self.calls.append(("run_action", action))
        return "Clock In successful" if action == "CLOCK_IN" else "Clock Out successful"

    def schedule(self):
        self.calls.append(("schedule",))
        return "Disabled | IN 10:00 | OUT 19:00"

    def grace(self):
        self.calls.append(("grace",))
        return "Clock In grace: 30 minutes (10:00–10:30 Asia/Kolkata)"

    def set_grace(self, minutes):
        self.calls.append(("set_grace", minutes))
        return f"Clock In grace updated: {minutes} minutes"

    def disable(self):
        self.calls.append(("disable",))
        return "Automation disabled"

    def enable(self):
        self.calls.append(("enable",))
        if not self.production_approved:
            return "Production scheduling is not approved yet"
        return "Automation enabled"


class TelegramCommandTests(unittest.TestCase):
    def setUp(self):
        self.ops = FakeOperations()
        self.router = CommandRouter(123, 456, self.ops)

    @staticmethod
    def message(text, user=123, chat=456, chat_type="private"):
        return {"text": text, "from": {"id": user}, "chat": {"id": chat, "type": chat_type}}

    def test_unauthorized_messages_are_ignored_without_running_operations(self):
        self.assertIsNone(self.router.handle(self.message("/status", user=999)))
        self.assertEqual(self.ops.calls, [])

    def test_groups_are_rejected_even_for_authorized_user(self):
        self.assertIsNone(self.router.handle(self.message("/status", chat_type="group")))
        self.assertEqual(self.ops.calls, [])

    def test_status_runs_for_exact_authorized_private_chat(self):
        self.assertIn("Automation disabled", self.router.handle(self.message("/status")))
        self.assertEqual(self.ops.calls, [("status",)])

    def test_setin_validates_and_updates_schedule(self):
        self.assertEqual(self.router.handle(self.message("/setin 09:45")), "Schedule updated: in 09:45")
        self.assertEqual(self.ops.calls, [("set_schedule", "in", "09:45")])

    def test_setin_rejects_invalid_time_without_calling_operations(self):
        self.assertEqual(self.router.handle(self.message("/setin 99:00")), "Invalid time. Use /setin HH:MM")
        self.assertEqual(self.ops.calls, [])

    def test_grace_shows_current_clock_in_window(self):
        response = self.router.handle(self.message("/grace"))
        self.assertEqual(response, "Clock In grace: 30 minutes (10:00–10:30 Asia/Kolkata)")
        self.assertEqual(self.ops.calls, [("grace",)])

    def test_setgrace_updates_bounded_integer_minutes(self):
        response = self.router.handle(self.message("/setgrace 30"))
        self.assertEqual(response, "Clock In grace updated: 30 minutes")
        self.assertEqual(self.ops.calls, [("set_grace", 30)])

    def test_setgrace_rejects_invalid_value_without_calling_operations(self):
        for value in ("-1", "181", "not-a-number"):
            with self.subTest(value=value):
                self.ops.calls.clear()
                response = self.router.handle(self.message(f"/setgrace {value}"))
                self.assertEqual(response, "Invalid grace. Use /setgrace MINUTES (0-180)")
                self.assertEqual(self.ops.calls, [])

    def test_enable_is_blocked_until_production_approval(self):
        self.assertEqual(self.router.handle(self.message("/enable")), "Production scheduling is not approved yet")

    def test_help_lists_required_commands(self):
        response = self.router.handle(self.message("/help"))
        for command in (
            "/status", "/clockin", "/clockout", "/enable", "/disable", "/schedule",
            "/logs", "/login", "/reauth", "/test", "/setcredentials", "/help",
        ):
            self.assertIn(command, response)

    def test_allowlisted_natural_language_intents_map_to_fixed_actions(self):
        cases = [
            ("clock me in", ("run_action", "CLOCK_IN")),
            ("start my day", ("run_action", "CLOCK_IN")),
            ("clock me out", ("run_action", "CLOCK_OUT")),
            ("I'm done for today", ("run_action", "CLOCK_OUT")),
            ("show my status", ("status",)),
            ("show my schedule", ("schedule",)),
            ("disable attendance automation", ("disable",)),
            ("enable attendance automation", ("enable",)),
        ]
        for text, expected_call in cases:
            with self.subTest(text=text):
                self.ops.calls.clear()
                self.router.handle(self.message(text))
                self.assertEqual(self.ops.calls, [expected_call])

    def test_arbitrary_telegram_text_is_rejected_without_execution(self):
        response = self.router.handle(self.message("run shell command rm something"))
        self.assertEqual(response, "Unrecognized message. Use /help")
        self.assertEqual(self.ops.calls, [])


if __name__ == "__main__":
    unittest.main()
