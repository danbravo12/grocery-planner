import { render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { oneStore, stopSteps, stopped } from '../test-fixtures'
import { RunPage } from './RunPage'

afterEach(() => vi.unstubAllGlobals())

function serve(routes: Record<string, { status: number; body: unknown }>) {
  vi.stubGlobal(
    'fetch',
    vi.fn((url: string) => {
      const hit = routes[url]
      const reply = hit ?? { status: 404, body: { code: 'not_found', message: 'No such run.' } }
      return Promise.resolve(new Response(JSON.stringify(reply.body), { status: reply.status }))
    }),
  )
}

const at = (id: string) =>
  render(
    <MemoryRouter initialEntries={[`/runs/${id}`]}>
      <Routes>
        <Route path="/runs/:id" element={<RunPage />} />
      </Routes>
    </MemoryRouter>,
  )

describe('RunPage', () => {
  it('shows the result of a finished run without opening a stream', async () => {
    const source = vi.fn()
    vi.stubGlobal('EventSource', source)
    serve({ '/api/runs/done': { status: 200, body: oneStore() } })
    at('done')
    expect(await screen.findByRole('heading', { level: 1, name: 'Shop at Woolworths' })).toBeInTheDocument()
    expect(source).not.toHaveBeenCalled()
  })

  it('shows what failed for a stopped run, using its steps', async () => {
    serve({
      '/api/runs/stopped': { status: 200, body: stopped() },
      '/api/runs/stopped/steps': { status: 200, body: { run_id: 'stopped', steps: stopSteps() } },
    })
    at('stopped')
    expect(await screen.findByText("We couldn't compare the stores")).toBeInTheDocument()
    expect(await screen.findByText('Dragon fruit')).toBeInTheDocument()
  })

  it('shows a not-found page for an unknown run', async () => {
    serve({})
    at('nope')
    expect(await screen.findByRole('heading', { name: "We can't find that page" })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Plan a new shop' })).toHaveAttribute('href', '/')
  })

  it('shows progress and follows the stream for a run in progress', async () => {
    class Source {
      static last: Source
      listeners: Record<string, ((e: MessageEvent<string>) => void)[]> = {}
      constructor(readonly url: string) {
        Source.last = this
      }
      addEventListener(t: string, fn: (e: MessageEvent<string>) => void) {
        ;(this.listeners[t] ??= []).push(fn)
      }
      close() {}
    }
    vi.stubGlobal('EventSource', Source)
    serve({ '/api/runs/live': { status: 200, body: { ...oneStore(), status: 'running', recommendation: null, comparison: [] } } })
    at('live')
    expect(await screen.findByRole('heading', { name: 'Making your plan' })).toBeInTheDocument()
    expect(Source.last.url).toBe('/api/runs/live/events?after=0')
    const fire = (type: string, data: unknown) =>
      Source.last.listeners[type]!.forEach((fn) => fn({ data: JSON.stringify(data) } as MessageEvent<string>))
    const { act } = await import('@testing-library/react')
    act(() =>
      fire('step_started', { seq: 1, type: 'step_started', run_id: 'live', step: 1, name: 'ingredient', attempt: 1, summary: null, data: {} }),
    )
    const current = screen.getByRole('list', { name: 'Progress' }).querySelector('[aria-current="step"]')!
    expect(current).toHaveTextContent('Ingredient agent')
    expect(screen.getByRole('status')).toHaveTextContent('Building your shopping list.')
  })
})
