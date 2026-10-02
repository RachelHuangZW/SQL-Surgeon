from typing import Annotated, TypedDict, List, Optional

class AgentState(TypedDict):
    original_sql: str
    ddl: str
    run_benchmark: bool
    explain_output: Optional[List[dict]]
    issues: List[str]
    advice: List[str]
    benchmark_result: Optional[List[dict]]
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
    rewritten_sql: Optional[str]
    rewrite_warnings: Optional[List[str]]



