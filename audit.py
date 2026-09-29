"""Append-only audit log: one JSON object per line."""
import json
import threading
from datetime import datetime, timezone


class AuditLog:
    def __init__(self, path):
        self._path = path
        self._lock = threading.Lock()

    def write(self, event: str, **fields) -> None:
        record = {"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"), "event": event, **fields}
        line = json.dumps(record, default=str, ensure_ascii=False)
        with self._lock, open(self._path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
