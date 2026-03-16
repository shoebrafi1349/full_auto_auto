"""
project_knowledge.py — Review Automation Project Context
==========================================================
All domain knowledge about the target project lives here.
Injected into Planner and Coder system prompts so the LLM
writes code that actually fits the existing codebase.

Update this file when the project evolves (new routes, new models, etc.)
"""

# ── Stack summary ──────────────────────────────────────────────────────────────
STACK = """
TECH STACK:
  - FastAPI + Jinja2 templates (NO React, NO Vue, NO Bootstrap, NO Tailwind CDN)
  - Vanilla HTML/CSS/JS only
  - SQLAlchemy ORM with SQLite database
  - JWT cookie auth (app/auth.py) — cookie name: "access_token"
  - Templates: Jinja2, always extend "base.html"
  - Static files: /static/ — CSS in /static/*.css, no JS frameworks
"""

# ── File layout ────────────────────────────────────────────────────────────────
FILE_LAYOUT = """
PROJECT STRUCTURE:
  app/main.py               — FastAPI app, all routes
  app/auth.py               — JWT auth, get_current_user, require_admin
  app/models.py             — SQLAlchemy ORM: User, ReviewSession, AuditLog
  app/database.py           — engine, get_db, Base
  app/admin.py              — admin router
  app/services/review_service.py — runs rules engine
  app/schemas/review_exception.py — ReviewException(code, message, severity, row_number)
  app/schemas/review_results.py   — ReviewResults(dataframe, exceptions, rules_executed)
  app/engines/journal_engine.py   — main rules engine
  app/engines/prior_year_engine.py — prior-year rules engine
  app/rules/base_rule.py          — BaseRule base class
  app/rules/journal/controls/     — CTRL_ rules
  app/rules/journal/current_year/ — CY_ rules
  app/rules/journal/prior_ye/     — PY_ rules
  app/utils/dataframe_utils.py    — normalise_columns, derive_missing_columns, validate_columns
  templates/                — Jinja2 templates (extend base.html)
  static/                   — CSS files (configure.css, style.css, etc.)
"""

# ── CSS design system ──────────────────────────────────────────────────────────
CSS_CONVENTIONS = """
CSS DESIGN SYSTEM (from configure.css and style.css):
  Font: Inter (Google Fonts)
  CSS Variables:
    --bg: #f8faff (light) / #05070d (dark)
    --bg-secondary: #f1f5ff / #0a1628
    --card-bg: #ffffff / #0d1626
    --card-border: rgba(99,102,241,0.12) / rgba(99,102,241,0.15)
    --text-primary: #0f172a / #f0f4ff
    --text-secondary: #64748b / #8892a4
    --accent: #6366f1  (indigo — used everywhere)
    --accent-glow: rgba(99,102,241,0.25)
    --accent-subtle: rgba(99,102,241,0.08)
    --success: #22c55e
    --warning: #f59e0b
    --danger: #ef4444
    --border-glass: rgba(99,102,241,0.12)
    --glass-bg: rgba(255,255,255,0.8) / rgba(13,22,38,0.9)
  Dark mode: data-theme="dark" on <html>
  Card pattern: class="card" with glassmorphism (backdrop-filter: blur(20px))
  Section badges: <span class="section-badge">N</span> inside <h2>
  Step indicator: class="steps" > class="step [done|active]"
  Bottom bar: class="configure-bottom-bar"
  Buttons: class="run-btn" (primary), class="back-btn" (secondary)
  NO Bootstrap, NO Tailwind CDN, NO external CSS frameworks.
"""

# ── Route patterns ─────────────────────────────────────────────────────────────
ROUTE_PATTERNS = """
FASTAPI ROUTE PATTERNS:
  Auth dependency:  current_user: User = Depends(get_current_user)
  Admin dependency: current_user: User = Depends(require_admin)
  DB dependency:    db: Session = Depends(get_db)
  Template render:  return templates.TemplateResponse("page.html", {"request": request, ...})
  Always include:   "request": request, "user": current_user, "active_page": "page_name"
  Form data:        form_data = await request.form()   (NOT Form(...) parameters — they consume the body)
  Redirects:        return RedirectResponse("/path", status_code=302)

EXISTING ROUTES (do NOT duplicate):
  GET  /                    → redirect
  GET  /login-page          → login form
  POST /login               → auth
  POST /logout              → clear cookie
  GET  /process             → upload page (home)
  POST /configure           → file upload + mapping page
  GET  /columns             → JSON column detection
  GET  /prev-columns        → JSON prev-year column detection
  POST /run-review          → run rules engine
  GET  /download-excel      → Excel export
  GET  /download-rules-docx → Word export
  GET  /dashboard           → user dashboard
  GET  /results             → recent sessions
  GET  /exceptions/{id}     → view past review
  GET  /profile             → user profile
  POST /profile/change-password
  GET  /rules               → rule documentation
  GET  /cost-estimator      → cost estimator
  GET  /health              → health check
"""

# ── Template conventions ───────────────────────────────────────────────────────
TEMPLATE_CONVENTIONS = """
TEMPLATE CONVENTIONS:
  All templates extend "base.html":
    {% extends "base.html" %}
    {% block page_title %}My Page{% endblock %}
    {% block head_extra %}<link rel="stylesheet" href="/static/mypage.css">{% endblock %}
    {% block content %}...{% endblock %}
    {% block scripts %}<script>...</script>{% endblock %}

  Configure page (configure.html) structure — sections numbered 1-4:
    Section 1: Data Source (source-grid with source-card radio buttons)
    Section 2: Header Row (row-config with number input + preview table)
    Section 3: Column Mapping (mapping-table with col-select dropdowns)
    Section 4: Rule Mode (rule-mode-grid with mode-card radio buttons)
    Coverage Card: real-time stats bar
    Bottom Bar: back button + run button

  To ADD A NEW SECTION to configure.html:
    1. Add it between existing sections (e.g. after section 4, before coverage card)
    2. Use the same card pattern: <div class="card card-sN">
    3. Give it a section-badge number (5, 6, etc.)
    4. Add corresponding CSS to configure.css using same --accent variable
    5. The form action is POST /run-review — add hidden inputs or named form fields
    6. Add any new form fields to the run_review() route in app/main.py

  The form ID is "configForm" and submits to /run-review via POST.
  All inputs must have a name= attribute to be included in form submission.
"""

# ── Account mapping section context ───────────────────────────────────────────
ACCOUNT_MAPPING_CONTEXT = """
ACCOUNT MAPPING SECTION CONTEXT:
  The user wants a new Section 5 on configure.html where they can map
  account names from their ledger to canonical categories used by rules.

  Common account mappings needed by rules:
    - Payroll accounts:  wages, salary, salaries, payroll (used by PAYROLL_ rules)
    - Pension accounts:  pension, workplace pension (used by PAYROLL_PENSION_MISMATCH)
    - HMRC accounts:     hmrc, paye, national insurance (used by PAYROLL_MISSING_HMRC_PAYMENT)
    - VAT accounts:      vat control, vat liability (used by CTRL_VAT_ACCOUNT_MISUSE)
    - Suspense accounts: suspense (used by CTRL_SUSPENSE_ACCOUNT_AGING)
    - Bank accounts:     bank, current account (used by PAYROLL_STALE_BANK_REFERENCE)
    - Director loan:     director, director loan (used by CTRL_DIRECTOR_LOAN_POSTING)

  The mapping should:
    1. Show a table with category label on left, text input on right
    2. Allow multiple comma-separated account names per category
    3. Submit as hidden/named form fields to /run-review
    4. Have sensible defaults pre-filled (e.g. "wages, salary, salaries" for Payroll)
    5. Be collapsible / optional — not required to run a review
    6. Match the existing card/section styling in configure.css
"""

# ── Models summary ─────────────────────────────────────────────────────────────
MODELS_SUMMARY = """
SQLALCHEMY MODELS (app/models.py):
  User:
    id, username (unique), password_hash, role ('user'/'admin'/'manager'),
    is_active, created_at, last_login
    relationships: sessions, audit_log

  ReviewSession:
    id, user_id (FK→users), upload_session_id, review_session_id,
    source_system, rule_mode, has_previous_year,
    rows_analysed, flags_raised, rules_executed (pipe-sep string),
    results_json, column_warnings_json, created_at, completed_at

  AuditLog:
    id, user_id (FK→users), action, detail, ip_address, user_agent, created_at

  Database: SQLite via app/database.py → engine, get_db(), Base
"""

# ── Rules system summary ───────────────────────────────────────────────────────
RULES_SUMMARY = """
RULES SYSTEM:
  BaseRule (app/rules/base_rule.py):
    Subclasses must define:
      rule_code   = "CTRL_MY_RULE"
      description = "Human readable"
      risk_level  = "High" | "Medium" | "Low"
    Single-signature rules (controls/, current_year/):
      def run(self, df: pd.DataFrame) -> List[ReviewException]:
    Dual-signature rules (prior_ye/ only):
      def run(self, previous_df: pd.DataFrame, current_df: pd.DataFrame) -> List[ReviewException]:

  ReviewException fields:
    code, message, severity, row_number (required), recommendation (optional),
    risk_score (optional), field (optional), entry_type (optional), context (optional)

  DataFrame columns available:
    _row_id, Date, Source, Description, Journal ID, Account, Account Type,
    Account Code, Debit, Credit, Net, VAT, VAT Rate, Gross, Invoice Number,
    Reference, Running Balance, VAT Rate Name

  Always use:
    working = df.copy()
    required_cols = {"col1", "_row_id"}
    if not required_cols.issubset(df.columns): return []
    flagged_ids: set = set()
    # ... populate flagged_ids ...
    for row_id in flagged_ids:
        exceptions.append(ReviewException(
            code=self.rule_code,
            message=self.description,
            severity=self.risk_level,
            row_number=int(row_id),
        ))
"""

# ── Combined system prompt block for Planner ──────────────────────────────────
PLANNER_PROJECT_CONTEXT = f"""
PROJECT CONTEXT — Review Automation (FastAPI + Jinja2 + SQLAlchemy):
{STACK}
{FILE_LAYOUT}
{ROUTE_PATTERNS}
{TEMPLATE_CONVENTIONS}
{MODELS_SUMMARY}
"""

# ── Combined context block for Coder ──────────────────────────────────────────
CODER_PROJECT_CONTEXT = f"""
PROJECT CONTEXT — Review Automation (FastAPI + Jinja2 + SQLAlchemy):
{STACK}
{FILE_LAYOUT}
{CSS_CONVENTIONS}
{ROUTE_PATTERNS}
{TEMPLATE_CONVENTIONS}
{MODELS_SUMMARY}
{RULES_SUMMARY}
"""
