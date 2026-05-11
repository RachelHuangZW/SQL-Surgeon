from typing import Annotated, TypedDict, List, Optional

class AgentState(TypedDict):
    original_sql: str
    ddl: str
    explain_output: Optional[List[dict]]
    issues: List[str]
    advice: List[str]
    optimized_Sql: Optional[str]
    error: Optional[str]



