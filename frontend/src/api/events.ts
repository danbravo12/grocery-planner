/** The run's event stream (server-sent events), with replay from the last event we saw. */

export type EventType = 'step_started' | 'step_completed' | 'loopback' | 'run_completed' | 'run_failed'

export interface RunEvent {
  seq: number
  type: EventType
  run_id: string
  step: number | null
  name: string | null
  attempt: number | null
  summary: string | null
  data: Record<string, unknown>
}

export interface QueuedEvent {
  position: number
}

export interface Handlers {
  onEvent: (event: RunEvent) => void
  onQueued: (queued: QueuedEvent) => void
  onTerminal: () => void
}

const EVENT_TYPES: EventType[] = ['step_started', 'step_completed', 'loopback', 'run_completed', 'run_failed']

/**
 * Follow a run. EventSource is opened with `?after=<last seq>` so a reconnect (ours, after an
 * error) gets exactly the events it missed; on a fresh page load that is 0, which replays the
 * whole history and rebuilds the progress view. Returns a function that stops following.
 */
export function followRun(
  runId: string,
  handlers: Handlers,
  options: { after?: number; retryMs?: number; create?: (url: string) => EventSource } = {},
): () => void {
  let last = options.after ?? 0
  let source: EventSource | null = null
  let timer: ReturnType<typeof setTimeout> | undefined
  let closed = false
  let attempts = 0
  const create = options.create ?? ((url: string) => new EventSource(url))

  const open = () => {
    if (closed) return
    source = create(`/api/runs/${encodeURIComponent(runId)}/events?after=${last}`)
    for (const type of EVENT_TYPES) {
      source.addEventListener(type, (raw) => {
        attempts = 0
        const event = JSON.parse((raw as MessageEvent<string>).data) as RunEvent
        if (event.seq <= last) return
        last = event.seq
        handlers.onEvent(event)
        if (type === 'run_completed' || type === 'run_failed') {
          stop()
          handlers.onTerminal()
        }
      })
    }
    source.addEventListener('queued', (raw) => {
      handlers.onQueued(JSON.parse((raw as MessageEvent<string>).data) as QueuedEvent)
    })
    source.addEventListener('error', () => {
      // The browser would retry by itself, but we want ?after= to carry what we have seen.
      source?.close()
      if (closed) return
      attempts += 1
      timer = setTimeout(open, Math.min((options.retryMs ?? 1000) * attempts, 10_000))
    })
  }

  const stop = () => {
    closed = true
    source?.close()
    if (timer) clearTimeout(timer)
  }
  open()
  return stop
}
