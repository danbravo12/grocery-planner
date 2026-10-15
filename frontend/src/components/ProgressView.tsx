import { AGENTS, type Progress, type StepProgress } from '../lib/progress'
import { Card } from './ui'

function Marker({ state }: { state: StepProgress['state'] }) {
  const base = 'grid size-7 shrink-0 place-items-center rounded-full text-sm font-semibold'
  if (state === 'done') return <span aria-hidden className={`${base} bg-accent text-white`}>✓</span>
  if (state === 'failed') return <span aria-hidden className={`${base} bg-red-600 text-white`}>!</span>
  if (state === 'running') {
    return <span aria-hidden className={`${base} animate-pulse border-2 border-accent bg-accent-soft text-accent-strong`}>•</span>
  }
  return <span aria-hidden className={`${base} border border-stone-300 bg-white text-stone-400`}>•</span>
}

const STATE_TEXT = { pending: 'Waiting', running: 'Working', done: 'Done', failed: 'Failed' }

function Row({ label, doing, step, nested }: { label: string; doing: string; step: StepProgress; nested?: boolean }) {
  const active = step.state === 'running'
  return (
    <li
      aria-current={active ? 'step' : undefined}
      className={`flex gap-3 rounded-lg p-3 ${nested ? 'ml-6' : ''} ${active ? 'bg-accent-soft ring-1 ring-accent' : ''}`}
    >
      <Marker state={step.state} />
      <div className="min-w-0">
        <p className="font-medium">
          {label} <span className="sr-only">: {STATE_TEXT[step.state]}</span>
        </p>
        <p className="text-sm text-stone-600">{step.summary ?? (active || step.state === 'pending' ? doing : '')}</p>
      </div>
    </li>
  )
}

export function ProgressView({ progress, status }: { progress: Progress; status: string }) {
  const queued = progress.queuePosition !== null && progress.queuePosition > 0 && status === 'queued'
  return (
    <Card className="mx-auto max-w-xl">
      <h1 className="text-xl font-semibold">Making your plan</h1>
      <p className="mt-1 text-sm text-stone-600">
        This usually takes 10 to 30 seconds. You can leave this page open, or come back to its link later.
      </p>
      {queued && (
        <p className="mt-3 rounded-lg bg-stone-100 p-3 text-sm">
          Plans are made a couple at a time. You are number <strong>{progress.queuePosition}</strong> in the queue.
        </p>
      )}
      <ol className="mt-4 space-y-1" aria-label="Progress">
        {AGENTS.map((agent) => (
          <Row key={agent.name} label={agent.label} doing={agent.doing} step={progress.steps[agent.name]} />
        ))}
        {progress.loopback && (
          <>
            <Row
              nested
              label="Re-checking doubtful matches"
              doing={`The planner sent ${progress.loopback.items} item${progress.loopback.items === 1 ? '' : 's'} back to the price agent`}
              step={progress.loopback.reprice}
            />
            <Row
              nested
              label="Planner, second look"
              doing="Deciding again with the re-checked prices"
              step={progress.loopback.planner2}
            />
          </>
        )}
      </ol>
      <p role="status" aria-live="polite" className="sr-only">
        {progress.announcement}
      </p>
    </Card>
  )
}
