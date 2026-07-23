from agent.state import AgentState
from db.client import DBClient
from agent.prompts import ANALYSIS_PROMPT
from agent.prompts import ADVICE_PROMPT
from agent.prompts import REVIEW_ADVICE_PROMPT

import re
import json
import psycopg2
from dotenv import load_dotenv
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.prompts import ChatPromptTemplate

import os

load_dotenv()
api_key = os.getenv("GOOGLE_API_KEY")

llm = ChatGoogleGenerativeAI(
    model="gemini-2.5-pro",
    google_api_key=api_key,
    temperature=0.0,
    timeout=60
)

def strip_code_block(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        text = text.rsplit("```", 1)[0]
    return text.strip()


EXTENSION_DEPS = {
    r'\bgin_trgm_ops\b': "CREATE EXTENSION IF NOT EXISTS pg_trgm;",
    r'\bbtree_gin\b':    "CREATE EXTENSION IF NOT EXISTS btree_gin;",
}


def inject_extension_deps(optimized_sql: str) -> str:
    needed = []
    for pattern, stmt in EXTENSION_DEPS.items():
        if re.search(pattern, optimized_sql, re.IGNORECASE):
            if stmt.lower() not in optimized_sql.lower():
                needed.append(stmt)
    if not needed:
        return optimized_sql
    marker = "-- Step 1:"
    idx = optimized_sql.find(marker)
    if idx != -1:
        line_end = optimized_sql.find("\n", idx) + 1
        return optimized_sql[:line_end] + "\n".join(needed) + "\n" + optimized_sql[line_end:]

    return "\n".join(needed) + "\n" + optimized_sql


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
        
        # Enrich DDL with existing index information
        table_names = list(set(re.findall(
            r'(?:FROM|JOIN|,)\s+([a-zA-Z_][a-zA-Z0-9_]*)', original_sql, re.IGNORECASE
        )))
        enriched_ddl = state.get("ddl") or ""
        try:
            idx_conn = psycopg2.connect(dsn)
            with idx_conn.cursor() as cur:
                # Auto-fetch column definitions if user didn't provide DDL
                if not enriched_ddl.strip():
                    for table in table_names:
                        cur.execute("""
                            SELECT column_name, data_type
                            FROM information_schema.columns
                            WHERE table_schema = 'public' AND table_name = %s
                            ORDER BY ordinal_position
                        """, (table,))
                        cols = cur.fetchall()
                        if cols:
                            col_defs = ", ".join(f"{name} {dtype}" for name, dtype in cols)
                            enriched_ddl += f"CREATE TABLE {table} ({col_defs});\n"
                # Always append existing index information
                for table in table_names:
                    cur.execute("""
                        SELECT indexname, indexdef FROM pg_indexes
                        WHERE schemaname = 'public' AND tablename = %s
                    """, (table,))
                    indexes = cur.fetchall()
                    if indexes:
                        lines = "\n".join(f"--   {name}: {defn}" for name, defn in indexes)
                        enriched_ddl += f"\n-- Existing indexes on {table}:\n{lines}"
                    else:
                        enriched_ddl += f"\n-- Existing indexes on {table}: NONE"
            idx_conn.close()
        except Exception:
            pass  # DDL enrichment is best-effort; don't fail the whole workflow

        return {
            "explain_output": plan,
            "ddl": enriched_ddl,
            "error": None
        }
    except Exception as e:
        return {
            "error": f"Database Execution Failure: {str(e)}"
        }


def identify_issues(state: AgentState):
    # Node 2: use LLM to identify DB issues from EXPLAIN PLAN
    prompt = ChatPromptTemplate.from_messages([
        ("system", ANALYSIS_PROMPT),
        ("user", "DDL: {ddl}\nExecution_Plan: {execution_plan}\nPrevious feedback: {feedback}")
    ])

    chain = prompt | llm

    response = chain.invoke({
        "ddl": state.get("ddl"),
        "execution_plan": state.get("explain_output"),
        "feedback": state.get("feedback") or "None"
    })

    usage = getattr(response, 'usage_metadata', None) or {}
    _in  = usage.get('input_tokens', 0)
    _out = usage.get('output_tokens', 0)
    
    try:
        issues = json.loads(strip_code_block(response.content))
        return {"issues": issues,
                "total_input_tokens":  (state.get("total_input_tokens")  or 0) + _in,
                "total_output_tokens": (state.get("total_output_tokens") or 0) + _out
            }
    except json.JSONDecodeError:
        return {"error": f"LLM returned unparseable response: {response.content}"}


def generate_advice(state: AgentState):
    # Node 3: generate advice based on issues found
    prompt = ChatPromptTemplate.from_messages([
        ("system", ADVICE_PROMPT),
        ("user", "SQL: {original_sql}\nIssues: {issues}\nPrevious review feedback: {feedback}")
    ])

    chain = prompt | llm

    response = chain.invoke({
        "original_sql": state.get("original_sql"),
        "issues": state.get("issues"),
        "feedback": state.get("feedback") or "None"
    })

    usage = getattr(response, 'usage_metadata', None) or {}
    _in  = usage.get('input_tokens', 0)
    _out = usage.get('output_tokens', 0)

    try:
        result = json.loads(strip_code_block(response.content))
        return {
            "advice": result["advice"],
            "optimized_sql": inject_extension_deps(result["optimized_sql"]),
            "total_input_tokens":  (state.get("total_input_tokens")  or 0) + _in,
            "total_output_tokens": (state.get("total_output_tokens") or 0) + _out
        }
    except (json.JSONDecodeError, KeyError) as e:
        return {"error": f"LLM returned unparseable response: {response.content}"}


def review_advice(state: AgentState):
    # Node 4: review advice generated by previous node
    prompt = ChatPromptTemplate.from_messages([
        ("system", REVIEW_ADVICE_PROMPT),
        ("user", "Advice: {advice}\nOptimized SQL: {optimized_sql}\nIssues: {issues}")
    ])

    chain = prompt | llm

    response = chain.invoke({
        "advice": state.get("advice"),
        "optimized_sql": state.get("optimized_sql"),
        "issues": state.get("issues")
    })

    usage = getattr(response, 'usage_metadata', None) or {}
    _in  = usage.get('input_tokens', 0)
    _out = usage.get('output_tokens', 0)

    try:
        result = json.loads(strip_code_block(response.content))
        new_retry_count = (state.get("retry_count") or 0)
        
        if result["verdict"] == "retry":
            new_retry_count += 1

        return {
            "verdict": result["verdict"],
            "feedback": result["feedback"],
            "retry_count": new_retry_count,
            "total_input_tokens":  (state.get("total_input_tokens")  or 0) + _in,
            "total_output_tokens": (state.get("total_output_tokens") or 0) + _out
        }
    except (json.JSONDecodeError, KeyError) as e:
        return {"error": f"LLM returned unparseable response: {response.content}"}


def generate_benchmark_schema(state: AgentState):
    # Node 5: create benchmark schema for testing
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        return {"error": "DATABASE_URL not set"}

    db_client = DBClient(dsn)

    original_sql = state.get("original_sql")
    if not state.get("optimized_sql"):
        return {"error": "No optimized SQL to benchmark"}

    table_names = list(set(re.findall(r'(?:FROM|JOIN|,)\s+([a-zA-Z_][a-zA-Z0-9_]*)', original_sql, re.IGNORECASE)))
    suggested_ddl = state.get("optimized_sql")

    try:
        new_plan = db_client.benchmark_in_sandbox(table_names, original_sql, suggested_ddl)
        return {
            "benchmark_result": new_plan
        }
    except Exception as e:
        return {
            "error": f"Database Execution Failure: {str(e)}"
        }
