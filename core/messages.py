"""
core/messages.py — Typed inter-agent message protocol
======================================================
All agent communication flows through Message objects.
No raw strings passed between agents.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class MessageType(str, Enum):
    # Planner → Coder
    TASK_PLAN         = "task_plan"
    # Coder → Tester
    IMPLEMENTATION    = "implementation"
    # Tester → Fixer / Orchestrator
    TEST_RESULTS      = "test_results"
    # Fixer → Tester
    PATCH_READY       = "patch_ready"
    # Any → Supervisor
    ESCALATION        = "escalation"
    # Supervisor → Any
    SUPERVISOR_RULING = "supervisor_ruling"
    # Orchestrator → Any
    DIRECTIVE         = "directive"
    # Any → Orchestrator
    STATUS            = "status"


class AgentRole(str, Enum):
    ORCHESTRATOR = "orchestrator"
    PLANNER      = "planner"
    CODER        = "coder"
    TESTER       = "tester"
    FIXER        = "fixer"
    SUPERVISOR   = "supervisor"


class TaskStatus(str, Enum):
    PENDING    = "pending"
    IN_PROGRESS = "in_progress"
    PASSED     = "passed"
    FAILED     = "failed"
    ESCALATED  = "escalated"
    RESOLVED   = "resolved"
    ABORTED    = "aborted"


@dataclass
class TestResult:
    passed:    int
    failed:    int
    errors:    list[str]
    output:    str  = ""
    duration_s: float = 0.0

    @property
    def all_passed(self) -> bool:
        return self.failed == 0 and len(self.errors) == 0

    def summary(self) -> str:
        return (
            f"passed={self.passed} failed={self.failed} "
            f"errors={len(self.errors)}"
        )


@dataclass
class GoalSpec:
    """A single coding sub-task produced by the Planner."""
    id:           str
    title:        str
    description:  str
    target_files: list[str]
    mode:         str = "feature"   # "rule" | "feature" | "refactor"
    context:      dict = field(default_factory=dict)


@dataclass
class Message:
    """
    The universal communication unit between agents.

    Every agent sends and receives Message objects only.
    The payload field carries type-specific data.
    """
    type:       MessageType
    sender:     AgentRole
    recipient:  AgentRole
    payload:    Any
    id:         str   = field(default_factory=lambda: str(uuid.uuid4())[:8])
    timestamp:  float = field(default_factory=time.monotonic)
    task_id:    str   = ""
    attempt:    int   = 0          # which fix attempt this is
    status:     TaskStatus = TaskStatus.PENDING

    def reply(
        self,
        type: MessageType,
        sender: AgentRole,
        payload: Any,
        status: TaskStatus = TaskStatus.IN_PROGRESS,
    ) -> "Message":
        """Convenience: build a reply that inherits task_id and increments attempt."""
        return Message(
            type=type,
            sender=sender,
            recipient=self.sender,
            payload=payload,
            task_id=self.task_id,
            attempt=self.attempt,
            status=status,
        )

    def __str__(self) -> str:
        return (
            f"[{self.sender.value}→{self.recipient.value}] "
            f"{self.type.value} | task={self.task_id} attempt={self.attempt}"
        )
