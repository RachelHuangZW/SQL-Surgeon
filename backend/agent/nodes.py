from agent.state import AgentState
from db.client import DBClient
from agent.prompts import ANALYSIS_PROMPT
from agent.prompts import ADVICE_PROMPT

import json
from dotenv import load_dotenv
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.prompts import ChatPromptTemplate

import os

load_dotenv()
api_key = os.getenv("GOOGLE_API_KEY")

llm = ChatGoogleGenerativeAI(
    model="gemini-1.5-pro",
    google_api_key=api_key,
    temperature=0.1,
)

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

        return {
            "explain_output": plan,
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
        ("user", "DDL: {ddl}\nExecution_Plan: {execution_plan}")
    ])

    chain = prompt | llm

    response = chain.invoke({
        "ddl": state.get("ddl"),
        "execution_plan": state.get("explain_output")
    })
    
    try:
        issues = json.loads(response.content)
        return {"issues": issues}
    except json.JSONDecodeError:
        return {"error": f"LLM returned unparseable response: {response.content}"}


def generate_advice(state: AgentState):
    # Node 3: generate advice based on issues found
    prompt = ChatPromptTemplate.from_messages([
        ("system", ADVICE_PROMPT),
        ("user", "SQL: {original_sql}\nIssues: {issues}")
    ])

    chain = prompt | llm

    response = chain.invoke({
        "original_sql": state.get("original_sql"),
        "issues": state.get("issues")
    })

    try:
        result = json.loads(response.content)
        return {
            "advice": result["advice"],
            "optimized_sql": result["optimized_sql"]
        }
    except (json.JSONDecodeError, KeyError) as e:
        return {"error": f"LLM returned unparseable response: {response.content}"}


def generate_benchmark_schema(state: AgentState):
    # Node 4: create benchmark schema for testing
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        return {"error": "DATABASE_URL not set"}
    
    db_client = DBClient(dsn)

    if not state.get("optimized_sql"):
        return {"error": "No optimized SQL to benchmark"}

    table_name = state.get("table_name")
    original_sql = state.get("original_sql")
    suggested_ddl = state.get("optimized_sql")

    try:
        new_plan = db_client.benchmark_in_sandbox(table_name, original_sql, suggested_ddl)
        return {
            "benchmark_result": new_plan
        }
    except Exception as e:
        return {
            "error": f"Database Execution Failure: {str(e)}"
        }
