const API_URL = process.env.NEXT_PUBLIC_API_URL ?? 'http://localhost:8000'

export interface IndexRecommendation {
  ddl: string
  reason: string
}

// One plan node, flattened by backend sql_surgeon/plan/table.py (depth-first order)
export interface PlanRow {
  id: number
  parent_id: number | null
  depth: number
  op: string
  table: string
  alias: string
  plan_rows: number
  actual_rows: number   // average per loop, not the total
  loops: number
  q_error: number | null   // null when the node never ran
  direction: 'ok' | 'over' | 'under' | 'not run'
  condition: string
  sort_method: string     // '' unless a Sort / Incremental Sort
  sort_spilled: boolean   // true if the leader or any worker sorted on disk
  sort_disk_kb: number    // largest spill of any one process
}

// Computed by backend sql_surgeon/plan/estimates.py; every id is a PlanRow id
// Origin: a node whose row estimate is badly wrong and not just inherited from its children
export interface EstimateOrigin {
  id: number
  q_error: number
  direction: 'over' | 'under'
  child_max_q: number   // largest q-error among its children (1 = all exact)
}

// Consequence: a slow operation caused by a misestimate
export interface EstimateConsequence {
  id: number
  type: 'hash_spill' | 'nested_loop_rescan' | 'sort_spill'
  time_share: number   // fraction of execution time (0.3 = 30%); can exceed 1 in parallel plans
  planned_batches?: number   // hash_spill only
  actual_batches?: number
  planned_loops?: number     // nested_loop_rescan only
  actual_loops?: number
  sort_method?: string       // sort_spill only
  disk_kb?: number
  input_id?: number          // the Sort's input node: its row counts are complete, the Sort's own may be cut short
  input_plan_rows?: number
  input_actual_rows?: number
  input_q_error?: number | null
  input_direction?: PlanRow['direction']
}

export interface EstimateAnalysis {
  origins: EstimateOrigin[]
  consequences: EstimateConsequence[]
}

export interface AnalysisResult {
  status: string
  rewrite_warnings: string[] | null
  explain_output: object[] | null
  plan_table: PlanRow[] | null
  estimate_analysis: EstimateAnalysis | null
  issues: string[]
  issue_node_ids: number[][] | null   // issue_node_ids[i] = PlanRow ids that issues[i] is about
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
