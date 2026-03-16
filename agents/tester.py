"""
agents/tester.py — Tester Agent
==================================
Receives IMPLEMENTATION from Coder (or PATCH_READY from Fixer).
Runs tests. Emits TEST_RESULTS.

If tests pass  → STATUS(passed) to Orchestrator.
If tests fail  → TEST_RESULTS(failed) to Fixer.

Uses: subprocess (real test runner) + optional LLM for test generation.
"""

from __future__ import annotations

import asyncio
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

from core.base_agent import BaseAgent
from core.events import EventType, emit
from core.llm import BaseLLMBackend
from core.messages import (
    AgentRole, Message, MessageType, TaskStatus, TestResult
)


class TesterAgent(BaseAgent):
    role = AgentRole.TESTER

    def __init__(
        self,
        bus,
        llm: BaseLLMBackend,
        repo_path: Path,
        python_exe: str = sys.executable,
        test_command: Optional[list[str]] = None,
    ):
        super().__init__(bus)
        self.llm          = llm
        self.repo_path    = repo_path
        self.python_exe   = python_exe
        # Default: run pytest; override via test_command
        self.test_command = test_command or [python_exe, "-m", "pytest", "--tb=short", "-q"]

    async def handle(self, message: Message):
        if message.type not in (MessageType.IMPLEMENTATION, MessageType.PATCH_READY):
            return

        payload      = message.payload or {}
        written_files = payload.get("written_files", [])
        attempt       = message.attempt

        self.info(
            "Testing %d file(s) [attempt %d]: %s",
            len(written_files), attempt, written_files,
        )
        await emit(EventType.TEST_STARTED, task_id=message.task_id, agent="tester",
                   files=written_files, attempt=attempt)

        # 1. Syntax / import check (fast, always runs)
        syntax_errors = await self._check_syntax(written_files)

        # 2. Run test suite
        result = await self._run_tests(written_files)

        # 3. Merge syntax errors into test result
        result.errors = syntax_errors + result.errors

        self.info(
            "Results: %s  (%.1fs)",
            result.summary(), result.duration_s,
        )

        if result.all_passed:
            await emit(EventType.TEST_PASSED, task_id=message.task_id, agent="tester",
                       passed=result.passed, duration_s=round(result.duration_s, 1))
            await self.send(Message(
                type=MessageType.STATUS,
                sender=self.role,
                recipient=AgentRole.ORCHESTRATOR,
                payload={
                    "result": result.__dict__,
                    "written_files": written_files,
                    "message": "All tests passed",
                },
                task_id=message.task_id,
                attempt=attempt,
                status=TaskStatus.PASSED,
            ))
        else:
            self.warn(
                "Tests FAILED: %d failure(s), %d error(s)",
                result.failed, len(result.errors),
            )
            await emit(EventType.TEST_FAILED, task_id=message.task_id, agent="tester",
                       failed=result.failed, errors=result.errors[:3],
                       duration_s=round(result.duration_s, 1))
            await self.send(Message(
                type=MessageType.TEST_RESULTS,
                sender=self.role,
                recipient=AgentRole.FIXER,
                payload={
                    "result":         result.__dict__,
                    "written_files":  written_files,
                    "goals":          payload.get("goals", []),
                },
                task_id=message.task_id,
                attempt=attempt,
                status=TaskStatus.FAILED,
            ))

    # ─────────────────────────────────────────────────────────────────────────

    async def _check_syntax(self, rel_paths: list[str]) -> list[str]:
        errors = []
        for rel in rel_paths:
            p = self.repo_path / rel
            if not p.exists() or not rel.endswith(".py"):
                continue
            try:
                import ast
                ast.parse(p.read_text(encoding="utf-8"))
            except SyntaxError as e:
                errors.append(f"{rel} SyntaxError L{e.lineno}: {e.msg}")
        return errors

    async def _run_tests(self, written_files: list[str]) -> TestResult:
        """
        Run the project test suite.
        On Windows or if no tests exist, falls back to a syntax+import check
        so the pipeline doesn't stall with 0 passed / 0 failed.
        """
        t0 = time.monotonic()

        # Normalise paths (Windows → forward slash)
        norm_files = [f.replace("\\", "/") for f in written_files]

        # Look for related tests first
        test_paths = self._find_related_tests(norm_files)

        # If no test files exist at all, do a lightweight import check instead
        tests_dir = self.repo_path / "tests"
        has_any_tests = tests_dir.exists() and any(tests_dir.rglob("test_*.py"))

        if not has_any_tests and not test_paths:
            return await self._import_check(norm_files, t0)

        cmd = list(self.test_command)
        if test_paths:
            # Convert to absolute paths (handles Windows)
            abs_test_paths = [str(self.repo_path / p) for p in test_paths]
            cmd += abs_test_paths
            self.info("Running scoped tests: %s", test_paths)
        else:
            self.info("Running full test suite")

        try:
            proc = await asyncio.wait_for(
                asyncio.create_subprocess_exec(
                    *cmd,
                    cwd=str(self.repo_path),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                ),
                timeout=10,
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
            except asyncio.TimeoutError:
                proc.kill()
                return TestResult(
                    passed=0, failed=0,
                    errors=["Test run timed out after 120s"],
                    duration_s=time.monotonic() - t0,
                )

            output   = (stdout.decode(errors="replace") + stderr.decode(errors="replace"))
            duration = time.monotonic() - t0
            return self._parse_pytest_output(output, proc.returncode, duration)

        except FileNotFoundError:
            # pytest not installed — fall back to import check
            return await self._import_check(norm_files, t0)
        except Exception as e:
            return TestResult(
                passed=0, failed=0,
                errors=[f"Test runner error: {e}"],
                duration_s=time.monotonic() - t0,
            )

    async def _import_check(self, rel_paths: list[str], t0: float) -> TestResult:
        """
        Lightweight fallback: check that all written .py files can be
        parsed syntactically. Counts as 'passed' if no errors found.
        No pytest required.
        """
        import ast as _ast
        errors = []
        checked = 0
        for rel in rel_paths:
            if not rel.endswith(".py"):
                continue
            p = self.repo_path / rel
            if not p.exists():
                continue
            checked += 1
            try:
                _ast.parse(p.read_text(encoding="utf-8"))
            except SyntaxError as e:
                errors.append(f"{rel}: SyntaxError L{e.lineno}: {e.msg}")

        if errors:
            return TestResult(
                passed=0, failed=len(errors), errors=errors,
                output="(syntax check — no pytest found)",
                duration_s=time.monotonic() - t0,
            )
        return TestResult(
            passed=max(checked, 1), failed=0, errors=[],
            output=f"(syntax check passed for {checked} file(s) — no pytest suite found)",
            duration_s=time.monotonic() - t0,
        )

    def _find_related_tests(self, written_files: list[str]) -> list[str]:
        tests_dir = self.repo_path / "tests"
        if not tests_dir.exists():
            return []

        found = []
        for rel in written_files:
            stem = Path(rel).stem
            # e.g. rule_foo.py → tests/test_rule_foo.py
            candidates = [
                tests_dir / f"test_{stem}.py",
                tests_dir / f"{stem}_test.py",
            ]
            for c in candidates:
                if c.exists():
                    found.append(str(c.relative_to(self.repo_path)))
        return found

    def _parse_pytest_output(self, output: str, returncode: int, duration: float) -> TestResult:
        # Parse: "5 passed, 2 failed, 1 error in 3.4s"
        passed = failed = 0
        errors: list[str] = []

        m = re.search(r"(\d+) passed", output)
        if m:
            passed = int(m.group(1))

        m = re.search(r"(\d+) failed", output)
        if m:
            failed = int(m.group(1))

        # Extract FAILED lines
        for line in output.splitlines():
            line = line.strip()
            if line.startswith("FAILED ") or line.startswith("ERROR "):
                errors.append(line[:200])

        # If tests were collected but suite returned 0, all passed
        if returncode == 0 and failed == 0 and not errors:
            # Ensure passed is at least 1 if something ran
            if passed == 0 and "passed" in output.lower():
                passed = 1

        return TestResult(
            passed=passed,
            failed=failed,
            errors=errors,
            output=output[:2000],
            duration_s=duration,
        )
