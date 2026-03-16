"""
agents/planner.py — Planner Agent
===================================
Receives a raw task string from the Orchestrator.
Breaks it into a list of GoalSpec objects.
Sends a TASK_PLAN message to the Coder.

Uses: low-cost local model (Gemini CLI / Codex class — OllamaBackend)
"""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

from core.base_agent import BaseAgent
from core.events import EventType, emit
from core.llm import BaseLLMBackend
from core.messages import (
    AgentRole, GoalSpec, Message, MessageType, TaskStatus
)

# Load project context if available
_PROJECT_CONTEXT = ""
try:
    _pk_path = Path(__file__).resolve().parent.parent / "project_knowledge.py"
    if _pk_path.exists():
        import importlib.util
        spec = importlib.util.spec_from_file_location("project_knowledge", _pk_path)
        _pk = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_pk)
        _PROJECT_CONTEXT = getattr(_pk, "PLANNER_PROJECT_CONTEXT", "")
except Exception:
    pass

PLANNER_SYSTEM = """You are a senior software engineer breaking a task into concrete coding goals.

OUTPUT FORMAT — valid JSON only, no prose, no markdown fences:
{
  "goals": [
    {
      "id": "snake_case_id",
      "title": "Max 8 words",
      "description": "Complete self-contained coding instructions.",
      "target_files": ["relative/path.py"],
      "mode": "feature"
    }
  ]
}

RULES:
1. One goal = one file modified or created.
2. Always produce at least 1 goal.
3. mode is one of: "feature", "rule", "refactor", "test", "fix"
4. description must be fully self-contained — the coder sees no other context.
5. No prose outside the JSON object.
6. For HTML template changes: target_files must include the .html file AND the .css file.
7. For new FastAPI routes: target_files must include app/main.py.
8. Never create duplicate routes — check EXISTING ROUTES in the project context.
"""


class PlannerAgent(BaseAgent):
    role = AgentRole.PLANNER

    def __init__(self, bus, llm: BaseLLMBackend):
        super().__init__(bus)
        self.llm = llm

    async def handle(self, message: Message):
        if message.type not in (MessageType.DIRECTIVE,):
            return

        task: str = message.payload.get("task", "") if isinstance(message.payload, dict) else str(message.payload)
        self.info("Planning task: %s", task[:120])
        await emit(EventType.PLAN_STARTED, task_id=message.task_id, agent="planner",
                   task=task[:120])

        goals = await self._plan(task, task_id=message.task_id)

        if goals:
            self.info("Produced %d goal(s)", len(goals))
            for i, g in enumerate(goals, 1):
                self.info("  %d. %s → %s", i, g.title, g.target_files)
            await emit(EventType.PLAN_READY, task_id=message.task_id, agent="planner",
                       count=len(goals), titles=[g.title for g in goals])

            await self.send(Message(
                type=MessageType.TASK_PLAN,
                sender=self.role,
                recipient=AgentRole.CODER,
                payload=goals,
                task_id=message.task_id,
                status=TaskStatus.IN_PROGRESS,
            ))
        else:
            self.error("Planner produced no goals — escalating")
            await emit(EventType.PLAN_FAILED, task_id=message.task_id, agent="planner",
                       task=task[:80])
            await self.send(Message(
                type=MessageType.STATUS,
                sender=self.role,
                recipient=AgentRole.ORCHESTRATOR,
                payload={"error": "planner_no_goals", "task": task},
                task_id=message.task_id,
                status=TaskStatus.FAILED,
            ))

    async def _plan(self, task: str, task_id: str) -> list[GoalSpec]:
        prompt = f"TASK: {task}\n\nRespond with valid JSON only:"
        if _PROJECT_CONTEXT:
            prompt = f"{_PROJECT_CONTEXT}\n\nTASK: {task}\n\nRespond with valid JSON only:"

        for attempt in range(3):
            if attempt > 0:
                prompt += f"\n\nAttempt {attempt + 1}: previous response was invalid JSON. Return ONLY the JSON object."
                self.warn("JSON repair attempt %d", attempt)

            resp = await self.llm.complete_with_retry(
                prompt=prompt,
                system=PLANNER_SYSTEM,
                max_tokens=2048,
                temperature=0.20,
            )

            if not resp.ok or not resp.text:
                continue

            goals = self._parse(resp.text, task_id)
            if goals:
                return goals

        return []

    def _parse(self, raw: str, task_id: str) -> list[GoalSpec]:
        # Strip <think> blocks
        raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
        # Strip markdown fences
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())

        try:
            data = json.loads(raw)
            raw_goals = data.get("goals", [])
        except json.JSONDecodeError:
            self.warn("JSON parse failed: %s", raw[:200])
            return []

        goals = []
        for g in raw_goals:
            try:
                goals.append(GoalSpec(
                    id=g.get("id") or f"goal_{uuid.uuid4().hex[:6]}",
                    title=g.get("title", "Untitled"),
                    description=g.get("description", ""),
                    target_files=g.get("target_files", []),
                    mode=g.get("mode", "feature"),
                ))
            except Exception as e:
                self.warn("Skipping malformed goal: %s", e)

        return goals
