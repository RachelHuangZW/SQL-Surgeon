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
                                                         |  pass
                                              benchmark (optional)
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
| `generate_benchmark` | (Optional) Runs the original and optimized query in a sandboxed schema to measure actual improvement |

If `review_advice` returns `retry`, the feedback is fed back into `identify_issues` and the loop runs again (max 2 retries).

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Agent orchestration | LangGraph + LangChain |
| LLM | Google Gemini 2.5 Pro |
| Backend API | FastAPI + Uvicorn |
| Database | PostgreSQL (via psycopg2) |
| Frontend | Next.js 14 (App Router) + Tailwind CSS |

---

## Project Structure

```
SQL-Surgeon/
├── backend/
│   ├── agent/
│   │   ├── state.py        # AgentState TypedDict (shared blackboard)
│   │   ├── prompts.py      # ANALYSIS, ADVICE, REVIEW_ADVICE prompts
│   │   ├── nodes.py        # All 5 graph node functions
│   │   └── graph.py        # LangGraph StateGraph definition + routing logic
│   ├── api/
│   │   └── main.py         # FastAPI app, /api/diagnose endpoint
│   ├── db/
│   │   └── client.py       # DBClient: execute_explain + benchmark_in_sandbox
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
  "benchmark_result": null,
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
