"""
main.py — Multi-Agent Autonomous Coding System
================================================
Entry point. Each agent has its own dedicated LLM provider.

Current assignment:
  Planner    → OpenAI GPT-4o           (structured JSON planning)
  Coder      → OpenAI o4-mini / Codex  (code generation, Responses API)
  Tester     → Ollama (local/free)     (syntax check, no API cost)
  Fixer      → Gemini 2.0 Flash        (fast debugging)
  Supervisor → Gemini 2.0 Flash        (temp — switch to Anthropic later)

To switch Supervisor to Claude later:
  set SUPERVISOR_BACKEND=anthropic
  set ANTHROPIC_API_KEY=sk-ant-...

Usage:
  python main.py --task "add account mapping section to configure page" ^
                 --repo "C:\\path\\to\\review_automation_poc"
  python main.py --task "..." --backend mock    # dry run, no API calls
  python main.py --batch tasks.txt
  python main.py --invent --max-rules 5
  python main.py --dashboard
  python main.py --status

Required environment variables:
  OPENAI_API_KEY    — Planner + Coder
  GEMINI_API_KEY    — Fixer + Supervisor (temporary)
  ANTHROPIC_API_KEY — Supervisor (future, when you switch)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from config import (
    PLANNER_BACKEND, PLANNER_MODEL,
    CODER_BACKEND,   CODER_MODEL,
    TESTER_BACKEND,  TESTER_MODEL,
    FIXER_BACKEND,   FIXER_MODEL,
    SUPERVISOR_BACKEND, SUPERVISOR_MODEL,
    GIT_ENABLED, GIT_AUTO_PUSH, GIT_COMMIT_PREFIX,
    DASHBOARD_HOST, DASHBOARD_PORT,
    STATE_PATH, INVENT_MAX_RULES, INVENT_SLEEP_BETWEEN_SEC,
    DEFAULT_TASK_TIMEOUT_SEC,
)

from core.bus    import AgentBus
from core.events import EventType, emit
from core.git    import GitManager
from core.llm    import make_backend

from agents.orchestrator import OrchestratorAgent
from agents.planner      import PlannerAgent
from agents.coder        import CoderAgent
from agents.tester       import TesterAgent
from agents.fixer        import FixerAgent
from agents.supervisor   import SupervisorAgent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(name)-14s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("main")


def _find_python_exe(repo_path: Path) -> str:
    """Auto-detect the repo's venv Python. Works on Windows and Unix."""
    for c in [
        repo_path / "venv" / "Scripts" / "python.exe",
        repo_path / "venv" / "bin" / "python",
        repo_path / ".venv" / "Scripts" / "python.exe",
        repo_path / ".venv" / "bin" / "python",
    ]:
        if c.exists():
            return str(c)
    return sys.executable


def build_system(
    repo_path:          Path,
    python_exe:         str  = sys.executable,
    test_command:       list = None,
    git_enabled:        bool = GIT_ENABLED,
    git_auto_push:      bool = GIT_AUTO_PUSH,
    mock_all:           bool = False,   # override all backends with Mock (dry run)
) -> OrchestratorAgent:
    """
    Wire all agents. Each agent gets its own backend.

    Provider map (from config.py / env vars):
      Planner    → PLANNER_BACKEND   / PLANNER_MODEL
      Coder      → CODER_BACKEND     / CODER_MODEL
      Tester     → TESTER_BACKEND    / TESTER_MODEL
      Fixer      → FIXER_BACKEND     / FIXER_MODEL
      Supervisor → SUPERVISOR_BACKEND/ SUPERVISOR_MODEL
    """
    bus = AgentBus()

    def _backend(b, m):
        return make_backend("mock") if mock_all else make_backend(b, model=m)

    planner_llm    = _backend(PLANNER_BACKEND,    PLANNER_MODEL)
    coder_llm      = _backend(CODER_BACKEND,      CODER_MODEL)
    tester_llm     = _backend(TESTER_BACKEND,     TESTER_MODEL)
    fixer_llm      = _backend(FIXER_BACKEND,      FIXER_MODEL)
    supervisor_llm = _backend(SUPERVISOR_BACKEND, SUPERVISOR_MODEL)

    git = GitManager(
        repo_path=repo_path,
        enabled=git_enabled and not mock_all,
        auto_push=git_auto_push,
        commit_prefix=GIT_COMMIT_PREFIX,
    )

    orchestrator = OrchestratorAgent(bus)
    PlannerAgent(bus,    llm=planner_llm)
    CoderAgent(bus,      llm=coder_llm,      repo_path=repo_path, git=git)
    TesterAgent(bus,     llm=tester_llm,     repo_path=repo_path,
                python_exe=python_exe,       test_command=test_command)
    FixerAgent(bus,      llm=fixer_llm,      repo_path=repo_path)
    SupervisorAgent(bus, llm=supervisor_llm, repo_path=repo_path)

    if not mock_all:
        log.info("Agents wired:")
        log.info("  Planner    → %-14s  %s", PLANNER_BACKEND,    PLANNER_MODEL)
        log.info("  Coder      → %-14s  %s", CODER_BACKEND,      CODER_MODEL)
        log.info("  Tester     → %-14s  %s", TESTER_BACKEND,     TESTER_MODEL)
        log.info("  Fixer      → %-14s  %s", FIXER_BACKEND,      FIXER_MODEL)
        log.info("  Supervisor → %-14s  %s", SUPERVISOR_BACKEND, SUPERVISOR_MODEL)
        log.info("  Repo       → %s", repo_path)

    return orchestrator


# ── Task runners ───────────────────────────────────────────────────────────────

async def run_task(task, repo_path, python_exe, timeout, mock_all, dashboard):
    orch = build_system(repo_path=repo_path, python_exe=python_exe, mock_all=mock_all)
    await emit(EventType.SESSION_START, agent="main", task=task[:80])

    if dashboard:
        from dashboard.server import run_server
        holder = {}
        async def _t():
            holder["rec"] = await orch.submit_task(task, timeout=timeout)
        await asyncio.gather(_t(), run_server(DASHBOARD_HOST, DASHBOARD_PORT),
                             return_exceptions=True)
        rec = holder.get("rec")
    else:
        rec = await orch.submit_task(task, timeout=timeout)

    orch.print_summary()
    passed = rec and rec.status.value == "passed"
    await emit(EventType.SESSION_END, agent="main", passed=passed)
    return 0 if passed else 1


async def run_batch(tasks_file, repo_path, python_exe, timeout, mock_all, dashboard):
    lines = [
        l.strip() for l in tasks_file.read_text(encoding="utf-8").splitlines()
        if l.strip() and not l.strip().startswith("#")
    ]
    if not lines:
        log.error("No tasks in %s", tasks_file); return 1

    log.info("Batch: %d task(s)", len(lines))
    orch = build_system(repo_path=repo_path, python_exe=python_exe, mock_all=mock_all)
    await emit(EventType.SESSION_START, agent="main", batch_size=len(lines))

    coros = [orch.submit_task(t, timeout=timeout) for t in lines]
    if dashboard:
        from dashboard.server import run_server
        results = await asyncio.gather(*coros, run_server(DASHBOARD_HOST, DASHBOARD_PORT),
                                       return_exceptions=True)
        results = results[:-1]
    else:
        results = await asyncio.gather(*coros, return_exceptions=True)

    orch.print_summary()
    passed = sum(1 for r in results
                 if not isinstance(r, Exception) and r.status.value == "passed")
    await emit(EventType.SESSION_END, agent="main", passed=passed, total=len(lines))
    return 0 if passed == len(lines) else 1


async def run_invent(repo_path, python_exe, max_rules, resume, mock_all, dashboard):
    from agents.inventor import run_invention_loop
    orch = build_system(repo_path=repo_path, python_exe=python_exe, mock_all=mock_all)
    await emit(EventType.SESSION_START, agent="main", mode="invent",
               max_rules=max_rules, resume=resume)

    invent_coro = run_invention_loop(
        orchestrator=orch, repo_path=repo_path, state_path=STATE_PATH,
        max_rules=max_rules, sleep_sec=INVENT_SLEEP_BETWEEN_SEC, resume=resume,
    )
    if dashboard:
        from dashboard.server import run_server
        await asyncio.gather(invent_coro, run_server(DASHBOARD_HOST, DASHBOARD_PORT),
                             return_exceptions=True)
    else:
        await invent_coro

    orch.print_summary()
    await emit(EventType.SESSION_END, agent="main", mode="invent")
    return 0


# ── CLI ────────────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(
        description="Multi-Agent Autonomous Coding System",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py --task "add account mapping section to configure page" --repo "C:\\path\\to\\repo"
  python main.py --task "..." --mock          # dry run, no API calls
  python main.py --batch tasks.txt
  python main.py --invent --max-rules 5
  python main.py --status

Required env vars:
  OPENAI_API_KEY    (Planner + Coder)
  GEMINI_API_KEY    (Fixer + Supervisor)

Optional env vars:
  ANTHROPIC_API_KEY (set SUPERVISOR_BACKEND=anthropic to use Claude as Supervisor)
        """,
    )

    p.add_argument("--task",       type=str)
    p.add_argument("--batch",      type=str, help="File with one task per line")
    p.add_argument("--invent",     action="store_true")
    p.add_argument("--max-rules",  type=int, default=INVENT_MAX_RULES)
    p.add_argument("--resume",     action="store_true")
    p.add_argument("--dashboard",  action="store_true")
    p.add_argument("--repo",       type=str, default=".")
    p.add_argument("--python",     type=str, default="")
    p.add_argument("--timeout",    type=float, default=DEFAULT_TASK_TIMEOUT_SEC)
    p.add_argument("--mock",       action="store_true",
                   help="Use mock backends — no API calls (dry run / CI)")
    p.add_argument("--status",     action="store_true")
    p.add_argument("--verbose",    action="store_true")
    args = p.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    if args.status:
        sp = Path("logs/session_summary.json")
        print(sp.read_text() if sp.exists() else "No session summary found.")
        return 0

    if args.dashboard and not (args.task or args.batch or args.invent):
        from dashboard.server import run_server
        log.info("Dashboard at ws://%s:%d — open dashboard/index.html",
                 DASHBOARD_HOST, DASHBOARD_PORT)
        asyncio.run(run_server(DASHBOARD_HOST, DASHBOARD_PORT))
        return 0

    repo_path = Path(args.repo).resolve()
    if not repo_path.exists():
        log.error("Repo path not found: %s", repo_path); return 1

    # Auto-detect venv python for the target repo
    python_exe = args.python if args.python else _find_python_exe(repo_path)
    if python_exe != sys.executable:
        log.info("Using repo Python: %s", python_exe)

    if args.invent:
        return asyncio.run(run_invent(
            repo_path=repo_path, python_exe=python_exe,
            max_rules=args.max_rules, resume=args.resume,
            mock_all=args.mock, dashboard=args.dashboard,
        ))

    if args.batch:
        tf = Path(args.batch)
        if not tf.exists():
            log.error("Batch file not found: %s", args.batch); return 1
        return asyncio.run(run_batch(
            tasks_file=tf, repo_path=repo_path, python_exe=python_exe,
            timeout=args.timeout, mock_all=args.mock, dashboard=args.dashboard,
        ))

    if args.task:
        return asyncio.run(run_task(
            task=args.task, repo_path=repo_path, python_exe=python_exe,
            timeout=args.timeout, mock_all=args.mock, dashboard=args.dashboard,
        ))

    p.error("Provide --task, --batch, --invent, --dashboard, or --status")
    return 1


if __name__ == "__main__":
    sys.exit(main())
