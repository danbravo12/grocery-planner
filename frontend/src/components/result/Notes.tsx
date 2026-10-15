import type { RunView } from '../../api/client'
import { title } from '../../lib/format'
import { Badge } from '../ui'

const FLAG_LABEL: Record<string, string> = {
  missing: 'Missing',
  substitute: 'Substitute',
  tier_gap: 'Quality tiers differ',
  pack_gap: 'Pack sizes differ',
  special_ending: 'Special ending soon',
}

export function Notes({ run }: { run: RunView }) {
  const rec = run.recommendation
  if (!rec) return null
  const name = (id: string) => title(run.comparison.find((c) => c.item_id === id)?.name ?? id)
  const assumptions = [...rec.assumptions, ...(run.ingredient_list?.assumptions ?? [])]
  if (rec.tips.length === 0 && rec.flags.length === 0 && assumptions.length === 0) return null
  return (
    <section aria-labelledby="notes-title" className="rounded-xl border border-stone-200 bg-white p-4 shadow-sm sm:p-6">
      <h2 id="notes-title" className="text-xl font-semibold">
        Good to know
      </h2>
      {rec.tips.length > 0 && (
        <div className="mt-3">
          <h3 className="font-semibold">Tips</h3>
          <ul className="mt-1 list-disc space-y-1 pl-5">
            {rec.tips.map((t) => (
              <li key={t.item_id + t.text}>
                <span className="font-medium">{name(t.item_id)}:</span> {t.text}
              </li>
            ))}
          </ul>
        </div>
      )}
      {rec.flags.length > 0 && (
        <div className="mt-4">
          <h3 className="font-semibold">Flags</h3>
          <ul className="mt-1 space-y-2">
            {rec.flags.map((f, i) => (
              <li key={i} className="flex flex-wrap items-start gap-2 text-sm">
                <Badge tone={f.kind === 'special_ending' ? 'accent' : 'warn'}>{FLAG_LABEL[f.kind] ?? f.kind}</Badge>
                <span className="min-w-0 flex-1">{f.message}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
      {assumptions.length > 0 && (
        <details className="mt-4">
          <summary className="cursor-pointer font-semibold">Assumptions ({assumptions.length})</summary>
          <ul className="mt-2 list-disc space-y-1 pl-5 text-sm text-stone-700">
            {assumptions.map((a, i) => (
              <li key={i}>{a}</li>
            ))}
          </ul>
        </details>
      )}
    </section>
  )
}
