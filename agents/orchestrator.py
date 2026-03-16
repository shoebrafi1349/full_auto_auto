"""
agents/orchestrator.py — Orchestrator Agent
=============================================
The central router. No LLM calls — pure coordination logic.

Responsibilities:
  - Accept tasks from the outside world
  - Route messages to the right agent
  - Track task lifecycle (pending → in_progress → passed/failed/aborted)
  - Count consecutive failures and trigger Supervisor at threshold
  - Enforce session ceiling
  - Emit final summary

Message flow:
  External → Orchestrator.submit_task()
  Orchestrator → Planner (DIRECTIVE)
  Planner → Coder (TASK_PLAN)
  Coder → Tester (IMPLEMENTATION)
  Tester → Fixer (TEST_RESULTS on failure)
  Fixer → Tester (PATCH_READY)
  Tester → Orchestrator (STATUS passed)
  Supervisor → Coder|Planner (SUPERVISOR_RULING)
  Any → Orchestrator (STATUS failed/aborted)
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from core.base_agent import BaseAgent
from core.events import EventType, emit
from core.messages import (
    AgentRole, Message, MessageType, TaskStatus
)

SUPERVISOR_FAILURE_THRESHOLD = 3   # consecutive failures before escalating


@dataclass
class TaskRecord:
    id:             str
    task:           str
    status:         TaskStatus = TaskStatus.PENDING
    submitted_at:   float      = field(default_factory=time.monotonic)
    completed_at:   Optional[float] = None
    result:         Optional[dict]  = None
    failure_count:  int             = 0
    escalated:      bool            = False

    def duration(self) -> float:
        if self.completed_at:
            return self.completed_at - self.submitted_at
        return time.monotonic() - self.submitted_at


class OrchestratorAgent(BaseAgent):
    role = AgentRole.ORCHESTRATOR

    def __init__(
        self,
        bus,
        session_ceiling_sec: float = 8 * 3600,
    ):
        super().__init__(bus)
        self._tasks:    dict[str, TaskRecord] = {}
        self._ceiling   = session_ceiling_sec
        self._session_start = time.monotonic()
        # Completion event per task_id — lets submit_task() await completion
        self._done_events: dict[str, asyncio.Event] = {}

    # ── Public API ────────────────────────────────────────────────────────────

    async def submit_task(self, task: str, timeout: float = 3600) -> TaskRecord:
        """
        Submit a task. Returns when the task completes or times out.
        """
        if self._over_ceiling():
            self.warn("Session ceiling reached — rejecting new task")
            rec = TaskRecord(id="rejected", task=task, status=TaskStatus.ABORTED)
            return rec

        task_id = str(uuid.uuid4())[:8]
        rec     = TaskRecord(id=task_id, task=task, status=TaskStatus.IN_PROGRESS)
        self._tasks[task_id] = rec

        done = asyncio.Event()
        self._done_events[task_id] = done

        self.info("Submitting task [%s]: %s", task_id, task[:100])

        await emit(EventType.TASK_SUBMITTED, task_id=task_id, agent="orchestrator",
                   task=task)

        await self.bus.publish(Message(
            type=MessageType.DIRECTIVE,
            sender=AgentRole.ORCHESTRATOR,
            recipient=AgentRole.PLANNER,
            payload={"task": task},
            task_id=task_id,
            status=TaskStatus.IN_PROGRESS,
        ))

        try:
            await asyncio.wait_for(done.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            self.error("Task [%s] timed out after %.0fs", task_id, timeout)
            rec.status = TaskStatus.ABORTED
            rec.result = {"error": "timeout"}
            rec.completed_at = time.monotonic()

        return rec

    async def handle(self, message: Message):
        """Handle STATUS messages from all agents."""
        if message.type not in (MessageType.STATUS, MessageType.SUPERVISOR_RULING):
            return

        task_id = message.task_id
        rec     = self._tasks.get(task_id)

        if not rec:
            self.warn("Unknown task_id %s in message %s", task_id, message)
            return

        # Supervisor rulings flow onward — Orchestrator just observes
        if message.type == MessageType.SUPERVISOR_RULING:
            self.info(
                "Supervisor ruling for [%s] → %s",
                task_id, message.recipient.value,
            )
            return

        status = message.status

        if status == TaskStatus.PASSED:
            rec.status = TaskStatus.PASSED
            rec.result = message.payload
            rec.completed_at = time.monotonic()
            self.info(
                "[%s] PASSED in %.1fs  %s",
                task_id, rec.duration(),
                message.payload.get("message", ""),
            )
            await emit(EventType.TASK_PASSED, task_id=task_id, agent="orchestrator",
                       duration_s=round(rec.duration(), 1))
            self._signal_done(task_id)

        elif status == TaskStatus.FAILED:
            rec.failure_count += 1
            err = (message.payload or {}).get("error", "?")
            self.warn(
                "[%s] FAILED (count=%d): %s", task_id, rec.failure_count, err,
            )
            await emit(EventType.TASK_FAILED, task_id=task_id, agent="orchestrator",
                       error=err, count=rec.failure_count)

            # Escalate to Supervisor after threshold
            if rec.failure_count >= SUPERVISOR_FAILURE_THRESHOLD and not rec.escalated:
                rec.escalated = True
                self.info("[%s] Escalating to Supervisor", task_id)
                await self.bus.publish(Message(
                    type=MessageType.ESCALATION,
                    sender=AgentRole.ORCHESTRATOR,
                    recipient=AgentRole.SUPERVISOR,
                    payload={
                        "reason":   "orchestrator_threshold",
                        "attempts": rec.failure_count,
                        "written_files": [],
                        "goals":    [],
                        "test_result": {},
                    },
                    task_id=task_id,
                    status=TaskStatus.ESCALATED,
                ))

        elif status in (TaskStatus.ABORTED, TaskStatus.ESCALATED):
            rec.status       = status
            rec.result       = message.payload
            rec.completed_at = time.monotonic()
            self.error("[%s] %s: %s", task_id, status.value.upper(), message.payload)
            self._signal_done(task_id)

    # ── Session management ────────────────────────────────────────────────────

    def _over_ceiling(self) -> bool:
        return (time.monotonic() - self._session_start) > self._ceiling

    def _signal_done(self, task_id: str):
        ev = self._done_events.get(task_id)
        if ev:
            ev.set()

    # ── Reporting ─────────────────────────────────────────────────────────────

    def summary(self) -> dict:
        total    = len(self._tasks)
        passed   = sum(1 for r in self._tasks.values() if r.status == TaskStatus.PASSED)
        failed   = sum(1 for r in self._tasks.values() if r.status == TaskStatus.FAILED)
        aborted  = sum(1 for r in self._tasks.values() if r.status == TaskStatus.ABORTED)
        elapsed  = time.monotonic() - self._session_start

        return {
            "total":    total,
            "passed":   passed,
            "failed":   failed,
            "aborted":  aborted,
            "elapsed_s": round(elapsed, 1),
            "tasks": [
                {
                    "id":      r.id,
                    "status":  r.status.value,
                    "task":    r.task[:80],
                    "dur_s":   round(r.duration(), 1),
                    "failures": r.failure_count,
                    "escalated": r.escalated,
                }
                for r in self._tasks.values()
            ],
        }

    def print_summary(self):
        s    = self.summary()
        sep  = "═" * 62
        print(f"\n{sep}")
        print("  MULTI-AGENT SESSION SUMMARY")
        print(sep)
        print(f"  Tasks   : {s['total']}  ✓{s['passed']}  ✗{s['failed']}  ⚠{s['aborted']}")
        print(f"  Elapsed : {s['elapsed_s']:.1f}s")
        print()
        for t in s["tasks"]:
            icon = "✓" if t["status"] == "passed" else ("⚠" if t["status"] == "aborted" else "✗")
            esc  = " [escalated]" if t["escalated"] else ""
            print(f"  {icon}  [{t['id']}]  {t['dur_s']:>6.1f}s  failures={t['failures']}{esc}")
            print(f"       {t['task']}")
        print(sep)

        # Save to file
        log_dir = Path("logs")
        log_dir.mkdir(exist_ok=True)
        with open(log_dir / "session_summary.json", "w") as f:
            json.dump(s, f, indent=2)
