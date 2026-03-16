"""
agents/fixer.py — Fixer Agent
================================
Receives TEST_RESULTS (failures) from Tester.
Patches code and sends PATCH_READY back to Tester.
Loops until tests pass OR max_attempts reached.
On exhaustion → escalates to Supervisor.

Uses: low-cost local model (OllamaBackend)
This is the SELF-HEALING core of the system.
"""

from __future__ import annotations

import ast
import re
import shutil
import time
from pathlib import Path
from typing import Optional

from core.base_agent import BaseAgent
from core.events import EventType, emit
from core.llm import BaseLLMBackend
from core.messages import (
    AgentRole, Message, MessageType, TaskStatus, TestResult
)

MAX_FIX_ATTEMPTS = 4   # before escalating to Supervisor

FIXER_SYSTEM = """You are a debugging expert. Fix broken Python code.

You will receive:
  - Failing test output with error details
  - The current broken file content

OUTPUT RULES — same as coder:
1. Output ONLY the files that need changing.
2. Each file in a fenced block with the relative path as the fence language tag:

```relative/path/to/file.py
# complete corrected file content here
```

3. Complete file every time — no diffs, no ellipsis.
4. Zero prose outside code fences.
5. Fix ONLY what's broken — do not change unrelated logic.
"""


class FixerAgent(BaseAgent):
    role = AgentRole.FIXER

    def __init__(self, bus, llm: BaseLLMBackend, repo_path: Path):
        super().__init__(bus)
        self.llm       = llm
        self.repo_path = repo_path
        self._backup_dir = repo_path / "_agent" / "backups"
        # Track attempts per task
        self._attempts: dict[str, int] = {}

    async def handle(self, message: Message):
        if message.type != MessageType.TEST_RESULTS:
            return

        task_id = message.task_id
        payload  = message.payload or {}
        result_d = payload.get("result", {})
        result   = TestResult(**result_d) if isinstance(result_d, dict) else result_d

        written_files: list[str] = payload.get("written_files", [])
        goals:         list[dict] = payload.get("goals", [])

        attempt = self._attempts.get(task_id, 0) + 1
        self._attempts[task_id] = attempt

        self.info(
            "Fix attempt %d/%d | %s",
            attempt, MAX_FIX_ATTEMPTS, result.summary(),
        )
        await emit(EventType.FIX_ATTEMPT, task_id=task_id, agent="fixer",
                   attempt=attempt, max=MAX_FIX_ATTEMPTS,
                   passed=result.passed, failed=result.failed)

        if attempt > MAX_FIX_ATTEMPTS:
            self.error(
                "Exhausted %d fix attempts — escalating to Supervisor", MAX_FIX_ATTEMPTS
            )
            await self.send(Message(
                type=MessageType.ESCALATION,
                sender=self.role,
                recipient=AgentRole.SUPERVISOR,
                payload={
                    "reason":         "fixer_exhausted",
                    "attempts":       attempt - 1,
                    "test_result":    result.__dict__,
                    "written_files":  written_files,
                    "goals":          goals,
                },
                task_id=task_id,
                attempt=attempt,
                status=TaskStatus.ESCALATED,
            ))
            return

        # Try pure-Python auto-fix first (fast, no LLM)
        auto_fixed = self._auto_fix(result, written_files)
        if auto_fixed:
            self.info("Auto-fixed %d issue(s) without LLM", len(auto_fixed))
            for f in auto_fixed:
                await emit(EventType.FIX_AUTO, task_id=task_id, agent="fixer", file=f)
        else:
            # Fall back to LLM-based fix
            fixed_files = await self._llm_fix(result, written_files, goals)
            if fixed_files:
                for f in fixed_files:
                    await emit(EventType.FIX_LLM, task_id=task_id, agent="fixer", file=f)
            if not fixed_files:
                self.warn("LLM fix produced no output — sending original files back")
                fixed_files = written_files

        files_to_test = list(set(written_files + (auto_fixed or [])))

        await self.send(Message(
            type=MessageType.PATCH_READY,
            sender=self.role,
            recipient=AgentRole.TESTER,
            payload={
                "written_files": files_to_test,
                "goals":         goals,
            },
            task_id=task_id,
            attempt=attempt,
            status=TaskStatus.IN_PROGRESS,
        ))

    # ─────────────────────────────────────────────────────────────────────────
    # Pure-Python auto-fix (no LLM, instant)
    # ─────────────────────────────────────────────────────────────────────────

    def _auto_fix(self, result: TestResult, written_files: list[str]) -> list[str]:
        """
        Attempt pattern-based fixes without LLM.
        Returns list of files patched, or empty list if nothing matched.
        """
        fixed = []
        for rel in written_files:
            if not rel.endswith(".py"):
                continue
            p = self.repo_path / rel
            if not p.exists():
                continue

            content = p.read_text(encoding="utf-8")
            new_content = content

            for error in result.errors:
                # Pattern: unused import causing NameError is less common;
                # focus on the most common auto-fixable patterns:

                # Fix 1: Missing trailing newline causing some linters to flag
                if not new_content.endswith("\n"):
                    new_content += "\n"

                # Fix 2: IndentationError — detect and skip (LLM needed)
                if "IndentationError" in error:
                    break

            if new_content != content:
                try:
                    ast.parse(new_content)   # only write if still valid
                    self._backup(rel)
                    p.write_text(new_content, encoding="utf-8")
                    fixed.append(rel)
                    self.info("Auto-fixed: %s", rel)
                except SyntaxError:
                    pass   # discard — LLM will handle it

        return fixed

    # ─────────────────────────────────────────────────────────────────────────
    # LLM-based fix
    # ─────────────────────────────────────────────────────────────────────────

    async def _llm_fix(
        self,
        result: TestResult,
        written_files: list[str],
        goals: list[dict],
    ) -> list[str]:

        context = self._build_context(written_files)
        errors_block = "\n".join(
            f"  - {e}" for e in (result.errors or ["(see output below)"])
        )

        prompt = f"""TEST FAILURES — fix the code below.

FAILURES:
  passed={result.passed}  failed={result.failed}
{errors_block}

TEST OUTPUT (last 1000 chars):
{result.output[-1000:] if result.output else '(none)'}

FILES TO FIX:
{chr(10).join(f'  - {f}' for f in written_files)}

CURRENT FILE STATE:
{context}

Output the corrected file(s) only:"""

        resp = await self.llm.complete_with_retry(
            prompt=prompt,
            system=FIXER_SYSTEM,
            max_tokens=4096,
            temperature=0.15,
        )

        if not resp.ok or not resp.text:
            self.warn("LLM fix returned empty response: %s", resp.error)
            return []

        updates = self._parse_response(resp.text)
        fixed = []
        for rel, content in updates.items():
            if rel not in written_files:
                self.warn("Fixer tried to modify non-target file %s — skipping", rel)
                continue
            try:
                ast.parse(content)   # only write if syntactically valid
                self._backup(rel)
                p = self.repo_path / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(content, encoding="utf-8")
                fixed.append(rel)
                self.info("LLM-fixed: %s", rel)
            except SyntaxError as e:
                self.warn("LLM fix produced invalid syntax for %s: %s", rel, e)

        return fixed

    def _build_context(self, written_files: list[str]) -> str:
        parts = []
        for rel in written_files[:4]:
            p = self.repo_path / rel
            if not p.exists():
                continue
            body = p.read_text(encoding="utf-8")
            ext  = Path(rel).suffix.lstrip(".") or "text"
            parts.append(f"### {rel}\n```{ext}\n{body[:3000]}\n```")
        return "\n\n".join(parts)

    def _parse_response(self, text: str) -> dict[str, str]:
        pattern = re.compile(r"```([^\n`]+[./\\][^\n`]*)\n(.*?)```", re.DOTALL)
        out = {}
        for path, code in pattern.findall(text):
            path = path.strip().lstrip("/\\").replace("\\", "/")
            code = code.strip()
            if path and code:
                out[path] = code
        return out

    def _backup(self, rel: str):
        src = self.repo_path / rel
        if not src.exists():
            return
        from datetime import datetime, timezone
        ts  = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        p   = Path(rel)
        dst = self._backup_dir / p.parent / f"{p.stem}__fix_{ts}{p.suffix}"
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
