# Budget Coach Agent

Local-first household budget assistant + a sandbox for learning the GCP Gemini Enterprise Agent Platform (ADK, Agent Runtime, etc.).

## Architecture in one paragraph

A Streamlit UI talks to a Postgres database and an ADK agent. The agent uses **Gemini 2.5 Flash** for language and **deterministic Python tools** for every number — the LLM never adds, sums, or compares amounts. Multi-tenant from day one (`household_id` on every row), so the same code can serve one couple or many.

```
Streamlit UI ──► ADK agent (Gemini 2.5 Flash) ──► function tools (pure Python)
       │                                                  │
       └──────────────► Postgres (SQLAlchemy) ◄───────────┘
```

## One-time setup

```bash
# Install Postgres
brew install postgresql@16
brew services start postgresql@16

# Create app DB
/opt/homebrew/opt/postgresql@16/bin/psql -d postgres -c "CREATE USER budget WITH PASSWORD 'budget';"
/opt/homebrew/opt/postgresql@16/bin/psql -d postgres -c "CREATE DATABASE budget_agent OWNER budget;"

# Python deps (uses your existing finn-agent conda env)
conda activate finn-agent
pip install -r requirements.txt

# Schema + sample data
cp .env.example .env  # then fill in GOOGLE_CLOUD_PROJECT or GOOGLE_API_KEY
python -m app.db.init_db --reset --seed
```

## Choose how to call Gemini

Option A — **AI Studio API key** (simplest):

```bash
# in .env
GOOGLE_GENAI_USE_VERTEXAI=false
GOOGLE_API_KEY=your_key_from_aistudio.google.com/apikey
```

Option B — **Vertex AI on your GCP project** (consulting-aligned):

```bash
gcloud auth application-default login
# in .env
GOOGLE_GENAI_USE_VERTEXAI=true
GOOGLE_CLOUD_PROJECT=your-project-id
GOOGLE_CLOUD_LOCATION=us-central1   # or europe-north1 if model is available there
```

Your account needs `roles/aiplatform.user` on the project.

## Run

```bash
# CLI sanity check
python -m app.agent.cli --once "How are we doing in May 2026?"

# UI
streamlit run app/ui/streamlit_app.py
```

## Day-to-day commands

```bash
python -m pytest                                          # tests
python -m app.db.init_db --reset --seed                   # rebuild from scratch
python -m scripts.import_csv data/sample/sample_import.csv  # import a CSV
```

## Project layout

```
app/
  config.py              env config dataclass
  agent/
    agent.py             ADK Agent factory
    tools.py             ADK tool functions (close over household_id)
    cli.py               interactive REPL
    prompts/system_instruction.md   the Budget Coach personality + rules
  db/
    models.py            SQLAlchemy models (households, users, ...)
    session.py           engine + session_scope context manager
    repository.py        ORM → dataclass conversion
    init_db.py           create_all + seed
  tools/
    types.py             pure dataclasses for the math layer
    budget_math.py       deterministic budget calculations
    csv_import.py        forgiving CSV importer
  ui/streamlit_app.py    UI with Overview / Transactions / Chat tabs
data/sample/             sample CSV
tests/                   pytest suite for the math layer
scripts/                 one-shot CLIs
```

## Design rules

1. **The agent never does math.** It calls tools. This is what makes the system auditable and stops it from inventing numbers.
2. **Tools are pure functions.** They take dataclasses, return numbers. The DB layer fetches and converts. This makes the math testable in 0.02s without spinning up Postgres.
3. **`household_id` on every row.** Multi-tenant from row one is much cheaper than retrofitting later.
4. **Date precision is honest.** `date_is_estimated=true` rows count toward monthly totals but are surfaced when the user is making month-by-month judgments.
5. **Uncategorized counts as expense.** Conservative default: don't make spending look smaller than it is.

## What's stubbed / next

- **Auth**: a sidebar dropdown picks the current household member. Replace with Firebase Auth (Google sign-in) before sharing.
- **Memory Bank**: not yet wired. Session state is conversation-scoped only.
- **LLM-assisted categorization**: deterministic pass exists; LLM fallback for unknown merchants is the next slice.
- **BigQuery analytics**: deferred. Postgres remains the system of record; BigQuery would be a sink.
- **Deploy target**: Cloud Run for the UI, Agent Runtime for the agent, when ready.
