from langgraph.graph import StateGraph, END
from agent.state import AgentState
from agent.nodes import (
    preprocess_sql_node,
    rewrite_sql_node,
    run_explain_node,
    identify_issues,
    generate_advice,
    review_advice,
    generate_benchmark_schema
)

def should_continue(state: AgentState):
    if state.get("error"):
        return "end"
    return "continue"

##def should_benchmark(state: AgentState):
    ##if state.get("error"):
        ##return "end"
    ##if state.get("table_name"):
        ##return "benchmark"
    ##return "end"

def route_after_review(state: AgentState):
    if state.get("error"):
        return "end"
    if (state.get("retry_count") or 0) >= 2:
        return "end"
    if state.get("verdict") == "retry":
        return "retry"
    if state.get("run_benchmark"):
        return "benchmark"
    return "end"


def create_graph():
    workflow = StateGraph(AgentState)

    workflow.add_node("preprocess_sql", preprocess_sql_node)
    workflow.add_node("rewrite_sql", rewrite_sql_node)
    workflow.add_node("run_explain", run_explain_node)
    workflow.add_node("identify_issue", identify_issues)
    workflow.add_node("generate_advice", generate_advice)
    workflow.add_node("review_advice", review_advice)
    workflow.add_node("generate_benchmark_schema", generate_benchmark_schema)

    workflow.set_entry_point("preprocess_sql")
    workflow.add_edge("preprocess_sql", "rewrite_sql")
    workflow.add_edge("rewrite_sql", "run_explain")

    workflow.add_conditional_edges("run_explain", should_continue, {"continue": "identify_issue", "end": END})
    workflow.add_conditional_edges("identify_issue", should_continue, {"continue": "generate_advice", "end": END})
    workflow.add_edge("generate_advice", "review_advice")
    workflow.add_conditional_edges("review_advice", route_after_review, {"retry": "generate_advice", "benchmark": "generate_benchmark_schema", "end": END})
    workflow.add_edge("generate_benchmark_schema", END)
    
    app = workflow.compile()
    return app

app = create_graph()