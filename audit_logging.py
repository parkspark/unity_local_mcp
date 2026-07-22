"""Session-wide audit log for every :class:`UnityTools` call.

Unlike ``RunLogger`` (one ``Agent.run_turn`` transcript), this logger lives at
the MCP client boundary.  It therefore records CLI, agent, verification, test,
and direct programmatic tool calls alike.  Audit failures are deliberately
non-fatal: observability must never decide whether a Unity operation runs.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime
from typing import Any


def _now() -> datetime:
    return datetime.now().astimezone()


class ToolAuditLogger:
    """Append-only JSONL audit stream for one ``UnityTools`` session."""

    def __init__(self, root_dir: str, source: str = "unity_tools"):
        started = _now()
        self.session_id = uuid.uuid4().hex[:10]
        day_dir = os.path.join(
            os.path.abspath(root_dir), started.strftime("%Y"), started.strftime("%m"),
            started.strftime("%d"),
        )
        os.makedirs(day_dir, exist_ok=True)
        stem = f"{started.strftime('%Y%m%d_%H%M%S_%f')[:-3]}_{source}_{self.session_id}.jsonl"
        self.path = os.path.join(day_dir, stem)
        self._handle = open(self.path, "x", encoding="utf-8", buffering=1)
        self._closed = False
        self._sequence = 0
        self.event("session_started", source=source)

    def event(self, event: str, **payload: Any) -> None:
        if self._closed:
            return
        record = {
            "timestamp": _now().isoformat(timespec="milliseconds"),
            "session_id": self.session_id,
            "event": event,
            **payload,
        }
        self._handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    def call_started(self, name: str, arguments: dict, tool_mode: str) -> tuple[int, float]:
        self._sequence += 1
        call_id = self._sequence
        self.event(
            "tool_call_started", call_id=call_id, name=name,
            arguments=arguments, tool_mode=tool_mode,
        )
        return call_id, time.monotonic()

    def call_finished(
        self, call_id: int, name: str, started: float, result: str,
        *, exception: BaseException | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "call_id": call_id,
            "name": name,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "result": result,
        }
        if exception is not None:
            payload.update(
                exception_type=type(exception).__name__, exception_message=str(exception)
            )
        self.event("tool_call_finished", **payload)

    def close(self, outcome: str = "completed", **payload: Any) -> None:
        if self._closed:
            return
        self.event("session_finished", outcome=outcome, **payload)
        self._closed = True
        self._handle.close()

    def abort(self) -> None:
        self._closed = True
        try:
            self._handle.close()
        except OSError:
            pass
