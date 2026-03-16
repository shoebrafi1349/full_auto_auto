# Multi-Agent Autonomous Coding System

A self-healing, cost-controlled multi-agent system that plans, implements, tests, and fixes code automatically.

---

## Architecture

```
External Task
      │
      ▼
┌─────────────────────────────────────────────────────────────────┐
│                         AgentBus (async pub/sub)                │
│                                                                 │
│  ┌──────────────┐    DIRECTIVE    ┌──────────────┐              │
│  │ Orchestrator │ ─────────────► │   Planner    │  (local LLM) │
│  │  (no LLM)   │                 └──────┬───────┘              │
│  └──────┬───────┘                       │ TASK_PLAN            │
│         │ STATUS                        ▼                       │
│         │              ┌──────────────────────────┐             │
│         │              │       Coder Agent        │  (local LLM)│
│         │              │  • parallel safe batches │             │
│         │              │  • retry on failure      │             │
│         │              └───────────┬──────────────┘             │
│         │                         │ IMPLEMENTATION              │
│         │                         ▼                             │
│         │              ┌──────────────────────────┐             │
│         │              │      Tester Agent        │  (runner)   │
│         │              │  • syntax check          │             │
│         │              │  • pytest / import check │             │
│         │              └───────┬──────────┬───────┘             │
│         │         TEST_RESULTS │          │ STATUS(passed)      │
│         │                      ▼          │                     │
│         │              ┌───────────────┐  │                     │
│         │              │  Fixer Agent  │  │  (local LLM)        │
│         │              │  ┌──────────┐ │  │                     │
│         │              │  │auto-fix  │ │  │  Self-healing loop  │
│         │              │  │ (no LLM) │ │  │  up to 4 attempts  │
│         │              │  └──────────┘ │  │                     │
│         │              │  LLM fix      │  │                     │
│         │              └───────┬───────┘  │                     │
│         │         PATCH_READY  │          │                     │
│         │              (back to Tester)   │                     │
│         │                                 │                     │
│         ◄─────────────────────────────────┘                     │
│         │                                                       │
│         │  (if 3+ failures)  ESCALATION                        │
│         ├──────────────────────────────►  ┌───────────────────┐ │
│         │                                 │ Supervisor Agent  │ │
│         │                                 │  (Claude / Opus)  │ │
│         │                                 │  max 5 calls/sess │ │
│         │                                 └────────┬──────────┘ │
│         │         SUPERVISOR_RULING                │            │
│         │         ◄─────────────────────────────── │            │
│         │         → Planner or Coder               │            │
└─────────────────────────────────────────────────────────────────┘
```

### Message flow legend

| Message type       | From        | To          | Meaning                              |
|--------------------|-------------|-------------|--------------------------------------|
| `DIRECTIVE`        | Orchestrator| Planner     | "Here is a raw task, plan it"        |
| `TASK_PLAN`        | Planner     | Coder       | List of `GoalSpec` objects           |
| `IMPLEMENTATION`   | Coder       | Tester      | Files written, ready to test         |
| `TEST_RESULTS`     | Tester      | Fixer       | Failures with error details          |
| `PATCH_READY`      | Fixer       | Tester      | Files patched, re-test please        |
| `ESCALATION`       | Fixer/Orch  | Supervisor  | Stuck — need Claude's help           |
| `SUPERVISOR_RULING`| Supervisor  | Coder/Planner| Revised instructions                |
| `STATUS`           | Any         | Orchestrator| Final outcome (passed/failed/aborted)|

---

## Cost model

| Agent       | Backend         | Cost   | When used                         |
|-------------|-----------------|--------|-----------------------------------|
| Planner     | Local (Ollama)  | Free   | Every task                        |
| Coder       | Local (Ollama)  | Free   | Every task, parallel where safe   |
| Tester      | subprocess      | Free   | Every implementation + every fix  |
| Fixer       | Local (Ollama)  | Free   | On test failure, up to 4×         |
| Supervisor  | **Claude/Opus** | Paid   | Only after 3+ consecutive failures, max 5×/session |

---

## Self-healing loop

```
Coder writes files
        ↓
Tester runs tests
        ↓ fail
Fixer: auto-fix (pure Python, instant)
        ↓ if not resolved
Fixer: LLM fix (local model)
        ↓
Tester re-runs
        ↓ fail again
... repeat up to 4 attempts
        ↓ still failing
Supervisor (Claude) reviews & produces revised instructions
        ↓
Coder retries with supervisor guidance
```

---

## Installation

```bash
pip install ollama anthropic
# Pull your local model
ollama pull qwen2.5-coder:7b
# Set Claude API key (optional — only used when agents get stuck)
export ANTHROPIC_API_KEY=sk-ant-...
```

---

## Usage

```bash
# Run a single task
python main.py --task "add rate limiting to the login endpoint"

# Run against a specific repo
python main.py --task "..." --repo /path/to/my/project

# Dry run — mock LLM, no API calls
python main.py --task "..." --backend mock

# Batch mode — one task per line in a file
python main.py --batch tasks.txt

# Check last session results
python main.py --status

# Verbose logging
python main.py --task "..." --verbose
```

---

## Configuration

All tunable constants live at the top of each agent file:

| File                    | Key constant              | Default        |
|-------------------------|---------------------------|----------------|
| `agents/fixer.py`       | `MAX_FIX_ATTEMPTS`        | 4              |
| `agents/supervisor.py`  | `MAX_SUPERVISOR_CALLS`    | 5              |
| `agents/orchestrator.py`| `SUPERVISOR_FAILURE_THRESHOLD` | 3         |
| `agents/coder.py`       | `MAX_RETRIES`             | 3              |
| `main.py`               | `--timeout`               | 3600s          |

---

## Adding a new agent

1. Create `agents/my_agent.py`, subclass `BaseAgent`, set `role = AgentRole.MY_ROLE`
2. Implement `async def handle(self, message: Message) -> None`
3. Add `MY_ROLE` to `AgentRole` enum in `core/messages.py`
4. Instantiate in `main.py → build_system()`

---

## Adapting for your project (review automation)

Replace the Tester's `test_command` with your behavioral sandbox:

```python
TesterAgent(
    bus, llm=local_llm, repo_path=repo_path,
    python_exe=python_exe,
    test_command=[python_exe, "_agent/sandbox_runner.py"],
)
```

The Coder already reuses file-writing patterns from `agent.py`.
The Supervisor's `SUPERVISOR_SYSTEM` prompt can be enriched with your `RULE_KNOWLEDGE` block.
