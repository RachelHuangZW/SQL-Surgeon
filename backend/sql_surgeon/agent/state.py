from typing import Annotated, TypedDict, List, Optional

class AgentState(TypedDict):
    original_sql: str
    ddl: str
    run_benchmark: bool
    explain_output: Optional[List[dict]]
    issues: List[str]
    advice: List[str]
    benchmark_result: Optional[List[dict]]      # EXPLAIN ANALYZE with the suggested indexes
    benchmark_baseline: Optional[List[dict]]    # the same copies before them (#16)
    benchmark_summary: Optional[dict]           # plan/benchmark.py benchmark_summary
    benchmark_error: Optional[str]              # why there is no benchmark; the rest of the result is still valid
    optimized_sql: Optional[str]
    verdict: Optional[str]
    feedback: Optional[str]
    retry_count: int
    error: Optional[str]
    total_input_tokens: Optional[int]
    total_output_tokens: Optional[int]
    seq_scan_analyses: Optional[List[dict]]
    normalized_sql: Optional[str]
    filtered_indexes: Optional[List[dict]]
    rewrite_warnings: Optional[List[str]]
    small_tables: Optional[dict]
    plan_table: Optional[List[dict]]
    estimate_analysis: Optional[dict]
    issue_node_ids: Optional[List[List[int]]]



