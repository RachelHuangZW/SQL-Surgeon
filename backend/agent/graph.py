from langgraph.graph import StateGraph, END
from agent.state import AgentState
from agent.nodes import (
    run_explain_node,
    identify_issues,
    generate_advice,
    generate_benchmark_schema
)
from db.client import DBClient

def create_graph():
    workflow = StateGraph(AgentState)

    workflow.add_node("run_explain", run_explain_node)
    workflow.add_node("identify_issue", identify_issues)
    workflow.add_node("generate_advice", generate_advice)
    workflow.add_node("generate_benchmark_schema", generate_benchmark_schema)

    workflow.set_entry_point("run_explain")

    workflow.add_edge("run_explain", "identify_issue")
    workflow.add_edge("identify_issue", "generate_advice")
    workflow.add_edge("generate_advice", "generate_benchmark_schema")
    workflow.add_edge("generate_benchmark_schema", END)
    
    app = workflow.compile()
    return app

app = create_graph()