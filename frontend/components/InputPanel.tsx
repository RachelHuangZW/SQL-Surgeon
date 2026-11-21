'use client'

import { useState } from 'react'

interface Props {
  onAnalyze: (sql: string, ddl: string, runBenchmark: boolean) => void
  loading: boolean
}

export default function InputPanel({ onAnalyze, loading }: Props) {
  const [sql, setSql] = useState('')
  const [ddl, setDdl] = useState('')
  const [benchmark, setBenchmark] = useState(false)

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    if (!sql.trim()) return
    onAnalyze(sql, ddl, benchmark)
  }

  return (
    <form onSubmit={handleSubmit} className="flex flex-col h-full p-5 gap-4">
      <div className="flex flex-col gap-1.5">
        <label className="text-xs font-medium text-slate-600 uppercase tracking-wider">
          SQL Query
        </label>
        <textarea
          value={sql}
          onChange={e => setSql(e.target.value)}
          placeholder={"SELECT * FROM clinical_records\nWHERE diagnosis_code = 'C30'\nORDER BY created_at DESC\nLIMIT 100;"}
          className="font-mono text-sm bg-white border border-slate-300 rounded-lg p-3 text-slate-900 placeholder-slate-400 resize-none h-52 focus:outline-none focus:ring-1 focus:ring-violet-500 focus:border-violet-500 transition-colors"
          spellCheck={false}
        />
      </div>

      <div className="flex flex-col gap-1.5">
        <label className="text-xs font-medium text-slate-600 uppercase tracking-wider">
          DDL{' '}
          <span className="text-slate-400 normal-case font-normal">
            — optional, improves analysis
          </span>
        </label>
        <textarea
          value={ddl}
          onChange={e => setDdl(e.target.value)}
          placeholder={"CREATE TABLE clinical_records (\n  id SERIAL PRIMARY KEY,\n  diagnosis_code TEXT,\n  created_at TIMESTAMP\n);"}
          className="font-mono text-sm bg-white border border-slate-300 rounded-lg p-3 text-slate-900 placeholder-slate-400 resize-none h-36 focus:outline-none focus:ring-1 focus:ring-violet-500 focus:border-violet-500 transition-colors"
          spellCheck={false}
        />
      </div>

      <div className="mt-auto flex flex-col gap-3">
        <label className="flex items-start gap-2 text-sm text-slate-700 cursor-pointer">
          <input
            type="checkbox"
            checked={benchmark}
            onChange={e => setBenchmark(e.target.checked)}
            className="mt-0.5 accent-violet-600"
          />
          <span>
            Benchmark the suggested indexes
            <span className="block text-xs text-slate-400">
              Runs the query before and after them on copies of ≤100k rows per table. Needs SURGEON_SANDBOX_DATABASE_URL.
            </span>
          </span>
        </label>
        <button
          type="submit"
          disabled={loading || !sql.trim()}
          className="w-full bg-violet-600 hover:bg-violet-500 disabled:bg-slate-200 disabled:text-slate-400 disabled:cursor-not-allowed text-white font-medium py-2.5 px-4 rounded-lg transition-colors text-sm flex items-center justify-center gap-2"
        >
          {loading ? (
            <>
              <span className="w-3.5 h-3.5 border-2 border-white/30 border-t-white rounded-full animate-spin" />
              Analyzing...
            </>
          ) : (
            'Analyze Query →'
          )}
        </button>
      </div>
    </form>
  )
}
