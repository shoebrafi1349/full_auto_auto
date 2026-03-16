# Setup Guide

## Folder structure

Create this as a **completely separate project folder** — it is independent from your review automation app.

```
my_coding_agent/           ← new folder (anywhere on your machine)
├── main.py
├── config.py
├── project_knowledge.py   ← knows your review automation project
├── requirements.txt
├── pytest.ini
├── .gitignore
├── README.md
├── SETUP.md
├── core/
│   ├── __init__.py
│   ├── messages.py
│   ├── bus.py
│   ├── base_agent.py
│   ├── llm.py
│   ├── events.py
│   └── git.py
├── agents/
│   ├── __init__.py
│   ├── orchestrator.py
│   ├── planner.py
│   ├── coder.py
│   ├── tester.py
│   ├── fixer.py
│   ├── supervisor.py
│   └── inventor.py
├── dashboard/
│   ├── server.py
│   └── index.html
└── tests/
    └── test_system.py
```

---

## Step 1 — Create a virtual environment (Windows)

Open a terminal in your `my_coding_agent/` folder:

```cmd
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

---

## Step 2 — Install and start Ollama

Download from https://ollama.com and run:

```cmd
ollama pull qwen2.5-coder:7b
```

Verify it works:
```cmd
ollama run qwen2.5-coder:7b "say hello"
```

---

## Step 3 — Set your Anthropic API key (optional)

Only needed when the Supervisor needs to use Claude (after 3+ consecutive failures).
Without it, the Supervisor uses a deterministic fallback — still works fine.

```cmd
set ANTHROPIC_API_KEY=sk-ant-...
```

To make it permanent, add it to your Windows environment variables.

---

## Step 4 — Smoke test

Run the system's own tests to confirm everything is wired correctly:

```cmd
pytest
```

Expected output: `6 passed`

---

## Step 5 — Run your first task

```cmd
python main.py ^
  --task "add a new section to the configure page where i can map accounts such as payroll pension etc" ^
  --repo "C:\Users\Shoeb.rafi\Downloads\Software\review_automation_poc - Copy" ^
  --backend ollama
```

`^` is the Windows line-continuation character. You can also write it on one line:

```cmd
python main.py --task "add a new section to the configure page where i can map accounts such as payroll pension etc" --repo "C:\Users\Shoeb.rafi\Downloads\Software\review_automation_poc - Copy" --backend ollama
```

---

## Step 6 — Watch it live (optional)

Add `--dashboard` to any command to stream agent events to your browser:

```cmd
python main.py --task "..." --repo "C:\..." --backend ollama --dashboard
```

Then open `dashboard\index.html` in your browser. You'll see all 5 agents
working in real time, fix attempts, and whether Claude was called.

---

## Common commands

```cmd
# Check last session results
python main.py --status

# Dry run — no LLM calls, just tests the wiring
python main.py --task "..." --repo "C:\..." --backend mock

# Autonomous invention loop (overnight)
python main.py --invent --max-rules 5 --repo "C:\..." --backend ollama

# Resume invention after a restart
python main.py --invent --resume --repo "C:\..." --backend ollama
```

---

## What the agent writes to your review automation project

When you run the account mapping task, the agent will:

1. **Add Section 5** to `templates/configure.html` — a card with a table
   mapping category labels (Payroll, Pension, HMRC, VAT Control, etc.)
   to text inputs where you type your account names
2. **Update `static/configure.css`** — styles for the new section
3. **Update `app/main.py`** — reads the new form fields in `run_review()`

All changes are backed up to `_agent/backups/` before writing, and
committed to git automatically if your project is a git repo.

---

## Rollback

If a change breaks something, every file is backed up before modification:

```
review_automation_poc/_agent/backups/
  templates/configure__20250316_143022.html   ← previous version
  app/main__20250316_143045.py
```

Just copy the backup file back over the modified file.

---

## Environment variables (all optional)

| Variable              | Default                | Effect                              |
|-----------------------|------------------------|-------------------------------------|
| `LOCAL_MODEL`         | `qwen2.5-coder:7b`    | Ollama model for all local agents   |
| `SUPERVISOR_MODEL`    | `claude-opus-4-5`      | Claude model for Supervisor         |
| `ANTHROPIC_API_KEY`   | (none)                 | Required to use Claude Supervisor   |
| `GIT_ENABLED`         | `true`                 | Auto-commit after each file write   |
| `GIT_AUTO_PUSH`       | `false`                | Push to origin after commit         |
| `DASHBOARD_PORT`      | `8765`                 | WebSocket port for live dashboard   |
| `MAX_FIX_ATTEMPTS`    | `4`                    | Fix loop retries before escalating  |
| `MAX_SUPERVISOR_CALLS`| `5`                    | Max Claude API calls per session    |

Set them in your terminal before running:
```cmd
set GIT_ENABLED=false
set LOCAL_MODEL=codellama:13b
python main.py --task "..." --repo "C:\..."
```
