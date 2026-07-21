"""v1.8 per-turn execution transcripts.

Every Agent.run_turn() can own one RunLogger.  The logger writes the same events
to a machine-readable JSONL file and a compact human-readable text file.  Logging
must never decide or interrupt agent behaviour, so callers treat I/O failures as
non-fatal and continue the Unity task.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from datetime import datetime
from typing import Any


def _now() -> datetime:
    return datetime.now().astimezone()


def _slug(text: str, limit: int = 48) -> str:
    text = re.sub(r"[^\w가-힣]+", "_", str(text), flags=re.UNICODE).strip("_")
    return (text[:limit] or "run").rstrip("_")


class RunLogger:
    """Append-only JSONL + text transcript for one agent turn."""

    def __init__(self, root_dir: str, request: str, model: str):
        started = _now()
        self.started_at = started
        self.started_monotonic = time.monotonic()
        self.run_id = uuid.uuid4().hex[:10]
        day_dir = os.path.join(
            os.path.abspath(root_dir), started.strftime("%Y"), started.strftime("%m"),
            started.strftime("%d"),
        )
        os.makedirs(day_dir, exist_ok=True)
        stem = f"{started.strftime('%Y%m%d_%H%M%S_%f')[:-3]}_{_slug(request)}_{self.run_id}"
        self.jsonl_path = os.path.join(day_dir, stem + ".jsonl")
        self.text_path = os.path.join(day_dir, stem + ".log")
        # Exclusive creation makes accidental overwrite impossible even when two
        # orchestration processes start during the same millisecond.
        self._jsonl = None
        self._text = None
        try:
            self._jsonl = open(self.jsonl_path, "x", encoding="utf-8", buffering=1)
            self._text = open(self.text_path, "x", encoding="utf-8", buffering=1)
        except OSError:
            for handle in (self._jsonl, self._text):
                if handle is not None:
                    handle.close()
            raise
        self._closed = False
        self.event("run_started", request=request, model=model)

    @property
    def paths(self) -> tuple[str, str]:
        return self.text_path, self.jsonl_path

    def event(self, event: str, **payload: Any) -> None:
        if self._closed:
            return
        timestamp = _now().isoformat(timespec="milliseconds")
        record = {
            "timestamp": timestamp,
            "run_id": self.run_id,
            "event": event,
            **payload,
        }
        self._jsonl.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        self._text.write(f"[{timestamp}] {event}\n")
        if payload:
            self._text.write(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n")
        self._text.write("\n")

    def close(self, outcome: str, **payload: Any) -> None:
        if self._closed:
            return
        self.event(
            "run_finished",
            outcome=outcome,
            elapsed_seconds=round(time.monotonic() - self.started_monotonic, 3),
            **payload,
        )
        self._closed = True
        self._jsonl.close()
        self._text.close()

    def abort(self) -> None:
        """Best-effort handle cleanup after a logging I/O failure."""
        self._closed = True
        for handle in (self._jsonl, self._text):
            if handle is None:
                continue
            try:
                handle.close()
            except OSError:
                pass
