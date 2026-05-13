from langgraph.graph import StateGraph, END
from agent.state import AgentState
from agent.nodes import (
    run_explain_node,
    identify_issues,
    generate_advice,
    generate_benchmark_schema
)

def should_continue(state: AgentState):
    if state.get("error"):
        return "end"
    return "continue"

def should_benchmark(state: AgentState):
    if state.get("error"):
        return "end"
    if state.get("table_name"):
        return "benchmark"
    return "end"


def create_graph():
    workflow = StateGraph(AgentState)

    workflow.add_node("run_explain", run_explain_node)
    workflow.add_node("identify_issue", identify_issues)
    workflow.add_node("generate_advice", generate_advice)
    workflow.add_node("generate_benchmark_schema", generate_benchmark_schema)

    workflow.set_entry_point("run_explain")

    workflow.add_conditional_edges("run_explain", should_continue, {"continue": "identify_issue", "end": END})
    workflow.add_conditional_edges("identify_issue", should_continue, {"continue": "generate_advice", "end": END})
    workflow.add_conditional_edges("generate_advice", should_benchmark, {"benchmark": "generate_benchmark_schema", "end": END})
    workflow.add_edge("generate_benchmark_schema", END)
    
    app = workflow.compile()
    return app

app = create_graph()