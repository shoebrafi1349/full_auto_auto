"""
core/events.py — Structured event stream
==========================================
All significant agent actions are emitted as typed events to a JSONL file
AND broadcast over a WebSocket for the live dashboard.

Events are separate from Messages — Messages are agent-to-agent routing;
Events are the audit trail / UI feed.

Usage (inside any agent):
    from core.events import emit
    await emit(EventType.GOAL_STARTED, task_id="t01", data={"goal": "..."})
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Coroutine

# Lazily import config to avoid circular imports
_EVENTS_PATH: Path | None = None


def _events_path() -> Path:
    global _EVENTS_PATH
    if _EVENTS_PATH is None:
        from config import EVENTS_PATH
        _EVENTS_PATH = EVENTS_PATH
        _EVENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    return _EVENTS_PATH


class EventType(str, Enum):
    # Lifecycle
    SESSION_START   = "session_start"
    SESSION_END     = "session_end"
    TASK_SUBMITTED  = "task_submitted"
    TASK_PASSED     = "task_passed"
    TASK_FAILED     = "task_failed"
    TASK_ABORTED    = "task_aborted"
    # Planning
    PLAN_STARTED    = "plan_started"
    PLAN_READY      = "plan_ready"
    PLAN_FAILED     = "plan_failed"
    # Coding
    GOAL_STARTED    = "goal_started"
    GOAL_PASSED     = "goal_passed"
    GOAL_FAILED     = "goal_failed"
    FILE_WRITTEN    = "file_written"
    # Testing
    TEST_STARTED    = "test_started"
    TEST_PASSED     = "test_passed"
    TEST_FAILED     = "test_failed"
    # Fixing
    FIX_ATTEMPT     = "fix_attempt"
    FIX_AUTO        = "fix_auto"
    FIX_LLM         = "fix_llm"
    # Supervision
    ESCALATION      = "escalation"
    SUPERVISOR_CALL = "supervisor_call"
    SUPERVISOR_DONE = "supervisor_done"
    # Git
    GIT_COMMIT      = "git_commit"
    GIT_PUSH        = "git_push"


@dataclass
class Event:
    type:     EventType
    task_id:  str
    agent:    str
    data:     dict = field(default_factory=dict)
    ts:       float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["type"] = self.type.value
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict())


# ── Subscriber registry ────────────────────────────────────────────────────────
# Any coroutine subscribed here receives every event (e.g. WebSocket broadcaster)
_subscribers: list[Callable[[Event], Coroutine]] = []


def subscribe_events(handler: Callable[[Event], Coroutine]):
    """Register an async handler that receives all events."""
    _subscribers.append(handler)


async def emit(
    type: EventType,
    task_id: str = "",
    agent: str = "system",
    **data: Any,
) -> None:
    """
    Emit a structured event.
    - Appends to JSONL log file (synchronous, tiny, always works)
    - Fans out to all registered subscribers (e.g. WebSocket broadcaster)
    """
    event = Event(type=type, task_id=task_id, agent=agent, data=data)

    # Write to file
    try:
        with open(_events_path(), "a", encoding="utf-8") as f:
            f.write(event.to_json() + "\n")
    except Exception:
        pass  # never crash on logging failure

    # Fan out to subscribers
    if _subscribers:
        await asyncio.gather(
            *[sub(event) for sub in _subscribers],
            return_exceptions=True,
        )


def read_events(task_id: str = "", last_n: int = 0) -> list[Event]:
    """Read events from the JSONL log. Optionally filter by task_id or last N."""
    path = _events_path()
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    events = []
    for line in lines:
        try:
            d = json.loads(line)
            events.append(Event(
                type=EventType(d["type"]),
                task_id=d.get("task_id", ""),
                agent=d.get("agent", ""),
                data=d.get("data", {}),
                ts=d.get("ts", 0),
            ))
        except Exception:
            continue
    if task_id:
        events = [e for e in events if e.task_id == task_id]
    if last_n:
        events = events[-last_n:]
    return events
