"""
config.py — Central configuration
===================================
All tunable constants live here. Agents import from config, never hardcode.
Override via environment variables where noted.

Per-agent backends (current assignment):
  Planner    → OpenAI GPT-4o
  Coder      → OpenAI o4-mini (Codex / Responses API)
  Tester     → Ollama (local, free)
  Fixer      → Gemini 2.0 Flash
  Supervisor → Gemini 2.0 Flash  (switch to anthropic later)

To switch Supervisor to Claude:
  set SUPERVISOR_BACKEND=anthropic
  set SUPERVISOR_MODEL=claude-opus-4-5
  set ANTHROPIC_API_KEY=sk-ant-...
"""

from __future__ import annotations

import os
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────────────
ROOT_DIR    = Path(__file__).resolve().parent
LOGS_DIR    = ROOT_DIR / "logs"
BACKUP_DIR  = ROOT_DIR / "_agent" / "backups"
STATE_PATH  = ROOT_DIR / "_agent" / "state.json"
EVENTS_PATH = LOGS_DIR / "events.jsonl"

# ── Per-agent backends and models ──────────────────────────────────────────────
# ALL AGENTS: Gemini 2.0 Flash (no Ollama, no local model needed)
# Requires: GEMINI_API_KEY environment variable

# Planner
PLANNER_BACKEND = os.environ.get("PLANNER_BACKEND", "gemini")
PLANNER_MODEL   = os.environ.get("PLANNER_MODEL",   "gemini-2.0-flash")

# Coder
CODER_BACKEND   = os.environ.get("CODER_BACKEND",   "gemini")
CODER_MODEL     = os.environ.get("CODER_MODEL",     "gemini-2.0-flash")

# Tester (syntax check only — model barely used but still needs a backend)
TESTER_BACKEND  = os.environ.get("TESTER_BACKEND",  "gemini")
TESTER_MODEL    = os.environ.get("TESTER_MODEL",    "gemini-2.0-flash")

# Fixer
FIXER_BACKEND   = os.environ.get("FIXER_BACKEND",   "gemini")
FIXER_MODEL     = os.environ.get("FIXER_MODEL",     "gemini-2.0-flash")

# Supervisor (switch to anthropic when you have a paid Claude key)
SUPERVISOR_BACKEND = os.environ.get("SUPERVISOR_BACKEND", "gemini")
SUPERVISOR_MODEL   = os.environ.get("SUPERVISOR_MODEL",   "gemini-2.0-flash")

# ── Legacy aliases (kept for backward compat) ──────────────────────────────────
DEFAULT_LOCAL_MODEL      = TESTER_MODEL
DEFAULT_SUPERVISOR_MODEL = SUPERVISOR_MODEL
DEFAULT_BACKEND          = TESTER_BACKEND

# ── LLM call tuning ────────────────────────────────────────────────────────────
CALL_TIMEOUT_SEC  = int(os.environ.get("CALL_TIMEOUT",   "120"))   # cloud APIs are fast
MAX_TOKENS        = int(os.environ.get("MAX_TOKENS",     "4096"))
MAX_TOKENS_RETRY  = int(os.environ.get("MAX_TOKENS_EXT", "8192"))
CODER_TEMP        = float(os.environ.get("CODER_TEMP",   "1.0"))   # o-series requires 1.0
PLANNER_TEMP      = float(os.environ.get("PLANNER_TEMP", "0.2"))
FIXER_TEMP        = float(os.environ.get("FIXER_TEMP",   "0.15"))
SLEEP_BETWEEN_SEC = float(os.environ.get("SLEEP_SEC",    "2"))     # less sleep needed for cloud

# ── Retry / convergence ────────────────────────────────────────────────────────
MAX_CODER_RETRIES        = int(os.environ.get("MAX_CODER_RETRIES",       "3"))
MAX_FIX_ATTEMPTS         = int(os.environ.get("MAX_FIX_ATTEMPTS",        "4"))
MAX_SUPERVISOR_CALLS     = int(os.environ.get("MAX_SUPERVISOR_CALLS",    "5"))
SUPERVISOR_FAIL_THRESHOLD = int(os.environ.get("SUPERVISOR_FAIL_THRESHOLD", "3"))

# ── Session ────────────────────────────────────────────────────────────────────
SESSION_CEILING_SEC      = int(os.environ.get("SESSION_CEILING_H",  "8")) * 3600
DEFAULT_TASK_TIMEOUT_SEC = int(os.environ.get("TASK_TIMEOUT",        "1800"))

# ── Invention loop ─────────────────────────────────────────────────────────────
INVENT_MAX_RULES         = int(os.environ.get("INVENT_MAX_RULES", "20"))
INVENT_SLEEP_BETWEEN_SEC = int(os.environ.get("INVENT_SLEEP",     "5"))
INVENT_PROGRESS_EVERY    = int(os.environ.get("INVENT_PROGRESS",  "3"))

# ── Risk / change control ──────────────────────────────────────────────────────
HIGH_RISK_FILES = {
    "app/main.py",
    "app/auth.py",
    "app/engines/journal_engine.py",
    "app/engines/prior_year_engine.py",
    "app/engines/engine.py",
}
CHANGE_THRESHOLD = {"high": 0.30, "medium": 0.75, "low": 1.00}

# ── Context window ─────────────────────────────────────────────────────────────
MAX_FILE_CHARS = int(os.environ.get("MAX_FILE_CHARS", "6000"))   # cloud models handle more

# ── Git ────────────────────────────────────────────────────────────────────────
GIT_ENABLED       = os.environ.get("GIT_ENABLED",      "true").lower()  == "true"
GIT_AUTO_PUSH     = os.environ.get("GIT_AUTO_PUSH",    "false").lower() == "true"
GIT_COMMIT_PREFIX = os.environ.get("GIT_COMMIT_PREFIX", "agent")

# ── Dashboard ──────────────────────────────────────────────────────────────────
DASHBOARD_HOST = os.environ.get("DASHBOARD_HOST", "127.0.0.1")
DASHBOARD_PORT = int(os.environ.get("DASHBOARD_PORT", "8765"))


def summary() -> dict:
    return {
        "planner":    f"{PLANNER_BACKEND}/{PLANNER_MODEL}",
        "coder":      f"{CODER_BACKEND}/{CODER_MODEL}",
        "tester":     f"{TESTER_BACKEND}/{TESTER_MODEL}",
        "fixer":      f"{FIXER_BACKEND}/{FIXER_MODEL}",
        "supervisor": f"{SUPERVISOR_BACKEND}/{SUPERVISOR_MODEL}",
        "git_enabled":    GIT_ENABLED,
        "git_auto_push":  GIT_AUTO_PUSH,
        "dashboard_port": DASHBOARD_PORT,
        "max_fix_attempts":      MAX_FIX_ATTEMPTS,
        "max_supervisor_calls":  MAX_SUPERVISOR_CALLS,
    }