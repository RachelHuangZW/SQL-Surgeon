'use client'

import { useState } from 'react'
import { AnalysisResult, PlanRow } from '@/lib/api'

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
      className="text-xs text-slate-600 hover:text-slate-800 px-2 py-1 rounded border border-slate-300 hover:border-slate-400 transition-colors"
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

const DIRECTION_STYLE: Record<PlanRow['direction'], string> = {
  ok: 'text-slate-500',
  over: 'text-sky-600',
  under: 'text-amber-600',
  'not run': 'text-slate-400 italic',
}

function PlanTable({ rows }: { rows: PlanRow[] }) {
  // Open by default: it has the Diagnosis tab to itself, so it no longer makes the page long
  const [open, setOpen] = useState(true)

  return (
    <div className="bg-white border border-slate-200 rounded-lg">
      <button
        onClick={() => setOpen(!open)}
        className="w-full flex items-center justify-between px-4 py-2.5 text-sm text-slate-700 hover:text-slate-900"
      >
        <span>{open ? '▾' : '▸'} {rows.length} plan nodes — estimated vs actual rows</span>
      </button>
      {open && (
        <div className="overflow-x-auto border-t border-slate-200">
          <table className="w-full text-xs font-mono">
            <thead className="text-slate-500">
              <tr className="text-left whitespace-nowrap">
                {/* Sticky + opaque background: node names stay visible while the numbers scroll under them */}
                <th className="px-2 py-2 font-normal sticky left-0 z-10 bg-white border-r border-slate-200">Node</th>
                <th className="px-2 py-2 font-normal text-right">Est. rows</th>
                <th className="px-2 py-2 font-normal text-right" title="Average per loop. Total rows = actual × loops">
                  Actual rows / loop
                </th>
                <th className="px-2 py-2 font-normal text-right">Loops</th>
                <th className="px-2 py-2 font-normal text-right">q-error</th>
                <th className="px-2 py-2 font-normal">Dir</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.id} className="border-t border-slate-100 align-top whitespace-nowrap">
                  {/* Indentation shows nesting: inner nodes run first and feed the outer ones */}
                  <td
                    className="px-2 py-1.5 sticky left-0 z-10 bg-white border-r border-slate-200"
                    style={{ paddingLeft: 8 + r.depth * 12 }}
                  >
                    <div className="text-slate-800 whitespace-nowrap">
                      {r.op}
                      {r.table && (
                        <span className="text-slate-600"> on {r.table}{r.alias !== r.table ? ` ${r.alias}` : ''}</span>
                      )}
                    </div>
                    {r.condition && (
                      <div className="text-slate-500 truncate max-w-xs" title={r.condition}>{r.condition}</div>
                    )}
                  </td>
                  <td className="px-2 py-1.5 text-right text-slate-700">{r.plan_rows.toLocaleString()}</td>
                  <td className="px-2 py-1.5 text-right text-slate-700">{r.actual_rows.toLocaleString()}</td>
                  <td className="px-2 py-1.5 text-right text-slate-600">{r.loops.toLocaleString()}</td>
                  <td className={`px-2 py-1.5 text-right ${r.q_error != null && r.q_error >= 10 ? 'text-red-600' : 'text-slate-700'}`}>
                    {r.q_error != null ? r.q_error.toFixed(1) : '–'}
                  </td>
                  <td className={`px-2 py-1.5 ${DIRECTION_STYLE[r.direction]}`}>{r.direction}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

function EmptyTab({ children }: { children: React.ReactNode }) {
  return (
    <div className="text-sm text-slate-500 bg-slate-50 border border-dashed border-slate-200 rounded-lg p-6 text-center">
      {children}
    </div>
  )
}

type TabId = 'summary' | 'diagnosis' | 'sql' | 'db'

// Shown first, generated last: Summary merges the other tabs' results
function ResultTabs({ result }: { result: AnalysisResult }) {
  // Own component (not inside ResultPanel) so each new analysis remounts it and lands on Summary again
  const [tab, setTab] = useState<TabId>('summary')

  const indexCount = result.filtered_indexes?.length ?? 0
  const tabs: { id: TabId; label: string; count?: number }[] = [
    { id: 'summary', label: 'Summary' },
    { id: 'diagnosis', label: 'Diagnosis', count: result.issues.length },
    { id: 'sql', label: 'SQL changes' },
    { id: 'db', label: 'Database changes', count: indexCount },
  ]

  return (
    <>
      <div className="flex gap-1 border-b border-slate-200">
        {tabs.map((t) => (
          <button
            key={t.id}
            onClick={() => setTab(t.id)}
            className={`px-3 py-2 text-sm -mb-px border-b-2 transition-colors ${
              tab === t.id
                ? 'border-violet-500 text-slate-900'
                : 'border-transparent text-slate-500 hover:text-slate-700'
            }`}
          >
            {t.label}
            {t.count ? <span className="ml-1.5 text-xs text-slate-500">{t.count}</span> : null}
          </button>
        ))}
      </div>

      {tab === 'summary' && (
        <>
          {result.advice && result.advice.length > 0 && (
            <Section title="Recommendations" color="text-violet-600">
              <ul className="flex flex-col gap-2">
                {result.advice.map((item, i) => (
                  <li key={i} className="flex gap-3 text-sm text-slate-700 bg-white rounded-lg p-3 border border-slate-200">
                    <span className="text-violet-600/70 font-mono text-xs mt-0.5 shrink-0 w-4">{i + 1}.</span>
                    <span className="leading-relaxed">{item}</span>
                  </li>
                ))}
              </ul>
            </Section>
          )}
          {indexCount > 0 && (
            <button
              onClick={() => setTab('db')}
              className="text-left text-sm text-slate-700 bg-white rounded-lg p-3 border border-slate-200 hover:border-slate-400"
            >
              <span className="text-emerald-600">{indexCount} index recommendation{indexCount > 1 ? 's' : ''}</span>
              <span className="text-slate-500"> → see Database changes</span>
            </button>
          )}
          <EmptyTab>A merged, prioritized list of SQL and database changes is coming soon.</EmptyTab>
        </>
      )}

      {tab === 'diagnosis' && (
        <>
          {result.plan_table && result.plan_table.length > 0 && (
            <Section title="Execution Plan" color="text-slate-600">
              <PlanTable rows={result.plan_table} />
            </Section>
          )}

          {result.issues.length > 0 && (
            <Section title={<>⚠ Issues Found <span className="text-slate-500 font-normal normal-case">({result.issues.length})</span></>} color="text-amber-600">
              <ul className="flex flex-col gap-2">
                {result.issues.map((issue, i) => (
                  <li key={i} className="flex gap-3 text-sm text-slate-700 bg-white rounded-lg p-3 border border-slate-200">
                    <span className="text-amber-600/70 font-mono text-xs mt-0.5 shrink-0 w-4">{i + 1}.</span>
                    <span className="leading-relaxed">{issue}</span>
                  </li>
                ))}
              </ul>
            </Section>
          )}

          {/* Mixes SQL anti-patterns, missing PKs and skipped small-table indexes; splitting them needs separate backend fields */}
          {result.rewrite_warnings && result.rewrite_warnings.length > 0 && (
            <Section title={<>⚡ Warnings <span className="text-slate-500 font-normal normal-case">({result.rewrite_warnings.length})</span></>} color="text-blue-600">
              <ul className="flex flex-col gap-2">
                {result.rewrite_warnings.map((warning, i) => (
                  <li key={i} className="flex gap-3 text-sm text-slate-700 bg-white rounded-lg p-3 border border-slate-200">
                    <span className="text-blue-600/70 shrink-0 mt-0.5">ℹ</span>
                    <span className="leading-relaxed">{warning}</span>
                  </li>
                ))}
              </ul>
            </Section>
          )}
        </>
      )}

      {tab === 'sql' && (
        <EmptyTab>SQL and application-level suggestions (for developers) are coming soon.</EmptyTab>
      )}

      {tab === 'db' && (
        <>
          {indexCount === 0 && <EmptyTab>No database changes recommended.</EmptyTab>}

          {result.filtered_indexes && result.filtered_indexes.length > 0 && (
            <Section title={<>✓ Index Recommendations <span className="text-slate-500 font-normal normal-case">({result.filtered_indexes.length})</span></>} color="text-emerald-600">
              <ul className="flex flex-col gap-3">
                {result.filtered_indexes.map((idx, i) => (
                  <li key={i} className="flex flex-col gap-1.5 bg-white rounded-lg p-3 border border-slate-200">
                    <div className="flex items-start justify-between gap-2">
                      <code className="text-xs font-mono text-green-700 leading-relaxed">{idx.ddl}</code>
                      <CopyButton text={idx.ddl} />
                    </div>
                    <p className="text-xs text-slate-600 leading-relaxed">{idx.reason}</p>
                  </li>
                ))}
              </ul>
            </Section>
          )}

          {result.optimized_sql && (
            <Section title="Optimized SQL" color="text-violet-600">
              <div className="relative">
                <div className="absolute top-2.5 right-2.5">
                  <CopyButton text={result.optimized_sql} />
                </div>
                <pre className="bg-white border border-slate-200 rounded-lg p-4 pr-20 text-sm font-mono text-green-700 overflow-x-auto whitespace-pre-wrap leading-relaxed">
                  {result.optimized_sql}
                </pre>
              </div>
            </Section>
          )}
        </>
      )}
    </>
  )
}

export default function ResultPanel({ result, loading, error }: Props) {
  if (loading) {
    return (
      <div className="flex flex-col h-full items-center justify-center gap-4">
        <div className="w-7 h-7 border-2 border-violet-500 border-t-transparent rounded-full animate-spin" />
        <div className="text-center">
          <p className="text-sm text-slate-700">Analyzing query...</p>
          <p className="text-xs text-slate-500 mt-1">Running EXPLAIN ANALYZE → identifying issues → generating advice</p>
        </div>
      </div>
    )
  }

  if (error) {
    return (
      <div className="flex h-full items-center justify-center p-6">
        <div className="bg-red-50 border border-red-200 rounded-lg p-4 max-w-md w-full">
          <p className="text-sm font-medium text-red-600 mb-1">Request failed</p>
          <p className="text-sm text-red-700/80">{error}</p>
        </div>
      </div>
    )
  }

  if (!result) {
    return (
      <div className="flex flex-col h-full items-center justify-center gap-2 text-center px-8">
        <p className="text-sm text-slate-600">Paste a slow query and hit Analyze</p>
        <p className="text-xs text-slate-400">The agent will run EXPLAIN ANALYZE, identify bottlenecks, and suggest fixes</p>
      </div>
    )
  }

  if (result.error) {
    return (
      <div className="flex h-full items-center justify-center p-6">
        <div className="bg-red-50 border border-red-200 rounded-lg p-4 max-w-md w-full">
          <p className="text-sm font-medium text-red-600 mb-1">Agent error</p>
          <p className="text-sm text-red-700/80">{result.error}</p>
        </div>
      </div>
    )
  }

  return (
    <div className="flex flex-col h-full overflow-y-auto p-5 gap-6">
      {result.execution_time_ms != null && (
        <div className="flex items-center gap-4 bg-white border border-slate-200 rounded-lg px-4 py-3 text-sm">
          <div className="flex flex-col">
            <span className="text-xs text-slate-500 uppercase tracking-wider">Current execution time</span>
            <span className="text-slate-900 font-mono font-medium">{result.execution_time_ms.toFixed(1)} ms</span>
          </div>
          {result.filtered_indexes && result.filtered_indexes.length > 0 && (
            <>
              <span className="text-slate-300">→</span>
              <div className="flex flex-col">
                <span className="text-xs text-slate-500 uppercase tracking-wider">Expected after indexes</span>
                <span className="text-emerald-600 font-medium">Faster (planner cost reduced)</span>
              </div>
            </>
          )}
        </div>
      )}

      <ResultTabs result={result} />
    </div>
  )
}
