import { useState } from 'react'
import type { RunView } from '../../api/client'
import { seconds } from '../../lib/format'
import { Button } from '../ui'

export function RunFooter({ run }: { run: RunView }) {
  const [copied, setCopied] = useState(false)
  async function copy() {
    try {
      await navigator.clipboard.writeText(window.location.href)
      setCopied(true)
      setTimeout(() => setCopied(false), 2500)
    } catch {
      setCopied(false)
    }
  }
  const t = run.totals
  return (
    <footer className="rounded-xl border border-dashed border-stone-300 bg-white p-4 text-sm text-stone-700 sm:p-6">
      <p>
        Prices are seeded and fictional (<code className="font-mono">data_source: seeded</code>).
      </p>
      <p className="mt-2">
        {t.requests} model request{t.requests === 1 ? '' : 's'} · {seconds(t.latency_ms)} · {t.models.join(', ') || 'cached'}
        {t.fell_back ? ' (fallback model used)' : ''} · would cost ${Number(t.would_cost_usd).toFixed(4)} on the paid tier
        (free tier: nothing charged)
      </p>
      <div className="mt-3 flex flex-wrap items-center gap-3">
        <Button variant="secondary" onClick={copy}>
          Copy link
        </Button>
        <span role="status" className="text-accent-strong">
          {copied ? 'Link copied' : ''}
        </span>
      </div>
      <p className="mt-3">Anyone with this link can view this run. Don&apos;t share it if the meals are private.</p>
    </footer>
  )
}
