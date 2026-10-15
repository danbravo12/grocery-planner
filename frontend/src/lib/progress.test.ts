import { describe, expect, it } from 'vitest'
import type { RunEvent } from '../api/events'
import { initialProgress, progressReducer, type Progress } from './progress'

let seq = 0
const ev = (type: RunEvent['type'], name: string | null, extra: Partial<RunEvent> = {}): RunEvent => ({
  seq: ++seq, type, run_id: 'r', step: 1, name, attempt: 1, summary: null, data: {}, ...extra,
})
const apply = (s: Progress, ...events: RunEvent[]) =>
  events.reduce((acc, event) => progressReducer(acc, { kind: 'event', event }), s)

describe('progressReducer', () => {
  it('moves through the three agents and announces each change', () => {
    let s = initialProgress()
    s = apply(s, ev('step_started', 'ingredient'))
    expect(s.steps.ingredient.state).toBe('running')
    expect(s.announcement).toMatch(/Building your shopping list/)
    s = apply(s, ev('step_completed', 'ingredient', { summary: '5 items from 3 dishes' }), ev('step_started', 'price'))
    expect(s.steps.ingredient).toEqual({ state: 'done', summary: '5 items from 3 dishes' })
    expect(s.steps.price.state).toBe('running')
    expect(s.steps.planner.state).toBe('pending')
  })

  it('shows a queue position and clears it once work starts', () => {
    let s = progressReducer(initialProgress(), { kind: 'queued', position: 3 })
    expect(s.queuePosition).toBe(3)
    expect(s.announcement).toMatch(/number 3 in the queue/)
    s = apply(s, ev('step_started', 'ingredient'))
    expect(s.queuePosition).toBeNull()
  })

  it('adds the loop-back rows only when the loop-back happens', () => {
    let s = apply(initialProgress(), ev('step_completed', 'planner', { summary: 'x' }))
    expect(s.loopback).toBeNull()
    s = apply(s, ev('loopback', null, { data: { item_ids: ['i1', 'i2'] }, summary: 'Re-checking 2 doubtful match(es)' }))
    expect(s.loopback?.items).toBe(2)
    s = apply(s, ev('step_started', 'reprice'), ev('step_completed', 'reprice', { summary: 'done' }), ev('step_started', 'planner_2'))
    expect(s.loopback?.reprice.state).toBe('done')
    expect(s.loopback?.planner2.state).toBe('running')
  })
})
