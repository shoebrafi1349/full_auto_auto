"""
agents/inventor.py — Autonomous Invention Loop
================================================
Ports the --invent mode from agent.py into the multi-agent system.

Runs continuously until:
  - Session ceiling hit
  - max_rules reached
  - All candidates exhausted AND free-invention fails twice

Each cycle:
  1. Picks a candidate rule not yet implemented
  2. Submits it as a task to the Orchestrator (which fans it through all agents)
  3. Records the new rule code on success
  4. Sleeps, then repeats

The Orchestrator/Coder/Tester/Fixer/Supervisor handle all the
implementation details — the Inventor only selects what to build next.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

log = logging.getLogger("inventor")

# ── Candidate rules to build (same as agent.py INVENTION_CANDIDATES) ──────────
# Each entry: (RULE_CODE, detailed_description_for_planner)
INVENTION_CANDIDATES: list[tuple[str, str]] = [
    (
        "CTRL_SEQUENTIAL_INVOICE_NUMBERS",
        "Create a rule file app/rules/journal/controls/rule_sequential_invoice_numbers.py "
        "then wire it into app/engines/journal_engine.py. "
        "Rule: flag Payable Invoice rows where the same vendor posts 3+ invoices with "
        "consecutive numeric suffixes (regex r'(\\d+)$') within any 30-day window. "
        "Class: SequentialInvoiceNumbersRule. rule_code='CTRL_SEQUENTIAL_INVOICE_NUMBERS'. "
        "risk_level='High'. Single-signature run(df). Required cols: Source, Description, "
        "Invoice Number, Date, Journal ID, _row_id. Filter: Source=='Payable Invoice'.",
    ),
    (
        "CTRL_DORMANT_ACCOUNT_POSTING",
        "Create app/rules/journal/controls/rule_dormant_account_posting.py then wire it. "
        "Rule: flag rows where Account had zero activity in the 90 days before row Date, "
        "but has at least 1 prior row anywhere in the dataset. "
        "Class: DormantAccountPostingRule. rule_code='CTRL_DORMANT_ACCOUNT_POSTING'. "
        "risk_level='Medium'. Single-signature. Required cols: Date, Account, _row_id.",
    ),
    (
        "CTRL_VAT_ON_EXEMPT_ACCOUNT",
        "Create app/rules/journal/controls/rule_vat_on_exempt_account.py then wire it. "
        "Rule: flag rows where Account Type=='Sales' AND abs(VAT)>0.01 AND Account "
        "(lowercased) contains any of: exempt, outside scope, zero rated, no vat, non-vat. "
        "Class: VatOnExemptAccountRule. rule_code='CTRL_VAT_ON_EXEMPT_ACCOUNT'. "
        "risk_level='High'. Single-signature. Required cols: Account Type, Account, VAT, _row_id.",
    ),
    (
        "CTRL_INTERCOMPANY_IMBALANCE",
        "Create app/rules/journal/controls/rule_intercompany_imbalance.py then wire it. "
        "Rule: flag all rows in journals where Description contains intercompany/recharge "
        "AND abs(sum(Debit) - sum(Credit)) > 0.01 per journal. "
        "Class: IntercompanyImbalanceRule. rule_code='CTRL_INTERCOMPANY_IMBALANCE'. "
        "risk_level='High'. Single-signature. Required cols: Journal ID, Description, "
        "Debit, Credit, _row_id.",
    ),
    (
        "CY_SALES_ACCOUNT_DEBIT_POSTING",
        "Create app/rules/journal/current_year/rule_sales_account_debit_posting.py then wire it. "
        "Rule: flag rows where Account Type=='Sales' AND Debit>0.01 AND Source NOT IN "
        "['Sales Credit Note', 'Manual Journal']. "
        "Class: SalesAccountDebitPostingRule. rule_code='CY_SALES_ACCOUNT_DEBIT_POSTING'. "
        "risk_level='High'. Single-signature. Required cols: Account Type, Debit, Source, _row_id.",
    ),
    (
        "CY_VAT_RECLAIM_ON_ENTERTAINMENT",
        "Create app/rules/journal/current_year/rule_vat_reclaim_on_entertainment.py then wire it. "
        "Rule: flag rows where Account Type in [Overhead, Expense, Direct Costs] AND abs(VAT)>0.01 "
        "AND Account (lowercased) contains any of: entertainment, hospitality, client lunch, "
        "staff lunch, subsistence, team event, christmas, staff party. "
        "Class: VatReclaimOnEntertainmentRule. rule_code='CY_VAT_RECLAIM_ON_ENTERTAINMENT'. "
        "risk_level='High'. Single-signature. Required cols: Account Type, Account, VAT, _row_id.",
    ),
    (
        "CTRL_SINGLE_APPROVER_HIGH_VALUE",
        "Create app/rules/journal/controls/rule_single_approver_high_value.py then wire it. "
        "Rule: flag Spend Money journals with exactly 2 lines where non-Bank line abs(Gross)>5000. "
        "Class: SingleApproverHighValueRule. rule_code='CTRL_SINGLE_APPROVER_HIGH_VALUE'. "
        "risk_level='High'. Single-signature. Required cols: Source, Journal ID, Account Type, "
        "Gross, _row_id. Filter: Source=='Spend Money'. Group by Journal ID.",
    ),
    (
        "CTRL_NEGATIVE_ASSET_BALANCE",
        "Create app/rules/journal/controls/rule_negative_asset_balance.py then wire it. "
        "Rule: flag rows where Account Type in [Fixed Asset, Current Asset] AND "
        "(Credit - Debit) > 0.01 AND Source NOT IN [Payable Credit Note, Sales Credit Note]. "
        "Class: NegativeAssetBalanceRule. rule_code='CTRL_NEGATIVE_ASSET_BALANCE'. "
        "risk_level='Medium'. Single-signature. Required cols: Account Type, Credit, Debit, "
        "Source, _row_id.",
    ),
    (
        "PY_EXPENSE_ACCOUNT_GROSS_SPIKE",
        "Create app/rules/journal/prior_ye/rule_expense_account_gross_spike.py then wire into "
        "app/engines/prior_year_engine.py. "
        "Rule: for each Account where Account Type in [Overhead, Expense, Direct Costs], "
        "flag all current-year rows where current sum(abs(Gross)) > 3x prior-year sum AND "
        "prior sum >= 500. "
        "Class: ExpenseAccountGrossSpikeRule. rule_code='PY_EXPENSE_ACCOUNT_GROSS_SPIKE'. "
        "risk_level='High'. DUAL-signature run(previous_df, current_df). "
        "Required cols: Account, Account Type, Gross, _row_id (current); "
        "Account, Account Type, Gross (previous).",
    ),
    (
        "PY_SALES_ACCOUNT_GROSS_DECLINE",
        "Create app/rules/journal/prior_ye/rule_sales_account_gross_decline.py then wire into "
        "app/engines/prior_year_engine.py. "
        "Rule: for each Account where Account Type=='Sales', flag all current-year rows where "
        "current sum(abs(Gross)) < 50% of prior-year sum AND prior sum >= 1000. "
        "Class: SalesAccountGrossDeclineRule. rule_code='PY_SALES_ACCOUNT_GROSS_DECLINE'. "
        "risk_level='High'. DUAL-signature run(previous_df, current_df). "
        "Required cols: Account, Account Type, Gross, _row_id (current); "
        "Account, Account Type, Gross (previous).",
    ),
]


def _scan_implemented_codes(repo_path: Path, state_path: Path) -> set[str]:
    """
    Combine:
      - rule_code values parsed from all .py files in app/rules/
      - rule_codes recorded in _agent/state.json
    """
    import ast
    codes: set[str] = set()

    rules_dir = repo_path / "app" / "rules"
    if rules_dir.exists():
        for py_file in rules_dir.rglob("*.py"):
            if "__pycache__" in str(py_file):
                continue
            try:
                tree = ast.parse(py_file.read_text(encoding="utf-8"))
                for node in ast.walk(tree):
                    if not isinstance(node, ast.ClassDef):
                        continue
                    for item in node.body:
                        if isinstance(item, ast.Assign):
                            for target in item.targets:
                                if (isinstance(target, ast.Name)
                                        and target.id == "rule_code"
                                        and isinstance(item.value, ast.Constant)):
                                    codes.add(item.value.value)
            except Exception:
                pass

    if state_path.exists():
        try:
            data = json.loads(state_path.read_text(encoding="utf-8"))
            codes.update(data.get("rule_codes", []))
            codes.update(data.get("invented_rules", []))
        except Exception:
            pass

    return codes


def _build_task(candidate: tuple[str, str], known_codes: set[str]) -> str:
    rule_code, description = candidate
    known_list = ", ".join(sorted(known_codes)) if known_codes else "none"
    return (
        f"Implement this accounting exception detection rule.\n\n"
        f"Rule code: {rule_code}\n"
        f"Full instructions:\n{description}\n\n"
        f"IMPORTANT: Do NOT reimplement any of these already-existing rules: {known_list}.\n"
        f"Produce EXACTLY TWO goals:\n"
        f"  Goal 1: create the rule file.\n"
        f"  Goal 2: wire it into the correct engine file."
    )


def _free_invention_task(known_codes: set[str]) -> str:
    known_list = ", ".join(sorted(known_codes)) if known_codes else "none"
    return (
        f"Invent a completely new accounting exception detection rule "
        f"not already in the system. Do NOT reimplement any of: {known_list}. "
        f"Choose a pattern that catches real financial fraud or misposting risk. "
        f"Place the rule file in app/rules/journal/controls/ with a CTRL_ prefix. "
        f"Produce EXACTLY TWO goals: Goal 1 = rule file, Goal 2 = wire into "
        f"app/engines/journal_engine.py."
    )


async def run_invention_loop(
    orchestrator,                  # OrchestratorAgent instance
    repo_path:    Path,
    state_path:   Path,
    max_rules:    int   = 20,
    sleep_sec:    float = 10,
    resume:       bool  = False,
    task_timeout: float = 1800,    # 30 min per rule
):
    """
    Autonomous rule invention loop.

    Args:
        orchestrator:  The running OrchestratorAgent to submit tasks to.
        repo_path:     Root of the target repo.
        state_path:    Path to _agent/state.json for persistence.
        max_rules:     Stop after this many successful rules.
        sleep_sec:     Pause between cycles.
        resume:        Skip candidates already on disk (--resume flag).
        task_timeout:  Max seconds per rule task.
    """
    log.info("=" * 60)
    log.info("  AUTONOMOUS INVENTION LOOP")
    log.info("  max_rules=%d  resume=%s  sleep=%ss", max_rules, resume, sleep_sec)
    log.info("=" * 60)

    known_codes   = _scan_implemented_codes(repo_path, state_path)
    rules_built   = 0
    candidate_idx = 0
    free_fails    = 0
    session_start = time.monotonic()

    # Load invented list for persistence
    invented: list[str] = []
    if state_path.exists():
        try:
            data = json.loads(state_path.read_text(encoding="utf-8"))
            invented = data.get("invented_rules", [])
        except Exception:
            pass

    def _save_progress():
        state_path.parent.mkdir(parents=True, exist_ok=True)
        existing = {}
        if state_path.exists():
            try:
                existing = json.loads(state_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        existing["invented_rules"] = invented
        existing["rule_codes"]     = list(known_codes)
        with open(state_path, "w") as f:
            json.dump(existing, f, indent=2)

    while True:
        # ── Ceiling / cap checks ───────────────────────────────────────
        elapsed = time.monotonic() - session_start
        if orchestrator._over_ceiling():
            log.info("[INVENTION] Session ceiling reached. Stopping.")
            break
        if rules_built >= max_rules:
            log.info("[INVENTION] Reached max_rules=%d. Stopping.", max_rules)
            break
        if free_fails >= 2:
            log.info("[INVENTION] Free invention failed twice. Stopping.")
            break

        # ── Pick next candidate ────────────────────────────────────────
        candidate = None
        tried = 0
        idx   = candidate_idx % len(INVENTION_CANDIDATES)
        while tried < len(INVENTION_CANDIDATES):
            code, desc = INVENTION_CANDIDATES[idx]
            if code not in known_codes:
                candidate = (code, desc)
                break
            idx   = (idx + 1) % len(INVENTION_CANDIDATES)
            tried += 1
        candidate_idx += 1

        # ── Build task string ──────────────────────────────────────────
        if candidate:
            task = _build_task(candidate, known_codes)
            target_code = candidate[0]
        else:
            log.info("[INVENTION] All %d candidates done — free invention mode",
                     len(INVENTION_CANDIDATES))
            task = _free_invention_task(known_codes)
            target_code = None

        # ── Progress banner ────────────────────────────────────────────
        log.info("─" * 60)
        log.info("  CYCLE %d/%d  |  %s",
                 rules_built + 1, max_rules, target_code or "FREE INVENTION")
        log.info("  Known codes: %d  |  Elapsed: %.0fm", len(known_codes), elapsed / 60)
        log.info("─" * 60)

        # ── Submit to orchestrator ─────────────────────────────────────
        rec = await orchestrator.submit_task(task, timeout=task_timeout)

        if rec.status.value == "passed":
            rules_built += 1
            free_fails   = 0

            # Detect what was built from the result
            result_code = (
                rec.result.get("rule_code") if isinstance(rec.result, dict) else None
            ) or target_code

            if result_code:
                known_codes.add(result_code)
                invented.append(result_code)
                _save_progress()
                log.info("[INVENTION ✓] Rule #%d: %s", rules_built, result_code)
            else:
                log.info("[INVENTION ✓] Cycle %d passed (code unknown)", rules_built)

        else:
            log.warning("[INVENTION] Cycle failed — status=%s", rec.status.value)
            if target_code is None:
                free_fails += 1

        await asyncio.sleep(sleep_sec)

    # ── Final report ──────────────────────────────────────────────────────
    log.info("=" * 60)
    log.info("  INVENTION SESSION COMPLETE — %d rule(s) built", rules_built)
    for code in invented:
        log.info("    ✓ %s", code)
    log.info("=" * 60)
    _save_progress()
