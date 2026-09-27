from __future__ import annotations

import fcntl
import json
from pathlib import Path
from typing import Any


LOG_FIELDS = (
    "date",
    "execution_timestamp",
    "action",
    "trigger",
    "result",
    "verification",
    "error_category",
)


class AttendanceStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.log_dir = self.root / "logs"
        self.log_path = self.log_dir / "attendance.jsonl"
        self.log_dir.mkdir(parents=True, exist_ok=True)

    def record(self, **values: Any) -> None:
        row = {field: values.get(field) for field in LOG_FIELDS}
        with self.log_path.open("a", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                handle.write(json.dumps(row, separators=(",", ":"), ensure_ascii=True) + "\n")
                handle.flush()
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def recent(self, limit: int = 10) -> list[dict[str, Any]]:
        if not self.log_path.exists():
            return []
        with self.log_path.open("r", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
            try:
                text = handle.read()
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        rows = []
        for line in text.splitlines():
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            rows.append({field: parsed.get(field) for field in LOG_FIELDS})
        return rows[-max(0, limit):]

    def was_successful(self, day: str, action: str) -> bool:
        for row in reversed(self.recent(limit=500)):
            if (
                row["date"] == day
                and row["action"] == action
                and row["result"] in {"SUCCESS", "ALREADY_DONE"}
                and row["verification"] == "UI_CONFIRMED"
            ):
                return True
        return False
