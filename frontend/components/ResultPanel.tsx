'use client'

import { useState } from 'react'
import { AnalysisResult } from '@/lib/api'

interface Props {
  result: AnalysisResult | null
  loading: boolean
  error: string | null
}

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false)

  async function copy() {
    await navigator.clipboard.writeText(text)
    setCopied(true)
    setTimeout(() => setCopied(false), 2000)
  }

  return (
    <button
      onClick={copy}
      className="text-xs text-slate-400 hover:text-slate-200 px-2 py-1 rounded border border-slate-700 hover:border-slate-500 transition-colors"
    >
      {copied ? '✓ Copied' : 'Copy'}
    </button>
  )
}

function Section({ title, color, children }: {
  title: React.ReactNode
  color: string
  children: React.ReactNode
}) {
  return (
    <section>
      <h3 className={`text-xs font-medium uppercase tracking-wider mb-3 ${color}`}>
        {title}
      </h3>
      {children}
    </section>
  )
}

export default function ResultPanel({ result, loading, error }: Props) {
  if (loading) {
    return (
      <div className="flex flex-col h-full items-center justify-center gap-4">
        <div className="w-7 h-7 border-2 border-violet-500 border-t-transparent rounded-full animate-spin" />
        <div className="text-center">
          <p className="text-sm text-slate-300">Analyzing query...</p>
          <p className="text-xs text-slate-500 mt-1">Running EXPLAIN ANALYZE → identifying issues → generating advice</p>
        </div>
      </div>
    )
  }

  if (error) {
    return (
      <div className="flex h-full items-center justify-center p-6">
        <div className="bg-red-950/50 border border-red-800 rounded-lg p-4 max-w-md w-full">
          <p className="text-sm font-medium text-red-400 mb-1">Request failed</p>
          <p className="text-sm text-red-300/80">{error}</p>
        </div>
      </div>
    )
  }

  if (!result) {
    return (
      <div className="flex flex-col h-full items-center justify-center gap-2 text-center px-8">
        <span className="text-3xl mb-1">🔪</span>
        <p className="text-sm text-slate-400">Paste a slow query and hit Analyze</p>
        <p className="text-xs text-slate-600">The agent will run EXPLAIN ANALYZE, identify bottlenecks, and suggest fixes</p>
      </div>
    )
  }

  if (result.error) {
    return (
      <div className="flex h-full items-center justify-center p-6">
        <div className="bg-red-950/50 border border-red-800 rounded-lg p-4 max-w-md w-full">
          <p className="text-sm font-medium text-red-400 mb-1">Agent error</p>
          <p className="text-sm text-red-300/80">{result.error}</p>
        </div>
      </div>
    )
  }

  return (
    <div className="flex flex-col h-full overflow-y-auto p-5 gap-6">
      {result.execution_time_ms != null && (
        <div className="flex items-center gap-4 bg-slate-900 border border-slate-800 rounded-lg px-4 py-3 text-sm">
          <div className="flex flex-col">
            <span className="text-xs text-slate-500 uppercase tracking-wider">Current execution time</span>
            <span className="text-slate-100 font-mono font-medium">{result.execution_time_ms.toFixed(1)} ms</span>
          </div>
          {result.filtered_indexes && result.filtered_indexes.length > 0 && (
            <>
              <span className="text-slate-700">→</span>
              <div className="flex flex-col">
                <span className="text-xs text-slate-500 uppercase tracking-wider">Expected after indexes</span>
                <span className="text-emerald-400 font-medium">Faster (planner cost reduced)</span>
              </div>
            </>
          )}
        </div>
      )}

      {result.rewrite_warnings && result.rewrite_warnings.length > 0 && (
        <Section title={<>⚡ SQL Anti-patterns <span className="text-slate-500 font-normal normal-case">({result.rewrite_warnings.length})</span></>} color="text-blue-400">
          <ul className="flex flex-col gap-2">
            {result.rewrite_warnings.map((warning, i) => (
              <li key={i} className="flex gap-3 text-sm text-slate-300 bg-slate-900 rounded-lg p-3 border border-slate-800">
                <span className="text-blue-400/70 shrink-0 mt-0.5">ℹ</span>
                <span className="leading-relaxed">{warning}</span>
              </li>
            ))}
          </ul>
        </Section>
      )}

      {result.issues.length > 0 && (
        <Section title={<>⚠ Issues Found <span className="text-slate-500 font-normal normal-case">({result.issues.length})</span></>} color="text-amber-400">
          <ul className="flex flex-col gap-2">
            {result.issues.map((issue, i) => (
              <li key={i} className="flex gap-3 text-sm text-slate-300 bg-slate-900 rounded-lg p-3 border border-slate-800">
                <span className="text-amber-500/70 font-mono text-xs mt-0.5 shrink-0 w-4">{i + 1}.</span>
                <span className="leading-relaxed">{issue}</span>
              </li>
            ))}
          </ul>
        </Section>
      )}

      {result.filtered_indexes && result.filtered_indexes.length > 0 && (
        <Section title={<>✓ Index Recommendations <span className="text-slate-500 font-normal normal-case">({result.filtered_indexes.length})</span></>} color="text-emerald-400">
          <ul className="flex flex-col gap-3">
            {result.filtered_indexes.map((idx, i) => (
              <li key={i} className="flex flex-col gap-1.5 bg-slate-900 rounded-lg p-3 border border-slate-800">
                <div className="flex items-start justify-between gap-2">
                  <code className="text-xs font-mono text-green-300 leading-relaxed">{idx.ddl}</code>
                  <CopyButton text={idx.ddl} />
                </div>
                <p className="text-xs text-slate-400 leading-relaxed">{idx.reason}</p>
              </li>
            ))}
          </ul>
        </Section>
      )}

      {result.optimized_sql && (
        <Section title="Optimized SQL" color="text-violet-400">
          <div className="relative">
            <div className="absolute top-2.5 right-2.5">
              <CopyButton text={result.optimized_sql} />
            </div>
            <pre className="bg-slate-900 border border-slate-800 rounded-lg p-4 pr-20 text-sm font-mono text-green-300 overflow-x-auto whitespace-pre-wrap leading-relaxed">
              {result.optimized_sql}
            </pre>
          </div>
        </Section>
      )}
    </div>
  )
}
