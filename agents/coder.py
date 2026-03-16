"""
agents/coder.py — Coder Agent
================================
Receives a TASK_PLAN (list of GoalSpec) from the Planner.
Executes goals in parallel where safe, sequentially otherwise.
Writes files to disk. Sends IMPLEMENTATION to Tester.

Uses: low-cost local model (OllamaBackend)
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import re
import shutil
import time
from pathlib import Path, PurePosixPath
from typing import Optional

from core.base_agent import BaseAgent
from core.events import EventType, emit
from core.git import GitManager
from core.llm import BaseLLMBackend
from core.messages import (
    AgentRole, GoalSpec, Message, MessageType, TaskStatus
)

# Load project context if available
_CODER_PROJECT_CONTEXT = ""
try:
    _pk_path = Path(__file__).resolve().parent.parent / "project_knowledge.py"
    if _pk_path.exists():
        import importlib.util
        spec = importlib.util.spec_from_file_location("project_knowledge", _pk_path)
        _pk = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_pk)
        _CODER_PROJECT_CONTEXT = getattr(_pk, "CODER_PROJECT_CONTEXT", "")
except Exception:
    pass

MAX_FILE_CHARS = 4000
MAX_TOKENS     = 4096
MAX_TOKENS_EXT = 6144
MAX_RETRIES    = 3


CODER_SYSTEM = """You are an expert Python coding agent.

OUTPUT RULES:
1. Output ONLY the files listed in FILES TO OUTPUT — no extra files.
2. Each file in a fenced block, path as the fence language tag:

```relative/path/to/file.py
# complete file content here
```

3. Complete file every time — no diffs, no ellipsis, no placeholders.
4. Zero prose outside code fences.
5. All Python must be syntactically valid.
6. Never use: os.system(), exec(), eval(), shutil.rmtree()
"""


class CoderAgent(BaseAgent):
    role = AgentRole.CODER

    def __init__(self, bus, llm: BaseLLMBackend, repo_path: Path,
                 git: Optional[GitManager] = None):
        super().__init__(bus)
        self.llm       = llm
        self.repo_path = repo_path
        self._backup_dir = repo_path / "_agent" / "backups"
        self.git = git

    async def handle(self, message: Message):
        if message.type != MessageType.TASK_PLAN:
            return

        goals: list[GoalSpec] = message.payload
        self.info("Received %d goal(s) to implement", len(goals))
        await emit(EventType.GOAL_STARTED, task_id=message.task_id, agent="coder",
                   count=len(goals))

        written_files: list[str] = []
        failed_goals:  list[str] = []

        # Execute goals sequentially (file-order dependency safety)
        # Parallelise only goals with non-overlapping target_files
        safe_parallel = self._split_parallel(goals)

        for batch in safe_parallel:
            results = await asyncio.gather(
                *[self._execute_goal(g) for g in batch],
                return_exceptions=False,
            )
            for goal, files_written in zip(batch, results):
                if files_written:
                    written_files.extend(files_written)
                else:
                    failed_goals.append(goal.id)

        if failed_goals:
            self.warn("Failed goals: %s", failed_goals)

        if not written_files:
            await self.send(Message(
                type=MessageType.STATUS,
                sender=self.role,
                recipient=AgentRole.ORCHESTRATOR,
                payload={"error": "coder_no_output", "failed_goals": failed_goals},
                task_id=message.task_id,
                status=TaskStatus.FAILED,
            ))
            return

        self.info("Written %d file(s): %s", len(written_files), written_files)
        await self.send(Message(
            type=MessageType.IMPLEMENTATION,
            sender=self.role,
            recipient=AgentRole.TESTER,
            payload={
                "written_files": written_files,
                "goals": [g.__dict__ for g in goals],
            },
            task_id=message.task_id,
            status=TaskStatus.IN_PROGRESS,
        ))

    # ──────────────────────────────────────────────────────────────────────────

    def _split_parallel(self, goals: list[GoalSpec]) -> list[list[GoalSpec]]:
        """
        Split goals into parallel batches — goals touching different files
        can run concurrently. Goals sharing files run sequentially.
        """
        batches: list[list[GoalSpec]] = []
        used_files: set[str] = set()

        current_batch: list[GoalSpec] = []
        for goal in goals:
            targets = set(goal.target_files)
            if targets & used_files:
                # Conflict — flush current batch, start new one
                if current_batch:
                    batches.append(current_batch)
                current_batch = [goal]
                used_files    = targets
            else:
                current_batch.append(goal)
                used_files |= targets

        if current_batch:
            batches.append(current_batch)

        return batches

    async def _execute_goal(self, goal: GoalSpec) -> list[str]:
        last_error: Optional[str] = None
        max_tokens  = MAX_TOKENS

        for attempt in range(MAX_RETRIES + 1):
            if attempt > 0:
                self.info("Retry %d/%d for goal '%s'", attempt, MAX_RETRIES, goal.id)
                await asyncio.sleep(4)

            context = self._build_context(goal)
            prompt  = self._build_prompt(goal, context, last_error, attempt, max_tokens)

            resp = await self.llm.complete_with_retry(
                prompt=prompt,
                system=CODER_SYSTEM,
                max_tokens=max_tokens,
                temperature=0.15,
            )

            if not resp.ok or not resp.text:
                last_error = f"Empty response ({resp.error})"
                continue

            updates = self._parse_response(resp.text)
            updates = {k: v for k, v in updates.items() if k in goal.target_files}

            if not updates:
                last_error = f"No target files in output. Preview: {resp.text[:300]}"
                continue

            # Check for truncation
            truncated = any(self._is_truncated(v, k) for k, v in updates.items())
            if truncated and attempt < MAX_RETRIES:
                last_error = "Output truncated — return the complete file"
                max_tokens = MAX_TOKENS_EXT
                continue

            # Validate
            errors = []
            for rel, content in updates.items():
                errors.extend(self._validate(rel, content))
            if errors:
                last_error = " | ".join(errors)
                self.warn("Validation failed: %s", last_error)
                continue

            # Write
            written = []
            for rel, content in updates.items():
                self._backup(rel)
                self._write(rel, content)
                written.append(rel)
                self.info("Wrote %s (%d chars)", rel, len(content))
                await emit(EventType.FILE_WRITTEN, task_id=goal.id, agent="coder",
                           file=rel, chars=len(content))

            # Git commit
            if self.git and written:
                await self.git.commit(
                    files=written,
                    message=goal.title,
                    task_id=goal.id,
                )
                await emit(EventType.GIT_COMMIT, task_id=goal.id, agent="coder",
                           files=written, message=goal.title)

            return written

        self.error("Goal '%s' failed after %d attempts: %s", goal.id, MAX_RETRIES + 1, last_error)
        return []

    def _build_context(self, goal: GoalSpec) -> str:
        parts = []
        for rel in goal.target_files[:4]:
            p = self.repo_path / rel
            label = "(MODIFY)" if p.exists() else "(CREATE NEW)"
            body  = p.read_text(encoding="utf-8") if p.exists() else "# Does not exist yet"
            if len(body) > MAX_FILE_CHARS:
                body = body[:MAX_FILE_CHARS] + "\n# ... truncated"
            ext = Path(rel).suffix.lstrip(".") or "text"
            parts.append(f"### {rel} {label}\n```{ext}\n{body}\n```")
        return "\n\n".join(parts)

    def _build_prompt(
        self, goal: GoalSpec, context: str,
        retry_error: Optional[str], attempt: int, max_tokens: int
    ) -> str:
        targets  = "\n".join(f"  - {f}" for f in goal.target_files)
        retry_block = (
            f"\nPREVIOUS ATTEMPT {attempt}/{MAX_RETRIES} FAILED:\n"
            f"  Error: {retry_error}\n"
            f"  Fix the error and output the complete corrected file.\n"
        ) if retry_error else ""

        project_block = f"\nPROJECT CONTEXT:\n{_CODER_PROJECT_CONTEXT}\n" if _CODER_PROJECT_CONTEXT else ""

        return f"""You are an expert Python/FastAPI/Jinja2 coding agent.
{project_block}
{retry_block}
TASK: {goal.title}
{goal.description.strip()}

FILES TO OUTPUT:
{targets}

CURRENT STATE:
{context}

Output now:"""

    def _parse_response(self, text: str) -> dict[str, str]:
        pattern = re.compile(r"```([^\n`]+[./\\][^\n`]*)\n(.*?)```", re.DOTALL)
        out = {}
        for path, code in pattern.findall(text):
            path = path.strip().lstrip("/\\").replace("\\", "/")
            code = code.strip()
            if path and code:
                out[path] = code
        return out

    def _is_truncated(self, content: str, rel: str) -> bool:
        if not rel.endswith(".py"):
            return False
        try:
            ast.parse(content)
            return False
        except SyntaxError as e:
            msg = e.msg or ""
            return "was never closed" in msg or "unexpected EOF" in msg

    def _validate(self, rel: str, content: str) -> list[str]:
        errors = []
        if not content.strip():
            return [f"{rel}: empty"]
        if rel.endswith(".py"):
            try:
                ast.parse(content)
            except SyntaxError as e:
                errors.append(f"SyntaxError L{e.lineno}: {e.msg}")
        for pat in [r"os\.system\(", r"(?<!\w)exec\(", r"(?<!\w)eval\("]:
            if re.search(pat, content):
                errors.append(f"Forbidden pattern: {pat}")
        return errors

    def _backup(self, rel: str):
        # Normalise slashes for Windows compatibility
        rel = rel.replace("\\", "/")
        src = self.repo_path / rel
        if not src.exists():
            return
        from datetime import datetime, timezone
        ts  = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        p   = Path(rel)
        dst = self._backup_dir / p.parent / f"{p.stem}__{ts}{p.suffix}"
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)

    def _write(self, rel: str, content: str):
        rel = rel.replace("\\", "/")
        p = self.repo_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
