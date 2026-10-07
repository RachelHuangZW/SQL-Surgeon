import json
import os, sys
sys.path.insert(0, "/Users/rachel/SQL-Surgeon/backend")
from dotenv import load_dotenv; load_dotenv("/Users/rachel/SQL-Surgeon/.env")
import psycopg2
from sql_surgeon.db.client import DBClient, UnsafeSQLError, QueryTimeoutError
from sql_surgeon.plan.table import flatten_plan

ADMIN, RO = os.environ["DATABASE_URL"], os.environ["SURGEON_READONLY_DATABASE_URL"]

def get_plan(sql):
    db = DBClient(ADMIN)
    py_list = db.execute_explain(sql)
    return py_list

def print_rows(rows):
    print(f"{'Node':<60} {'Plan Rows':>10} {'Actual Rows':>12} {'Loops':>8} {'q-error':>8}  {'Dir':<7}  Condition")
    for r in rows:
        name = r["op"]
        if r["table"]:
            name += f" on {r['table']}"
            if r["alias"] != r["table"]:
                name += f" {r['alias']}"
        # Indent first, then pad: otherwise the indentation pushes the other columns out of line
        label = "  " * r["depth"] + name
        q_text = f"{r['q_error']:.1f}" if r["q_error"] is not None else "-"
        print(f"{label:<60.60} {r['plan_rows']:>10} {r['actual_rows']:>12} {r['loops']:>8} {q_text:>8}  "
              f"{r['direction']:<7}  {r['condition'][:70]}")

query = sys.argv[1] if len(sys.argv) > 1 else "11d"
path = os.path.expanduser(f"~/join-order-benchmark/{query}.sql")
out_path = os.path.join(os.path.dirname(__file__), f"{query}_plan.json")

with open(path) as f:
    sql = f.read().strip()

py_list = get_plan(sql)

with open(out_path, "w") as f:
    json.dump(py_list, f, indent=2)

print_rows(flatten_plan(py_list))
