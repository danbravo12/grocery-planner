import { describe, expect, it, vi } from 'vitest'
import { followRun, type RunEvent } from './events'

class FakeSource {
  listeners: Record<string, ((e: MessageEvent<string>) => void)[]> = {}
  closed = false
  constructor(readonly url: string) {}
  addEventListener(type: string, fn: (e: MessageEvent<string>) => void) {
    ;(this.listeners[type] ??= []).push(fn)
  }
  close() {
    this.closed = true
  }
  emit(type: string, data: unknown) {
    for (const fn of this.listeners[type] ?? []) fn({ data: JSON.stringify(data) } as MessageEvent<string>)
  }
}

const event = (seq: number, type: RunEvent['type'], name: string | null = null): RunEvent => ({
  seq, type, run_id: 'r', step: 1, name, attempt: 1, summary: null, data: {},
})

describe('followRun', () => {
  it('replays from the last event it saw after an error', () => {
    vi.useFakeTimers()
    const sources: FakeSource[] = []
    const seen: number[] = []
    const stop = followRun(
      'abc',
      { onEvent: (e) => seen.push(e.seq), onQueued: () => undefined, onTerminal: () => undefined },
      { create: (url) => (sources.push(new FakeSource(url)), sources.at(-1) as unknown as EventSource), retryMs: 100 },
    )
    expect(sources[0]!.url).toBe('/api/runs/abc/events?after=0')
    sources[0]!.emit('step_started', event(1, 'step_started', 'ingredient'))
    sources[0]!.emit('step_completed', event(2, 'step_completed', 'ingredient'))
    sources[0]!.emit('error', {})
    expect(sources[0]!.closed).toBe(true)
    vi.advanceTimersByTime(150)
    expect(sources[1]!.url).toBe('/api/runs/abc/events?after=2')
    sources[1]!.emit('step_completed', event(2, 'step_completed', 'ingredient')) // a duplicate is ignored
    sources[1]!.emit('step_started', event(3, 'step_started', 'price'))
    expect(seen).toEqual([1, 2, 3])
    stop()
    expect(sources[1]!.closed).toBe(true)
    vi.useRealTimers()
  })

  it('stops after the terminal event and reports it', () => {
    const onTerminal = vi.fn()
    let source!: FakeSource
    followRun(
      'abc',
      { onEvent: () => undefined, onQueued: () => undefined, onTerminal },
      { create: (url) => ((source = new FakeSource(url)) as unknown as EventSource) },
    )
    source.emit('run_completed', event(7, 'run_completed'))
    expect(onTerminal).toHaveBeenCalledOnce()
    expect(source.closed).toBe(true)
  })

  it('passes queue positions on', () => {
    const onQueued = vi.fn()
    let source!: FakeSource
    followRun(
      'abc',
      { onEvent: () => undefined, onQueued, onTerminal: () => undefined },
      { create: (url) => ((source = new FakeSource(url)) as unknown as EventSource) },
    )
    source.emit('queued', { position: 2 })
    expect(onQueued).toHaveBeenCalledWith({ position: 2 })
  })
})
