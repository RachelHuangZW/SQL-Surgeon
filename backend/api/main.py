from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional

from agent.graph import app as agent_graph

app = FastAPI(
    title = "SQL Surgeon",
    description = "AI-powered PostgreSQL query optimizer",
    version = "1.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # 实际生产环境可以限制为前端域名
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class DiagnoseRequest(BaseModel):
    original_sql: str
    ddl: str
    table_name: str

class DiagnoseResponse(BaseModel):
    status: str
    explain_output: Optional[List[dict]]
    issues: List[str]
    advice: Optional[List[str]]
    benchmark_result: Optional[List[dict]]
    optimized_sql: Optional[str]
    error: Optional[str]

@app.get("/api/health")
def health_check():
    return{"status": "healthy", "engine": "SQL-Surgeon backend is running"}

@app.post("/api/diagnose")
async def diagnose_sql(request: DiagnoseRequest):
    try:
        intial_state = {
            "original_sql": request.original_sql,
            "ddl": request.ddl,
            "table_name": request.table_name,
            "issues": [],
            "advice": []
        }
        final_state = agent_graph.invoke(intial_state)

        if final_state.get("error"):
            return DiagnoseResponse(
                status = "failed",
                explain_output = None,
                issues = final_state.get("issues", []),
                advice = None,
                benchmark_result = None,
                optimized_sql = None,
                error = final_state["error"]
            )
        
        return DiagnoseResponse(
            status = "success",
            explain_output = final_state.get("explain_output"),
            issues = final_state.get("issues", []),
            advice = final_state.get("advice"),
            benchmark_result = final_state.get("benchmark_result"),
            optimized_sql = final_state.get("optimized_sql"),
            error = None
        )

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal Server Error: {str(e)}")

