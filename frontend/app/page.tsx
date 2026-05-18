'use client'

import { useState } from 'react'
import InputPanel from '@/components/InputPanel'
import ResultPanel from '@/components/ResultPanel'
import { analyze, AnalysisResult } from '@/lib/api'

export default function Home() {
  const [result, setResult] = useState<AnalysisResult | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function handleAnalyze(sql: string, ddl: string) {
    setLoading(true)
    setError(null)
    setResult(null)
    try {
      const res = await analyze(sql, ddl)
      setResult(res)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Unknown error')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="flex flex-col h-screen">
      <header className="shrink-0 border-b border-slate-800 px-6 py-4 flex items-center gap-3">
        <span className="text-lg">🔪</span>
        <div>
          <h1 className="text-base font-semibold tracking-tight">
            <span className="text-violet-400">SQL</span> Surgeon
          </h1>
          <p className="text-xs text-slate-500">AI-powered PostgreSQL query optimizer</p>
        </div>
      </header>

      <div className="flex flex-1 min-h-0 divide-x divide-slate-800">
        <div className="w-2/5 min-w-0">
          <InputPanel onAnalyze={handleAnalyze} loading={loading} />
        </div>
        <div className="w-3/5 min-w-0">
          <ResultPanel result={result} loading={loading} error={error} />
        </div>
      </div>
    </div>
  )
}
