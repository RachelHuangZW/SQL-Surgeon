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

SQL Surgeon includes an offline evaluation harness benchmarked on the **Join Order Benchmark (JOB)** — 113 analytical queries over the IMDb dataset (~12M rows in `cast_info`).

### Design: Two Parallel Eval Paths

The eval framework has two distinct mechanisms. They look similar ("would this index help?") but serve different audiences and run in completely different contexts.

#### Path 1: `generate_benchmark_schema` (in-graph, user-facing)

- **When:** Final node in the agent pipeline, after the surgeon passes review
- **Triggered by:** User submitting a query with `table_name` via the API
- **What it does:** Creates a temp schema (`surgeon_tmp_<timestamp>`), copies ≤100k rows, applies surgeon's DDL, runs `EXPLAIN ANALYZE`, drops schema in `finally`
- **Output:** Full `EXPLAIN ANALYZE` JSON plan returned to user as `benchmark_result`
- **Limitation:** Slow (seconds per query), not suitable for batch eval over 113 queries

#### Path 2: `evaluate_with_hypopg` (offline eval harness, developer-facing)

- **When:** Offline, called from `main()` in `eval/run_eval.py`
- **Triggered by:** `python -m eval.run_eval`
- **What it does:** Uses [hypopg](https://hypopg.readthedocs.io/) — a PostgreSQL extension that registers hypothetical (in-memory) indexes. The planner sees them during `EXPLAIN` without any index actually existing on disk.
- **Speed:** Milliseconds per query — feasible for 113 queries
- **Critical limitation:** hypopg only supports B-tree indexes. GIN, GiST, BRIN are not implemented in hypopg. Passing `CREATE INDEX ... USING gin` to `hypopg_create_index` raises an error.

#### Path 3: `eval_gin_adoption` (GIN-specific, real DB)

Because hypopg cannot simulate GIN indexes, a third path was added for queries where the surgeon recommends GIN:

- **What it does:** Creates real GIN indexes on the real DB, runs `EXPLAIN` only (no ANALYZE), checks which indexes appear in the plan, drops them in `finally`
- **Why not sandbox:** A 100k-row sandbox has wrong planner statistics for GIN decisions. PostgreSQL's planner decides to use a GIN index based on `pg_class.reltuples` and `pg_statistic`. On a 100k sandbox, the planner often prefers sequential scan. On the real 12M-row table, it correctly chooses GIN. We need real table statistics.
- **Why EXPLAIN not ANALYZE:** We don't want to actually execute the query — just check if the planner *would use* the index.
- **Output:** `(gin_cost, adopted_indexes)` — cost with GIN applied, and list of index names the planner chose to use

### Why They Can't Be Merged

| | `generate_benchmark_schema` | `evaluate_with_hypopg` |
|--|--|--|
| Audience | End user | Developer / researcher |
| Runs in | Agent graph (production) | Offline eval script |
| Data | User's real data (≤100k rows copied) | No data — planner memory only |
| Execution | `EXPLAIN ANALYZE` (query actually runs) | `EXPLAIN` (estimate only) |
| Requires hypopg | No | Yes |
| Speed | Seconds per query | Milliseconds per query |

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

To reproduce: verify your settings with:
```sql
SELECT name, setting FROM pg_settings
WHERE name IN ('seq_page_cost','random_page_cost','cpu_tuple_cost','cpu_index_tuple_cost','cpu_operator_cost');
```

### Baseline Metrics (B1, B2)

| Symbol | Meaning | Implementation |
|--------|---------|---------------|
| `b1_cost` | Planner cost, no indexes — the floor | `get_explain_cost()` |
| `b2_btree_cost` | Oracle ceiling using only B-tree indexes | `greedy_oracle_b2()` via hypopg |
| `b2_gin_cost` | Oracle ceiling using only GIN indexes | `greedy_oracle_b2_gin()` via real DB |

### Surgeon Metrics

| Symbol | Meaning | Implementation |
|--------|---------|---------------|
| `surgeon_btree_cost` | Planner cost with surgeon's B-tree indexes applied | `evaluate_with_hypopg()` |
| `surgeon_gin_cost` | Planner cost with surgeon's GIN indexes applied (real DB) | `eval_gin_adoption()` |
| `surgeon_gin_indexes` | List of GIN DDL statements surgeon recommended | extracted from `optimized_sql` |

### Agent Health (L3)

| Symbol | Meaning |
|--------|---------|
| `retry_count` | How many times the review loop fired |
| `verdict` | Final review verdict (`pass` / `retry`) |
| `hit_max_retry` | Whether the agent hit the 2-retry ceiling |

### What's Missing: Combined (GIN + B-tree) Cost

Currently `b2_btree_cost` and `b2_gin_cost` are measured independently. The true optimal would combine both — real GIN indexes + hypothetical B-tree indexes simultaneously. This is actually feasible because hypopg hypothetical indexes and real indexes coexist in the PostgreSQL planner. A `b2_combined_cost` implementation would:

1. Create real GIN indexes on the real DB
2. Register B-tree hypothetical indexes via hypopg
3. Run `EXPLAIN` — planner sees both simultaneously
4. Drop the real GIN indexes

This is planned but not yet implemented.

---

## The B2 Oracle: Greedy Algorithm Design

### What "oracle" means

"Oracle" is a standard term in ML evaluation. It refers to a baseline with perfect information — it can try every possible option and always pick the best one. A DBA or the surgeon can only guess which indexes will help. The oracle measures directly and picks the best.

### The greedy algorithm

```
candidates = all (table, column) pairs from the query's tables
selected = []
current_cost = b1_cost

Round 1:
  try each candidate column (via hypopg for btree / real DB for GIN)
  pick the one that reduces cost the most → add to selected

Round 2:
  try each remaining candidate, stacking on top of Round 1's selection
  pick the best again → add to selected

... repeat until no candidate improves cost ...

return current_cost, selected  →  b2_cost, b2_indexes
```

### Why two nested loops

The algorithm has two nested loops for a reason:
- The **inner `for` loop** runs every remaining candidate in a single round to find the one with the highest cost reduction (the "compare all options" step)
- The **outer `while` loop** advances to the next round after committing to the best candidate

A single loop would pick the first candidate that improves cost — not the best. That's sequential selection, not greedy search.

**Concrete example** with 3 candidates and b1_cost = 10,000:

```
Round 1:
  try status      → 3000  ← best this round
  try created_at  → 5000
  try user_id     → 8000
  → select status, current_cost = 3000

Round 2:
  try created_at (+ status) → 800  ← best this round
  try user_id    (+ status) → 2500
  → select created_at, current_cost = 800

Round 3:
  try user_id (+ status + created_at) → 850  ← worse, break

b2_cost = 800, b2_indexes = [status, created_at]
```

### Is greedy optimal? The Index Selection Problem is NP-hard.

**Short answer: No, greedy is not globally optimal. But no better practical algorithm exists.**

The Index Selection Problem (ISP) — finding the minimum-cost index set for a query — is a well-known NP-hard combinatorial optimization problem (established in DB research since the 1980s). No polynomial-time exact algorithm exists.

What "NP-hard" means in practice here: with N candidate columns, the exact optimum requires evaluating all 2^N subsets. At N=50 (realistic for a multi-table join query), that's 2^50 ≈ 10^15 EXPLAIN calls — computationally infeasible.

**Why greedy is still the right choice for this eval:**

1. **It's the industry standard.** Microsoft's Database Tuning Advisor, IBM DB2 Design Advisor, and PostgreSQL's own pg_hint_plan-based tools all use greedy search. Our b2 is methodologically consistent with professional tooling.

2. **The approximation bound is theoretically grounded.** For submodular cost functions (which index cost reduction approximately satisfies), greedy achieves at least (1 - 1/e) ≈ 63% of optimal. In practice, for query optimization, greedy tends to get much closer — index effects are mostly additive (each covers a different part of the query).

3. **The alternatives don't improve the bound enough to justify the cost:**

| Algorithm | Quality | Eval cost |
|-----------|---------|-----------|
| Greedy (current) | Good approximation | O(N² × EXPLAIN_cost) |
| Beam search (width K) | Slightly better | K × greedy cost |
| Simulated annealing | Escapes some local optima | No guarantee, high variance |
| ILP / DP | Exact? | Requires linear cost model — not available (hypopg is a black box) |
| Brute force | Exact | 2^N EXPLAIN calls — infeasible |

ILP (Integer Linear Programming) would give an exact solution *if* the cost function were linear. But the PostgreSQL planner's cost model is a black box we can only query via `EXPLAIN` — it cannot be expressed as a linear objective function. DP similarly requires cost decomposability, which the planner's joint index interactions violate.

**What b2 actually guarantees:** b2 is an upper bound on what greedy can achieve, not a theoretical ceiling. The true optimal could be lower cost (better) than b2. This means our eval is *conservative*: when surgeon_cost approaches b2_cost, the surgeon might already be at or near true optimal.

**Practical implication:** The gap between greedy b2 and true optimal is small for join-heavy analytical queries like JOB (where index effects are largely independent). This is a known and acceptable limitation — it's documented here rather than hidden.

---

## Index Types Covered

See `docs/pg_index_types.md` for the full reference. Summary of what B2 oracle covers:

| Index type | B2 oracle covers? | Notes |
|------------|------------------|-------|
| B-tree (single column) | ✓ | `greedy_oracle_b2()` via hypopg |
| GIN (trgm, text search) | ✓ | `greedy_oracle_b2_gin()` via real DB |
| B-tree composite / partial / covering | ✗ | Search space explodes; not implemented |
| BRIN | ✗ | Potentially useful for `production_year`; not implemented |
| GiST / SP-GiST / Hash | ✗ | Not applicable to IMDb schema |

---

## Sample Result: JOB Query 10a

```json
{
  "query": "10a",
  "status": "success",
  "b1_cost": 514132.26,
  "b2_btree_cost": 41218.6,
  "b2_btree_indexes": [
    "CREATE INDEX ON cast_info(movie_id);",
    "CREATE INDEX ON title(id);",
    "CREATE INDEX ON company_name(country_code);",
    "CREATE INDEX ON role_type(id);",
    "CREATE INDEX ON char_name(id);",
    "CREATE INDEX ON company_type(id);"
  ],
  "b2_gin_cost": 100651.33,
  "b2_gin_indexes": [
    "CREATE INDEX _b2gin_cast_info_note ON cast_info USING gin (note gin_trgm_ops);"
  ],
  "surgeon_btree_cost": 43828.58,
  "surgeon_gin_cost": 101545.64,
  "surgeon_gin_indexes": [
    "CREATE INDEX idx_ci_note_trgm ON cast_info USING gin (note gin_trgm_ops);"
  ],
  "retry_count": 0,
  "verdict": "pass"
}
```

**Interpretation:**

| Metric | Value | Meaning |
|--------|-------|---------|
| `b1_cost` | 514,132 | No indexes — full sequential scans across all tables |
| `b2_btree_cost` | 41,219 | Best achievable with only B-tree indexes (6 join column indexes) |
| `b2_gin_cost` | 100,651 | Best achievable with only GIN (cast_info.note trgm) |
| `surgeon_btree_cost` | 43,829 | Surgeon's B-tree indexes — **6% from oracle** |
| `surgeon_gin_cost` | 101,546 | Surgeon's GIN index — **<1% from oracle** |

The surgeon performed nearly optimally on both dimensions independently. The true optimal (combining both) is unmeasured but likely lower than 41k, since the GIN filter on `cast_info.note` reduces row count before the B-tree join indexes kick in.

**What the surgeon recommended for 10a:**
- `CREATE INDEX idx_ci_note_trgm ON cast_info USING gin (note gin_trgm_ops)` — correct, matches b2_gin oracle
- `CREATE INDEX idx_mc_movie_id ON movie_companies (movie_id)` — correct, in b2_btree oracle
- `CREATE INDEX idx_ci_movie_id ON cast_info (movie_id)` — correct, in b2_btree oracle
- `CREATE INDEX idx_cn_country_code ON company_name (country_code)` — correct, in b2_btree oracle
- Rewrote implicit comma-join syntax to explicit JOINs
- Reordered joins to apply most selective filters first

---

## JOB Benchmark Query Characteristics

JOB queries have several quirks that affect how the pipeline behaves. These are systematic patterns across all 113 queries, not isolated cases.

**Implicit comma-join syntax**

JOB queries use the old SQL-89 style:
```sql
FROM cast_info ci, title t, movie_companies mc
WHERE ci.movie_id = t.id AND ...
```

The eval pipeline's `get_table_names()` regex explicitly handles comma-separated table lists. The surgeon systematically rewrites these to explicit `INNER JOIN ... ON` syntax — this is a consistent behavior across all queries, not just 10a.

**Leading wildcard LIKE (`LIKE '%pattern%'`)**

Many JOB queries filter on text columns with patterns like `ci.note LIKE '%(voice)%'`. B-tree indexes are useless here — they require a fixed prefix. GIN with `pg_trgm` is the correct tool for arbitrary trigram matching.

The surgeon correctly identifies this: when EXPLAIN shows a sequential scan on a text column with a LIKE filter, it recommends GIN. The planner's decision to actually *use* the GIN index depends on cost estimates — it may still prefer a sequential scan if it estimates the filter is not selective enough.

**Constant selectivity**

Some queries use constants that are highly selective (`production_year > 2005`) or nearly non-selective (`production_year > 1900`). The pipeline does not pre-process these. The EXPLAIN output implicitly captures the planner's selectivity estimates, and the surgeon's recommendations are conditioned on what EXPLAIN reveals. If a constant makes an index useless, the planner won't use it, EXPLAIN won't show it, and the surgeon won't recommend it.

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Agent orchestration | LangGraph + LangChain |
| LLM | Google Gemini 2.5 Pro (temperature=0.0 for eval reproducibility) |
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
│   │   ├── run_eval.py     # Eval harness: B1/B2 baselines, surgeon metrics
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
├── docs/
│   ├── eval_design.md      # Deep dive on the three eval paths and their trade-offs
│   ├── eval_progress.md    # Task tracking
│   └── pg_index_types.md   # PostgreSQL index type reference (B-tree, GIN, BRIN, etc.)
├── requirements.txt
└── .env                    # DATABASE_URL + GOOGLE_API_KEY (not committed)
```

---

## Getting Started

### Prerequisites

- PostgreSQL instance (local or remote) with hypopg and pg_trgm extensions
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
# Requires: PostgreSQL with IMDb data, hypopg + pg_trgm extensions
# Queries from: ~/join-order-benchmark/*.sql
cd backend
python -m eval.run_eval
```

Each query produces a JSON in `eval/results/<query_name>.json`.

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

`optimized_sql` is a complete, copy-pasteable script. When `table_name` is set, `benchmark_result` contains an `EXPLAIN ANALYZE` result from the sandbox benchmark.

### `GET /api/health`

```json
{ "status": "healthy", "engine": "SQL-Surgeon backend is running" }
```

---

## Known Limitations and Open Problems

### 1. b2_combined_cost not yet measured

`b2_btree_cost` and `b2_gin_cost` are measured independently. Their combination (real GIN + hypothetical B-tree simultaneously) is feasible but not yet implemented. For queries with both LIKE filters and join columns (like 10a), the true oracle is likely significantly better than either individual b2.

### 2. Greedy oracle is approximate, not globally optimal

As described above: ISP is NP-hard, greedy is the industry standard approximation. The b2 values are greedy upper bounds, not theoretical ceilings. The true optimal could be lower (better).

### 3. hypopg does not support GIN/GiST/BRIN

This means surgeon_btree_cost underestimates surgeon quality for LIKE-heavy queries. The `surgeon_gin_indexes` field flags this: a non-empty list means the surgeon's true impact is better than `surgeon_btree_cost` alone suggests.

### 4. eval measures planner cost, not wall-clock time

All cost values are PostgreSQL's internal planner estimates (unit-less). Actual query runtime requires `EXPLAIN ANALYZE`, which executes the full query against 12M rows. This is expensive and not currently included in the eval harness.

### 5. L2 index quality metrics not yet implemented

Precision and recall of surgeon's index recommendations vs. b2 oracle indexes — planned but not started.

---

## What SQL Surgeon is NOT

- Not a Text-to-SQL tool — it tunes queries you already have
- Not a black box — every issue and recommendation is explained
- Not production automation — it suggests; you decide when to apply

---

## License

MIT
