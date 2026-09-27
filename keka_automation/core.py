from __future__ import annotations

import re
from datetime import date
from typing import Any, Iterable


class ConfigError(ValueError):
    pass


def parse_hhmm(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"(\d{2}):(\d{2})", value or "")
    if not match:
        raise ConfigError("Time must use HH:MM in 24-hour format")
    hour, minute = (int(part) for part in match.groups())
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ConfigError("Time is outside the valid 24-hour range")
    return hour, minute


def is_working_day(day: date, working_days: Iterable[int]) -> bool:
    return day.isoweekday() in set(working_days)


def is_authorized_update(message: dict[str, Any], user_id: int, chat_id: int) -> bool:
    sender = message.get("from") or {}
    chat = message.get("chat") or {}
    return (
        chat.get("type") == "private"
        and sender.get("id") == user_id
        and chat.get("id") == chat_id
    )
