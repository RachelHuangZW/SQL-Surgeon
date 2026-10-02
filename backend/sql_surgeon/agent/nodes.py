from .state import AgentState
from ..db.client import DBClient
from .prompts import ANALYSIS_PROMPT
from .prompts import ADVICE_PROMPT
from .prompts import REVIEW_ADVICE_PROMPT

import re
import json
from dotenv import load_dotenv
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.prompts import ChatPromptTemplate

import os

load_dotenv()
api_key = os.getenv("GOOGLE_API_KEY")

llm = ChatGoogleGenerativeAI(
    model="gemini-2.5-pro", google_api_key=api_key, temperature=0.0, timeout=60
)


def _parse_from_tables(from_clause: str) -> list:
    """Return [(alias, table_name), ...] preserving original case."""
    entries = []
    for entry in from_clause.split(","):
        entry = re.sub(r"\s+", " ", entry).strip()
        m = re.match(r"(\w+)\s+(?:AS\s+)?(\w+)\s*$", entry, re.IGNORECASE)
        if m:
            entries.append((m.group(2), m.group(1)))  # (alias, table_name)
        else:
            m2 = re.match(r"^(\w+)$", entry)
            if m2:
                entries.append((m2.group(1), m2.group(1)))
    return entries


def _split_and_conditions(clause: str) -> list:
    """Split SQL conditions on top-level AND, respecting parentheses."""
    parts, depth, start = [], 0, 0
    upper = clause.upper()
    i = 0
    while i < len(clause):
        if clause[i] == "(":
            depth += 1
        elif clause[i] == ")":
            depth -= 1
        elif depth == 0 and upper[i : i + 3] == "AND":
            before_ok = i == 0 or not clause[i - 1].isalnum() and clause[i - 1] != "_"
            after_ok = i + 3 >= len(clause) or (
                not clause[i + 3].isalnum() and clause[i + 3] != "_"
            )
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
    s = re.sub(r"[ \t]+", " ", sql).strip()

    m_from = re.search(r"\bFROM\b", s, re.IGNORECASE)
    m_where = re.search(r"\bWHERE\b", s, re.IGNORECASE)
    if not m_from or not m_where or m_from.start() > m_where.start():
        return sql

    from_clause = s[m_from.end() : m_where.start()].strip()
    if "," not in from_clause:
        return sql

    table_entries = _parse_from_tables(from_clause)
    if len(table_entries) < 2:
        return sql

    alias_map = {alias.lower(): (alias, tname) for alias, tname in table_entries}
    all_aliases = set(alias_map.keys())

    rest = s[m_where.end() :].strip()
    trailing_m = re.search(
        r"\b(GROUP\s+BY|ORDER\s+BY|HAVING|LIMIT|UNION)\b", rest, re.IGNORECASE
    )
    if trailing_m:
        where_str = rest[: trailing_m.start()].rstrip()
        trailing = "\n" + rest[trailing_m.start() :]
    else:
        trailing = ";" if rest.rstrip().endswith(";") else ""
        where_str = rest.rstrip(";").strip()

    conditions = _split_and_conditions(where_str)
    join_graph = {}
    filter_conds = []
    join_pat = re.compile(r"^(\w+)\.(\w+)\s*=\s*(\w+)\.(\w+)$", re.IGNORECASE)

    for cond in conditions:
        mc = join_pat.match(cond.strip())
        if mc:
            a1, _, a2, _ = mc.groups()
            if (
                a1.lower() in all_aliases
                and a2.lower() in all_aliases
                and a1.lower() != a2.lower()
            ):
                key = tuple(sorted([a1.lower(), a2.lower()]))
                join_graph.setdefault(key, []).append(cond.strip())
                continue
        filter_conds.append(cond.strip())

    # BFS to build JOIN chain
    # Lists, not sets: set order of str changes per process (hash seed), which made the
    # rewritten JOIN order — and so the LLM input and eval results — vary between runs.
    first_alias_lower = table_entries[0][0].lower()
    joined = [first_alias_lower]
    remaining = [e[0].lower() for e in table_entries[1:]]
    join_clauses = []

    for _ in range(len(table_entries)):
        if not remaining:
            break
        progress = False
        for al in list(remaining):
            # Collect conditions to EVERY already-joined table, not just the first match:
            # a dropped condition turns a filter into a cross product and changes the result.
            on_conds = []
            for jl in joined:
                on_conds.extend(join_graph.get(tuple(sorted([al, jl])), []))
            if on_conds:
                on_clause = " AND ".join(on_conds)
                orig_alias, tname = alias_map[al]
                clause = (
                    f"JOIN {tname} AS {orig_alias} ON {on_clause}"
                    if orig_alias.lower() != tname.lower()
                    else f"JOIN {tname} ON {on_clause}"
                )
                join_clauses.append(clause)
                joined.append(al)
                remaining.remove(al)
                progress = True
        if not progress:
            return sql  # disconnected graph — fall back to original

    if remaining:
        return sql

    select_part = s[: m_from.start()].strip()
    orig_first_alias, first_tname = alias_map[first_alias_lower]
    from_part = (
        f"FROM {first_tname} AS {orig_first_alias}"
        if orig_first_alias.lower() != first_tname.lower()
        else f"FROM {first_tname}"
    )

    lines = [select_part, from_part] + join_clauses
    if filter_conds:
        lines.append("WHERE " + "\n  AND ".join(filter_conds))

    return "\n".join(lines) + trailing


def preprocess_sql_node(state: AgentState):
    # Node 0: deterministically normalize SQL before LLM analysis
    original_sql = state.get("original_sql") or ""
    return {"normalized_sql": rewrite_comma_join(original_sql)}


def rewrite_sql_node(state: AgentState):
    # Rule-based SQL rewriting: safe structural rewrites + warnings for risky patterns.
    sql = state.get("normalized_sql") or state.get("original_sql", "")
    rewritten = sql
    rewrites_applied = []
    warnings = []

    # Warn: SELECT * — downstream consumer unknown, can't safely pick columns
    if re.search(r"\bSELECT\s+\*", rewritten, re.IGNORECASE):
        warnings.append(
            "SELECT * detected — list only the columns your application needs "
            "to reduce I/O and enable index-only scans"
        )

    # Warn: correlated EXISTS/NOT EXISTS subquery
    if re.search(r"\b(?:NOT\s+)?EXISTS\s*\(\s*SELECT", rewritten, re.IGNORECASE):
        warnings.append(
            "EXISTS subquery detected — PostgreSQL usually unnests these automatically; "
            "if cost is still high, consider rewriting as a JOIN"
        )

    # Rewrite: DISTINCT where GROUP BY already guarantees uniqueness
    distinct_m = re.search(
        r"SELECT\s+DISTINCT\s+([\w\s,\.]+?)\s+FROM", rewritten, re.IGNORECASE
    )
    groupby_m = re.search(
        r"\bGROUP\s+BY\s+([\w\s,\.]+?)(?:\s+(?:HAVING|ORDER|LIMIT|UNION|$))",
        rewritten,
        re.IGNORECASE | re.DOTALL,
    )
    if distinct_m and groupby_m:
        sel_cols = {
            c.strip().lower().split(".")[-1] for c in distinct_m.group(1).split(",")
        }
        grp_cols = {
            c.strip().lower().split(".")[-1] for c in groupby_m.group(1).split(",")
        }
        if sel_cols and sel_cols == grp_cols:
            rewritten = re.sub(
                r"\bSELECT\s+DISTINCT\b",
                "SELECT",
                rewritten,
                count=1,
                flags=re.IGNORECASE,
            )
            rewrites_applied.append(
                "Removed redundant DISTINCT — GROUP BY already guarantees uniqueness"
            )

    return {
        "rewritten_sql": rewritten if rewrites_applied else None,
        "rewrite_warnings": warnings,
    }


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
            results.append(
                {
                    "table": node.get("Relation Name", "unknown"),
                    "selectivity": round(selectivity, 3),
                    "absolute_rows": actual_rows,
                    "verdict": verdict,
                }
            )
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
    r"\bgin_trgm_ops\b": "CREATE EXTENSION IF NOT EXISTS pg_trgm;",
    r"\bbtree_gin\b": "CREATE EXTENSION IF NOT EXISTS btree_gin;",
}


def inject_extension_deps(optimized_sql: str) -> str:
    # Detect extension-dependent index ops (e.g. gin_trgm_ops) and prepend the required CREATE EXTENSION statement if missing.
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
        return (
            optimized_sql[:line_end]
            + "\n".join(needed)
            + "\n"
            + optimized_sql[line_end:]
        )

    return "\n".join(needed) + "\n" + optimized_sql


def run_explain_node(state: AgentState):
    # Node 1: Execute EXPLAIN ANALYZE
    # DBClient picks SURGEON_READONLY_DATABASE_URL internally for every query it runs
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        return {"error": "DATABASE_URL not set"}

    db_client = DBClient(dsn)

    original_sql = state.get("original_sql")

    if not original_sql:
        return {"error": "No Original SQL found"}

    sql_to_explain = (
        state.get("rewritten_sql") or state.get("normalized_sql") or original_sql
    )

    # Extract table names up front so we can do PK check before EXPLAIN
    # Lowercased: unquoted identifiers are stored lowercase in the catalog ('Title' -> 'title')
    table_names = list(
        set(
            t.lower()
            for t in re.findall(
                r"(?:FROM|JOIN|,)\s+([a-zA-Z_][a-zA-Z0-9_]*)",
                sql_to_explain,
                re.IGNORECASE,
            )
        )
    )

    try:
        plan = db_client.execute_explain(sql_to_explain)

        pk_cols = db_client.get_primary_keys(table_names)
        # Only names that resolve to real tables are keys (CTE names, typos etc. drop out)
        metadata = db_client.get_table_metadata(table_names)

        # A PRIMARY KEY always comes with its index, so the thing worth flagging is a table with no PK at all
        pk_tables = {table for table, _ in pk_cols}
        tables_without_pk = sorted(t for t in metadata if t not in pk_tables)

        enriched_ddl = state.get("ddl") or ""
        # Auto-fetch column definitions if user didn't provide DDL
        if not enriched_ddl.strip():
            for table, info in metadata.items():
                col_defs = ", ".join(f"{name} {dtype}" for name, dtype in info["columns"])
                enriched_ddl += f"CREATE TABLE {table} ({col_defs});\n"

        # Always append existing index information
        for table, info in metadata.items():
            indexes = info["indexes"]
            if indexes:
                lines = "\n".join(f"--   {name}: {defn}" for name, defn in indexes)
                enriched_ddl += f"\n-- Existing indexes on {table}:\n{lines}"
            else:
                enriched_ddl += f"\n-- Existing indexes on {table}: NONE"

        # Tell the LLM (via DDL) and the user (via warnings) about tables missing a PK
        for table in tables_without_pk:
            enriched_ddl += f"\n-- WARNING: table {table} has no PRIMARY KEY"

        # Append, don't replace: rewrite_sql_node already put its warnings in this field
        warnings = (state.get("rewrite_warnings") or []) + [
            f"Table '{table}' has no primary key — duplicate rows are possible and "
            f"lookups/joins on its key column may have no index; consider adding a PRIMARY KEY"
            for table in tables_without_pk
        ]

        return {
            "explain_output": plan,
            "ddl": enriched_ddl,
            "seq_scan_analyses": compute_seq_scan_analysis(plan),
            "rewrite_warnings": warnings,
            "error": None,
        }
    except Exception as e:
        return {"error": f"Database Execution Failure: {str(e)}"}


def identify_issues(state: AgentState):
    # Node 2: use LLM to identify DB issues from EXPLAIN PLAN
    prompt = ChatPromptTemplate.from_messages(
        [
            ("system", ANALYSIS_PROMPT),
            (
                "user",
                "DDL: {ddl}\nExecution_Plan: {execution_plan}\nSeq Scan Analysis: {seq_scan_analyses}\nPrevious feedback: {feedback}",
            ),
        ]
    )

    chain = prompt | llm

    response = chain.invoke(
        {
            "ddl": state.get("ddl"),
            "execution_plan": state.get("explain_output"),
            "seq_scan_analyses": state.get("seq_scan_analyses") or [],
            "feedback": state.get("feedback") or "None",
        }
    )

    usage = getattr(response, "usage_metadata", None) or {}
    _in = usage.get("input_tokens", 0)
    _out = usage.get("output_tokens", 0)

    try:
        issues = json.loads(strip_code_block(response.content))
        return {
            "issues": issues,
            "total_input_tokens": (state.get("total_input_tokens") or 0) + _in,
            "total_output_tokens": (state.get("total_output_tokens") or 0) + _out,
        }
    except json.JSONDecodeError:
        return {"error": f"LLM returned unparseable response: {response.content}"}


def generate_advice(state: AgentState):
    # Node 3: generate advice based on issues found
    prompt = ChatPromptTemplate.from_messages(
        [
            ("system", ADVICE_PROMPT),
            (
                "user",
                "SQL: {original_sql}\nIssues: {issues}\nPrevious review feedback: {feedback}",
            ),
        ]
    )

    chain = prompt | llm

    response = chain.invoke(
        {
            "original_sql": state.get("normalized_sql") or state.get("original_sql"),
            "issues": state.get("issues"),
            "feedback": state.get("feedback") or "None",
        }
    )

    usage = getattr(response, "usage_metadata", None) or {}
    _in = usage.get("input_tokens", 0)
    _out = usage.get("output_tokens", 0)

    try:
        result = json.loads(strip_code_block(response.content))
        return {
            "advice": result["advice"],
            "filtered_indexes": result["indexes"],
            "optimized_sql": inject_extension_deps(result["optimized_sql"]),
            "total_input_tokens": (state.get("total_input_tokens") or 0) + _in,
            "total_output_tokens": (state.get("total_output_tokens") or 0) + _out,
        }
    except (json.JSONDecodeError, KeyError) as e:
        return {"error": f"LLM returned unparseable response: {response.content}"}


def _build_analyze_statement(filtered_indexes: list) -> str:
    # Extract unique table names from filtered index DDLs
    tables = []
    for idx in filtered_indexes or []:
        m = re.search(r"\bON\s+(\w+)\s*\(", idx.get("ddl", ""), re.IGNORECASE)
        if m:
            t = m.group(1).lower()
            if t not in tables:
                tables.append(t)
    if not tables:
        return ""
    return (
        "-- Step 3: Update planner statistics after index creation\nANALYZE "
        + ", ".join(tables)
        + ";"
    )


def review_advice(state: AgentState):
    # Node 4: review advice generated by previous node
    prompt = ChatPromptTemplate.from_messages(
        [
            ("system", REVIEW_ADVICE_PROMPT),
            (
                "user",
                "DDL: {ddl}\nIndexes: {indexes}\nAdvice: {advice}\nOptimized SQL: {optimized_sql}\nIssues: {issues}",
            ),
        ]
    )

    chain = prompt | llm

    response = chain.invoke(
        {
            "ddl": state.get("ddl"),
            "indexes": state.get("filtered_indexes"),
            "advice": state.get("advice"),
            "optimized_sql": state.get("optimized_sql"),
            "issues": state.get("issues"),
        }
    )

    usage = getattr(response, "usage_metadata", None) or {}
    _in = usage.get("input_tokens", 0)
    _out = usage.get("output_tokens", 0)

    try:
        result = json.loads(strip_code_block(response.content))
        new_retry_count = state.get("retry_count") or 0

        if result["verdict"] == "retry":
            new_retry_count += 1

        filtered_sql = result.get("filtered_optimized_sql") or state.get(
            "optimized_sql"
        )
        analyze_stmt = _build_analyze_statement(result.get("filtered_indexes"))
        if analyze_stmt:
            filtered_sql = (filtered_sql or "") + "\n\n" + analyze_stmt

        return {
            "verdict": result["verdict"],
            "feedback": result["feedback"],
            "filtered_indexes": result["filtered_indexes"]
            or state.get("filtered_indexes"),
            "optimized_sql": inject_extension_deps(filtered_sql),
            "retry_count": new_retry_count,
            "total_input_tokens": (state.get("total_input_tokens") or 0) + _in,
            "total_output_tokens": (state.get("total_output_tokens") or 0) + _out,
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

    table_names = list(
        set(
            re.findall(
                r"(?:FROM|JOIN|,)\s+([a-zA-Z_][a-zA-Z0-9_]*)",
                original_sql,
                re.IGNORECASE,
            )
        )
    )
    suggested_ddl = state.get("optimized_sql")

    try:
        new_plan = db_client.benchmark_in_sandbox(
            table_names, original_sql, suggested_ddl
        )
        return {"benchmark_result": new_plan}
    except Exception as e:
        return {"error": f"Database Execution Failure: {str(e)}"}
