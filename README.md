# SQL Surgeon

**An agentic SQL tuning tool powered by LLMs and real PostgreSQL execution plan analysis.**

SQL Surgeon takes a slow query + table DDL, runs `EXPLAIN ANALYZE` against your database, and uses an LLM-driven agent loop to identify bottlenecks, generate optimization advice, and produce a ready-to-run index + query script — with a self-reflection step to verify quality before returning results.

---

## How It Works

SQL Surgeon is built as a **LangGraph state machine**. The graph has a built-in review-and-retry loop so the agent can catch and correct low-quality advice before surfacing it. When `run_benchmark: true` is passed via the API, the graph also runs a live sandbox benchmark to validate the optimized SQL.

```
run_explain → identify_issue → generate_advice → review_advice
                                      ↑                 |
                                      └── retry (max 2) ┤
                                                         |
                                          (run_benchmark: true)
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
| `generate_advice` | LLM produces concrete recommendations + a two-step executable script (CREATE INDEX + optimized query). If `review_advice` returned feedback from a prior retry, that feedback is injected into this prompt so the LLM directly addresses the reviewer's criticisms. |
| `review_advice` | LLM acts as a senior DBA reviewer — returns `pass` or `retry` with feedback |
| `generate_benchmark_schema` | Creates an isolated schema, copies ≤100k rows from all tables referenced in the query, applies suggested indexes, runs `EXPLAIN ANALYZE`, then drops the schema |

**Retry flow:** If `review_advice` returns `retry`, feedback routes back to `generate_advice` (not `identify_issue`). The diagnosis is not re-run — only the advice generation step is retried, with the reviewer's feedback included in the prompt. Max 2 retries.

**Extension dependency injection:** Before returning `optimized_sql`, `generate_advice` scans the generated DDL for operator classes like `gin_trgm_ops` or `btree_gin`. If found, the corresponding `CREATE EXTENSION IF NOT EXISTS` statement is automatically prepended to the script. This prevents the user from hitting missing-extension errors when running the output.

---

## Agent State

The shared state (`AgentState` TypedDict) passed between nodes:

| Field | Type | Description |
|-------|------|-------------|
| `original_sql` | str | Input query |
| `ddl` | str | Table DDL |
| `run_benchmark` | bool | Whether to run sandbox benchmark after advice passes review |
| `explain_output` | list[dict] | EXPLAIN ANALYZE JSON plan |
| `issues` | list[str] | Identified bottlenecks |
| `advice` | list[str] | Human-readable recommendations |
| `optimized_sql` | str | Two-step DDL + query script |
| `feedback` | str | Reviewer's feedback if verdict was `retry` |
| `verdict` | str | `pass` or `retry` |
| `retry_count` | int | Number of retries so far |
| `benchmark_result` | list[dict] | EXPLAIN ANALYZE from sandbox benchmark |
| `error` | str | First error encountered |

---

## Evaluation Framework

SQL Surgeon includes an offline evaluation harness benchmarked on the **Join Order Benchmark (JOB)** — 113 analytical queries over the IMDb dataset (~12M rows in `cast_info`).

### Design: Three Eval Paths

#### Path 1: `generate_benchmark_schema` (in-graph, user-facing)

- **When:** Final node in the agent pipeline, after advice passes review
- **Triggered by:** API request with `run_benchmark: true`
- **What it does:** Creates a temp schema (`surgeon_tmp_<timestamp>`), copies ≤100k rows from all query-referenced tables, applies surgeon's DDL, runs `EXPLAIN ANALYZE`, drops schema in `finally`
- **Output:** Full `EXPLAIN ANALYZE` JSON plan returned as `benchmark_result`
- **Limitation:** Slow (seconds per query), not suitable for batch eval

#### Path 2: `evaluate_with_hypopg` (offline eval harness, developer-facing)

- **When:** Offline, called from `eval/run_eval.py`
- **What it does:** Uses [hypopg](https://hypopg.readthedocs.io/) — a PostgreSQL extension that registers hypothetical (in-memory) indexes. The planner sees them during `EXPLAIN` without any index actually existing on disk.
- **Speed:** Milliseconds per query — feasible for 113 queries
- **Critical limitation:** hypopg only supports B-tree indexes. GIN/GiST/BRIN are not supported.

#### Path 3: `eval_surgeon_gin` + `oracle_greedy_gin` (GIN-specific, real DB)

Because hypopg cannot simulate GIN indexes, a third path handles queries where the surgeon recommends GIN:

- **What it does:** Creates real GIN indexes on the real DB, runs `EXPLAIN` only (no ANALYZE), checks which indexes appear in the plan, drops them in `finally`
- **Why real DB, not sandbox:** A 100k-row sandbox has wrong planner statistics for GIN decisions. PostgreSQL's planner decides to use GIN based on `pg_class.reltuples` and `pg_statistic`. On the real 12M-row table it correctly chooses GIN; on a 100k sandbox it often prefers a sequential scan.

### Combined Oracle (`oracle_greedy_combined`)

Beyond the independent B-tree and GIN oracles, the harness implements a **combined oracle** that finds the optimal mix of both index types in a single greedy search:

- Each greedy round picks the best index across all B-tree and GIN candidates simultaneously
- Committed B-tree indexes are held as hypopg hypotheticals (re-passed each round)
- Committed GIN indexes are created as real indexes on the DB so the planner sees them alongside hypopg's hypotheticals during the same `EXPLAIN` call
- All real GIN indexes are dropped in `finally`

This produces `b2_combined_cost`, `b2_combined_btree_indexes`, and `b2_combined_gin_indexes` — the true oracle ceiling.

The surgeon's combined cost (`surgeon_combined_cost`) is evaluated the same way: real GIN indexes on DB + surgeon's B-tree DDLs as hypopg hypotheticals, then a single `EXPLAIN` call.

---

## Metrics

### Cost Unit

All `*_cost` fields are **PostgreSQL planner cost estimates** — a unitless internal number, not milliseconds or bytes. The planner computes it from configurable parameters:

| Parameter | Default | Meaning |
|-----------|---------|---------|
| `seq_page_cost` | 1.0 | Cost of reading one page sequentially |
| `random_page_cost` | 4.0 | Cost of a random page read |
| `cpu_tuple_cost` | 0.01 | Cost of processing one row |
| `cpu_index_tuple_cost` | 0.005 | Cost of processing one index entry |
| `cpu_operator_cost` | 0.0025 | Cost of one operator evaluation |

These defaults vary by PostgreSQL version. The eval in this repo was run on **PostgreSQL 17** with default cost parameters (no tuning). Cost numbers from a different version or with tuned `pg_settings` will not be directly comparable.

```sql
SELECT name, setting FROM pg_settings
WHERE name IN ('seq_page_cost','random_page_cost','cpu_tuple_cost','cpu_index_tuple_cost','cpu_operator_cost');
```

### Baseline Metrics (B1, B2)

| Symbol | Meaning |
|--------|---------|
| `b1_cost` | Planner cost with no indexes — the unoptimized floor |
| `b2_btree_cost` | Greedy oracle using only B-tree indexes (via hypopg) |
| `b2_gin_cost` | Greedy oracle using only GIN indexes (via real DB) |
| `b2_combined_cost` | Greedy oracle using the optimal mix of B-tree + GIN |

### Surgeon Metrics

| Symbol | Meaning |
|--------|---------|
| `surgeon_btree_cost` | Planner cost with surgeon's B-tree indexes (via hypopg) |
| `surgeon_gin_cost` | Planner cost with surgeon's GIN indexes (via real DB) |
| `surgeon_combined_cost` | Planner cost with surgeon's full index set (real GIN + hypopg B-tree) |
| `surgeon_gin_indexes` | GIN DDL statements surgeon recommended |

### Index Quality (L2)

| Symbol | Meaning |
|--------|---------|
| `l2_btree` | Precision/recall/F1 of surgeon's B-tree indexes vs. `b2_btree_indexes` |
| `l2_combined` | Precision/recall/F1 of surgeon's full index set vs. `b2_combined` oracle |

L2 compares (table, column) pairs between surgeon output and oracle output. A surgeon index on `cast_info(movie_id)` matches the oracle's `cast_info(movie_id)` regardless of index name.

### Agent Health (L3)

| Symbol | Meaning |
|--------|---------|
| `retry_count` | How many times the review loop fired |
| `verdict` | Final review verdict (`pass` / `retry`) |
| `hit_max_retry` | Whether the agent hit the 2-retry ceiling |

---

## The B2 Oracle: Greedy Algorithm Design

### What "oracle" means

"Oracle" is a standard term in ML evaluation — a baseline with perfect information that measures the best achievable outcome. The oracle tries every possible option; the surgeon can only reason about likely options.

### The greedy algorithm

```
candidates = all (table, column) pairs from the query's tables
selected = []
current_cost = b1_cost

Each round:
  try each remaining candidate (on top of all already-selected)
  pick the one that reduces cost the most → add to selected
  repeat until no candidate improves cost
```

### Is greedy optimal?

No. The Index Selection Problem (ISP) is NP-hard — with N candidates, the exact optimum requires evaluating all 2^N subsets. Greedy is the industry standard approximation used by Microsoft DTA, IBM DB2 Design Advisor, and others.

For submodular cost functions (which index cost reduction approximately satisfies), greedy achieves at least (1 - 1/e) ≈ 63% of optimal. In practice it gets much closer because index effects are mostly independent.

ILP would give an exact solution if the cost function were linear — but the PostgreSQL planner is a black box queryable only via `EXPLAIN`, not a linear function. Brute force is infeasible at N=50+ candidates.

**What b2 actually guarantees:** b2 is an upper bound on what greedy can achieve, not a theoretical ceiling. The surgeon approaching b2 means the surgeon is at or near greedy-optimal — the true optimum could be even lower.

---

## Index Types Covered

| Index type | B2 oracle covers? | Notes |
|------------|------------------|-------|
| B-tree (single column) | ✓ | `oracle_greedy_btree()` via hypopg |
| GIN (trgm) | ✓ | `oracle_greedy_gin()` via real DB |
| B-tree + GIN combined | ✓ | `oracle_greedy_combined()` |
| B-tree composite / partial / covering | ✗ | Search space explodes; not implemented |
| BRIN | ✗ | Potentially useful for `production_year`; not implemented |
| GiST / SP-GiST / Hash | ✗ | Not applicable to IMDb schema |

---

## Eval Infrastructure

### Result Versioning

Each eval run generates a 6-character `RUN_ID` at module load time (e.g., `xkqfmb`). All result files for that run are written to `eval/results/{RUN_ID}/`. This makes it safe to run evals concurrently and enables A/B comparison between prompt versions, model changes, or code changes.

The `RUN_ID` is also embedded in oracle GIN index names (e.g., `_b2gin_xkqfmb_cast_info_note`) to prevent collisions between concurrent eval processes on the shared database.

### Structured Logging

Each per-query result JSON includes:
- `run_id` — which run this belongs to
- `git_commit` — short SHA of HEAD at eval time
- `elapsed_seconds` — wall-clock seconds for the full query eval
- `peak_memory_mb` — peak Python heap usage via `tracemalloc`

A `summary.json` is saved alongside the per-query files with run-level metadata.

### Three-Layer Timeout Protection

| Layer | Mechanism | Limit | Guards against |
|-------|-----------|-------|---------------|
| LLM HTTP | `timeout=60` in LangChain LLM init | 60s | Gemini API hangs |
| PostgreSQL statement | `SET statement_timeout = '10min'` | 10 min | Runaway SQL during oracle search |
| Python signal | `signal.SIGALRM` + `conn.cancel()` | 20 min | Full eval loop hang (entire query processing) |

The signal layer catches cases the statement timeout can't — Python-side hangs, library bugs, non-SQL blocking calls. On timeout, `conn.cancel()` sends a cancellation to PostgreSQL, then `conn.rollback()` is called before moving to the next query. The run continues; the timed-out query is recorded with `status: "timeout"`.

### Resume Support

If a result file already exists at `eval/results/{RUN_ID}/{query_name}.json`, that query is skipped. Interrupted runs can be resumed without re-processing completed queries.

### Summarize and Compare

```bash
# Summarize the most recent run
cd backend && python -m eval.summarize

# Summarize a specific run
cd backend && python -m eval.summarize xkqfmb

# Regression test: compare two runs
cd backend && python -m eval.compare <baseline_run_id> <current_run_id>
```

`summarize.py` outputs a per-query table with `b1_cost`, `surgeon_combined_cost`, `b2_combined_cost`, cost reduction %, `l2_combined` F1, elapsed time, verdict, and retry count. Aggregate stats use **median + IQR** (robust to skewed distributions).

`compare.py` computes median cost reduction, median L2 F1, and success rate for both runs. Any metric degrading by more than 5% triggers `REGRESSION DETECTED` and exits with code 1.

---

## Sample Result: JOB Query 10a

```json
{
  "query": "10a",
  "run_id": "xkqfmb",
  "git_commit": "70c832b",
  "elapsed_seconds": 47.3,
  "peak_memory_mb": 12.1,
  "status": "success",
  "b1_cost": 514132.26,
  "b2_btree_cost": 41218.6,
  "b2_gin_cost": 100651.33,
  "b2_combined_cost": 38900.0,
  "surgeon_btree_cost": 43828.58,
  "surgeon_gin_cost": 101545.64,
  "surgeon_combined_cost": 41200.0,
  "l2_combined": {"precision": 0.857, "recall": 0.857, "f1": 0.857},
  "retry_count": 0,
  "verdict": "pass"
}
```

**Interpretation:**

| Metric | Value | Meaning |
|--------|-------|---------|
| `b1_cost` | 514,132 | No indexes — full sequential scans across all tables |
| `b2_combined_cost` | ~38,900 | Best achievable with optimal B-tree + GIN mix |
| `surgeon_combined_cost` | ~41,200 | Surgeon's full index set — ~6% from oracle |
| L2 F1 | 0.857 | 6 of 7 oracle index columns matched |

---

## JOB Benchmark Query Characteristics

JOB queries have systematic patterns that affect pipeline behavior across all 113 queries.

**Implicit comma-join syntax:** JOB queries use SQL-89 style (`FROM a, b, c WHERE a.id = b.id`). The surgeon consistently rewrites these to explicit `INNER JOIN ... ON` syntax. The eval's `get_table_names()` regex handles comma-separated table lists explicitly.

**Leading wildcard LIKE (`LIKE '%pattern%'`):** Many queries filter text columns with arbitrary patterns. B-tree indexes are useless here. The surgeon correctly identifies this and recommends GIN with `pg_trgm`. Whether the planner actually uses the GIN index depends on selectivity estimates — low-selectivity patterns may still trigger a sequential scan.

**Constant selectivity:** Some queries use constants that are highly selective (`production_year > 2005`) or nearly non-selective (`production_year > 1900`). The pipeline doesn't pre-process these — the EXPLAIN output implicitly captures the planner's estimates, and surgeon recommendations are conditioned on what EXPLAIN reveals.

---

## Known Limitations

### 1. b1_cost instability (GIN statistics pollution)

Creating real GIN indexes (for oracle search) triggers PostgreSQL to compute trigram frequencies and write them to `pg_statistic`. These persist after `DROP INDEX`. A subsequent `b1_cost` measurement (no indexes) may be lower than the first because the planner now has better selectivity estimates for LIKE filters, even though no index exists.

This makes `b1_cost` non-reproducible across separate runs that both used GIN oracle search. It's a known limitation, not fixable without resetting `pg_statistic`.

### 2. Greedy oracle is approximate, not globally optimal

As described above: ISP is NP-hard, b2 values are greedy upper bounds, not theoretical ceilings.

### 3. hypopg does not support GIN/GiST/BRIN

`surgeon_btree_cost` underestimates surgeon quality for LIKE-heavy queries. `surgeon_combined_cost` is the correct metric to use when the surgeon recommends GIN indexes.

### 4. eval measures planner cost, not wall-clock time

All cost values are PostgreSQL's internal planner estimates. Actual runtime requires `EXPLAIN ANALYZE` against 12M rows — expensive and not included in the offline eval.

### 5. Sandbox benchmark (Path 1) uses 100k-row sample

The user-facing `generate_benchmark_schema` copies only ≤100k rows. For LIKE queries on large text columns, GIN index adoption may not be visible in the sandbox because planner statistics differ from the real table.

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Agent orchestration | LangGraph + LangChain |
| LLM | Google Gemini 2.5 Pro (`temperature=0.0`, `timeout=60`) |
| Backend API | FastAPI + Uvicorn |
| Database | PostgreSQL 17 (via psycopg2) |
| Hypothetical indexes | hypopg |
| Text search indexes | pg_trgm |
| Evaluation dataset | Join Order Benchmark (JOB) over IMDb |
| Frontend | Next.js 14 (App Router) + Tailwind CSS |

---

## Project Structure

```
SQL-Surgeon/
├── backend/
│   ├── agent/
│   │   ├── state.py        # AgentState TypedDict
│   │   ├── prompts.py      # ANALYSIS_PROMPT, ADVICE_PROMPT, REVIEW_ADVICE_PROMPT
│   │   ├── nodes.py        # All graph node functions + inject_extension_deps
│   │   └── graph.py        # LangGraph StateGraph + routing logic
│   ├── api/
│   │   └── main.py         # FastAPI app — /api/diagnose + /api/health
│   ├── db/
│   │   └── client.py       # DBClient: execute_explain + benchmark_in_sandbox
│   ├── eval/
│   │   ├── run_eval.py     # Eval harness: B1/B2/combined baselines + surgeon metrics
│   │   ├── summarize.py    # Per-run summary table + aggregate stats
│   │   ├── compare.py      # Regression test between two runs
│   │   └── results/        # {RUN_ID}/ subdirs with per-query JSON + summary.json
│   └── test_agent.py       # End-to-end smoke test (bypasses API)
├── frontend/
│   ├── app/
│   │   ├── page.tsx
│   │   └── layout.tsx
│   ├── components/
│   │   ├── InputPanel.tsx
│   │   └── ResultPanel.tsx
│   └── lib/
│       └── api.ts
├── docs/
│   ├── eval_design.md
│   ├── timeout_design.md   # Three-layer timeout design rationale
│   └── pg_index_types.md
├── requirements.txt
└── .env                    # DATABASE_URL + GOOGLE_API_KEY (not committed)
```

---

## Getting Started

### Prerequisites

- PostgreSQL 17 instance with `hypopg` and `pg_trgm` extensions installed
- Google Gemini API key (paid tier for `gemini-2.5-pro`)
- Python 3.12+, Node.js 18+

### 1. Clone and install

```bash
git clone https://github.com/RachelHuangZW/SQL-Surgeon.git
cd SQL-Surgeon
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cd frontend && npm install
```

### 2. Configure environment

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

### 4. Run eval harness

```bash
# Requires: PostgreSQL with IMDb data loaded, hypopg + pg_trgm extensions
# Queries from: ~/join-order-benchmark/*.sql
cd backend
python -m eval.run_eval

# View results
python -m eval.summarize

# Compare two runs
python -m eval.compare <baseline_run_id> <current_run_id>
```

Results are written to `eval/results/{RUN_ID}/`.

---

## API

### `POST /api/diagnose`

**Request:**
```json
{
  "original_sql": "SELECT * FROM orders WHERE status = 'pending' ORDER BY created_at DESC LIMIT 100",
  "ddl": "CREATE TABLE orders (id SERIAL PRIMARY KEY, status TEXT, created_at TIMESTAMPTZ);",
  "run_benchmark": false
}
```

**Response:**
```json
{
  "status": "success",
  "issues": ["Sequential scan on orders — no index on status column"],
  "advice": ["Create a composite index on (status, created_at DESC)"],
  "optimized_sql": "-- Step 1: Create indexes (run once)\nCREATE INDEX ...\n\n-- Step 2: Run the optimized query\nSELECT ...",
  "explain_output": [...],
  "benchmark_result": null,
  "error": null
}
```

When `run_benchmark: true`, `benchmark_result` contains an `EXPLAIN ANALYZE` result from the sandbox benchmark (all tables in the query are copied to the sandbox schema).

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
