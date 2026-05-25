from typing import Annotated, TypedDict, List, Optional

class AgentState(TypedDict):
    original_sql: str
    ddl: str
    table_name: str
    explain_output: Optional[List[dict]]
    issues: List[str]
    advice: List[str]
    benchmark_result: Optional[List[dict]]
    optimized_sql: Optional[str]
    verdict: Optional[str]
    feedback: Optional[str]
    retry_count: int
    error: Optional[str]



