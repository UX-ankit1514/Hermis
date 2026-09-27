from __future__ import annotations

from pathlib import Path

from .core import parse_hhmm


SCHEDULE_LABELS = {
    "CLOCK_IN": "com.ankit.keka-attendance.clockin",
    "CLOCK_OUT": "com.ankit.keka-attendance.clockout",
}
TELEGRAM_LABEL = "com.ankit.keka-attendance.telegram"


def calendar_intervals(
    value: str, working_days: list[int], grace_minutes: int = 0
) -> list[dict[str, int]]:
    hour, minute = parse_hhmm(value)
    intervals = []
    start_minutes = hour * 60 + minute
    for day in working_days:
        for offset in range(grace_minutes + 1):
            absolute_minutes = start_minutes + offset
            if absolute_minutes >= 24 * 60:
                break
            interval_hour, interval_minute = divmod(absolute_minutes, 60)
            intervals.append(
                {
                    "Weekday": day % 7,
                    "Hour": interval_hour,
                    "Minute": interval_minute,
                }
            )
    return intervals


def _base_plist(root: Path, label: str, arguments: list[str]) -> dict:
    root = Path(root)
    return {
        "Label": label,
        "ProgramArguments": [str(root / "bin" / "keka-attendance"), *arguments],
        "WorkingDirectory": str(root),
        "EnvironmentVariables": {
            "HOME": str(Path.home()),
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin",
        },
        "StandardOutPath": str(root / "logs" / f"{label}.stdout.log"),
        "StandardErrorPath": str(root / "logs" / f"{label}.stderr.log"),
        "ProcessType": "Background",
    }


def build_scheduled_plist(
    root: Path,
    action: str,
    value: str,
    working_days: list[int],
    grace_minutes: int = 0,
) -> dict:
    if action not in SCHEDULE_LABELS:
        raise ValueError("Unsupported scheduled action")
    arguments = ["clockin-dispatch"] if action == "CLOCK_IN" else ["scheduled", action]
    plist = _base_plist(root, SCHEDULE_LABELS[action], arguments)
    plist.update(
        {
            "StartCalendarInterval": calendar_intervals(
                value, working_days, grace_minutes
            ),
            "RunAtLoad": action == "CLOCK_IN",
            "LowPriorityIO": True,
        }
    )
    return plist


def build_telegram_plist(root: Path) -> dict:
    plist = _base_plist(root, TELEGRAM_LABEL, ["telegram"])
    plist.update({"KeepAlive": True, "RunAtLoad": True, "ThrottleInterval": 10})
    return plist
