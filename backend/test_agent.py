from dotenv import load_dotenv
load_dotenv()

from agent.graph import app

result = app.invoke({
    "original_sql": "SELECT * FROM clinical_records WHERE diagnosis_code = 'C30' ORDER BY created_at DESC LIMIT 100",
    "ddl": "CREATE TABLE clinical_records (id SERIAL PRIMARY KEY, patient_name TEXT, diagnosis_code TEXT, notes TEXT, created_at TIMESTAMP DEFAULT now());",
    "table_name": None,
    "explain_output": None,
    "issues": [],
    "advice": [],
    "benchmark_result": None,
    "optimized_sql": None,
    "error": None
})

print("Issues:", result.get("issues"))
print("Advice:", result.get("advice"))
print("Optimized SQL:", result.get("optimized_sql"))
print("Error:", result.get("error"))
