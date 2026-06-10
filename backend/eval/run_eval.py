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
    pattern = r'\b(?:FROM|JOIN)\s+([a-zA-Z_][a-zA-Z0-9_]*)'
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
            state = agent_graph.invoke({
                "original_sql": sql,
                "ddl": ddl,
                "table_name": "",
                "retry_count": 0,
            })

            result = {
                "query": query_name,
                "status": "error" if state.get("error") else "success",
                # L1 execution cost
                "b1_cost": b1_cost,
                # Surgeon execution cost
                "surgeon_cost": None,
                # Agent output
                "issues": state.get("issues"),
                "advice": state.get("advice"),
                "optimized_sql": state.get("optimized_sql"),
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
