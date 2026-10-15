import type { RunView, Step } from '../../api/client'
import { quantity, title } from '../../lib/format'
import { Badge } from '../ui'
import { TracePanel } from '../TracePanel'
import { RunFooter } from './RunFooter'
import { ShoppingList } from './ShoppingList'

interface FailedItem {
  name: string
  quantity: string
  unit: string
  found_nowhere: boolean
  stores: Record<string, { match_tier: string; product: string | null; reason: string | null }>
}

/** The items a stopped run could not compare, from the stop step's output (written for the public). */
export function failedItemsFrom(steps: Step[] | null): FailedItem[] {
  const stop = steps?.find((s) => s.name === 'stop')
  const out = stop?.output as { failed_items?: FailedItem[] } | null | undefined
  return Array.isArray(out?.failed_items) ? out.failed_items : []
}

export function StoppedView({ run, steps }: { run: RunView; steps: Step[] | null }) {
  const stopped = run.status === 'stopped'
  const failed = failedItemsFrom(steps)
  return (
    <div className="space-y-6">
      <section className="rounded-xl border-2 border-amber-400 bg-white p-5 shadow-sm sm:p-8" aria-labelledby="stopped-title">
        <h1 id="stopped-title" className="text-3xl font-bold tracking-tight">
          {stopped ? "We couldn't compare the stores" : "This plan didn't finish"}
        </h1>
        <p className="mt-3 text-lg text-stone-800">{run.message ?? 'Something went wrong. Please try again.'}</p>
        {run.ingredient_list && (
          <p className="mt-2 text-stone-700">Your shopping list was built, so you can still shop from it below.</p>
        )}
      </section>

      {failed.length > 0 && (
        <section aria-labelledby="failed-title" className="rounded-xl border border-stone-200 bg-white p-4 shadow-sm sm:p-6">
          <h2 id="failed-title" className="text-xl font-semibold">
            What we couldn&apos;t match
          </h2>
          <ul className="mt-3 divide-y divide-dashed divide-stone-300">
            {failed.map((item) => (
              <li key={item.name} className="py-3">
                <p className="font-semibold">
                  {title(item.name)} <span className="font-normal text-stone-600">({quantity(item.quantity, item.unit)})</span>
                </p>
                <ul className="mt-1 space-y-1 text-sm">
                  {Object.entries(item.stores).map(([store, d]) => (
                    <li key={store} className="flex flex-wrap items-start gap-2">
                      <span className="w-24 font-medium">{title(store)}</span>
                      <Badge tone={d.match_tier === 'substitute' ? 'warn' : 'bad'}>
                        {d.match_tier === 'not_found' ? 'Not found' : title(d.match_tier)}
                      </Badge>
                      <span className="min-w-0 flex-1 text-stone-700">
                        {d.product ? `${d.product}. ` : ''}
                        {d.reason ?? ''}
                      </span>
                    </li>
                  ))}
                </ul>
              </li>
            ))}
          </ul>
        </section>
      )}

      {run.ingredient_list && <ShoppingList items={run.ingredient_list.items} />}
      <TracePanel run={run} />
      <RunFooter run={run} />
    </div>
  )
}
