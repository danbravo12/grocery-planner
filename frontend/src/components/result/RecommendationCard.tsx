import type { Condition, Recommendation, RunView } from '../../api/client'
import { money, shortDate, title } from '../../lib/format'
import { Badge } from '../ui'

const CONFIDENCE = {
  high: { tone: 'accent' as const, text: 'High confidence: nearly everything matched like for like.' },
  medium: { tone: 'warn' as const, text: 'Medium confidence: some matches are not exact, see the flags below.' },
  low: { tone: 'bad' as const, text: 'Low confidence: treat this as a rough guide and check the flagged items.' },
}

function otherStore(stores: string[], store: string | null | undefined): string {
  return stores.find((s) => s !== store) ?? stores[0] ?? ''
}

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

export function RecommendationCard({ run }: { run: RunView }) {
  const rec = run.recommendation
  if (!rec) return null
  const stores = Object.keys(rec.rule_verdict.totals)
  const condition = rec.rule_verdict.condition
  const conf = CONFIDENCE[rec.confidence]
  const saves = Number(rec.savings) > 0

  let detail: string
  if (rec.recommendation === 'one_store' && rec.store) {
    detail = `${money(rec.rule_verdict.gap)} cheaper than ${title(otherStore(stores, rec.store))} on the items both stores stock.`
  } else if (rec.recommendation === 'split') {
    detail = 'Buying some items at the other store saves enough to be worth the extra trip.'
  } else {
    detail = 'The stores are too close to call, so shop wherever suits you.'
  }

  const assignment = rec.split_assignment ?? {}
  const elsewhere = Object.entries(assignment)
  const base = otherStore(
    stores,
    elsewhere.length ? stores.find((s) => !elsewhere.every(([, st]) => st === s)) : undefined,
  )

  return (
    <section
      aria-labelledby="verdict"
      className="rounded-xl border-2 border-accent bg-white p-5 shadow-sm sm:p-8"
    >
      <p className="text-sm font-medium uppercase tracking-wide text-accent-strong">Our recommendation</p>
      <h1 id="verdict" className="mt-1 text-3xl font-bold tracking-tight sm:text-4xl">
        {verdictHeading(rec)}
      </h1>
      <p className="mt-2 text-lg text-stone-800">{detail}</p>
      <p className="mt-3 rounded-lg bg-stone-100 p-3 font-mono text-sm text-stone-800">{rec.summary_line}</p>

      {saves && (
        <p className="mt-4 text-stone-700">
          Saving: <strong className="font-mono text-xl text-stone-900">{money(rec.savings)}</strong>
        </p>
      )}

      {rec.recommendation === 'split' && elsewhere.length > 0 && (
        <p className="mt-3 text-stone-800">
          Buy {elsewhere.map(([id]) => nameOf(run, id)).join(', ')} at the cheaper store for them, and the rest at{' '}
          {title(base)}.
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

      <p className="mt-4 text-stone-800">{rec.reasoning}</p>
      <p className="mt-4 flex flex-wrap items-center gap-2 text-sm text-stone-700">
        <Badge tone={conf.tone}>Confidence: {rec.confidence}</Badge>
        <span>{conf.text}</span>
      </p>
    </section>
  )
}
