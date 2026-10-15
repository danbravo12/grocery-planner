import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { Step } from '../api/client'
import { run } from '../test-fixtures'
import { TracePanel } from './TracePanel'

afterEach(() => vi.unstubAllGlobals())

function step(over: Partial<Step>): Step {
  return {
    seq: 1, name: 'ingredient', agent: 'ingredient', attempt: 1, status: 'completed', summary: '5 items from 3 dishes (1 cached)',
    input: { meals: '1 entries' }, output: { items: 5, cache_hits: 1, cache_misses: 2, assumptions: [] }, tool_calls: [],
    requests: 1, tokens_in: 1958, tokens_out: 400, thinking_tokens: 0, latency_ms: 5200, model: 'gemini-3.1-flash-lite',
    fell_back: false, error: null, started_at: '2026-10-19T01:00:00Z', finished_at: '2026-10-19T01:00:05Z', ...over,
  }
}

const STEPS: Step[] = [
  step({}),
  step({
    seq: 2, name: 'price', agent: 'price', summary: 'priced 2 items at 2 stores, 2 flags, 2 downgrades',
    output: {
      rows: 4, rounds: 2, flags: [{ item_id: 'i1', kind: 'different_food' }, { item_id: 'i2', kind: 'exact_downgraded' }],
      downgrades: [
        { item_id: 'i1', kind: 'different_food', from: 'equivalent', to: 'substitute', reason: 'not the same food' },
        { item_id: 'i2', kind: 'exact_downgraded', from: 'exact', to: 'equivalent', reason: 'brands differ' },
      ],
    },
    requests: 2, tokens_in: 3539, tokens_out: 762, latency_ms: 9100,
  }),
  step({
    seq: 3, name: 'planner', agent: 'planner', summary: 'Shop at Woolworths', output: { recommendation: 'split', confidence: 'high' },
    requests: 2, tool_calls: [{ name: 'what_if_split', args: { assignments: [{ key: 'i1', store: 'woolworths' }] }, result: '{"total":"95.00","meets_threshold":true}', latency_ms: 0.4, error: null }],
    fell_back: true,
  }),
]

function serve(status = 200, body: unknown = { run_id: 'r', steps: STEPS }) {
  const fetchMock = vi.fn(() => Promise.resolve(new Response(JSON.stringify(body), { status })))
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

describe('TracePanel', () => {
  it('is collapsed by default and loads nothing until it is opened', () => {
    const fetchMock = serve()
    render(<TracePanel run={run()} />)
    const details = screen.getByText('How it worked').closest('details')!
    expect(details).not.toHaveAttribute('open')
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('shows one row per step with model, requests, tokens, latency and a fallback flag', async () => {
    const fetchMock = serve()
    const user = userEvent.setup()
    render(<TracePanel run={run()} />)
    await user.click(screen.getByText('How it worked'))
    expect(await screen.findByText(/1\. Ingredient agent/)).toBeInTheDocument()
    expect(fetchMock).toHaveBeenCalledWith(`/api/runs/${run().id}/steps`, expect.anything())
    expect(screen.getByText(/2\. Price agent/)).toBeInTheDocument()
    expect(screen.getByText(/3\. Planner/)).toBeInTheDocument()
    expect(screen.getAllByText(/gemini-3\.1-flash-lite · 1 request · 1958 in \/ 400 out tokens/)).toHaveLength(1)
    expect(screen.getByText(/gemini-3\.1-flash-lite · 2 requests · 3539 in \/ 762 out tokens/)).toBeInTheDocument()
    expect(screen.getAllByText('5.2 s').length).toBeGreaterThan(0)
    expect(screen.getByText('fallback model')).toBeInTheDocument()
    expect(screen.queryByText(/system_prompt/)).not.toBeInTheDocument() // no raw prompts
  })

  it('shows the request count against the run\'s reservation', async () => {
    serve()
    const user = userEvent.setup()
    render(<TracePanel run={run()} />)
    await user.click(screen.getByText('How it worked'))
    await screen.findByText(/1\. Ingredient agent/)
    const line = screen.getByText((_, el) => el?.tagName === 'P' && /requests reserved for this run were used/.test(el.textContent ?? ''))
    expect(line).toHaveTextContent('3 of the 9 requests reserved for this run were used')
  })

  it('expands to cache hits, downgrades by reason and tool calls', async () => {
    serve()
    const user = userEvent.setup()
    render(<TracePanel run={run()} />)
    await user.click(screen.getByText('How it worked'))
    await user.click(await screen.findByText(/1\. Ingredient agent/))
    expect(screen.getByText(/Dish cache:/).closest('p')).toHaveTextContent('1 hit, 2 expanded by the model')

    await user.click(screen.getByText(/2\. Price agent/))
    const price = screen.getByText('Downgrades by reason').closest('div')!
    expect(within(price).getByText('different food')).toBeInTheDocument()
    expect(price).toHaveTextContent('beef mince (equivalent to substitute)')
    expect(within(price).getByText('exact downgraded')).toBeInTheDocument()
    expect(screen.getByText(/Model calls in this step:/).closest('p')).toHaveTextContent('2')

    await user.click(screen.getByText(/3\. Planner/))
    const tools = screen.getByText('Tool calls').closest('div')!
    expect(within(tools).getByText(/what_if_split\(\{"assignments":\[\{"key":"i1","store":"woolworths"\}\]\}\)/)).toBeInTheDocument()
    expect(within(tools).getByText('{"total":"95.00","meets_threshold":true}')).toBeInTheDocument()
  })

  it('says when nothing was downgraded', async () => {
    serve(200, { run_id: 'r', steps: [step({ seq: 2, name: 'price', output: { rounds: 1, flags: [], downgrades: [] } })] })
    const user = userEvent.setup()
    render(<TracePanel run={run()} />)
    await user.click(screen.getByText('How it worked'))
    await user.click(await screen.findByText(/2\. Price agent/))
    expect(screen.getByText(/None: every pick kept the tier the model gave it/)).toBeInTheDocument()
  })

  it('shows a failed step and a load error without breaking the page', async () => {
    serve(200, { run_id: 'r', steps: [step({ status: 'failed', error: 'SchemaValidationFailed' })] })
    const user = userEvent.setup()
    const { unmount } = render(<TracePanel run={run()} />)
    await user.click(screen.getByText('How it worked'))
    await user.click(await screen.findByText(/1\. Ingredient agent/))
    expect(screen.getAllByText('failed').length).toBeGreaterThan(0)
    expect(screen.getByText('SchemaValidationFailed')).toBeInTheDocument()
    unmount()

    serve(404, { code: 'not_found', message: 'No such run.' })
    render(<TracePanel run={run()} />)
    await user.click(screen.getByText('How it worked'))
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('No such run.'))
  })
})
