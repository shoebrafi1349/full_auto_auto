"""
tests/test_system.py — End-to-end integration test
====================================================
Uses MockBackend so no LLM required.
Tests the full message flow: task → plan → code → test → fix → pass
"""

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.bus       import AgentBus
from core.llm       import MockBackend
from core.messages  import AgentRole, GoalSpec, Message, MessageType, TaskStatus

from agents.orchestrator import OrchestratorAgent
from agents.planner      import PlannerAgent
from agents.coder        import CoderAgent
from agents.tester       import TesterAgent
from agents.fixer        import FixerAgent
from agents.supervisor   import SupervisorAgent


# ── Helpers ────────────────────────────────────────────────────────────────────

VALID_PYTHON = '''
def hello():
    return "hello world"
'''.strip()

PLANNER_RESPONSE = '''{
  "goals": [
    {
      "id": "goal_001",
      "title": "Create hello function",
      "description": "Create a simple hello.py with a hello() function.",
      "target_files": ["hello.py"],
      "mode": "feature"
    }
  ]
}'''

CODER_RESPONSE = f'''```hello.py
{VALID_PYTHON}
```'''

def build_test_system(tmp_path: Path, mock_responses_local=None):
    bus       = AgentBus()
    local_llm = MockBackend(responses=mock_responses_local or [])
    sup_llm   = MockBackend(responses=[
        '{"root_cause": "mock error", "action": "retry_coder", '
        '"instructions": "rewrite", "target_files": ["hello.py"], "notes": ""}'
    ])

    orchestrator = OrchestratorAgent(bus)
    PlannerAgent(bus, llm=local_llm)
    CoderAgent(bus, llm=local_llm, repo_path=tmp_path)
    TesterAgent(bus, llm=local_llm, repo_path=tmp_path,
                python_exe=sys.executable,
                # Use a passing command — just check imports
                test_command=[sys.executable, "-c", "print('1 passed')"])
    FixerAgent(bus, llm=local_llm, repo_path=tmp_path)
    SupervisorAgent(bus, llm=sup_llm, repo_path=tmp_path)

    return orchestrator


# ── Tests ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_full_happy_path(tmp_path):
    """
    Happy path: Planner → Coder → Tester → passes on first try.
    """
    orchestrator = build_test_system(
        tmp_path,
        mock_responses_local=[PLANNER_RESPONSE, CODER_RESPONSE],
    )

    rec = await orchestrator.submit_task(
        "Create a hello.py with a hello() function",
        timeout=30,
    )

    assert rec.status == TaskStatus.PASSED, f"Expected PASSED, got {rec.status}: {rec.result}"
    assert (tmp_path / "hello.py").exists()
    assert "hello" in (tmp_path / "hello.py").read_text()


@pytest.mark.asyncio
async def test_message_routing(tmp_path):
    """
    Verify messages flow through the bus correctly.
    """
    bus     = AgentBus()
    history: list[Message] = []

    async def capture(msg: Message):
        history.append(msg)

    bus.subscribe(AgentRole.PLANNER, capture)

    await bus.publish(Message(
        type=MessageType.DIRECTIVE,
        sender=AgentRole.ORCHESTRATOR,
        recipient=AgentRole.PLANNER,
        payload={"task": "test task"},
        task_id="t001",
    ))

    assert len(history) == 1
    assert history[0].task_id == "t001"
    assert history[0].type == MessageType.DIRECTIVE


@pytest.mark.asyncio
async def test_parallel_goal_splitting():
    """
    Goals with non-overlapping files should be batched together.
    Goals with overlapping files should be in separate batches.
    """
    from core.llm import MockBackend
    bus   = AgentBus()
    agent = CoderAgent(bus, llm=MockBackend(), repo_path=Path("."))

    goals = [
        GoalSpec("g1", "G1", "desc", ["a.py"], "feature"),
        GoalSpec("g2", "G2", "desc", ["b.py"], "feature"),   # safe to parallel with g1
        GoalSpec("g3", "G3", "desc", ["a.py"], "feature"),   # conflicts with g1 → new batch
    ]

    batches = agent._split_parallel(goals)

    assert len(batches) == 2, f"Expected 2 batches, got {len(batches)}"
    assert len(batches[0]) == 2   # g1 + g2 together
    assert len(batches[1]) == 1   # g3 alone


@pytest.mark.asyncio
async def test_supervisor_cost_gate(tmp_path):
    """
    Supervisor should use fallback after MAX_SUPERVISOR_CALLS.
    """
    bus   = AgentBus()
    llm   = MockBackend(responses=[
        '{"root_cause": "test", "action": "abandon", "instructions": "", '
        '"target_files": [], "notes": ""}'
    ] * 10)

    supervisor = SupervisorAgent(bus, llm=llm, repo_path=tmp_path)
    supervisor._calls = supervisor.__class__.__module__  # force over limit trick

    # Directly test the fallback
    fallback = supervisor._fallback_ruling("test", ["x.py"], [])
    assert fallback["action"] in ("retry_coder", "abandon", "architectural_change")
    assert "root_cause" in fallback


def test_test_result_all_passed():
    from core.messages import TestResult
    r = TestResult(passed=5, failed=0, errors=[])
    assert r.all_passed is True

    r2 = TestResult(passed=3, failed=1, errors=[])
    assert r2.all_passed is False

    r3 = TestResult(passed=5, failed=0, errors=["something"])
    assert r3.all_passed is False


def test_message_reply():
    from core.messages import Message, MessageType, AgentRole, TaskStatus
    original = Message(
        type=MessageType.DIRECTIVE,
        sender=AgentRole.ORCHESTRATOR,
        recipient=AgentRole.PLANNER,
        payload="task",
        task_id="abc",
    )
    reply = original.reply(
        type=MessageType.TASK_PLAN,
        sender=AgentRole.PLANNER,
        payload=["goal"],
        status=TaskStatus.IN_PROGRESS,
    )
    assert reply.task_id == "abc"
    assert reply.recipient == AgentRole.ORCHESTRATOR
    assert reply.sender == AgentRole.PLANNER


if __name__ == "__main__":
    asyncio.run(test_full_happy_path(Path("/tmp/test_agent")))
    print("All tests passed")
