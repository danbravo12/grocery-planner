import type { ComparisonItem, Pick, RunView } from '../../api/client'
import { money, quantity, shortDate, title } from '../../lib/format'
import { Badge } from '../ui'

const USABLE = new Set(['exact', 'equivalent'])
const isUsable = (p: Pick | undefined) => !!p && USABLE.has(p.match_tier)

function itemFlags(run: RunView, itemId: string) {
  return (run.recommendation?.flags ?? []).filter((f) => f.item_id === itemId)
}

function PickCell({ pick, cheaper }: { pick: Pick | undefined; cheaper: boolean }) {
  if (!pick || pick.match_tier === 'not_found') {
    return (
      <div>
        <Badge tone="bad">Not found</Badge>
        {pick?.reason && <p className="mt-1 text-sm text-stone-600">{pick.reason}</p>}
      </div>
    )
  }
  return (
    <div>
      <p className="font-medium">{pick.product_name}</p>
      <p className="text-sm text-stone-600">
        {quantity(pick.pack_size, pick.pack_unit)} × {pick.packs_needed} · buys {quantity(pick.purchased_qty, pick.pack_unit)}
      </p>
      <p className="mt-1 flex flex-wrap items-center gap-1.5">
        <span className={`font-mono text-lg font-semibold tabular-nums ${isUsable(pick) ? '' : 'text-stone-500 line-through'}`}>
          {money(pick.total_cost)}
        </span>
        {cheaper && <Badge tone="accent">Cheaper</Badge>}
        {pick.match_tier === 'substitute' && <Badge tone="warn">Substitute, not in totals</Badge>}
        {pick.match_tier === 'exact' && <Badge>Same product</Badge>}
        {pick.match_tier === 'equivalent' && <Badge>{pick.brand_label === 'branded' ? 'Branded' : 'House brand'}</Badge>}
        {pick.match_confidence === 'low' && <Badge tone="warn">Low confidence</Badge>}
      </p>
      {pick.on_special && pick.special_end_date && (
        <p className="mt-1">
          <Badge tone="accent">On special until {shortDate(pick.special_end_date)}</Badge>{' '}
          <span className="text-sm text-stone-600">regular {money(pick.regular_total_cost)}</span>
        </p>
      )}
    </div>
  )
}

export function ComparisonTable({ run }: { run: RunView }) {
  const stores = run.recommendation
    ? Object.keys(run.recommendation.rule_verdict.totals)
    : (run.parameters.stores ?? [])
  const totals = run.recommendation?.rule_verdict.totals ?? {}
  const common = run.comparison.filter((c) => stores.every((s) => isUsable(c.stores[s])))
  const left = run.comparison.filter((c) => !common.includes(c))
  const lowest = stores.reduce<string | null>(
    (best, s) => (best === null || Number(totals[s]) < Number(totals[best]) ? s : best),
    null,
  )
  const tied = stores.length > 1 && stores.every((s) => Number(totals[s]) === Number(totals[lowest ?? s]))

  function cheaperStore(item: ComparisonItem): string | null {
    const costs = stores.map((s) => [s, Number(item.stores[s]?.total_cost)] as const)
    if (!stores.every((s) => isUsable(item.stores[s]))) return null
    const min = Math.min(...costs.map(([, c]) => c))
    const winners = costs.filter(([, c]) => c === min)
    return winners.length === 1 ? (winners[0]?.[0] ?? null) : null
  }

  return (
    <section aria-labelledby="comparison-title" className="rounded-xl border border-stone-200 bg-white p-4 shadow-sm sm:p-6">
      <h2 id="comparison-title" className="text-xl font-semibold">
        Store by store
      </h2>
      <p className="mt-1 text-sm text-stone-600">
        The cost of the packs you would actually buy. Leftovers are shown but not credited.
      </p>
      <table className="mt-4 block w-full border-collapse md:table">
        <thead className="sr-only md:not-sr-only md:table-header-group">
          <tr className="border-b-2 border-stone-300 text-left text-sm">
            <th scope="col" className="py-2 pr-3 font-semibold">Item</th>
            {stores.map((s) => (
              <th key={s} scope="col" className="px-3 py-2 font-semibold">
                {title(s)}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="block md:table-row-group">
          {run.comparison.map((item) => {
            const flags = itemFlags(run, item.item_id).filter((f) => f.kind === 'tier_gap' || f.kind === 'pack_gap')
            const cheaper = cheaperStore(item)
            return (
              <tr key={item.item_id} className="block border-b border-dashed border-stone-300 py-3 md:table-row">
                <th scope="row" className="block py-1 pr-3 text-left align-top md:table-cell md:py-3">
                  <span className="block font-semibold">{title(item.name)}</span>
                  <span className="block text-sm font-normal text-stone-600">
                    need {quantity(item.quantity, item.unit)}
                  </span>
                  <span className="mt-1 flex flex-wrap gap-1">
                    {flags.some((f) => f.kind === 'tier_gap') && <Badge tone="warn">Different quality tiers</Badge>}
                    {flags.some((f) => f.kind === 'pack_gap') && <Badge tone="warn">Different pack sizes</Badge>}
                  </span>
                </th>
                {stores.map((s) => (
                  <td key={s} className="block py-1 md:table-cell md:px-3 md:py-3 md:align-top">
                    <span className="mb-1 block text-xs font-semibold uppercase tracking-wide text-stone-500 md:hidden">
                      {title(s)}
                    </span>
                    <PickCell pick={item.stores[s]} cheaper={cheaper === s} />
                  </td>
                ))}
              </tr>
            )
          })}
        </tbody>
        {run.recommendation && (
          <tfoot className="block md:table-footer-group">
            <tr className="block border-t-2 border-stone-900 pt-3 md:table-row">
              <th scope="row" className="block py-1 pr-3 text-left md:table-cell md:py-3">
                Total on {common.length} common item{common.length === 1 ? '' : 's'}
              </th>
              {stores.map((s) => (
                <td key={s} className="block py-1 md:table-cell md:px-3 md:py-3">
                  <span className="mb-1 block text-xs font-semibold uppercase tracking-wide text-stone-500 md:hidden">
                    {title(s)}
                  </span>
                  <span className="font-mono text-xl font-bold tabular-nums">{money(totals[s])}</span>
                  {!tied && lowest === s && <span className="ml-2"><Badge tone="accent">Lowest</Badge></span>}
                </td>
              ))}
            </tr>
          </tfoot>
        )}
      </table>
      {left.length > 0 && (
        <div className="mt-4 rounded-lg bg-stone-100 p-3 text-sm">
          <p className="font-semibold">Left out of the totals</p>
          <ul className="mt-1 list-disc pl-5">
            {left.map((item) => (
              <li key={item.item_id}>
                <span className="font-medium">{title(item.name)}</span>:{' '}
                {stores
                  .map((s) => {
                    const p = item.stores[s]
                    const state =
                      !p || p.match_tier === 'not_found' ? 'not found' : p.match_tier === 'substitute' ? `only a substitute (${p.product_name})` : 'available'
                    return `${title(s)} ${state}`
                  })
                  .join(', ')}
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  )
}
