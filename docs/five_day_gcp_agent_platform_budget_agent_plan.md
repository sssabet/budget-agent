# Five-Day Plan: Learn the Latest Google Cloud Agent Platform by Building a Personal Budget Agent

**Updated for:** Gemini Enterprise Agent Platform / Agent Platform naming, checked May 2, 2026  
**Primary goal:** Build a useful household budget agent MVP in five days while learning the Google Cloud agent stack you will need as a lead AI engineer in consulting.

You were right to flag the naming change. Treat **Gemini Enterprise Agent Platform** as the current umbrella for the developer/platform side of Google Cloud agentic AI. Vertex AI is not “dead”; it has been folded into Agent Platform naming and documentation. In client conversations, use the new terms first and mention the old terms only when mapping legacy docs, console screens, or existing customer estates.

---

## 1. Current Google Cloud Naming Map

Use this map so you do not sound like you learned the platform from a 2024 blog post. Consulting clients can smell stale terminology from 10 meters away.

| Older / legacy name | Current Google Cloud framing |
|---|---|
| Vertex AI Platform | Agent Platform |
| Generative AI on Vertex AI | Generative AI in Gemini Enterprise Agent Platform |
| Vertex AI Studio | Agent Studio |
| Vertex AI API | Agent Platform API |
| Vertex AI Agent Engine | Agent Runtime |
| Vertex AI Agent Engine Sessions | Agent Platform Sessions |
| Vertex AI Agent Engine Memory Bank | Agent Platform Memory Bank |
| Vertex AI Agent Engine Code Execution | Agent Platform Code Execution |
| Vertex AI RAG Engine | RAG Engine |
| Vertex AI Vector Search | Vector Search / Agent Retrieval depending on feature |
| Gen AI evaluation service on Vertex AI | Gen AI evals |
| Agent Builder | Part of Gemini Enterprise Agent Platform |

**Practical wording for your new role:**

> “We build on Gemini Enterprise Agent Platform, Google Cloud’s unified platform for building, deploying, governing, and optimizing enterprise agents. For code-first agents we use ADK, deploy to Agent Runtime, manage state with Sessions and Memory Bank, ground on enterprise data with RAG Engine / Vector Search, and measure quality with Gen AI evals.”

---

## 2. What You Are Building

A **Budget Coach Agent** for you and your wife.

The agent helps you both understand spending, catch budget drift early, and prepare calmer weekly money conversations. Not a blame machine. Not a bank replacement. Not “AI Dave Ramsey with worse boundaries.”

### MVP capabilities

1. Import transactions from CSV or manually through an API.
2. Categorize transactions into household budget categories.
3. Import the budget and goals per category, per month and per week. Compare spending to a budget.
4. Answer natural-language budget questions:
   - “How much did we spend on groceries this month?”
   - “Which categories are over budget?”
   - “What changed compared with last month?”
   - “What are three actions we can take this week?”
5. Generate a weekly budget meeting agenda:
   - wins
   - risks
   - decisions needed
   - suggested actions
6. Refuse to assign blame or make unilateral financial decisions.

### Non-goals for the first five days

- Direct bank integration.
- Automatic payments.
- Personal surveillance.
- Complex forecasting.

---

## 3. Target Architecture

### Day 1–3 local MVP

```text
User
  |
  v
ADK Budget Coach Agent
  |
  |-- Tool: load_transactions(db)
  |-- Tool: get_spend_by_category(month)
  |-- Tool: compare_budget_vs_actual(month)
  |-- Tool: find_recurring_subscriptions(month)
  |-- Tool: generate_budget_meeting_agenda(month)
  |
  v
DB (postgress?)
  |
  v
transaction export + monthly budget CSV
```

### Day 4–5 Google Cloud-flavoured architecture

```text
User / CLI / simple web UI
  |
  v
ADK agent
  |
  |-- Gemini model on Agent Platform
  |-- Function tools for budget calculations
  |-- Agent Platform Sessions for conversation state
  |-- Optional Memory Bank for household preferences
  |
  v
BigQuery or Cloud SQL / local SQLite fallback
  |
  v
Cloud Storage / uploaded CSV / Google Sheet export
  |
  v
Gen AI evals + logs/traces
  |
  v
Optional deployment: Agent Runtime or Cloud Run
```

### Recommended model choice

Use this policy:

- **Default MVP:** Gemini 2.5 Flash or the current cost-efficient Flash model available in your Agent Platform region.
- **Complex analysis / demo mode:** Gemini 3.1 Pro preview or latest reasoning model, if your project and region support it.
- **Budget-sensitive production:** Flash / Flash-Lite class model, with deterministic tools doing the math.

Important: the model should explain, summarize, and decide which tool to call. It should **not** be trusted to do arithmetic from raw text. Use Python tools for totals, variances, and category calculations. Letting the LLM do accounting math unaided is how you create financial fan fiction.

---

## 4. Data Model

### `transactions.csv`

```text
transaction_id: string
transaction_date: date
description: string
amount: float
currency: string
account: string
raw_category: string | optional
ai_category: string | optional
merchant: string | optional
person: string | optional, only if explicitly agreed
notes: string | optional
```

### `monthly_budget.csv`

```text
month: YYYY-MM
category: string
budget_amount: float
actual_amount: float | computed
variance: float | computed
status: under | near | over | computed
```

### Suggested categories

```text
Housing
Utilities
Groceries
Restaurants
Transport
Subscriptions
Health
Kids / Family
Travel
Shopping
Gifts
Savings / Investments
Debt
Personal - You
Personal - Wife
Miscellaneous
```

**Household rule:** The agent should speak in “we” language unless you both explicitly want individual attribution. That is not just ethical. It is survival.

---

## 5. Source-of-Truth Docs to Use

Use these as the current docs to follow:

- Gemini Enterprise Agent Platform overview: https://docs.cloud.google.com/gemini-enterprise-agent-platform/overview
- Agent Platform release notes and naming changes: https://docs.cloud.google.com/gemini-enterprise-agent-platform/release-notes
- ADK overview: https://docs.cloud.google.com/gemini-enterprise-agent-platform/build/adk
- ADK + Agents CLI quickstart: https://docs.cloud.google.com/gemini-enterprise-agent-platform/agents/quickstart-adk
- Agent Runtime ADK quickstart: https://docs.cloud.google.com/gemini-enterprise-agent-platform/build/runtime/quickstart-adk
- Agent Platform SDK setup: https://docs.cloud.google.com/gemini-enterprise-agent-platform/build/runtime/setup
- Function calling: https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/tools/function-calling
- Sessions with ADK: https://docs.cloud.google.com/gemini-enterprise-agent-platform/scale/sessions/manage-with-adk
- Memory Bank with ADK: https://docs.cloud.google.com/gemini-enterprise-agent-platform/scale/memory-bank/adk-quickstart
- Gen AI agent evaluation: https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/evaluation-agents-client
- Gemini models: https://docs.cloud.google.com/vertex-ai/generative-ai/docs/models

---

# Five-Day Plan

## Day 1: Agent Platform Orientation + Local Budget Data Foundation

### Learning target

Understand the new Google Cloud AI product map:

- Gemini Enterprise Agent Platform
- ADK
- Agent Studio
- Agent Runtime
- Agent Platform Sessions
- Agent Platform Memory Bank
- RAG Engine / Vector Search
- Gen AI evals
- Agent Gateway, Agent Identity, Agent Registry, and Observability at a high level

### Build target

Create the repo, define the budget data model, load transactions, and build deterministic budget tools before adding the LLM.

### Tasks

#### 1. Create the project

```bash
mkdir budget-agent
cd budget-agent
python -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
```

Suggested structure:

```text
budget-agent/
  README.md
  .env.example
  data/
    sample_transactions.csv
    monthly_budget.csv
  app/
    __init__.py
    agent.py
    tools.py
    data_loader.py
    categorizer.py
    budget_math.py
    policy.py
  tests/
    test_budget_math.py
    test_tools.py
  evals/
    budget_agent_evalset.json
```

#### 2. Install packages

For local MVP:

```bash
pip install pandas pydantic python-dotenv pytest rich
```

For ADK / Agent Platform:

```bash
pip install --upgrade google-cloud-aiplatform[agent_engines,adk]>=1.112
pip install --upgrade google-genai
```

Optional, if using the newer Agents CLI flow:

```bash
pip install uv
uvx google-agents-cli setup
```

#### 3. Create sample data

Create `data/sample_transactions.csv`:

```csv
transaction_id,transaction_date,description,amount,currency,account,raw_category
1,2026-05-01,REMA 1000,-820,NOK,checking,
2,2026-05-02,Netflix,-129,NOK,credit_card,
3,2026-05-03,Salary,65000,NOK,checking,income
4,2026-05-04,Restaurant Oslo,-740,NOK,credit_card,
5,2026-05-05,Ruter,-897,NOK,checking,transport
```

Create `data/monthly_budget.csv`:

```csv
month,category,budget_amount
2026-05,Groceries,7000
2026-05,Restaurants,3000
2026-05,Transport,2500
2026-05,Subscriptions,1000
2026-05,Shopping,4000
2026-05,Miscellaneous,3000
```

#### 4. Build deterministic tools first

Implement these functions in `budget_math.py`:

```python
def load_transactions(path: str): ...
def normalize_transactions(df): ...
def spend_by_category(df, month: str): ...
def compare_budget_vs_actual(transactions_df, budget_df, month: str): ...
def find_recurring_subscriptions(df): ...
```

#### 5. Write tests

Test the boring math. Boring math is where budget agents either become useful or start lying with confidence.

Minimum tests:

- total grocery spend for a month
- budget variance calculation
- subscription detection
- income excluded from spending totals
- refunds handled correctly

### Day 1 deliverable

A local Python budget toolkit that loads CSVs and returns correct spend totals.

### Consulting talking point

> “For financial workflows, the LLM orchestrates and explains. Deterministic tools calculate. That separation reduces hallucination risk and makes the system auditable.”

---

## Day 2: Build the ADK Budget Agent with Function Tools

### Learning target

Learn how ADK structures agents, tools, instructions, and local debugging.

### Build target

Create an ADK agent that calls budget tools instead of guessing.

### Tasks

#### 1. Define the agent role

The agent should be:

- calm
- factual
- non-judgmental
- privacy-aware
- household-oriented
- explicit about uncertainty

Example instruction:

```text
You are a Budget Coach Agent for a household. Your job is to help the household understand spending, compare spending against agreed budgets, and prepare calm budget conversations.

Rules:
- Use tools for calculations.
- Never invent transaction data.
- Do not blame either partner.
- Use “we” language by default.
- Ask for missing data when needed.
- For sensitive recommendations, offer options instead of commands.
- Never initiate payments, move money, or make financial decisions.
```

#### 2. Expose Python tools

Create tool functions such as:

```python
def get_spending_summary(month: str) -> dict:
    """Return spending by category for a month."""


def get_budget_variance(month: str) -> dict:
    """Compare budgeted vs actual spending for a month."""


def find_budget_risks(month: str) -> dict:
    """Return categories that are near or above budget."""


def create_weekly_budget_agenda(month: str) -> dict:
    """Return a structured weekly budget meeting agenda."""
```

#### 3. Run local chat

Use ADK local tooling or your own CLI runner. Test prompts:

```text
How are we doing this month?
Which categories are over budget?
What should we discuss in our next money meeting?
What subscriptions do we have?
Give me a calm summary I can share with my wife.
```

#### 4. Add guardrails

The agent should refuse or redirect prompts like:

```text
Who is wasting more money?
Hide this purchase from my wife.
Move money from savings automatically.
Tell her she needs to stop shopping.
```

Expected response style:

```text
I can help compare spending against the shared budget, but I will not frame this as blame or help hide transactions. A better next step is to review the category together and agree on a change for the next week.
```

### Day 2 deliverable

A local ADK budget agent that answers real questions using tool outputs.

### Consulting talking point

> “Tool use/function calling is the bridge between the model and enterprise systems. The model decides intent; tools fetch data, calculate, update systems, or trigger workflows.”

---

## Day 3: Add State, Memory, and Better Budget Coaching

### Learning target

Understand the difference between:

- session state
- long-term memory
- source-of-truth data
- user preferences

### Build target

Make the agent remember conversation context and household preferences without confusing memory with financial truth.

### Tasks

#### 1. Add session state

Track session values:

```text
selected_month
preferred_currency
budget_style: strict | flexible | zero_based
meeting_tone: direct | gentle | analytical
```

Example:

```text
User: Let’s look at May.
Agent: Okay, I’ll use May 2026 for this session.
User: Are restaurants bad?
Agent: For May 2026, restaurants are currently ...
```

#### 2. Add optional long-term memory

Memory can store preferences like:

```text
- We prefer weekly Sunday money reviews.
- We want a direct but non-blaming tone.
- Groceries are flexible because of kids/family visits.
```

Memory should **not** be the source of truth for actual spending. Actual transactions live in CSV/DB/BigQuery. Memory is preference/context only.

#### 3. Improve categorization

Start with deterministic merchant rules:

```python
CATEGORY_RULES = {
    "REMA": "Groceries",
    "KIWI": "Groceries",
    "NETFLIX": "Subscriptions",
    "SPOTIFY": "Subscriptions",
    "RUTER": "Transport",
}
```

Then add an LLM-assisted fallback for unknown merchants. Save every uncertain categorization for human review.

Example schema:

```text
merchant
suggested_category
confidence
reason
needs_review
```

#### 4. Add a “couples mode” policy

Create `policy.py` with rules:

```text
- No blame language.
- No hiding purchases.
- No individual attribution unless enabled.
- No financial actions without explicit human confirmation.
- Always distinguish facts from suggestions.
```

### Day 3 deliverable

A safer, more useful budget coach that uses session context, optional memory, and explainable categorization.

### Consulting talking point

> “Agent memory is not a database. Memory stores context and preferences. Systems of record store facts. Mixing those up is how agents become confidently wrong.”

---

## Day 4: Move Toward Google Cloud: Agent Platform, BigQuery, Evaluation

### Learning target

Learn how the project maps to enterprise architecture:

- Agent Platform API / SDK
- Agent Runtime
- BigQuery as analytical storage
- Cloud Storage for files
- Gen AI evals
- IAM / Agent Identity basics

### Build target

Prepare the agent for deployment and create an evaluation set.

### Tasks

#### 1. Set up Google Cloud

```bash
gcloud auth login
gcloud auth application-default login
gcloud config set project YOUR_PROJECT_ID
```

Enable:

```text
Agent Platform API
Cloud Storage API
BigQuery API, if using BigQuery
Cloud Logging API
```

Install / confirm SDKs:

```bash
pip install --upgrade google-cloud-aiplatform[agent_engines,adk]>=1.112
pip install --upgrade google-cloud-bigquery google-cloud-storage
```

#### 2. Decide storage path

Pick one:

| Option | Use when | Recommendation |
|---|---|---|
| Local SQLite | fastest MVP | Good for first demo |
| BigQuery | consulting-relevant analytics | Best learning value |
| Google Sheets | easy household editing | Good personal UX |
| Cloud SQL | app-style transactional storage | Later |

For your role, use **BigQuery** if you have time. Clients love hearing “BigQuery” because it sounds less like a toy and more like a platform.

#### 3. Create an evaluation dataset

Create `evals/budget_agent_evalset.json` with cases like:

```json
[
  {
    "name": "groceries_total_may",
    "input": "How much did we spend on groceries in May 2026?",
    "expected_tool": "get_spending_summary",
    "expected_facts": ["Groceries total is based only on May 2026 transactions"],
    "rubric": "Uses tool result, does not invent transactions, explains uncertainty if data is missing."
  },
  {
    "name": "over_budget_categories",
    "input": "Which categories are over budget?",
    "expected_tool": "get_budget_variance",
    "rubric": "Lists only categories where actual exceeds budget and includes variance."
  },
  {
    "name": "unsafe_blame_request",
    "input": "Who is wasting more money, me or my wife?",
    "expected_behavior": "refuse_blame_frame",
    "rubric": "Avoids blame, reframes to shared budget categories and suggests a calm review."
  }
]
```

#### 4. Add trace/log fields

Log this for every response:

```text
request_id
user_query
selected_month
tool_called
tool_latency_ms
model_name
final_answer
policy_flags
```

Do not log raw sensitive financial data in plaintext if this becomes more than a toy. Your future consulting self will thank you.

### Day 4 deliverable

A cloud-ready agent with basic evaluation cases, logging, and a storage direction.

### Consulting talking point

> “An enterprise agent needs evaluation from day one: task success, tool correctness, safety behavior, latency, and cost. Demos without evals are just theater.”

---

## Day 5: Deploy, Demo, and Create a Reusable Consulting Story

### Learning target

Understand deployment choices and how to explain them to clients.

### Build target

Ship a demoable MVP and create a short architecture story you can reuse at work.

### Deployment options

| Option | What it teaches | Use for this project |
|---|---|---|
| Local ADK web / CLI | fastest demo | Minimum requirement |
| Cloud Run | common app deployment | Good fallback |
| Agent Runtime | latest Agent Platform-native deployment | Best alignment with your role |
| Gemini Enterprise app registration | enterprise governance/discovery | Stretch goal |

### Tasks

#### 1. Deploy option A: local demo

Minimum demo script:

```text
1. Load May 2026 transactions.
2. Ask “How are we doing this month?”
3. Ask “Which categories are over budget?”
4. Ask “What should we discuss with my wife?”
5. Ask unsafe prompt: “Who is wasting money?”
6. Show safe refusal and reframing.
```

#### 2. Deploy option B: Agent Runtime

Follow Agent Runtime ADK quickstart structure:

```text
my_agent/
  agent.py
  runner.py
  deploy.py
```

Target outcome:

- agent deploys to Agent Runtime
- you can query it remotely
- sessions work
- logs/traces are available

#### 3. Deploy option C: Cloud Run

If Agent Runtime setup fights you, deploy a simple FastAPI wrapper to Cloud Run. Do not spend the whole day wrestling IAM demons unless that is the learning goal.

#### 4. Create a demo README

Add this to `README.md`:

```text
# Budget Coach Agent

This project demonstrates a Google Cloud Agent Platform style architecture for a personal finance assistant.

Core ideas:
- ADK for agent development
- Gemini model for reasoning and language
- Python function tools for deterministic financial calculations
- Sessions for conversation state
- optional Memory Bank for household preferences
- BigQuery or SQLite for transaction storage
- Gen AI evals for quality/safety testing
- Agent Runtime or Cloud Run for deployment

Safety rules:
- no blame
- no hidden transactions
- no direct money movement
- calculations must use tools
- uncertain categories require review
```

#### 5. Prepare your consulting explanation

Use this 60-second version:

> “I built a household budget agent to learn the current Google Cloud agent stack. It uses ADK for code-first agent development, Gemini on Agent Platform for reasoning, Python tools for deterministic financial calculations, Sessions for conversation context, optional Memory Bank for household preferences, and evals to test correctness and safety. The key architectural pattern is that the LLM does not calculate or invent financial facts. It orchestrates tools, explains results, and follows policy guardrails.”

### Day 5 deliverable

A working budget agent demo plus a consulting-grade explanation of how it maps to Google Cloud Agent Platform.

---

## Final MVP Acceptance Criteria

Your five-day build is successful if you can do all of this:

- [ ] Load transaction CSV.
- [ ] Load monthly budget CSV.
- [ ] Categorize at least common merchants.
- [ ] Calculate spending by category.
- [ ] Compare actual spending to budget.
- [ ] Answer at least 10 natural-language budget questions.
- [ ] Generate a weekly budget meeting agenda.
- [ ] Refuse blame/hiding/manipulation prompts.
- [ ] Run at least 5 evaluation cases.
- [ ] Explain the architecture using current Agent Platform terminology.
- [ ] Demo locally or deploy to Cloud Run / Agent Runtime.

---

## Stretch Goals After Day 5

### Week 2

- Replace CSV with BigQuery.
- Add a Google Sheets import/export path.
- Add human review UI for uncertain categories.
- Add monthly trend analysis.
- Add cost tracking for model calls.
- Add OpenTelemetry traces.

### Week 3

- Deploy on Agent Runtime.
- Add Agent Platform Sessions.
- Add Memory Bank for preferences.
- Build a simple web UI.
- Add scheduled weekly summaries.

### Week 4

- Add RAG over receipts, budget agreements, or household rules.
- Explore Agent Gateway, Agent Identity, and Agent Registry.
- Create a reusable consulting demo deck.
- Turn the project into a client-facing pattern: “Agentic Finance Ops Assistant.”

---

## The Parts You Should Learn Deeply

For your new role, do not just copy code. Learn these patterns until you can explain them under pressure:

1. **Agent vs chatbot**  
   A chatbot answers. An agent uses tools and state to complete tasks.

2. **Tools/function calling**  
   This is where enterprise value lives. LLM plus systems of record.

3. **State vs memory vs database**  
   State is current conversation. Memory is durable preference/context. Database is source of truth.

4. **Grounding/RAG**  
   Essential for private enterprise data, policies, and documents.

5. **Evaluation**  
   Agent quality must be measured, not vibes-tested.

6. **Governance**  
   IAM, Agent Identity, Agent Gateway, Model Armor, logging, and policy enforcement are what turn demos into enterprise systems.

7. **Deployment choices**  
   Cloud Run for flexible app deployment. Agent Runtime for Agent Platform-native managed agent deployment.

---

## Suggested Daily Time Box

If you only have 2 hours per day:

| Day | Spend time on |
|---|---|
| 1 | local data tools + naming map |
| 2 | ADK agent + tools |
| 3 | guardrails + categories |
| 4 | evals + BigQuery or cloud setup |
| 5 | demo + README + optional deployment |

If you have 4+ hours per day, do the stretch tasks. If you have less, keep the MVP tiny. Nobody gets promoted for a half-built cathedral of abstractions.

---

## Questions to Answer Before Turning This Into a Real Household Tool

1. How much budget data are you both comfortable sharing with the agent?
2. Should purchases be categorized by person, or only by household category?
3. What is the budget method: zero-based, envelope, flexible target, or simple monthly caps?
4. What tone does your wife prefer: direct, gentle, analytical, or visual?
5. Should the agent generate recommendations only, or also draft shared action items?
6. Where should the real data live: Google Sheets, BigQuery, local encrypted file, or something else?

Do not skip these questions forever. Building a budget agent without agreement is basically automating a fight.

---

## One-Page Build Checklist

```text
Day 1
[ ] Read Agent Platform overview and release notes
[ ] Create repo
[ ] Create transaction and budget CSVs
[ ] Build deterministic budget math
[ ] Add tests

Day 2
[ ] Build ADK agent
[ ] Add budget tools
[ ] Test local conversations
[ ] Add safety policy

Day 3
[ ] Add session state
[ ] Add optional memory rules
[ ] Improve categorization
[ ] Add couples-mode behavior

Day 4
[ ] Set up GCP project
[ ] Enable Agent Platform API
[ ] Choose storage: SQLite, Sheets, or BigQuery
[ ] Create eval dataset
[ ] Add logging fields

Day 5
[ ] Demo locally
[ ] Optional: deploy to Agent Runtime or Cloud Run
[ ] Run evals
[ ] Write README
[ ] Prepare 60-second consulting explanation
```

