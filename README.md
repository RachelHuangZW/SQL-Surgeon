# SQL Surgeon

**An agentic SQL tuning tool powered by LLMs and real PostgreSQL execution plan analysis.**

SQL Surgeon takes a slow query (+ optional DDL), runs `EXPLAIN ANALYZE` against your database in a read-only, time-limited transaction, and uses an LLM-driven agent loop to identify bottlenecks, generate optimization advice, and produce a ready-to-run index + query script — with a review step that checks the advice before returning it.

The LLM is never trusted on its own. Deterministic code around it pinpoints where the planner's row estimates go wrong, keeps index suggestions off tiny tables, and checks every recommended index against the real schema (with hypopg) before anything reaches the user.

The pipeline ships as the `sql_surgeon` Python package, which the [sql-surgeon-mcp](https://github.com/RachelHuangZW/sql-surgeon-mcp) server also uses to expose it to Claude.

---

## How It Works

SQL Surgeon is built as a **LangGraph state machine** with a review-and-retry loop. When `run_benchmark: true` is passed via the API, the graph also runs a sandbox benchmark of the suggested indexes.

```
preprocess_sql → rewrite_sql → run_explain → identify_issue → generate_advice → review_advice
                                                                    ↑                 │
                                                                    └──── retry ──────┤
                                                                                      │
                                                       (run_benchmark: true) ─────────┤
                                                                ↓                     ↓
                                                   generate_benchmark_schema → END   END
```

**Node-by-node:**

| Node | What it does |
|------|-------------|
| `preprocess_sql` | Rewrites comma joins (`FROM a, b WHERE a.id = b.id`) into explicit `JOIN ... ON` as `normalized_sql`, **for the LLM to read only**. Every join predicate is kept and the output is deterministic. |
| `rewrite_sql` | Rule-based warnings, no rewriting: `SELECT *`, `EXISTS` subqueries, `DISTINCT` already implied by `GROUP BY` |
| `run_explain` | Runs `EXPLAIN (ANALYZE, COSTS, VERBOSE, BUFFERS, FORMAT JSON)` on the **user's own SQL** (above `join_collapse_limit` = 8 tables, an explicit-JOIN rewrite can get a different plan) in a read-only, time-limited transaction (see [Security](#security)). Then flattens the plan into `plan_table`, runs the [misestimate analysis](#misestimate-analysis), fetches existing indexes (and column definitions, when no DDL was given) for every real table in the query, finds tables under 64 kB, and warns about tables without a primary key. |
| `identify_issue` | LLM reads the plan table, DDL and misestimate analysis, returns specific bottlenecks, each linked to the plan nodes it is about (`issue_node_ids`) |
| `generate_advice` | LLM produces recommendations, index DDL with a reason for each, and a two-step script (`-- Step 1` indexes, `-- Step 2` the query). On a retry, the review feedback is in its prompt. It is **not** given the DDL. |
| `review_advice` | LLM acts as a senior DBA reviewer (`pass` / `retry` + feedback), then the deterministic guarantees below run on its output |
| `generate_benchmark_schema` | Logs in as the low-privilege `sql_surgeon_sandbox` role, copies ≤100k rows of each table in the query into a temporary schema, applies the suggested indexes, runs `EXPLAIN ANALYZE` — all in one transaction that is always rolled back (see [Security](#the-sandbox-benchmark)) |

**Deterministic guarantees in `review_advice`** (applied whatever the LLMs said):

- **No index on a small table.** Tables under `SURGEON_SMALL_TABLE_BYTES` (64 kB) are read in a few pages by a sequential scan, so their indexes are removed from the list and the script, with a warning.
- **No index on a column that doesn't exist.** LLMs invent columns from their training data (e.g. IMDb's `tconst`). Every recommended `CREATE INDEX` is checked with `hypopg_create_index` against the real schema, in the read-only transaction, without building anything. A rejected index sends the advice back to `generate_advice` with PostgreSQL's error and the table's real columns. If no retry is left, it is dropped with a warning. GIN/GiST/BRIN indexes can't be simulated by hypopg and are kept unchecked.
- **`-- Step 3: ANALYZE` only for expression indexes**, where it collects the statistics the planner needs. For a plain column index it would be a placebo.
- **Extension dependencies.** Operator classes like `gin_trgm_ops` / `btree_gin` get their `CREATE EXTENSION IF NOT EXISTS` prepended to the script.

**Retry flow:** a `retry` verdict (from the reviewer or from a rejected index) routes back to `generate_advice`, not `identify_issue`: the diagnosis is not re-run. `retry_count` goes up on each retry verdict, and the loop ends once it reaches `MAX_RETRY_COUNT` (2), so `generate_advice` runs at most twice.

---

## Agent State

The shared state (`AgentState` TypedDict) passed between nodes:

| Field | Type | Description |
|-------|------|-------------|
| `original_sql` | str | Input query |
| `normalized_sql` | str | Same query with explicit JOINs, for the LLM to read only |
| `ddl` | str | Table DDL (user-provided or auto-fetched), plus existing indexes and missing-PK notes |
| `run_benchmark` | bool | Whether to run sandbox benchmark after advice passes review |
| `explain_output` | list[dict] | EXPLAIN ANALYZE JSON plan |
| `plan_table` | list[dict] | The plan flattened to one row per node: estimated vs. actual rows, loops, q-error, processes, conditions |
| `estimate_analysis` | dict | `{"origins": [...], "consequences": [...]}` from the [misestimate analysis](#misestimate-analysis) |
| `seq_scan_analyses` | list[dict] | Per sequential scan: selectivity, rows and a verdict on whether an index could help |
| `small_tables` | dict | Tables under the small-table threshold, with their size |
| `issues` | list[str] | Identified bottlenecks |
| `issue_node_ids` | list[list[int]] | `issue_node_ids[i]` = `plan_table` ids that `issues[i]` is about |
| `advice` | list[str] | Human-readable recommendations |
| `filtered_indexes` | list[dict] | Recommended indexes, `{"ddl", "reason"}`, after review and the deterministic checks |
| `optimized_sql` | str | Script: Step 1 indexes, Step 2 query, Step 3 ANALYZE when needed |
| `rewrite_warnings` | list[str] | Anti-pattern warnings, tables without a PK, dropped indexes and why |
| `feedback` | str | Reviewer's (and the schema check's) feedback if verdict was `retry` |
| `verdict` | str | `pass` or `retry` |
| `retry_count` | int | Number of retry verdicts so far |
| `benchmark_result` | list[dict] | EXPLAIN ANALYZE from sandbox benchmark |
| `total_input_tokens` / `total_output_tokens` | int | LLM token usage across all calls |
| `error` | str | First error encountered |

---

## Misestimate Analysis

Most slow plans are wrong because the planner's **row estimates** are wrong: it expects 10 rows, gets a million, and picks a Nested Loop or an in-memory hash that can't cope. `sql_surgeon/plan/estimates.py` finds these in the plan deterministically and gives the result both to the LLM and to the Diagnosis tab.

**q-error** of a node = how far off the estimate is, `max(estimated / actual, actual / estimated)`, always ≥ 1.

**Origins** — where an estimation error *starts*. Errors propagate upward through the plan, so a bad node is only an origin if:

- its q-error is ≥ 10 and it handled ≥ 1,000 rows (estimated or actual, × loops), and
- its children are accurate (q-error < 2), **or** it is ≥ 10× worse than its worst child (a second, independent source).

Children of a `Merge Join` or `Limit` may be stopped early, so their actual row counts can be partial; an overestimate there isn't trusted (except below a `Sort` / `Hash`, which reads its whole input first).

**Consequences** — slow operations the misestimates caused, reported only if they take ≥ 10% of execution time:

| Type | Detected when |
|------|---------------|
| `hash_spill` | A Hash needed more batches than planned (it spilled to disk) |
| `nested_loop_rescan` | A Nested Loop's inner side ran ≥ 10× more times than planned. Under a `Memoize`, only cache misses count: those are the node below it actually running. |

Time shares account for parallel plans: below a `Gather`, the leader and the launched workers run the subtree at the same time, so `time × loops` is divided by that number of processes.

---

## Evaluation Framework

SQL Surgeon includes an offline evaluation harness benchmarked on the **Join Order Benchmark (JOB)** — 113 analytical queries over the IMDb dataset (36M rows in `cast_info`, 15M in `movie_info`).

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
- **Why real DB, not sandbox:** A 100k-row sandbox has wrong planner statistics for GIN decisions. PostgreSQL's planner decides to use GIN based on `pg_class.reltuples` and `pg_statistic`. On the full-size table it correctly chooses GIN; on a 100k sandbox it often prefers a sequential scan.

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

**Implicit comma-join syntax:** JOB queries use SQL-89 style (`FROM a, b, c WHERE a.id = b.id`). `preprocess_sql` gives the LLM an explicit-JOIN version to read, but `EXPLAIN` always runs the original: 51 of the 113 JOB queries join more than 8 tables, and above `join_collapse_limit` PostgreSQL stops reordering explicit JOINs, so the rewrite could be planned differently. The eval's `get_table_names()` regex handles comma-separated table lists explicitly.

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

All cost values are PostgreSQL's internal planner estimates. Actual runtime requires `EXPLAIN ANALYZE` against the full dataset (36M rows in `cast_info`) — expensive and not included in the offline eval.

### 5. Sandbox benchmark (Path 1) uses 100k-row sample

The user-facing `generate_benchmark_schema` copies only ≤100k rows. For LIKE queries on large text columns, GIN index adoption may not be visible in the sandbox because planner statistics differ from the real table.

### 6. Suggested trigram GIN indexes fail to build in the sandbox

Suggested `pg_trgm` GIN indexes fail to build in the sandbox benchmark because its `search_path` excludes `public` ([#2](https://github.com/RachelHuangZW/SQL-Surgeon/issues/2)).

### 7. GIN/GiST/BRIN recommendations are not checked against the schema

hypopg can only simulate B-tree-like indexes, so a made-up column in a GIN index (roughly 1 in 10 recommendations on JOB) still reaches the user.

### 8. generate_advice writes indexes without seeing the DDL

The review-time schema check catches invented columns, but only after the fact. Giving `generate_advice` the DDL could prevent them; it would change the prompt and the eval baseline, so it hasn't been done yet.

### 9. Eval results predate recent changes

The 30-query runs in `eval/results/` (July–August) predate the database rebuild and the comma-join predicate fix. The latest run (`ysmbdh`, 5 queries, after the small-table filter) predates diagnosing the original SQL, the misestimate analysis and the schema check. A new full baseline is needed before comparing new runs against them.

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Agent orchestration | LangGraph + LangChain |
| LLM | Google Gemini 2.5 Pro (`temperature=0.0`, `timeout=60`) |
| Backend API | FastAPI + Uvicorn |
| Database | PostgreSQL 17 (via psycopg2) |
| Hypothetical indexes | hypopg (eval oracle + schema check of recommended indexes) |
| Text search indexes | pg_trgm |
| Evaluation dataset | Join Order Benchmark (JOB) over IMDb |
| Frontend | Next.js 14 (App Router) + Tailwind CSS |

---

## Project Structure

```
SQL-Surgeon/
├── backend/
│   ├── pyproject.toml      # Packages sql_surgeon/ (installable; used by sql-surgeon-mcp)
│   ├── sql_surgeon/        # The reusable library: agent + plan analysis + DB client
│   │   ├── agent/
│   │   │   ├── state.py    # AgentState TypedDict
│   │   │   ├── prompts.py  # ANALYSIS_PROMPT, ADVICE_PROMPT, REVIEW_ADVICE_PROMPT
│   │   │   ├── nodes.py    # Graph nodes + deterministic checks (small tables, schema check, rewrite)
│   │   │   └── graph.py    # LangGraph StateGraph + routing logic
│   │   ├── plan/
│   │   │   ├── table.py    # Flatten EXPLAIN JSON into per-node rows (q-error, loops, processes)
│   │   │   └── estimates.py # Misestimate origins and consequences
│   │   └── db/
│   │       ├── client.py   # DBClient: hardened read-only query/EXPLAIN, metadata, hypopg index check, sandbox benchmark
│   │       └── config.py   # Security settings from env (read-only DSN, timeouts, pool size)
│   ├── api/
│   │   └── main.py         # FastAPI app — /api/diagnose + /api/health
│   ├── tests/              # Plain scripts; "(DB)" ones need the job database
│   │   ├── test_rewrite.py          # Comma-join rewrite keeps every predicate, deterministic
│   │   ├── test_sql_guards.py       # Single-statement check, query allowlist, benchmark DDL allowlist
│   │   ├── test_db_regression.py    # (DB) Read-only transaction, timeouts, pool, session reset
│   │   ├── test_plan_table.py       # Plan flattening, q-error, processes under Gather
│   │   ├── test_estimates.py        # Origins, consequences, Memoize, parallel time share
│   │   ├── test_small_tables.py     # Small-table filter
│   │   ├── test_analyze_step.py     # Step 3 ANALYZE only for expression indexes
│   │   ├── test_issue_parsing.py    # Issues linked to plan nodes
│   │   ├── test_invalid_indexes.py  # Dropping / feeding back indexes the schema rejects
│   │   ├── test_index_check.py      # (DB) hypopg schema check + review_advice retry
│   │   └── test_sandbox_benchmark.py  # (DB) Sandbox login refusals; escapes denied under sql_surgeon_sandbox
│   ├── scripts/            # Saved JOB 11d plans used as test fixtures, CSV import helper
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
├── scripts/
│   └── setup_security_role.sql  # Creates the sql_surgeon_readonly (and, optionally, sql_surgeon_sandbox) role
├── requirements.txt
└── .env                    # DATABASE_URL + GOOGLE_API_KEY (not committed)
```

---

## Getting Started

### Prerequisites

- PostgreSQL 17 instance with the `pg_trgm` and `hypopg` extensions (`hypopg` is required for the eval harness; without it, the schema check of recommended indexes is skipped)
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

# Recommended: least-privilege connection for running user SQL (see Security)
SURGEON_READONLY_DATABASE_URL=postgresql://sql_surgeon_readonly:password@localhost:5432/yourdb

# Only needed for run_benchmark: true; without it the benchmark refuses to run (see Security)
SURGEON_SANDBOX_DATABASE_URL=postgresql://sql_surgeon_sandbox:password@localhost:5432/yourdb
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

### 5. Run tests

Each test file is a plain script that prints `PASS` / `FAIL` per check and exits non-zero on failure. Most need no database; `test_db_regression.py`, `test_index_check.py` and `test_sandbox_benchmark.py` need the `job` database (with hypopg) and `.env`. `test_sandbox_benchmark.py` skips its last section unless `SURGEON_SANDBOX_DATABASE_URL` is set.

```bash
cd backend
for t in tests/test_*.py; do echo "== $t"; python "$t" | tail -1; done
```

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
  "rewrite_warnings": ["SELECT * detected — list only the columns your application needs ..."],
  "explain_output": [...],
  "plan_table": [{"id": 0, "op": "Limit", "plan_rows": 100, "actual_rows": 100, "loops": 1, "q_error": 1.0, ...}, ...],
  "estimate_analysis": {"origins": [{"id": 3, "q_error": 4210.0, "direction": "under", "child_max_q": 1.0}], "consequences": []},
  "issues": ["Sequential scan on orders — no index on status column"],
  "issue_node_ids": [[3]],
  "advice": ["Create a composite index on (status, created_at DESC)"],
  "filtered_indexes": [{"ddl": "CREATE INDEX idx_orders_status_created ON orders (status, created_at DESC);", "reason": "..."}],
  "execution_time_ms": 812.4,
  "optimized_sql": "-- Step 1: Create indexes (run once)\nCREATE INDEX ...\n\n-- Step 2: Run the optimized query\nSELECT ...",
  "benchmark_result": null,
  "error": null
}
```

`plan_table` ids are what `estimate_analysis` and `issue_node_ids` refer to. The frontend shows the result in tabs: **Summary** (recommendations), **Diagnosis** (the plan table with misestimate origins and consequences marked; clicking an issue highlights its nodes) and **Database changes** (each index with its reason, and the full script to copy). A **SQL changes** tab for query/application-level suggestions is a placeholder for now.

When `run_benchmark: true`, `benchmark_result` contains an `EXPLAIN ANALYZE` result from the sandbox benchmark (all tables in the query are copied to the sandbox schema). It needs `SURGEON_SANDBOX_DATABASE_URL`; without it, `error` says so and `benchmark_result` stays `null`.

### `GET /api/health`

```json
{ "status": "healthy", "engine": "SQL-Surgeon backend is running" }
```

---

## Security

`EXPLAIN ANALYZE` **really executes** the query it analyzes, and in SQL Surgeon that query comes from the user. Treat every input as untrusted SQL running against your database.

### Defense in depth

User SQL goes through `DBClient.execute_explain` ([backend/sql_surgeon/db/client.py](backend/sql_surgeon/db/client.py)). It has to pass several independent layers, so a gap in one is caught by the next:

| # | Layer | What it stops |
|---|-------|---------------|
| 1 | **Single-statement check**: a quote/comment/dollar-quote aware scanner rejects input with more than one statement | `SELECT 1; COMMIT; DROP TABLE t`. psycopg2 uses PostgreSQL's simple query protocol, which runs multiple statements, and a `COMMIT` would end the read-only transaction before the `DROP`. |
| 2 | **Query-only allowlist**: only `SELECT`, `WITH`, `VALUES`, and `TABLE` are accepted | `EXPLAIN ANALYZE CREATE TABLE AS …` / `CREATE MATERIALIZED VIEW …`, which PostgreSQL (verified on 17) does **not** block inside a read-only transaction |
| 3 | **Read-only transaction**: `SET TRANSACTION READ ONLY` before the user SQL runs | `INSERT`/`UPDATE`/`DELETE` in data-modifying CTEs (`WITH d AS (DELETE …) SELECT …`), `nextval()`, DDL |
| 4 | **`statement_timeout` + `lock_timeout`**: `SET LOCAL`, default 5000 ms / 2000 ms | Slow queries, `pg_sleep()`, and lock waits tying up connections (DoS) |
| 5 | **Always roll back + reset**: `ROLLBACK` then `DISCARD ALL` before the connection returns to the pool; broken connections are closed, not reused | Leftover effects, session state such as advisory locks (which survive `ROLLBACK`), and connection leaks |
| 6 | **Least-privilege role** `sql_surgeon_readonly`: `SELECT` only, no `CREATE`, no server-file or signal roles | Everything above, if a layer is bypassed. Also covers anyone who gets these credentials and connects directly. |

Connections come from a bounded `ThreadedConnectionPool` (`SURGEON_DB_POOL_MAX`), so concurrent requests can't open unlimited backends.

### Configuration

| Variable | Default | Purpose |
|----------|---------|---------|
| `SURGEON_READONLY_DATABASE_URL` | *(falls back to `DATABASE_URL` with a warning)* | Connection used for user SQL. Point it at `sql_surgeon_readonly`. |
| `SURGEON_SANDBOX_DATABASE_URL` | *(none: the benchmark refuses to run)* | Login for the sandbox benchmark. Point it at `sql_surgeon_sandbox`; a superuser or privileged login is rejected. No fallback to `DATABASE_URL`. |
| `SURGEON_STATEMENT_TIMEOUT_MS` | `5000` | Maximum run time of one analyzed query |
| `SURGEON_LOCK_TIMEOUT_MS` | `2000` | Maximum wait for a lock |
| `SURGEON_DB_POOL_MIN` / `SURGEON_DB_POOL_MAX` | `1` / `10` | Pool bounds for the read-only connection |
| `SURGEON_SMALL_TABLE_BYTES` | `65536` (64 kB) | Tables smaller than this (`pg_relation_size`) never get an index suggestion; skipped indexes are reported in `rewrite_warnings` |

> **Note:** 5 s is too short for many JOB queries on the full IMDb dataset. For the eval harness, raise it, e.g. `SURGEON_STATEMENT_TIMEOUT_MS=120000 python -m eval.run_eval`.

### Setting up the restricted role

Run [scripts/setup_security_role.sql](scripts/setup_security_role.sql) once per database, as a superuser or the database owner:

```bash
psql "$ADMIN_DATABASE_URL" \
     -v ro_password='a-strong-password' \
     -v sb_password='another-strong-password' \
     -f scripts/setup_security_role.sql
```

`-v sb_password` is optional: it also creates `sql_surgeon_sandbox` for the [sandbox benchmark](#the-sandbox-benchmark). Re-running the script resets each role's password to the value you pass, so pass the ones in your `.env`.

The script is idempotent and ends by printing a verification row per role (expected `t | f | f | f | f` for `sql_surgeon_readonly`, `t | f | f | t | f` for `sql_surgeon_sandbox`). For `sql_surgeon_readonly` it:

- creates `sql_surgeon_readonly` with `LOGIN` only (`NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS`, connection limit 20);
- grants `CONNECT`, `USAGE` on `public`, and `SELECT` on existing and future tables in `public`, and revokes every other table and sequence privilege;
- removes membership in `pg_write_all_data`, `pg_read_server_files`, `pg_write_server_files`, `pg_execute_server_program`, `pg_signal_backend`, and `pg_checkpoint`;
- sets role defaults as a backstop: `default_transaction_read_only = on`, `statement_timeout = 5s`, `lock_timeout = 2s`, `idle_in_transaction_session_timeout = 30s`.

Optional flags:

| Flag | Effect |
|------|--------|
| `-v owner_role=<role>` | Role that creates future tables (default: the role running the script). Future-table grants only apply to objects this role creates. |
| `-v all_schemas=on` | Also grant `SELECT` on future schemas and tables DB-wide, not only `public` |
| `-v revoke_from_public=on` | Revoke `dblink*` function `EXECUTE` from `PUBLIC`. dblink can open a second connection and write through it. Because a per-role `REVOKE` can't remove a `PUBLIC` grant, this is the only way to block it. It affects every role, so it's opt-in. |

Then set `SURGEON_READONLY_DATABASE_URL` to the new role and restart the backend. If the `WARNING: SURGEON_READONLY_DATABASE_URL not set` log line no longer appears, the backend is using it.

### The sandbox benchmark

The sandbox benchmark (`run_benchmark: true`, `benchmark_in_sandbox`) is the one path that can't be read-only: it creates a schema, copies data and builds the suggested indexes. So it logs in as its own role, `sql_surgeon_sandbox` (`SURGEON_SANDBOX_DATABASE_URL`), which the setup script creates with `-v sb_password=...`:

- `CONNECT` + `CREATE` on the database (for the sandbox schema), `USAGE` + `SELECT` on `public` (to copy from), nothing else: no write on any real table, no `CREATE` in `public`, `NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS`, connection limit 5;
- no membership in `pg_write_all_data`, `pg_signal_backend`, `pg_read_server_files`, `pg_write_server_files`, `pg_execute_server_program`, `pg_checkpoint`;
- role defaults `statement_timeout = 300s`, `idle_in_transaction_session_timeout = 60s` as a backstop.

Why a separate **login** and not `SET ROLE` from `DATABASE_URL`: a rollback can't undo side effects outside the transaction (`pg_terminate_backend` on another session), and `SET ROLE` is no boundary, because the query can switch back with `set_config('role', 'postgres', true)` whenever the login role is allowed to. Verified: from a superuser login, a `SET LOCAL ROLE` sandbox was escaped this way and another session terminated ([#1](https://github.com/RachelHuangZW/SQL-Surgeon/issues/1)). Only the login identity holds.

Its guards, on top of the role:

- No fallback: without `SURGEON_SANDBOX_DATABASE_URL` it raises `SandboxNotConfiguredError` instead of using `DATABASE_URL`.
- Before running anything it checks `session_user` and refuses a superuser or a member of the roles above, in case the URL points at the wrong login.
- Everything runs in **one transaction that is always rolled back**: the schema, the copies and the indexes never reach the database.
- The **LLM-generated** script is allowlisted: only `CREATE INDEX` / `ANALYZE` on unqualified (sandbox) tables and `CREATE EXTENSION` for `pg_trgm`, `btree_gin`, `btree_gist`. Anything else is rejected before running.
- The original query must pass the single-statement check and the query-only allowlist; table and schema names are quoted.

`tests/test_sandbox_benchmark.py` checks that a data-modifying CTE on a real table, `pg_terminate_backend` on another session and `set_config('role', ...)` are all denied under this role.

Everything else (`run_explain`'s plan, metadata and primary-key lookups, the hypopg schema check) runs on the read-only path and never writes. Earlier versions created missing primary-key indexes here; that was removed — a table without a primary key is now only reported.

### Production checklist

- **Never** point `DATABASE_URL` or `SURGEON_READONLY_DATABASE_URL` at a primary with real customer data. Use a read replica or a restored snapshot. `EXPLAIN ANALYZE` consumes real CPU and I/O.
- Always set `SURGEON_READONLY_DATABASE_URL`. The fallback keeps layers 1–5 but loses role isolation.
- Set `SURGEON_SANDBOX_DATABASE_URL` only if you use `run_benchmark`, and only to `sql_surgeon_sandbox`. The benchmark still really runs the user's query, so give it a replica or snapshot too.
- Keep `SURGEON_STATEMENT_TIMEOUT_MS` as low as your workload allows, and size `SURGEON_DB_POOL_MAX` below the role's `CONNECTION LIMIT`.
- Restrict the API: CORS is currently `allow_origins=["*"]` and there is no authentication. Put the backend behind auth and rate limiting before exposing it.
- Require TLS (`?sslmode=require` or stricter) for remote databases, and keep credentials in a secrets manager, not in `.env` on shared hosts.
- Monitor sessions with `application_name = 'sql_surgeon_readonly'` (and `'sql_surgeon_benchmark'` for the sandbox) in `pg_stat_activity`.

---

## What SQL Surgeon is NOT

- Not a Text-to-SQL tool — it tunes queries you already have
- Not a black box — every issue and recommendation is explained
- Not production automation — it suggests; you decide when to apply

---

## License

MIT
