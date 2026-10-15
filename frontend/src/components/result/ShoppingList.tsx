import type { IngredientItem } from '../../api/client'
import { quantity, title } from '../../lib/format'
import { Badge } from '../ui'

export function ShoppingList({ items, heading = 'Your shopping list' }: { items: IngredientItem[]; heading?: string }) {
  const byCategory = new Map<string, IngredientItem[]>()
  for (const item of items) byCategory.set(item.category, [...(byCategory.get(item.category) ?? []), item])
  return (
    <section aria-labelledby="list-title" className="rounded-xl border border-stone-200 bg-white p-4 shadow-sm sm:p-6">
      <h2 id="list-title" className="text-xl font-semibold">
        {heading}
      </h2>
      {items.length === 0 ? (
        <p className="mt-2 text-stone-700">Nothing to buy: everything you need is a pantry staple.</p>
      ) : (
        [...byCategory.entries()].map(([category, group]) => (
          <div key={category} className="mt-4">
            <h3 className="text-sm font-semibold uppercase tracking-wide text-stone-500">{title(category)}</h3>
            <ul className="mt-1 divide-y divide-dashed divide-stone-300">
              {group.map((item) => (
                <li key={item.item_id} className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1 py-2">
                  <span>
                    <span className="font-medium">{title(item.name)}</span>
                    {item.brand && <span className="text-stone-600"> ({item.brand})</span>}
                    <span className="block text-sm text-stone-600">for {item.meals.join(', ')}</span>
                    {item.flags.map((f) => (
                      <span key={f} className="mt-1 block">
                        <Badge tone="warn">{f}</Badge>
                      </span>
                    ))}
                  </span>
                  <span className="font-mono tabular-nums">{quantity(item.quantity, item.unit)}</span>
                </li>
              ))}
            </ul>
          </div>
        ))
      )}
    </section>
  )
}
