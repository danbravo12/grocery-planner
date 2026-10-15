import { useState } from 'react'
import { api, ApiError, type RunView, type Step } from '../api/client'
import { seconds } from '../lib/format'
import { Badge } from './ui'

const LABEL: Record<string, string> = {
  ingredient: 'Ingredient agent',
  price: 'Price agent',
  planner: 'Planner',
  reprice: 'Price agent (re-check)',
  planner_2: 'Planner (second look)',
  stop: 'Stop check',
}

interface Out {
  items?: number | unknown[]
  cache_hits?: number | null
  cache_misses?: number | null
  rounds?: number | null
  flags?: { item_id: string; kind: string }[]
  downgrades?: { item_id: string; kind?: string | null; from?: string | null; to?: string; reason?: string | null }[]
  recommendation?: string
  confidence?: string
  overridden?: boolean
  override_reason?: string | null
}

function groupBy<T>(rows: T[], key: (row: T) => string): [string, T[]][] {
  const out = new Map<string, T[]>()
  for (const row of rows) out.set(key(row), [...(out.get(key(row)) ?? []), row])
  return [...out.entries()]
}

function StepDetails({ step, names }: { step: Step; names: Record<string, string> }) {
  const out = (step.output ?? {}) as Out
  const downgrades = out.downgrades ?? []
  const flags = out.flags ?? []
  const nm = (id: string) => names[id] ?? id
  return (
    <div className="space-y-3 border-t border-dashed border-stone-300 px-3 py-3 text-sm">
      {step.summary && <p className="text-stone-700">{step.summary}</p>}
      {step.name === 'ingredient' && out.cache_hits !== undefined && out.cache_hits !== null && (
        <p>
          Dish cache: <strong>{out.cache_hits}</strong> hit{out.cache_hits === 1 ? '' : 's'},{' '}
          <strong>{out.cache_misses ?? 0}</strong> expanded by the model.
        </p>
      )}
      {(step.name === 'price' || step.name === 'reprice') && (
        <>
          <p>
            Model calls in this step: <strong>{out.rounds ?? 0}</strong> (a second one means a retry round).
          </p>
          <div>
            <p className="font-semibold">Downgrades by reason</p>
            {downgrades.length === 0 ? (
              <p className="text-stone-600">None: every pick kept the tier the model gave it.</p>
            ) : (
              <ul className="mt-1 space-y-1">
                {groupBy(downgrades, (d) => d.kind ?? 'other').map(([kind, rows]) => (
                  <li key={kind}>
                    <Badge tone="warn">{kind.replace(/_/g, ' ')}</Badge> × {rows.length}:{' '}
                    {rows.map((d) => `${nm(d.item_id)} (${d.from ?? '?'} to ${d.to})`).join(', ')}
                  </li>
                ))}
              </ul>
            )}
          </div>
          {flags.length > 0 && (
            <p>
              Flags: {groupBy(flags, (f) => f.kind).map(([kind, rows]) => `${kind.replace(/_/g, ' ')} × ${rows.length}`).join(', ')}
            </p>
          )}
        </>
      )}
      {step.tool_calls.length > 0 && (
        <div>
          <p className="font-semibold">Tool calls</p>
          <ul className="mt-1 space-y-2">
            {step.tool_calls.map((t, i) => (
              <li key={i} className="rounded-md bg-stone-100 p-2">
                <p className="font-mono text-xs">
                  {t.name}({JSON.stringify(t.args)}) {t.latency_ms !== null && t.latency_ms !== undefined ? `· ${Math.round(t.latency_ms)} ms` : ''}
                </p>
                <p className="mt-1 break-words font-mono text-xs text-stone-700">{t.error ? `error: ${t.error}` : t.result}</p>
              </li>
            ))}
          </ul>
        </div>
      )}
      {step.error && (
        <p>
          <Badge tone="bad">Failed</Badge> {step.error}
        </p>
      )}
    </div>
  )
}

export function TracePanel({ run }: { run: RunView }) {
  const [state, setState] = useState<
    { phase: 'idle' } | { phase: 'loading' } | { phase: 'error'; message: string } | { phase: 'ready'; steps: Step[] }
  >({ phase: 'idle' })
  const names = Object.fromEntries(run.comparison.map((c) => [c.item_id, c.name]))

  function onToggle(event: React.SyntheticEvent<HTMLDetailsElement>) {
    if (!event.currentTarget.open || state.phase === 'loading' || state.phase === 'ready') return
    setState({ phase: 'loading' })
    api
      .getSteps(run.id)
      .then((r) => setState({ phase: 'ready', steps: r.steps }))
      .catch((e) => setState({ phase: 'error', message: e instanceof ApiError ? e.message : 'Could not load the steps.' }))
  }

  const t = run.totals
  return (
    <details onToggle={onToggle} className="rounded-xl border border-stone-200 bg-white p-4 shadow-sm sm:p-6">
      <summary className="cursor-pointer text-lg font-semibold">How it worked</summary>
      <div className="mt-3">
        <p className="text-sm text-stone-600">
          What each agent did, in order. Inputs and prompts are not shown; the numbers are the real ones for this run.
        </p>
        {state.phase === 'loading' && <p role="status" className="mt-3 text-stone-600">Loading the steps…</p>}
        {state.phase === 'error' && <p role="alert" className="mt-3 text-red-700">{state.message}</p>}
        {state.phase === 'ready' && (
          <>
            <ol className="mt-3 space-y-2">
              {state.steps.map((step) => (
                <li key={step.seq}>
                  <details className="rounded-lg border border-stone-200">
                    <summary className="cursor-pointer list-none px-3 py-2">
                      <span className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
                        <span className="font-medium">
                          {step.seq}. {LABEL[step.name] ?? step.name}
                          {step.status === 'failed' && <span className="ml-2"><Badge tone="bad">failed</Badge></span>}
                          {step.fell_back && <span className="ml-2"><Badge tone="warn">fallback model</Badge></span>}
                        </span>
                        <span className="text-sm tabular-nums text-stone-700">{seconds(step.latency_ms)}</span>
                      </span>
                      <span className="mt-1 block text-sm text-stone-600">
                        {step.model ?? 'no model call'} · {step.requests} request{step.requests === 1 ? '' : 's'} ·{' '}
                        {step.tokens_in} in / {step.tokens_out} out tokens
                        {step.thinking_tokens ? ` / ${step.thinking_tokens} thinking` : ''}
                      </span>
                    </summary>
                    <StepDetails step={step} names={names} />
                  </details>
                </li>
              ))}
            </ol>
            <p className="mt-3 text-sm text-stone-700">
              <strong>{t.requests}</strong> of the <strong>{t.request_budget}</strong> requests reserved for this run were used
              (the reservation is the worst case, and the rest is returned to the day&apos;s budget).
            </p>
          </>
        )}
      </div>
    </details>
  )
}
