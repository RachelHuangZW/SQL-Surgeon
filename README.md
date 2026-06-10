# SQL Surgeon

**An agentic SQL tuning tool powered by LLMs and real PostgreSQL execution plan analysis.**

SQL Surgeon takes a slow query + table DDL, runs `EXPLAIN ANALYZE` against your database, and uses an LLM-driven agent loop to identify bottlenecks, generate optimization advice, and produce a ready-to-run index + query script — with a self-reflection step to verify quality before returning results.

---

## How It Works

SQL Surgeon is built as a **LangGraph state machine**, not a simple LLM chain. The graph has a built-in review-and-retry loop so the agent can catch and correct low-quality advice before surfacing it.

```
run_explain → identify_issues → generate_advice → review_advice
                    ↑                                    |
                    └──────────── retry (max 2) ─────────┘
                                                         |
                                                        END
```

**Node-by-node:**

| Node | What it does |
|------|-------------|
| `run_explain` | Connects to PostgreSQL, runs `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)`, rolls back (no side effects) |
| `identify_issues` | LLM reads the execution plan + DDL, returns a list of specific bottlenecks |
| `generate_advice` | LLM produces concrete recommendations + a two-step executable script (CREATE INDEX + optimized query) |
| `review_advice` | LLM acts as a senior DBA reviewer — returns `pass` or `retry` with feedback |

If `review_advice` returns `retry`, the feedback is fed back into `identify_issues` and the loop runs again (max 2 retries).

---

## Evaluation Framework

SQL Surgeon includes a rigorous evaluation harness benchmarked on the **Join Order Benchmark (JOB)** — 113 real-world analytical queries over the IMDb dataset (~36M rows in `cast_info` alone).

### Metrics (L1–L3)

| Metric | What it measures |
|--------|-----------------|
| **L1** — Execution cost | PostgreSQL planner cost before vs. after surgeon's suggested indexes (via hypopg) |
| **L2** — Structural quality | Precision/recall of surgeon's index recommendations vs. oracle greedy search |
| **L3** — Agent health | `retry_count`, `verdict`, `hit_max_retry` — internal loop behavior |

### Baselines (B1–B2)

| Baseline | Description |
|----------|-------------|
| **B1** | Raw query cost with no indexes — `EXPLAIN (FORMAT JSON)` on the original SQL |
| **B2** | Oracle greedy search — hypothetically adds indexes one by one (via hypopg), each round picking the column that drops cost the most |

**L × B matrix:** B1 is the floor, B2 is the theoretical ceiling under PostgreSQL's cost model. SQL Surgeon is evaluated against both.

### hypopg

Evaluation uses [hypopg](https://hypopg.readthedocs.io/) — a PostgreSQL extension that registers indexes in memory without building them. This lets us measure the planner cost improvement of suggested indexes in milliseconds, without touching the real database.

### Running the eval

```bash
# Requires: Docker container postgres-job-docker-job-1 running with IMDb data
cd backend
python -m eval.run_eval
```

Each query produces a JSON result in `eval/results/`:

```json
{
  "query": "10a",
  "status": "success",
  "b1_cost": 514234.57,
  "surgeon_cost": 89123.4,
  "b2_cost": 71200.1,
  "b2_indexes": ["CREATE INDEX ON title(production_year)", "..."],
  "issues": [...],
  "advice": [...],
  "optimized_sql": "...",
  "retry_count": 0,
  "verdict": "pass",
  "hit_max_retry": false
}
```

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Agent orchestration | LangGraph + LangChain |
| LLM | Google Gemini 2.5 Pro |
| Backend API | FastAPI + Uvicorn |
| Database | PostgreSQL 16 (via psycopg2) |
| Hypothetical indexes | hypopg |
| Evaluation dataset | Join Order Benchmark (JOB) over IMDb |
| Frontend | Next.js 14 (App Router) + Tailwind CSS |

---

## Project Structure

```
SQL-Surgeon/
├── backend/
│   ├── agent/
│   │   ├── state.py        # AgentState TypedDict (shared blackboard)
│   │   ├── prompts.py      # ANALYSIS, ADVICE, REVIEW_ADVICE prompts
│   │   ├── nodes.py        # All graph node functions
│   │   └── graph.py        # LangGraph StateGraph definition + routing logic
│   ├── api/
│   │   └── main.py         # FastAPI app, /api/diagnose endpoint
│   ├── db/
│   │   └── client.py       # DBClient: execute_explain + benchmark_in_sandbox
│   ├── eval/
│   │   ├── run_eval.py     # Eval harness: L1/L2/L3 metrics, B1/B2 baselines, hypopg
│   │   └── results/        # Per-query JSON results + summary
│   └── test_agent.py       # End-to-end test (bypasses API)
├── frontend/
│   ├── app/
│   │   ├── page.tsx        # Main page, orchestrates state + API calls
│   │   └── layout.tsx
│   ├── components/
│   │   ├── InputPanel.tsx  # SQL + DDL textarea form
│   │   └── ResultPanel.tsx # Issues, advice, optimized SQL with copy button
│   └── lib/
│       └── api.ts          # fetch wrapper for /api/diagnose
├── postgres-job-docker/    # Docker setup for JOB benchmark database
│   └── Dockerfile          # PostgreSQL 16 + hypopg
├── requirements.txt
└── .env                    # DATABASE_URL + GOOGLE_API_KEY (not committed)
```

---

## Getting Started

### Prerequisites

- PostgreSQL instance (local Docker or remote)
- Google Gemini API key (paid tier for `gemini-2.5-pro`)
- Python 3.12+, Node.js 18+

### 1. Clone and install

```bash
git clone https://github.com/RachelHuangZW/SQL-Surgeon.git
cd SQL-Surgeon
```

```bash
# Backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

```bash
# Frontend
cd frontend && npm install
```

### 2. Configure environment

Create a `.env` file in the project root:

```env
DATABASE_URL=postgresql://user:password@localhost:5432/yourdb
GOOGLE_API_KEY=your_gemini_api_key
```

### 3. Run

```bash
# Backend (from /backend)
uvicorn api.main:app --reload --port 8000

# Frontend (from /frontend)
npm run dev
```

Open [http://localhost:3000](http://localhost:3000), paste your slow query + DDL, and click Analyze.

---

## API

### `POST /api/diagnose`

**Request:**
```json
{
  "original_sql": "SELECT * FROM orders WHERE status = 'pending' ORDER BY created_at DESC LIMIT 100",
  "ddl": "CREATE TABLE orders (id SERIAL PRIMARY KEY, status TEXT, created_at TIMESTAMPTZ);",
  "table_name": "orders"
}
```

**Response:**
```json
{
  "status": "success",
  "issues": ["Sequential scan on orders — no index on status column", "..."],
  "advice": ["Create a composite index on (status, created_at DESC)", "..."],
  "optimized_sql": "-- Step 1: Create index (run once)\nCREATE INDEX ...\n\n-- Step 2: Run the optimized query\nSELECT ...",
  "explain_output": [...],
  "error": null
}
```

The `optimized_sql` field is a complete, copy-pasteable script you can run directly in DBeaver or psql.

---

## What SQL Surgeon is NOT

- Not a Text-to-SQL tool — it tunes queries you already have
- Not a black box — every issue and recommendation is explained
- Not production automation — it suggests; you decide when to apply

---

## License

MIT
