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

def _parse_from_tables(from_clause: str) -> list:
    """Return [(alias, table_name), ...] preserving original case."""
    entries = []
    for entry in from_clause.split(','):
        entry = re.sub(r'\s+', ' ', entry).strip()
        m = re.match(r'(\w+)\s+(?:AS\s+)?(\w+)\s*$', entry, re.IGNORECASE)
        if m:
            entries.append((m.group(2), m.group(1)))  # (alias, table_name)
        else:
            m2 = re.match(r'^(\w+)$', entry)
            if m2:
                entries.append((m2.group(1), m2.group(1)))
    return entries


def _split_and_conditions(clause: str) -> list:
    """Split SQL conditions on top-level AND, respecting parentheses."""
    parts, depth, start = [], 0, 0
    upper = clause.upper()
    i = 0
    while i < len(clause):
        if clause[i] == '(':
            depth += 1
        elif clause[i] == ')':
            depth -= 1
        elif depth == 0 and upper[i:i+3] == 'AND':
            before_ok = i == 0 or not clause[i-1].isalnum() and clause[i-1] != '_'
            after_ok  = i+3 >= len(clause) or (not clause[i+3].isalnum() and clause[i+3] != '_')
            if before_ok and after_ok:
                parts.append(clause[start:i].strip())
                i += 3
                start = i
                continue
        i += 1
    parts.append(clause[start:].strip())
    return [p for p in parts if p]


def rewrite_comma_join(sql: str) -> str:
    """Convert comma-style implicit joins to explicit JOIN syntax.
    Returns original SQL unchanged if no comma-join pattern is detected or rewrite fails.
    """
    s = re.sub(r'[ \t]+', ' ', sql).strip()

    m_from  = re.search(r'\bFROM\b',  s, re.IGNORECASE)
    m_where = re.search(r'\bWHERE\b', s, re.IGNORECASE)
    if not m_from or not m_where or m_from.start() > m_where.start():
        return sql

    from_clause = s[m_from.end():m_where.start()].strip()
    if ',' not in from_clause:
        return sql

    table_entries = _parse_from_tables(from_clause)
    if len(table_entries) < 2:
        return sql

    alias_map = {alias.lower(): (alias, tname) for alias, tname in table_entries}
    all_aliases = set(alias_map.keys())

    rest = s[m_where.end():].strip()
    trailing_m = re.search(
        r'\b(GROUP\s+BY|ORDER\s+BY|HAVING|LIMIT|UNION)\b', rest, re.IGNORECASE
    )
    if trailing_m:
        where_str = rest[:trailing_m.start()].rstrip()
        trailing  = '\n' + rest[trailing_m.start():]
    else:
        trailing  = ';' if rest.rstrip().endswith(';') else ''
        where_str = rest.rstrip(';').strip()

    conditions   = _split_and_conditions(where_str)
    join_graph   = {}
    filter_conds = []
    join_pat     = re.compile(r'^(\w+)\.(\w+)\s*=\s*(\w+)\.(\w+)$', re.IGNORECASE)

    for cond in conditions:
        mc = join_pat.match(cond.strip())
        if mc:
            a1, _, a2, _ = mc.groups()
            if a1.lower() in all_aliases and a2.lower() in all_aliases and a1.lower() != a2.lower():
                key = tuple(sorted([a1.lower(), a2.lower()]))
                join_graph.setdefault(key, []).append(cond.strip())
                continue
        filter_conds.append(cond.strip())

    # BFS to build JOIN chain
    first_alias_lower = table_entries[0][0].lower()
    joined    = {first_alias_lower}
    remaining = {e[0].lower() for e in table_entries[1:]}
    join_clauses = []

    for _ in range(len(table_entries)):
        if not remaining:
            break
        progress = False
        for al in list(remaining):
            for jl in list(joined):
                key = tuple(sorted([al, jl]))
                if key in join_graph:
                    on_clause = ' AND '.join(join_graph[key])
                    orig_alias, tname = alias_map[al]
                    clause = f'JOIN {tname} AS {orig_alias} ON {on_clause}' if orig_alias.lower() != tname.lower() else f'JOIN {tname} ON {on_clause}'
                    join_clauses.append(clause)
                    joined.add(al)
                    remaining.discard(al)
                    progress = True
                    break
        if not progress:
            return sql  # disconnected graph — fall back to original

    if remaining:
        return sql

    select_part = s[:m_from.start()].strip()
    orig_first_alias, first_tname = alias_map[first_alias_lower]
    from_part = f'FROM {first_tname} AS {orig_first_alias}' if orig_first_alias.lower() != first_tname.lower() else f'FROM {first_tname}'

    lines = [select_part, from_part] + join_clauses
    if filter_conds:
        lines.append('WHERE ' + '\n  AND '.join(filter_conds))

    return '\n'.join(lines) + trailing


def preprocess_sql_node(state: AgentState):
    # Node 0: deterministically normalize SQL before LLM analysis
    original_sql = state.get("original_sql") or ""
    return {"normalized_sql": rewrite_comma_join(original_sql)}


def _traverse_plan(node: dict, results: list):
    if node.get("Node Type") == "Seq Scan":
        rows_removed = node.get("Rows Removed by Filter", 0)
        actual_rows = node.get("Actual Rows", 0)
        total_scanned = rows_removed + actual_rows
        if total_scanned > 0:
            selectivity = actual_rows / total_scanned
            if selectivity > 0.30:
                verdict = "seq_scan_optimal"
            elif actual_rows < 10_000:
                verdict = "index_likely_helpful"
            else:
                verdict = "gray_zone"
            results.append({
                "table": node.get("Relation Name", "unknown"),
                "selectivity": round(selectivity, 3),
                "absolute_rows": actual_rows,
                "verdict": verdict
            })
    for child in node.get("Plans", []):
        _traverse_plan(child, results)


def compute_seq_scan_analysis(explain_output: list) -> list:
    results = []
    if not explain_output:
        return results
    _traverse_plan(explain_output[0].get("Plan", {}), results)
    return results


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

    sql_to_explain = state.get("normalized_sql") or original_sql

    try:
        plan = db_client.execute_explain(sql_to_explain)

        # Enrich DDL with existing index information
        table_names = list(set(re.findall(
            r'(?:FROM|JOIN|,)\s+([a-zA-Z_][a-zA-Z0-9_]*)', sql_to_explain, re.IGNORECASE
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
            "seq_scan_analyses": compute_seq_scan_analysis(plan),
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
        ("user", "DDL: {ddl}\nExecution_Plan: {execution_plan}\nSeq Scan Analysis: {seq_scan_analyses}\nPrevious feedback: {feedback}")
    ])

    chain = prompt | llm

    response = chain.invoke({
        "ddl": state.get("ddl"),
        "execution_plan": state.get("explain_output"),
        "seq_scan_analyses": state.get("seq_scan_analyses") or [],
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
        "original_sql": state.get("normalized_sql") or state.get("original_sql"),
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
            "filtered_indexes": result["indexes"],
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
        ("user", "DDL: {ddl}\nIndexes: {indexes}\nAdvice: {advice}\nOptimized SQL: {optimized_sql}\nIssues: {issues}")
    ])

    chain = prompt | llm

    response = chain.invoke({
        "ddl": state.get("ddl"),
        "indexes": state.get("filtered_indexes"),
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

        filtered_sql = result.get("filtered_optimized_sql") or state.get("optimized_sql")

        return {
            "verdict": result["verdict"],
            "feedback": result["feedback"],
            "filtered_indexes": result["filtered_indexes"] or state.get("filtered_indexes"),
            "optimized_sql": inject_extension_deps(filtered_sql),
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
