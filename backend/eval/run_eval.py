import os
import sys
import json
import re
import psycopg2
from glob import glob
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from agent.graph import app as agent_graph

QUERY_DIR = os.path.expanduser("~/join-order-benchmark")
RESULTS_DIR = os.path.join(os.path.dirname(__file__), "results")


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


def get_gin_candidate_columns(conn, table_names: list) -> list:
    # Returns [(table_name, column_name), ...] for TEXT/VARCHAR columns only
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
                candidates.append((table, col))
    return candidates


def greedy_oracle_b2_gin(conn, sql: str, table_names: list) -> tuple:
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

    candidates = get_gin_candidate_columns(conn, table_names)
    selected_ddls = []
    selected_names = []
    current_cost = get_explain_cost(conn, sql)

    try:
        while candidates:
            best_cost = current_cost
            best_candidate = None

            for table, col in candidates:
                idx_name = f"_b2gin_{table}_{col}"
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


def greedy_oracle_b2(conn, sql: str, table_names: list) -> tuple:
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


def eval_gin_adoption(conn, sql: str, gin_ddls: list) -> tuple:
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
    print(f"Found {len(sql_files)} queries to evaluate")

    conn = psycopg2.connect(dsn)
    results = []

    for i, filepath in enumerate(sql_files):
        query_name = os.path.basename(filepath).replace(".sql", "")
        print(f"[{i+1}/{len(sql_files)}] Running: {query_name}")

        with open(filepath, "r") as f:
            sql = f.read().strip()

        table_names = get_table_names(sql)
        ddl_parts = [fetch_ddl(conn, t) for t in table_names]
        ddl = "\n".join(filter(None, ddl_parts))

        try:
            b1_cost = get_explain_cost(conn, sql)
            b2_btree_cost, b2_btree_indexes = greedy_oracle_b2(conn, sql, table_names)
            b2_gin_cost, b2_gin_indexes = greedy_oracle_b2_gin(conn, sql, table_names)
            state = agent_graph.invoke({
                "original_sql": sql,
                "ddl": ddl,
                "table_name": "",
                "retry_count": 0,
            })

            surgeon_btree_cost = None
            surgeon_gin_cost = None
            surgeon_gin_indexes = []
            surgeon_gin_adopted = []
            optimized_sql = state.get("optimized_sql")
            if optimized_sql:
                btree_ddls, gin_ddls = extract_index_ddls(optimized_sql)
                surgeon_gin_indexes = gin_ddls
                if btree_ddls:
                    surgeon_btree_cost = evaluate_with_hypopg(conn, sql, btree_ddls)
                if gin_ddls:
                    surgeon_gin_cost, surgeon_gin_adopted = eval_gin_adoption(conn, sql, gin_ddls)

            result = {
                "query": query_name,
                "status": "error" if state.get("error") else "success",
                # L1 execution cost
                "b1_cost": b1_cost,
                # B2 Oracle evaluation cost 
                "b2_btree_cost": b2_btree_cost,
                "b2_btree_indexes": b2_btree_indexes,
                "b2_gin_cost": b2_gin_cost,
                "b2_gin_indexes": b2_gin_indexes,
                # Surgeon execution cost (btree only; GIN excluded due to hypopg limitation)
                "surgeon_btree_cost": surgeon_btree_cost,
                "surgeon_gin_cost": surgeon_gin_cost,
                "surgeon_gin_indexes": surgeon_gin_indexes,
                # Agent output
                "issues": state.get("issues"),
                "advice": state.get("advice"),
                "optimized_sql": optimized_sql,
                # L3 internal health
                "retry_count": state.get("retry_count"),
                "verdict": state.get("verdict"),
                "hit_max_retry": state.get("retry_count", 0) >= 2,
                "error": state.get("error"),
                "timestamp": datetime.now().isoformat(),
            }
        except Exception as e:
            result = {
                "query": query_name,
                "status": "exception",
                "error": str(e),
                "timestamp": datetime.now().isoformat(),
            }

        results.append(result)

        out_path = os.path.join(RESULTS_DIR, f"{query_name}.json")
        with open(out_path, "w") as f:
            json.dump(result, f, indent=2)

    conn.close()

    summary_path = os.path.join(RESULTS_DIR, f"summary_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
    with open(summary_path, "w") as f:
        json.dump(results, f, indent=2)

    success = sum(1 for r in results if r["status"] == "success")
    print(f"\nDone. {success}/{len(results)} queries succeeded.")
    print(f"Results saved to {RESULTS_DIR}")


if __name__ == "__main__":
    main()
