"""
core/git.py — Git integration
==============================
Auto-commits and optionally pushes after successful goals.
Used by CoderAgent after writing files, and auto_wire after wiring.

All git operations are fire-and-forget — failures are logged but never
crash the agent pipeline.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
from pathlib import Path
from typing import Optional

log = logging.getLogger("git")


class GitManager:
    """
    Thin async wrapper around git CLI.
    All methods are safe to call even if git is not installed or the
    directory is not a git repo — they log a warning and return False.
    """

    def __init__(
        self,
        repo_path: Path,
        enabled: bool = True,
        auto_push: bool = False,
        commit_prefix: str = "agent",
    ):
        self.repo_path    = repo_path
        self.enabled      = enabled
        self.auto_push    = auto_push
        self.prefix       = commit_prefix
        self._git_ok: Optional[bool] = None   # cached availability check

    async def commit(
        self,
        files: list[str],
        message: str,
        task_id: str = "",
    ) -> bool:
        """
        Stage `files` and commit with `message`.
        If auto_push is True, also pushes to origin.
        Returns True on success.
        """
        if not self.enabled:
            return False
        if not await self._is_available():
            return False
        if not files:
            return False

        tag = f"[{task_id}] " if task_id else ""
        full_msg = f"{self.prefix}: {tag}{message}"

        try:
            # Stage
            abs_files = [str(self.repo_path / f) for f in files]
            ok = await self._run(["git", "add", "--"] + abs_files)
            if not ok:
                return False

            # Nothing staged?
            diff = await self._run(["git", "diff", "--cached", "--quiet"], check=False)
            if diff:   # returncode 0 means no diff
                log.info("Nothing to commit for: %s", files)
                return True

            # Commit
            ok = await self._run(["git", "commit", "-m", full_msg])
            if not ok:
                return False

            log.info("Committed: %s", full_msg)

            # Push (optional)
            if self.auto_push:
                branch = await self._current_branch()
                await self._run(["git", "push", "origin", branch])
                log.info("Pushed to origin/%s", branch)

            return True

        except Exception as e:
            log.warning("Git commit error: %s", e)
            return False

    async def current_branch(self) -> str:
        return await self._current_branch()

    async def status(self) -> str:
        """Return `git status --short` output."""
        if not await self._is_available():
            return "(git unavailable)"
        proc = await asyncio.create_subprocess_exec(
            "git", "status", "--short",
            cwd=str(self.repo_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        return stdout.decode(errors="replace").strip()

    async def log_recent(self, n: int = 5) -> list[str]:
        """Return last N commit messages."""
        if not await self._is_available():
            return []
        proc = await asyncio.create_subprocess_exec(
            "git", "log", f"-{n}", "--oneline",
            cwd=str(self.repo_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        return stdout.decode(errors="replace").strip().splitlines()

    # ── Internals ─────────────────────────────────────────────────────────────

    async def _is_available(self) -> bool:
        if self._git_ok is not None:
            return self._git_ok
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "rev-parse", "--is-inside-work-tree",
                cwd=str(self.repo_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()
            self._git_ok = proc.returncode == 0
        except FileNotFoundError:
            log.warning("git not found in PATH — git integration disabled")
            self._git_ok = False
        except Exception:
            self._git_ok = False
        return self._git_ok

    async def _current_branch(self) -> str:
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "rev-parse", "--abbrev-ref", "HEAD",
                cwd=str(self.repo_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            return stdout.decode().strip() or "main"
        except Exception:
            return "main"

    async def _run(self, cmd: list[str], check: bool = True, timeout: float = 30) -> bool:
        """
        Run a git command. Returns True if returncode == 0.
        If check=False, returns True for returncode != 0 (used for diff --quiet).
        """
        try:
            proc = await asyncio.wait_for(
                asyncio.create_subprocess_exec(
                    *cmd,
                    cwd=str(self.repo_path),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                ),
                timeout=5,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            rc = proc.returncode

            if rc != 0 and check:
                log.warning(
                    "git %s failed (rc=%d): %s",
                    cmd[1], rc, stderr.decode(errors="replace")[:200],
                )
                return False

            return rc == 0 if check else rc != 0

        except asyncio.TimeoutError:
            log.warning("git %s timed out", cmd[1] if len(cmd) > 1 else cmd)
            return False
        except Exception as e:
            log.warning("git %s error: %s", cmd[1] if len(cmd) > 1 else cmd, e)
            return False
