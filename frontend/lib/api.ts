const API_URL = process.env.NEXT_PUBLIC_API_URL ?? 'http://localhost:8000'

export interface IndexRecommendation {
  ddl: string
  reason: string
}

export interface AnalysisResult {
  status: string
  rewrite_warnings: string[] | null
  explain_output: object[] | null
  issues: string[]
  advice: string[] | null
  filtered_indexes: IndexRecommendation[] | null
  benchmark_result: object[] | null
  optimized_sql: string | null
  execution_time_ms: number | null
  error: string | null
}

export async function analyze(sql: string, ddl: string, table_name = ''): Promise<AnalysisResult> {
  const response = await fetch(`${API_URL}/api/diagnose`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ original_sql: sql, ddl, table_name }),
  })

  if (!response.ok) {
    throw new Error(`API returned ${response.status}: ${await response.text()}`)
  }

  return response.json()
}
