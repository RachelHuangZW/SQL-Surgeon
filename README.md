# SQL Surgeon

**An agentic SQL tuning tool powered by LLMs and real PostgreSQL execution plan analysis.**

SQL Surgeon takes a slow query + table DDL, runs `EXPLAIN ANALYZE` against your database, and uses an LLM-driven agent loop to identify bottlenecks, generate optimization advice, and produce a ready-to-run index + query script — with a self-reflection step to verify quality before returning results.

---

## How It Works

SQL Surgeon is built as a **LangGraph state machine**, not a simple LLM chain. The graph has a built-in review-and-retry loop so the agent can catch and correct low-quality advice before surfacing it. When a `table_name` is provided, the graph also runs a live sandbox benchmark to validate the optimized SQL.

```
run_explain → identify_issue → generate_advice → review_advice
                   ↑                                    |
                   └──────────── retry (max 2) ─────────┤
                                                         |
                                               (table_name provided)
                                                         ↓
                                         generate_benchmark_schema → END
                                                         |
                                                        END
```

**Node-by-node:**

| Node | What it does |
|------|-------------|
| `run_explain` | Connects to PostgreSQL, runs `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)`, rolls back (no side effects) |
| `identify_issue` | LLM reads the execution plan + DDL, returns a list of specific bottlenecks |
| `generate_advice` | LLM produces concrete recommendations + a two-step executable script (CREATE INDEX + optimized query) |
| `review_advice` | LLM acts as a senior DBA reviewer — returns `pass` or `retry` with feedback |
| `generate_benchmark_schema` | Creates an isolated schema, copies ≤100k rows, applies suggested indexes, runs `EXPLAIN ANALYZE`, then drops the schema — a safe before/after comparison |

If `review_advice` returns `retry`, the feedback is fed back into `identify_issue` and the loop runs again (max 2 retries).

---

## Evaluation Framework

SQL Surgeon includes an evaluation harness benchmarked on the **Join Order Benchmark (JOB)** — analytical queries over the IMDb dataset.

### Metrics

| Metric | What it measures | Status |
|--------|-----------------|--------|
| **L1** — Baseline cost | PostgreSQL planner cost on the original query with no indexes (B1) | ✓ Implemented |
| **L3** — Agent health | `retry_count`, `verdict`, `hit_max_retry` — internal loop behavior | ✓ Implemented |
| **L2** — Index quality | Precision/recall of surgeon's index recommendations vs. oracle greedy | Planned |

### Baselines

| Baseline | Description | Status |
|----------|-------------|--------|
| **B1** | Raw query cost — `EXPLAIN (FORMAT JSON)` on the original SQL, no indexes | ✓ Implemented |
| **B2** | Oracle greedy search via hypopg — adds indexes one-by-one, picks the one that drops cost the most each round | Planned |

### hypopg

Evaluation uses [hypopg](https://hypopg.readthedocs.io/) — a PostgreSQL extension that registers indexes in memory without building them. This lets us measure planner cost improvement of suggested indexes without touching the real database.

### Running the eval

```bash
# Requires: PostgreSQL with IMDb data and hypopg extension
cd backend
python -m eval.run_eval
```

Each query produces a JSON result in `eval/results/`:

```json
{
  "query": "10a",
  "status": "success",
  "b1_cost": 514234.57,
  "surgeon_cost": null,
  "issues": ["Sequential scan on cast_info...", "..."],
  "advice": ["Create a GIN index on cast_info.note...", "..."],
  "optimized_sql": "-- Step 1: Create indexes\n...\n-- Step 2: Run query\n...",
  "retry_count": 0,
  "verdict": "pass",
  "hit_max_retry": false,
  "error": null,
  "timestamp": "2026-06-06T17:38:14.531441"
}
```

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Agent orchestration | LangGraph + LangChain |
| LLM | Google Gemini 2.5 Pro |
| Backend API | FastAPI + Uvicorn |
| Database | PostgreSQL (via psycopg2) |
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
│   │   └── main.py         # FastAPI app, /api/diagnose + /api/health endpoints
│   ├── db/
│   │   └── client.py       # DBClient: execute_explain + benchmark_in_sandbox
│   ├── eval/
│   │   ├── run_eval.py     # Eval harness: L1/L3 metrics, B1 baseline, hypopg
│   │   └── results/        # Per-query JSON results
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
├── requirements.txt
└── .env                    # DATABASE_URL + GOOGLE_API_KEY (not committed)
```

---

## Getting Started

### Prerequisites

- PostgreSQL instance (local or remote)
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
  "benchmark_result": [...],
  "error": null
}
```

The `optimized_sql` field is a complete, copy-pasteable script you can run directly in DBeaver or psql. When `table_name` is set, `benchmark_result` contains an `EXPLAIN ANALYZE` result from the sandbox benchmark (indexes applied, ≤100k sampled rows).

### `GET /api/health`

```json
{ "status": "healthy", "engine": "SQL-Surgeon backend is running" }
```

---

## What SQL Surgeon is NOT

- Not a Text-to-SQL tool — it tunes queries you already have
- Not a black box — every issue and recommendation is explained
- Not production automation — it suggests; you decide when to apply

---

## License

MIT
