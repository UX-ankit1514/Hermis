from __future__ import annotations

from typing import Any

from .core import ConfigError, is_authorized_update, parse_hhmm


NATURAL_LANGUAGE_INTENTS = {
    "clock me in": ("run_action", "CLOCK_IN"),
    "start my day": ("run_action", "CLOCK_IN"),
    "clock me out": ("run_action", "CLOCK_OUT"),
    "i'm done for today": ("run_action", "CLOCK_OUT"),
    "show my status": ("status",),
    "show my schedule": ("schedule",),
    "disable attendance automation": ("disable",),
    "enable attendance automation": ("enable",),
}


def normalize_intent_text(value: str) -> str:
    return " ".join(value.strip().casefold().replace("’", "'").split())


HELP_TEXT = """Keka attendance commands:
/status — automation and today's attendance
/clockin — manual Clock In with duplicate check
/clockout — manual Clock Out with duplicate check
/enable — enable approved schedules
/disable — disable schedules
/schedule — show times and working days
/grace — show Clock In grace window
/setgrace MINUTES — set Clock In grace (0-180)
/setin HH:MM — change Clock In time
/setout HH:MM — change Clock Out time
/workingdays — show working days
/setworkingdays 1,2,3,4,5 — ISO weekdays (Mon=1)
/logs — recent sanitized results
/login — open the dedicated Keka login profile
/reauth — reopen authentication
/setcredentials — open secure local Keychain entry
/test — non-destructive browser/session test
/help — show this list

Natural language (exact allowlist): clock me in; start my day; clock me out; I'm done for today; show my status; show my schedule; disable attendance automation; enable attendance automation"""


class CommandRouter:
    def __init__(self, authorized_user_id: int, authorized_chat_id: int, operations: Any):
        self.authorized_user_id = authorized_user_id
        self.authorized_chat_id = authorized_chat_id
        self.operations = operations

    def handle(self, message: dict[str, Any]) -> str | None:
        if not is_authorized_update(message, self.authorized_user_id, self.authorized_chat_id):
            return None
        text = (message.get("text") or "").strip()
        if not text.startswith("/"):
            intent = NATURAL_LANGUAGE_INTENTS.get(normalize_intent_text(text))
            if intent is None:
                return "Unrecognized message. Use /help"
            method = getattr(self.operations, intent[0])
            return method(*intent[1:])
        parts = text.split()
        command = parts[0].split("@", 1)[0].lower()
        argument = parts[1] if len(parts) > 1 else None

        if command == "/help":
            return HELP_TEXT
        if command == "/status":
            return self.operations.status()
        if command == "/clockin":
            return self.operations.run_action("CLOCK_IN")
        if command == "/clockout":
            return self.operations.run_action("CLOCK_OUT")
        if command == "/enable":
            return self.operations.enable()
        if command == "/disable":
            return self.operations.disable()
        if command == "/schedule":
            return self.operations.schedule()
        if command == "/grace":
            return self.operations.grace()
        if command == "/setgrace":
            try:
                minutes = int(argument or "")
                if minutes < 0 or minutes > 180:
                    raise ValueError
            except ValueError:
                return "Invalid grace. Use /setgrace MINUTES (0-180)"
            return self.operations.set_grace(minutes)
        if command == "/logs":
            return self.operations.logs()
        if command in {"/login", "/reauth"}:
            return self.operations.login(reauth=command == "/reauth")
        if command == "/setcredentials":
            return self.operations.setcredentials()
        if command == "/test":
            return self.operations.test()
        if command == "/workingdays":
            return self.operations.working_days()
        if command in {"/setin", "/setout"}:
            action = "in" if command == "/setin" else "out"
            try:
                parse_hhmm(argument or "")
            except ConfigError:
                return f"Invalid time. Use /set{action} HH:MM"
            return self.operations.set_schedule(action, argument)
        if command == "/setworkingdays":
            try:
                days = [int(value) for value in (argument or "").split(",")]
                if not days or len(days) != len(set(days)) or any(day < 1 or day > 7 for day in days):
                    raise ValueError
            except ValueError:
                return "Invalid working days. Use /setworkingdays 1,2,3,4,5"
            return self.operations.set_working_days(days)
        return HELP_TEXT
