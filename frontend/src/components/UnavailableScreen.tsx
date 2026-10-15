import { Link } from 'react-router-dom'
import { localTime } from '../lib/format'
import { Card } from './ui'

/** Shown instead of the form when the day's free AI capacity is used up. */
export function UnavailableScreen({ resetUtc, sampleRunId }: { resetUtc: string; sampleRunId?: string | null }) {
  return (
    <Card role="status" className="mx-auto max-w-xl text-center sm:p-10">
      <h1 className="text-2xl font-semibold tracking-tight">Today&apos;s free AI capacity is used up</h1>
      <p className="mt-3 text-lg text-stone-800">
        Come back at <strong>{localTime(resetUtc)}</strong>, when it resets.
      </p>
      <p className="mt-3 text-stone-700">
        This is a free-tier demo: the AI provider gives a fixed number of requests a day, shared by everyone, so
        it can run out before the day ends.
      </p>
      {sampleRunId && (
        <p className="mt-6">
          <Link
            className="inline-flex min-h-11 items-center rounded-lg bg-accent px-5 font-semibold text-white hover:bg-accent-strong"
            to={`/runs/${sampleRunId}`}
          >
            See a sample run instead
          </Link>
        </p>
      )}
    </Card>
  )
}
