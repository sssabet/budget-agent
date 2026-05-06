# Budget Coach Agent

Cloud-hosted household budget assistant on Cloud Run, Postgres, and Vertex AI.

## Architecture in one paragraph

A Cloud Run FastAPI service talks to Postgres and an ADK agent. The agent uses **Gemini 2.5 Flash** for language and **deterministic Python tools** for every number — the LLM never adds, sums, or compares amounts. Multi-tenant from day one (`household_id` on every row), so the same code can serve one couple or many.

```
Client ──► Cloud Run FastAPI ──► ADK agent (Gemini 2.5 Flash) ──► function tools
                         │                                           │
                         └────────► Postgres (SQLAlchemy) ◄──────────┘
```

## Cloud Setup

```bash
# One-time GCP setup
gcloud auth login
gcloud config set project "$GOOGLE_CLOUD_PROJECT"
gcloud services enable run.googleapis.com cloudbuild.googleapis.com \
                       artifactregistry.googleapis.com aiplatform.googleapis.com

# Point at Cloud SQL, VPC Postgres, or serverless Postgres.
export DATABASE_URL='postgresql+psycopg://USER:PW@HOST/budget_agent'
```

## Vertex AI

```bash
export GOOGLE_GENAI_USE_VERTEXAI=true
export GOOGLE_CLOUD_PROJECT=your-project-id
export GOOGLE_CLOUD_LOCATION=us-central1   # or europe-north1 if model is available there
```

The Cloud Run service account needs `roles/aiplatform.user` on the project.

## Day-to-day commands

```bash
python -m pytest                                          # tests
python -m app.db.init_db --reset --seed                   # rebuild from scratch
python -m scripts.import_csv data/sample/sample_import.csv  # import a CSV
python -m evals.run_evals                                 # run the agent eval suite
python -m evals.run_evals --cases blame_refusal --verbose # one case + full output
python -m scripts.demo                                    # demo prompt sequence
python -m scripts.demo --pause                            # pause between prompts (good for screencasts)
python -m scripts.sync_to_bigquery --dry-run              # preview BQ sync (no writes)
python -m scripts.sync_to_bigquery                        # full refresh into BigQuery
```

## BigQuery analytics sink

Postgres stays the system of record. BigQuery exists for Looker Studio
dashboards, ad-hoc SQL, and joining household data with external sources.
The agent itself uses pure-Python analytics (`app/tools/analytics.py`) and
does not depend on BQ.

The sync script does a full-refresh load of `categories`, `budgets`, and a
denormalized `transactions` table (category name + partner names inlined for
dashboard convenience):

```bash
# requires GOOGLE_CLOUD_PROJECT in .env and `gcloud auth application-default login`
python -m scripts.sync_to_bigquery --dry-run            # preview what would land
python -m scripts.sync_to_bigquery                       # full refresh
python -m scripts.sync_to_bigquery --dataset my_house    # custom dataset name
```

Quick sanity query after a sync:

```sql
SELECT category_name, ROUND(SUM(amount_nok), 2) AS total
FROM `forzify-stats.budget_agent.transactions`
WHERE is_income IS NOT TRUE AND transaction_date >= '2026-01-01'
GROUP BY 1 ORDER BY 2 DESC
```

The agent gained three trend tools in this slice:
`get_month_over_month_spend`, `get_top_merchants`, and
`find_recurring_subscriptions_tool` — try *"What recurring subscriptions are
we paying for?"* in chat.

## Deploy

The plan deliberately picks Cloud Run as the first deploy target and Agent
Runtime as the next step — see `docs/consulting_story.md` for the
"deferred to later" rationale.

```bash
# One-time GCP setup
gcloud auth login
gcloud config set project "$GOOGLE_CLOUD_PROJECT"
gcloud services enable run.googleapis.com cloudbuild.googleapis.com \
                       artifactregistry.googleapis.com aiplatform.googleapis.com

# Deploy (builds the Dockerfile via Cloud Build, pushes, rolls out)
export DATABASE_URL='postgresql+psycopg://USER:PW@HOST/budget_agent'
export AUTH_MODE=firebase              # or: google
export FIREBASE_PROJECT_ID='your-firebase-project-id'
# for AUTH_MODE=google instead:
# export GOOGLE_OAUTH_CLIENT_ID='your-oauth-client-id.apps.googleusercontent.com'
./scripts/deploy_cloud_run.sh

# Smoke test (the deploy script prints the URL in a green banner and writes
# it to .last_deploy_url; ./scripts/url.sh re-fetches it any time).
URL=$(./scripts/url.sh)
curl -fsS "$URL/readyz"
curl -fsS "$URL/me" -H "authorization: Bearer $ID_TOKEN"
curl -fsS -X POST "$URL/chat" -H "authorization: Bearer $ID_TOKEN" -H 'content-type: application/json' \
  -d '{"prompt":"How are we doing this month?"}' | jq
```

## Observability

Every agent turn (CLI or chat tab) emits one JSON line via the
`budget_agent.turn` logger: request_id, user_query, model, tools called with
their args + per-call latency, total latency, and any policy flags
(`blame`, `hide`, `money_movement`, `individual_attribution_request`)
detected by `app/agent/policy.py`.

To pipe the CLI's turn logs to a file:

```bash
python -m app.agent.cli 2> turns.jsonl
```

Policy flags are observation-only — refusals are produced by the LLM
following the system instruction. The flag set lets you grep logs and
write evals that pin down "this kind of prompt was correctly identified."

## Project layout

```
app/
  config.py              env config dataclass
  agent/
    agent.py             ADK Agent factory (accepts default_month for context)
    tools.py             ADK tool functions (close over household_id)
    policy.py            unsafe-prompt flagger (blame / hide / money_movement)
    turn_log.py          per-turn structured logging
    cli.py               interactive REPL
    prompts/system_instruction.md   the Budget Coach personality + rules
  api/main.py            FastAPI wrapper for Cloud Run deploys
  db/
    models.py            SQLAlchemy models (households, users, ...)
    session.py           engine + session_scope context manager
    repository.py        ORM → dataclass conversion
    init_db.py           create_all + seed
  tools/
    types.py             pure dataclasses for the math layer
    budget_math.py       deterministic budget calculations
    csv_import.py        forgiving CSV importer
    categorizer.py       merchant-rules categorizer (proposes, doesn't apply)
    analytics.py         month-over-month, top merchants, recurring subscriptions
evals/
  budget_agent_evalset.json   eval cases (data + safety + policy)
  run_evals.py                runner with heuristic rubrics
docs/
  consulting_story.md         60-second pitch + naming map + demo flow
  five_day_gcp_agent_platform_budget_agent_plan.md
data/sample/             sample CSV
tests/                   pytest suite for the math + policy + categorizer layers
scripts/                 one-shot CLIs (import_csv, demo, deploy_cloud_run, url, sync_to_bigquery)
Dockerfile               Cloud Run image
```

## Design rules

1. **The agent never does math.** It calls tools. This is what makes the system auditable and stops it from inventing numbers.
2. **Tools are pure functions.** They take dataclasses, return numbers. The DB layer fetches and converts. This makes the math testable in 0.02s without spinning up Postgres.
3. **`household_id` on every row.** Multi-tenant from row one is much cheaper than retrofitting later.
4. **Date precision is honest.** `date_is_estimated=true` rows count toward monthly totals but are surfaced when the user is making month-by-month judgments.
5. **Uncategorized counts as expense.** Conservative default: don't make spending look smaller than it is.

## Auth

The FastAPI surface requires an authenticated user for `/me` and `/chat`.
`AUTH_MODE=firebase` accepts Firebase ID tokens and `AUTH_MODE=google` accepts
Google Sign-In ID tokens. The token email must match a row in `users`, and that
user must be linked through `household_users`; `household_name` requests are
rejected unless the user belongs to that household.

## iPhone PWA

The service now serves a mobile-first PWA at `/`. It uses the same bearer-token
auth as the API and exposes dashboard, category, transaction, chat, and reminder
flows from the browser.

For Firebase sign-in in the browser, set the public web config values from your
Firebase project:

```bash
FIREBASE_API_KEY=...
FIREBASE_AUTH_DOMAIN=...
FIREBASE_APP_ID=...
FIREBASE_MESSAGING_SENDER_ID=...
```

Daily reminders use Web Push. On iPhone, the app must be added to the Home
Screen and the user must allow notifications. Configure VAPID keys:

```bash
WEB_PUSH_VAPID_PUBLIC_KEY=...
WEB_PUSH_VAPID_PRIVATE_KEY=...
WEB_PUSH_VAPID_SUBJECT=mailto:you@example.com
```

Reminders are dispatched from a best-effort loop running inside the FastAPI
process (see `_reminder_loop` in `app/api/main.py`) — no Cloud Scheduler
needed. Trade-off: Cloud Run scales to zero, so a reminder only fires if the
instance happens to be warm at the user's local reminder time.

## What's stubbed / next

- **Memory Bank**: not yet wired. Session state is conversation-scoped only.
- **LLM-assisted categorization**: deterministic pass exists; LLM fallback for unknown merchants is the next slice.
- **BigQuery analytics**: shipped as a full-refresh sync (`scripts/sync_to_bigquery.py`). Postgres remains the system of record. Hook a Looker Studio dashboard to the synced tables when you want a UI for non-agent analytics.
- **Cloud Run sessions**: the FastAPI wrapper holds sessions in process memory. For multi-instance scale, swap to Agent Platform Sessions.
- **Agent Runtime**: Cloud Run is the first deploy target; Agent Runtime is the Agent-Platform-native destination.

For the consulting framing — naming map, demo flow, and what each piece
demonstrates to a client — see [`docs/consulting_story.md`](docs/consulting_story.md).
