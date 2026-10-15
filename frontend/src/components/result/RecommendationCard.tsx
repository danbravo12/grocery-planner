import type { Condition, Recommendation, RunView } from '../../api/client'
import { money, shortDate, title } from '../../lib/format'
import { Badge } from '../ui'

const CONFIDENCE = {
  high: { tone: 'accent' as const, text: 'nearly everything matched like for like' },
  medium: { tone: 'warn' as const, text: 'some matches are not exact, see the flags below' },
  low: { tone: 'bad' as const, text: 'treat this as a rough guide and check the flagged items' },
}

const USABLE = new Set(['exact', 'equivalent'])

function nameOf(run: RunView, itemId: string): string {
  return run.comparison.find((c) => c.item_id === itemId)?.name ?? itemId
}

function fallbackText(condition: Condition): string {
  const fb = condition.fallback
  if (fb.kind === 'one_store' && fb.store) return `${title(fb.store)} is ${money(fb.gap)} cheaper`
  if (fb.kind === 'split') return `a split saves ${money(fb.split_savings)}`
  return `no meaningful difference (the stores are ${money(fb.gap)} apart, against a ${money(fb.threshold)} threshold)`
}

export function verdictHeading(rec: Recommendation): string {
  if (rec.recommendation === 'one_store' && rec.store) return `Shop at ${title(rec.store)}`
  if (rec.recommendation === 'split') return 'Split your shop'
  return 'No meaningful difference'
}

/** The gap and the threshold in one plain line. */
export function gapLine(run: RunView, rec: Recommendation, stores: string[]): string {
  const v = rec.rule_verdict
  const common = run.comparison.filter((c) => stores.every((s) => c.stores[s] && USABLE.has(c.stores[s].match_tier))).length
  const items = `${common} common item${common === 1 ? '' : 's'}`
  const threshold = money(v.threshold)
  if (rec.recommendation === 'split') {
    return `Splitting saves ${money(rec.savings)} across ${items}, enough to clear the ${threshold} threshold.`
  }
  const lowest = [...stores].sort((a, b) => Number(v.totals[a]) - Number(v.totals[b]))[0] ?? ''
  if (Number(v.gap) === 0) return `The stores cost the same across ${items}.`
  const cheaper = `${title(lowest)} is ${money(v.gap)} cheaper across ${items}`
  return rec.recommendation === 'one_store'
    ? `${cheaper}, over the ${threshold} threshold.`
    : `${cheaper}; it takes ${threshold} to be worth acting on.`
}

export function RecommendationCard({ run }: { run: RunView }) {
  const rec = run.recommendation
  if (!rec) return null
  const stores = Object.keys(rec.rule_verdict.totals)
  const totals = rec.rule_verdict.totals
  const condition = rec.rule_verdict.condition
  const conf = CONFIDENCE[rec.confidence]
  const lowest = stores.reduce<string | null>((best, s) => (best === null || Number(totals[s]) < Number(totals[best]) ? s : best), null)
  const tied = stores.every((s) => Number(totals[s]) === Number(totals[lowest ?? s]))
  const assignment = Object.entries(rec.split_assignment ?? {})
  const elsewhere = assignment.filter(([, st]) => st !== (lowest ?? ''))

  return (
    <section aria-labelledby="verdict" className="rounded-xl border-2 border-accent bg-white p-5 shadow-sm sm:p-8">
      <p className="text-sm font-medium uppercase tracking-wide text-accent-strong">Our recommendation</p>
      <h1 id="verdict" className="mt-1 text-3xl font-bold tracking-tight sm:text-4xl">
        {verdictHeading(rec)}
      </h1>

      <dl className="mt-5 grid grid-cols-2 gap-3 sm:gap-4" aria-label="Basket totals by store">
        {stores.map((s) => {
          const isLowest = !tied && s === lowest
          return (
            <div
              key={s}
              className={`rounded-lg border p-3 sm:p-4 ${isLowest ? 'border-accent bg-accent-soft' : 'border-stone-200 bg-stone-50'}`}
            >
              <dt className="flex flex-wrap items-center gap-2 text-sm font-medium text-stone-700">
                {title(s)}
                {isLowest && <Badge tone="accent">Lowest</Badge>}
              </dt>
              <dd className="mt-1 font-mono text-3xl font-bold tabular-nums sm:text-4xl">{money(totals[s])}</dd>
            </div>
          )
        })}
      </dl>
      <p className="mt-3 text-lg text-stone-800">{gapLine(run, rec, stores)}</p>

      {rec.recommendation === 'split' && elsewhere.length > 0 && (
        <p className="mt-3 text-stone-800">
          Buy {elsewhere.map(([id, st]) => `${nameOf(run, id)} at ${title(st)}`).join(', ')}, and the rest at{' '}
          {title(lowest ?? '')}.
        </p>
      )}

      {condition && (
        <div className="mt-4 rounded-lg border border-amber-300 bg-amber-50 p-4 text-amber-950">
          <p className="font-semibold">This depends on a special that ends {shortDate(condition.valid_until)}</p>
          <p className="mt-1 text-sm">
            If you shop by {shortDate(condition.valid_until)} ({condition.items.map((i) => nameOf(run, i)).join(' and ')}{' '}
            on special), the result is as above. After that: {fallbackText(condition)}.
          </p>
        </div>
      )}

      {rec.overridden && rec.override_reason === 'match_quality' && (
        <p className="mt-4 rounded-lg border border-amber-300 bg-amber-50 p-3 text-sm text-amber-950">
          The cheaper-looking result was held back because some matches are not like for like.
        </p>
      )}

      <div className="mt-5">
        <p className="text-xs font-semibold uppercase tracking-wide text-stone-500">Why</p>
        <p className="mt-1 text-stone-800">{rec.reasoning}</p>
      </div>
      <p className="mt-4 flex flex-wrap items-center gap-2 text-sm text-stone-700">
        <Badge tone={conf.tone}>Confidence: {rec.confidence}</Badge>
        <span>{conf.text}</span>
      </p>
    </section>
  )
}
