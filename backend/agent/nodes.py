from agent.state import AgentState
from db.client import DBClient
import os

def run_explain_node(state: AgentState):
    # Node 1: Execute EXPLAIN ANALYZE
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        return {"error": "DATABASE_URL not set"}
    
    db_client = DBClient(dsn)

    original_sql = state.get("original_sql")

    if not original_sql:
        return {"error": "No Original SQL found"}

    try:
        plan = db_client.execute_explain(original_sql)

        return {
            "explain_output": plan,
            "error": None
        }
    
    except Exception as e:
        return {
            "error": f"Database Execution Failure: {str(e)}"
        }
