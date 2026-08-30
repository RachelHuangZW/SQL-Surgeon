import os
import sys
import json
import re
import signal
import time
import tracemalloc
import subprocess
import random, string
import psycopg2
from glob import glob
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from agent.graph import app as agent_graph

QUERY_DIR = os.path.expanduser("~/join-order-benchmark")
RESULTS_DIR = os.path.join(os.path.dirname(__file__), "results")
RUN_ID = ''.join(random.choices(string.ascii_lowercase, k=6))

try:
    GIT_COMMIT = subprocess.run(
        ['git', 'rev-parse', '--short', 'HEAD'],
        capture_output=True, text=True,
        cwd=os.path.dirname(__file__)
    ).stdout.strip() or "unknown"
except Exception:
    GIT_COMMIT = "unknown"

PASS_REDUCTION_THRESHOLD = 0.10  # surgeon must reduce planner cost by at least 10%
PASS_F1_THRESHOLD = 0.50         # surgeon's index set must score at least F1=0.5 vs oracle


def _compute_score(b1, s_comb, l2_combined, error) -> int:
    if error:
        return 0
    if not (b1 and s_comb and b1 > 0):
        return 0
    if (b1 - s_comb) / b1 < PASS_REDUCTION_THRESHOLD:
        return 0
    f1 = (l2_combined or {}).get("f1")
    if f1 is None or f1 < PASS_F1_THRESHOLD:
        return 0
    return 1


def get_table_names(sql: str) -> list:
    # Match tables after FROM, JOIN, or comma (handles implicit join syntax)
    pattern = r'(?:FROM|JOIN|,)\s+([a-zA-Z_][a-zA-Z0-9_]*)'
    matches = re.findall(pattern, sql, re.IGNORECASE)
    return list(set(matches))


def fetch_ddl(conn, table_name: str) -> str:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT column_name, data_type
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = %s
            ORDER BY ordinal_position
        """, (table_name,))
        cols = cur.fetchall()

    if not cols:
        return ""
    col_defs = ", ".join(f"{name} {dtype}" for name, dtype in cols)
    return f"CREATE TABLE {table_name} ({col_defs});"


def get_explain_cost(conn, sql: str) -> float:
    with conn.cursor() as cur:
        cur.execute(f"EXPLAIN (FORMAT JSON) {sql}")
        plan = cur.fetchone()[0]
        total_cost = plan[0]["Plan"]["Total Cost"]
    return total_cost


def extract_index_ddls(optimized_sql: str) -> tuple:
    # Returns (btree_ddls, gin_ddls) — hypopg only supports btree
    all_ddls = re.findall(r'CREATE INDEX(?:\s+IF\s+NOT\s+EXISTS)?\s+\w[^;]*;', optimized_sql, re.IGNORECASE)
    unsupported = re.compile(r'\bUSING\s+(gin|gist|brin)\b', re.IGNORECASE)
    btree = [ddl for ddl in all_ddls if not unsupported.search(ddl)]
    gin   = [ddl for ddl in all_ddls if unsupported.search(ddl)]
    return btree, gin


def evaluate_with_hypopg(conn, sql: str, index_ddls: list) -> float:
    with conn.cursor() as cur:
        # First clean last hypopg execution
        cur.execute("SELECT hypopg_reset()")
        for ddl in index_ddls:
            cur.execute("SELECT * FROM hypopg_create_index(%s)", (ddl,))
        cur.execute(f"EXPLAIN (FORMAT JSON) {sql}")
        plan = cur.fetchone()[0]
        total_cost = plan[0]["Plan"]["Total Cost"]
        # Clean hypopg before exit
        cur.execute("SELECT hypopg_reset()")
    return total_cost


def get_candidate_columns(conn, table_names: list) -> list:
    # Returns [(table_name, column_name), ...] for all columns in given tables
    candidates = []
    with conn.cursor() as cur:
        for table in table_names:
            cur.execute(
                """
                SELECT column_name FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = %s
                ORDER by ordinal_position
                """
                , (table,)
            )
            for (col,) in cur.fetchall():
                candidates.append((table, col))
    return candidates


def get_gin_candidate_columns(conn, table_names: list, sql: str = None) -> list:
    # Returns [(table_name, column_name), ...] for TEXT/VARCHAR columns only.
    # If sql is provided, restricts to columns that appear in LIKE predicates —
    # GIN trgm is only useful for LIKE/ILIKE patterns, not arbitrary text columns.
    like_cols = set()
    if sql:
        like_cols = set(re.findall(r'\b(\w+)\s+(?:NOT\s+)?LIKE\b', sql, re.IGNORECASE))

    candidates = []
    with conn.cursor() as cur:
        for table in table_names:
            cur.execute(
                """
                SELECT column_name FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = %s
                  AND data_type IN ('text', 'character varying', 'character')
                ORDER BY ordinal_position
                """,
                (table,)
            )
            for (col,) in cur.fetchall():
                if not like_cols or col in like_cols:
                    candidates.append((table, col))
    return candidates


def oracle_greedy_gin(conn, sql: str, table_names: list) -> tuple:
    # Returns (b2_gin_cost, b2_gin_indexes)
    # Greedy search using real GIN indexes — slow but accurate.
    # Selected indexes stay on DB across rounds so EXPLAIN sees their combined effect.
    # All created indexes are dropped in finally.
    try:
        with conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
            conn.commit()
    except Exception:
        conn.rollback()

    candidates = get_gin_candidate_columns(conn, table_names, sql=sql)
    selected_ddls = []
    selected_names = []
    current_cost = get_explain_cost(conn, sql)

    try:
        while candidates:
            best_cost = current_cost
            best_candidate = None

            for table, col in candidates:
                idx_name = f"_b2gin_{RUN_ID}_{table}_{col}"
                ddl = f"CREATE INDEX {idx_name} ON {table} USING gin ({col} gin_trgm_ops);"
                created = False
                try:
                    with conn.cursor() as cur:
                        cur.execute(ddl)
                        conn.commit()
                    created = True
                    cost = get_explain_cost(conn, sql)
                    if cost < best_cost:
                        best_cost = cost
                        best_candidate = (table, col, idx_name, ddl)
                except Exception:
                    conn.rollback()
                finally:
                    if created:
                        with conn.cursor() as cur:
                            cur.execute(f"DROP INDEX IF EXISTS {idx_name}")
                            conn.commit()

            if best_candidate is None:
                break

            # Keep best index on DB so next round's EXPLAIN sees it
            table, col, idx_name, ddl = best_candidate
            with conn.cursor() as cur:
                cur.execute(ddl)
                conn.commit()
            selected_ddls.append(ddl)
            selected_names.append(idx_name)
            current_cost = best_cost
            candidates.remove((table, col))

    finally:
        with conn.cursor() as cur:
            for name in selected_names:
                cur.execute(f"DROP INDEX IF EXISTS {name}")
            conn.commit()

    return current_cost, selected_ddls


def oracle_greedy_btree(conn, sql: str, table_names: list) -> tuple:
    # Returns (b2_btree_cost, b2_btree_indexes)
    candidates = get_candidate_columns(conn, table_names)
    selected_ddl = []
    current_cost = get_explain_cost(conn, sql)

    while candidates:
        best_cost = current_cost
        best_candidate = None
        for table, col in candidates:
            ddl = f"CREATE INDEX ON {table}({col});"
            cost = evaluate_with_hypopg(conn, sql, selected_ddl + [ddl])
            if cost < best_cost:
                best_cost = cost
                best_candidate = (table, col, ddl)
        
        if best_candidate is None:
            break # no column improved cost this round
        
        selected_ddl.append(best_candidate[2])
        current_cost = best_cost
        candidates.remove((best_candidate[0], best_candidate[1]))
    
    return current_cost, selected_ddl


def eval_surgeon_gin(conn, sql: str, gin_ddls: list) -> tuple:
    # Returns (gin_cost, adopted_indexes)
    # gin_cost: planner Total Cost with real GIN indexes applied (parallel to surgeon_btree_cost)
    # adopted_indexes: list of index names the planner actually chose to use
    created = []
    adopted = []
    gin_cost = None
    try:
        with conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
            conn.commit()
    except Exception:
        conn.rollback()

    try:
        with conn.cursor() as cur:
            for ddl in gin_ddls:
                match = re.search(r'CREATE INDEX(?:\s+IF\s+NOT\s+EXISTS)?\s+(\w+)', ddl, re.IGNORECASE)
                if not match:
                    continue
                index = match.group(1)
                try:
                    cur.execute(ddl)
                    conn.commit()
                    created.append(index)
                except Exception:
                    conn.rollback()
            cur.execute(f"EXPLAIN (FORMAT JSON) {sql}")
            plan = cur.fetchone()[0]
            plan_text = json.dumps(plan)
            gin_cost = plan[0]["Plan"]["Total Cost"]
            adopted = [index for index in created if index in plan_text]
    finally:
        with conn.cursor() as cur:
            for index_name in created:
                cur.execute(f"DROP INDEX IF EXISTS {index_name}")
            conn.commit()
    return gin_cost, adopted


def extract_index_columns(ddls: list) -> set:
    # Parses CREATE INDEX DDL strings into (table, column) pairs for set comparison.
    # Handles: CREATE INDEX ON t(col), CREATE INDEX name ON t (col), USING gin (col ops)
    result = set()
    for ddl in ddls:
        match = re.search(r'\bON\s+(\w+)\s*(?:USING\s+\w+\s+)?\((\w+)', ddl, re.IGNORECASE)
        if match:
            result.add((match.group(1).lower(), match.group(2).lower()))
    return result


def compute_l2_metrics(surgeon_ddls: list, oracle_ddls: list) -> dict:
    # Returns precision, recall, f1, and matched columns for L2 index quality evaluation.
    surgeon_set = extract_index_columns(surgeon_ddls)
    oracle_set  = extract_index_columns(oracle_ddls)

    if not surgeon_set and not oracle_set:
        return {"precision": None, "recall": None, "f1": None, "matched": []}

    intersection = surgeon_set & oracle_set
    precision = len(intersection) / len(surgeon_set) if surgeon_set else 0.0
    recall    = len(intersection) / len(oracle_set)  if oracle_set  else 0.0
    f1        = (2 * precision * recall / (precision + recall)) if (precision + recall) > 0 else 0.0

    return {
        "precision": round(precision, 3),
        "recall":    round(recall, 3),
        "f1":        round(f1, 3),
        "matched":   sorted(intersection),
    }


def oracle_greedy_combined(conn, sql: str, table_names: list) -> tuple:
    # Returns (b2_combined_cost, btree_ddls, gin_ddls)
    # Each round picks the best index across both btree AND GIN candidates.
    # Committed btree: kept as hypopg hypotheticals (re-passed each eval).
    # Committed GIN: kept as real indexes on DB so planner sees them during hypopg EXPLAIN.
    # All real GIN indexes are dropped in finally.
    try:
        with conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
            conn.commit()
    except Exception:
        conn.rollback()

    btree_candidates = get_candidate_columns(conn, table_names)
    gin_candidates = get_gin_candidate_columns(conn, table_names, sql=sql)
    selected_btree_ddls = []
    selected_gin_ddls = []
    selected_gin_names = []
    current_cost = get_explain_cost(conn, sql)

    try:
        while btree_candidates or gin_candidates:
            best_cost = current_cost
            best_candidate = None

            for table, col in btree_candidates:
                ddl = f"CREATE INDEX ON {table}({col});"
                cost = evaluate_with_hypopg(conn, sql, selected_btree_ddls + [ddl])
                if cost < best_cost:
                    best_cost = cost
                    best_candidate = ('btree', table, col, ddl)

            for table, col in gin_candidates:
                idx_name = f"_b2comb_{RUN_ID}_{table}_{col}"
                ddl = f"CREATE INDEX {idx_name} ON {table} USING gin ({col} gin_trgm_ops);"
                created = False
                try:
                    with conn.cursor() as cur:
                        cur.execute(ddl)
                        conn.commit()
                    created = True
                    # Real GIN is on DB; hypopg sees it alongside the btree hypotheticals
                    cost = evaluate_with_hypopg(conn, sql, selected_btree_ddls)
                    if cost < best_cost:
                        best_cost = cost
                        best_candidate = ('gin', table, col, idx_name, ddl)
                except Exception:
                    conn.rollback()
                finally:
                    if created:
                        with conn.cursor() as cur:
                            cur.execute(f"DROP INDEX IF EXISTS {idx_name}")
                            conn.commit()

            if best_candidate is None:
                break

            if best_candidate[0] == 'btree':
                _, table, col, ddl = best_candidate
                selected_btree_ddls.append(ddl)
                btree_candidates.remove((table, col))
            else:
                _, table, col, idx_name, ddl = best_candidate
                with conn.cursor() as cur:
                    cur.execute(ddl)
                    conn.commit()
                selected_gin_ddls.append(ddl)
                selected_gin_names.append(idx_name)
                gin_candidates.remove((table, col))

            current_cost = best_cost

    finally:
        with conn.cursor() as cur:
            for name in selected_gin_names:
                cur.execute(f"DROP INDEX IF EXISTS {name}")
            conn.commit()

    return current_cost, selected_btree_ddls, selected_gin_ddls


def evaluate_surgeon_combined(conn, sql: str, btree_ddls: list, gin_ddls: list) -> float:
    # Returns cost with surgeon's real GIN indexes on DB + btree as hypopg hypotheticals.
    created_gin = []
    try:
        with conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
            conn.commit()
    except Exception:
        conn.rollback()
    try:
        for ddl in gin_ddls:
            match = re.search(r'CREATE INDEX(?:\s+IF\s+NOT\s+EXISTS)?\s+(\w+)', ddl, re.IGNORECASE)
            if not match:
                continue
            idx_name = match.group(1)
            try:
                with conn.cursor() as cur:
                    cur.execute(ddl)
                    conn.commit()
                created_gin.append(idx_name)
            except Exception:
                conn.rollback()
        cost = evaluate_with_hypopg(conn, sql, btree_ddls)
    finally:
        with conn.cursor() as cur:
            for name in created_gin:
                cur.execute(f"DROP INDEX IF EXISTS {name}")
            conn.commit()
    return cost


def main():
    os.makedirs(RESULTS_DIR, exist_ok=True)

    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        print("Error: DATABASE_URL not set")
        return

    sql_files = sorted(glob(os.path.join(QUERY_DIR, "*.sql")))
    if not sql_files:
        print(f"No .sql files found in {QUERY_DIR}")
        return
    sql_files = sql_files[:30]

    RUN_RESULTS_DIR = os.path.join(RESULTS_DIR, RUN_ID)
    os.makedirs(RUN_RESULTS_DIR, exist_ok=True)
    print(f"Found {len(sql_files)} queries to evaluate")
    print(f"Run ID: {RUN_ID} | Git: {GIT_COMMIT} | Output: {RUN_RESULTS_DIR}")

    conn = psycopg2.connect(dsn)
    with conn.cursor() as cur:
        cur.execute("SET statement_timeout = '10min'")

    def _timeout_handler(signum, frame):
        try:
            conn.cancel()
        except Exception:
            pass
        raise TimeoutError("Query exceeded 20-minute limit")

    results = []

    for i, filepath in enumerate(sql_files):
        query_name = os.path.basename(filepath).replace(".sql", "")
        print(f"[{i+1}/{len(sql_files)}] Running: {query_name}")

        with open(filepath, "r") as f:
            sql = f.read().strip()

        table_names = get_table_names(sql)
        ddl_parts = [fetch_ddl(conn, t) for t in table_names]
        ddl = "\n".join(filter(None, ddl_parts))

        out_path = os.path.join(RUN_RESULTS_DIR, f"{query_name}.json")
        if os.path.exists(out_path):
            print(f"[{i+1}/{len(sql_files)}] Skip {query_name} (already done)")
            continue

        tracemalloc.start()
        query_start = time.time()
        signal.signal(signal.SIGALRM, _timeout_handler)
        signal.alarm(20 * 60)
        try:
            b1_cost = get_explain_cost(conn, sql)
            b2_btree_cost, b2_btree_indexes = oracle_greedy_btree(conn, sql, table_names)
            b2_gin_cost, b2_gin_indexes = oracle_greedy_gin(conn, sql, table_names)
            b2_combined_cost, b2_combined_btree_indexes, b2_combined_gin_indexes = oracle_greedy_combined(conn, sql, table_names)
            state = agent_graph.invoke({
                "original_sql": sql,
                "ddl": ddl,
                "run_benchmark": False,
                "retry_count": 0,
            })

            surgeon_btree_cost = None
            surgeon_gin_cost = None
            surgeon_combined_cost = None
            surgeon_gin_indexes = []
            surgeon_gin_adopted = []
            l2_btree = {}
            l2_combined = {}
            btree_ddls, gin_ddls = [], []
            optimized_sql = state.get("optimized_sql")
            if optimized_sql:
                btree_ddls, gin_ddls = extract_index_ddls(optimized_sql)
                surgeon_gin_indexes = gin_ddls
                if btree_ddls:
                    surgeon_btree_cost = evaluate_with_hypopg(conn, sql, btree_ddls)
                if gin_ddls:
                    surgeon_gin_cost, surgeon_gin_adopted = eval_surgeon_gin(conn, sql, gin_ddls)
                if btree_ddls or gin_ddls:
                    surgeon_combined_cost = evaluate_surgeon_combined(conn, sql, btree_ddls, gin_ddls)
                l2_btree    = compute_l2_metrics(btree_ddls, b2_btree_indexes)
                l2_combined = compute_l2_metrics(btree_ddls + gin_ddls, b2_combined_btree_indexes + b2_combined_gin_indexes)

            elapsed = round(time.time() - query_start, 1)
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()

            agent_error = state.get("error")
            result = {
                "query": query_name,
                "run_id": RUN_ID,
                "git_commit": GIT_COMMIT,
                "input": sql,
                "elapsed_seconds": elapsed,
                "peak_memory_mb": round(peak / 1024 / 1024, 1),
                "status": "error" if agent_error else "success",
                "score": _compute_score(b1_cost, surgeon_combined_cost, l2_combined, agent_error),
                # L1 execution cost
                "b1_cost": b1_cost,
                # B2 Oracle evaluation cost
                "b2_btree_cost": b2_btree_cost,
                "b2_btree_indexes": b2_btree_indexes,
                "b2_gin_cost": b2_gin_cost,
                "b2_gin_indexes": b2_gin_indexes,
                "b2_combined_cost": b2_combined_cost,
                "b2_combined_btree_indexes": b2_combined_btree_indexes,
                "b2_combined_gin_indexes": b2_combined_gin_indexes,
                # Surgeon evaluation cost
                "surgeon_btree_cost": surgeon_btree_cost,
                "surgeon_gin_cost": surgeon_gin_cost,
                "surgeon_combined_cost": surgeon_combined_cost,
                "surgeon_gin_indexes": surgeon_gin_indexes,
                # L2 index quality
                "l2_btree": l2_btree,
                "l2_combined": l2_combined,
                # Agent output
                "normalized_sql": state.get("normalized_sql"),
                "issues": state.get("issues"),
                "advice": state.get("advice"),
                "filtered_indexes": state.get("filtered_indexes"),
                "optimized_sql": optimized_sql,
                # L3 internal health
                "retry_count": state.get("retry_count"),
                "verdict": state.get("verdict"),
                "total_input_tokens":  state.get("total_input_tokens"),
                "total_output_tokens": state.get("total_output_tokens"),
                "hit_max_retry": state.get("retry_count", 0) >= 2,
                "error": agent_error,
                "timestamp": datetime.now().isoformat(),
            }
        except TimeoutError as e:
            tracemalloc.stop()
            try:
                conn.rollback()
            except Exception:
                pass
            result = {
                "query": query_name,
                "run_id": RUN_ID,
                "git_commit": GIT_COMMIT,
                "input": sql,
                "elapsed_seconds": round(time.time() - query_start, 1),
                "status": "timeout",
                "score": 0,
                "error": str(e),
                "timestamp": datetime.now().isoformat(),
            }
        except Exception as e:
            tracemalloc.stop()
            result = {
                "query": query_name,
                "run_id": RUN_ID,
                "git_commit": GIT_COMMIT,
                "input": sql,
                "elapsed_seconds": round(time.time() - query_start, 1),
                "status": "exception",
                "score": 0,
                "error": str(e),
                "timestamp": datetime.now().isoformat(),
            }
        finally:
            signal.alarm(0)

        results.append(result)
        with open(out_path, "w") as f:
            json.dump(result, f, indent=2)

    conn.close()

    summary_path = os.path.join(RUN_RESULTS_DIR, "summary.json")
    with open(summary_path, "w") as f:
        json.dump({"run_id": RUN_ID, "git_commit": GIT_COMMIT, "results": results}, f, indent=2)

    success = sum(1 for r in results if r["status"] == "success")
    print(f"\nDone. {success}/{len(results)} queries succeeded.")
    print(f"Results saved to {RUN_RESULTS_DIR}")


if __name__ == "__main__":
    main()
