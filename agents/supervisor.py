"""
agents/supervisor.py — Supervisor Agent (Claude-backed)
=========================================================
EXPENSIVE — used ONLY when cheaper agents are stuck.

Trigger conditions:
  - Fixer exhausted MAX_FIX_ATTEMPTS
  - 3+ consecutive failed coding attempts
  - Architectural change needed

On receipt of ESCALATION:
  1. Reviews all context (history, errors, files)
  2. Produces a structured remediation plan
  3. Dispatches SUPERVISOR_RULING to the appropriate agent

Cost guard:
  - Tracks total Anthropic API calls this session
  - If cost ceiling hit → emits a deterministic fallback plan
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from core.base_agent import BaseAgent
from core.events import EventType, emit
from core.llm import BaseLLMBackend
from core.messages import (
    AgentRole, GoalSpec, Message, MessageType, TaskStatus, TestResult
)

SUPERVISOR_SYSTEM = """You are an expert software architect performing a critical code review.

An autonomous coding agent has failed to produce working code after multiple attempts.
Your job is to diagnose the root cause and produce a precise remediation plan.

OUTPUT FORMAT — valid JSON only, no prose, no fences:
{
  "root_cause": "One sentence diagnosis",
  "action": "retry_coder" | "architectural_change" | "abandon",
  "instructions": "Detailed corrected implementation instructions for the coder",
  "target_files": ["relative/path.py"],
  "notes": "Any other observations"
}

action meanings:
  retry_coder        — the fix is a code-level change; send revised instructions to Coder
  architectural_change — the design is wrong; broader refactor needed
  abandon            — task is infeasible or too risky; log and stop

Be decisive. Pick one action.
"""

# Hard cap: max Claude calls per session (cost control)
MAX_SUPERVISOR_CALLS = 5


class SupervisorAgent(BaseAgent):
    role = AgentRole.SUPERVISOR

    def __init__(self, bus, llm: BaseLLMBackend, repo_path: Path):
        super().__init__(bus)
        self.llm       = llm
        self.repo_path = repo_path
        self._calls    = 0    # session call counter

    async def handle(self, message: Message):
        if message.type != MessageType.ESCALATION:
            return

        self._calls += 1
        payload   = message.payload or {}
        reason    = payload.get("reason", "unknown")
        attempts  = payload.get("attempts", 0)
        files     = payload.get("written_files", [])
        goals     = payload.get("goals", [])
        test_res  = payload.get("test_result", {})

        self.info(
            "ESCALATION received | reason=%s attempts=%d files=%s",
            reason, attempts, files,
        )
        await emit(EventType.ESCALATION, task_id=message.task_id, agent="supervisor",
                   reason=reason, attempts=attempts)

        # ── Cost gate ──────────────────────────────────────────────────
        if self._calls > MAX_SUPERVISOR_CALLS:
            self.warn(
                "Supervisor call limit (%d) reached — using fallback ruling",
                MAX_SUPERVISOR_CALLS,
            )
            ruling = self._fallback_ruling(reason, files, goals)
        else:
            self.info("Calling Claude (call %d/%d)...", self._calls, MAX_SUPERVISOR_CALLS)
            await emit(EventType.SUPERVISOR_CALL, task_id=message.task_id, agent="supervisor",
                       call_n=self._calls, max=MAX_SUPERVISOR_CALLS)
            ruling = await self._consult_claude(payload, files, test_res)
            await emit(EventType.SUPERVISOR_DONE, task_id=message.task_id, agent="supervisor",
                       action=ruling.get("action"), root_cause=ruling.get("root_cause", ""))

        self.info("Ruling: action=%s cause=%s", ruling.get("action"), ruling.get("root_cause"))

        action = ruling.get("action", "abandon")

        if action == "retry_coder":
            # Build a revised GoalSpec and send to Coder
            revised_goals = self._build_revised_goals(ruling, goals)
            await self.send(Message(
                type=MessageType.SUPERVISOR_RULING,
                sender=self.role,
                recipient=AgentRole.CODER,
                payload=revised_goals,
                task_id=message.task_id,
                attempt=message.attempt,
                status=TaskStatus.IN_PROGRESS,
            ))

        elif action == "architectural_change":
            # Send back to Planner with expanded instructions
            await self.send(Message(
                type=MessageType.SUPERVISOR_RULING,
                sender=self.role,
                recipient=AgentRole.PLANNER,
                payload={
                    "task": ruling.get("instructions", "Refactor required"),
                    "notes": ruling.get("notes", ""),
                },
                task_id=message.task_id,
                attempt=message.attempt,
                status=TaskStatus.IN_PROGRESS,
            ))

        else:   # abandon
            self.error("Supervisor ruling: ABANDON task %s", message.task_id)
            await self.send(Message(
                type=MessageType.STATUS,
                sender=self.role,
                recipient=AgentRole.ORCHESTRATOR,
                payload={
                    "error":      "supervisor_abandon",
                    "root_cause": ruling.get("root_cause", ""),
                    "notes":      ruling.get("notes", ""),
                },
                task_id=message.task_id,
                status=TaskStatus.ABORTED,
            ))

    # ─────────────────────────────────────────────────────────────────────────

    async def _consult_claude(
        self, payload: dict, files: list[str], test_res: dict
    ) -> dict:
        context = self._build_context(files)
        errors  = test_res.get("errors", [])
        output  = test_res.get("output", "")

        prompt = f"""ESCALATION DETAILS:
reason     : {payload.get('reason')}
attempts   : {payload.get('attempts')}
test errors: {chr(10).join(errors[:10])}

TEST OUTPUT (last 500 chars):
{output[-500:] if output else '(none)'}

BROKEN FILES:
{context}

Produce your JSON ruling:"""

        t0   = time.monotonic()
        resp = await self.llm.complete_with_retry(
            prompt=prompt,
            system=SUPERVISOR_SYSTEM,
            max_tokens=2048,
            temperature=0.1,
        )
        self.info("Claude responded in %.1fs", time.monotonic() - t0)

        if not resp.ok:
            self.warn("Claude failed: %s — using fallback", resp.error)
            return self._fallback_ruling(payload.get("reason", ""), files, [])

        return self._parse_ruling(resp.text) or self._fallback_ruling("parse_failed", files, [])

    def _parse_ruling(self, raw: str) -> dict:
        raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            self.warn("Could not parse Claude ruling JSON: %s", raw[:200])
            return {}

    def _fallback_ruling(self, reason: str, files: list[str], goals: list) -> dict:
        """Deterministic fallback when Claude is unavailable or over budget."""
        return {
            "root_cause": f"Auto-fallback: {reason}",
            "action":     "retry_coder",
            "instructions": (
                "Review the failing tests carefully. "
                "Ensure all imports are present, all required columns are guarded, "
                "and ReviewException includes row_number. "
                "Rewrite the file from scratch if needed."
            ),
            "target_files": files,
            "notes":      "Supervisor fallback — Claude not consulted",
        }

    def _build_revised_goals(self, ruling: dict, original_goals: list[dict]) -> list[GoalSpec]:
        """Rebuild GoalSpec objects with Supervisor's corrected instructions."""
        target_files = ruling.get("target_files") or [
            g["target_files"][0] for g in original_goals if g.get("target_files")
        ]
        return [
            GoalSpec(
                id=f"supervisor_retry_{i}",
                title=f"Supervisor-directed fix {i + 1}",
                description=ruling.get("instructions", "Fix the implementation"),
                target_files=target_files[i:i+1],
                mode="fix",
            )
            for i in range(len(target_files))
        ]

    def _build_context(self, written_files: list[str], max_chars: int = 3000) -> str:
        parts = []
        for rel in written_files[:3]:
            p = self.repo_path / rel
            if not p.exists():
                continue
            body = p.read_text(encoding="utf-8")
            ext  = Path(rel).suffix.lstrip(".") or "text"
            parts.append(f"### {rel}\n```{ext}\n{body[:max_chars]}\n```")
        return "\n\n".join(parts)
