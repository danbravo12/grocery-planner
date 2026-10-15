import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { FormPage } from './FormPage'

const OPEN = {
  available: true, reason: null, message: 'available',
  reset_at_utc: '2026-10-16T07:00:00+00:00', reset_at_sydney: '2026-10-16T18:00:00+11:00',
  sample_run_id: 'sample-run',
}

type Reply = { status: number; body: unknown }
let replies: { availability: Reply; create: Reply }
let posted: unknown[]

function json(reply: Reply) {
  return Promise.resolve(new Response(JSON.stringify(reply.body), { status: reply.status, headers: { 'content-type': 'application/json' } }))
}

beforeEach(() => {
  posted = []
  replies = {
    availability: { status: 200, body: OPEN },
    create: { status: 202, body: { run_id: 'run-123', run_url: '/runs/run-123', events_url: '/api/runs/run-123/events' } },
  }
  vi.stubGlobal(
    'fetch',
    vi.fn((url: string, init?: RequestInit) => {
      if (url === '/api/availability') return json(replies.availability)
      if (url === '/api/runs' && init?.method === 'POST') {
        posted.push(JSON.parse(String(init.body)))
        return json(replies.create)
      }
      return json({ status: 404, body: { code: 'not_found', message: 'Not found.' } })
    }),
  )
})
afterEach(() => vi.unstubAllGlobals())

function renderForm() {
  return render(
    <MemoryRouter initialEntries={['/']}>
      <Routes>
        <Route path="/" element={<FormPage />} />
        <Route path="/runs/:id" element={<p>Run page</p>} />
      </Routes>
    </MemoryRouter>,
  )
}

async function fillValid(user: ReturnType<typeof userEvent.setup>) {
  await user.type(screen.getByLabelText('Meal name'), 'Friday dinner')
  await user.type(screen.getByLabelText('Mains 1'), 'fillet mignon')
}

describe('validation', () => {
  it('shows field errors and does not call the API', async () => {
    const user = userEvent.setup()
    renderForm()
    await user.click(screen.getByRole('button', { name: 'Make my plan' }))
    expect(await screen.findByText(/Give the meal a name/)).toBeInTheDocument()
    expect(screen.getByText('Add at least one main dish.')).toBeInTheDocument()
    expect(screen.getByText('Please fix the highlighted fields.')).toBeInTheDocument()
    expect(screen.getByLabelText('Meal name')).toHaveAttribute('aria-invalid', 'true')
    expect(posted).toEqual([])
  })

  it('checks people and days against the API limits', async () => {
    const user = userEvent.setup()
    renderForm()
    await fillValid(user)
    await user.clear(screen.getByLabelText('People'))
    await user.type(screen.getByLabelText('People'), '13')
    await user.clear(screen.getByLabelText('Days'))
    await user.type(screen.getByLabelText('Days'), '15')
    await user.click(screen.getByRole('button', { name: 'Make my plan' }))
    expect(await screen.findByText('People must be a whole number from 1 to 12.')).toBeInTheDocument()
    expect(screen.getByText('Days must be a whole number from 1 to 14.')).toBeInTheDocument()
    expect(posted).toEqual([])
  })

  it('stops adding meals at 7 and dishes at 20', async () => {
    const user = userEvent.setup()
    renderForm()
    const add = screen.getByRole('button', { name: '+ Add another meal' })
    for (let n = 0; n < 6; n++) await user.click(add)
    expect(screen.getAllByLabelText('Meal name')).toHaveLength(7)
    expect(add).toBeDisabled()
    expect(screen.getByText(/7 of 7 meals/)).toBeInTheDocument()
  })

  it('lets you add and remove meals and dishes', async () => {
    const user = userEvent.setup()
    renderForm()
    await user.click(screen.getByRole('button', { name: '+ Add a side' }))
    expect(screen.getByLabelText('Sides 1')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Remove side 1' }))
    expect(screen.queryByLabelText('Sides 1')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Remove meal/ })).not.toBeInTheDocument() // the only meal
    await user.click(screen.getByRole('button', { name: '+ Add another meal' }))
    await user.click(screen.getByRole('button', { name: 'Remove meal 2' }))
    expect(screen.getAllByLabelText('Meal name')).toHaveLength(1)
  })

  it('has a notice about personal information beside Avoid, and every input has a label', () => {
    renderForm()
    expect(screen.getByText(/Please don't enter anything personal/)).toBeInTheDocument()
    for (const input of screen.getAllByRole('textbox')) expect(input).toHaveAccessibleName()
  })
})

describe('advanced settings', () => {
  it('are collapsed, show defaults and ranges, and are sent as fractions when changed', async () => {
    const user = userEvent.setup()
    renderForm()
    const details = screen.getByText('Advanced settings').closest('details')!
    expect(details).not.toHaveAttribute('open')
    await user.click(screen.getByText('Advanced settings'))
    expect(screen.getByText('Default 8$, range 3 to 25$')).toBeInTheDocument()
    expect(screen.getByText('Default 50%, range 40 to 80%')).toBeInTheDocument()
    await fillValid(user)
    const dollars = screen.getByLabelText('Savings threshold (dollars) ($)')
    await user.clear(dollars)
    await user.type(dollars, '10')
    await user.click(screen.getByRole('button', { name: 'Make my plan' }))
    await waitFor(() => expect(posted).toHaveLength(1))
    expect(posted[0]).toMatchObject({ parameters: { savings_threshold_dollars: '10', savings_threshold_pct: '0.08' } })
  })

  it('open and flag a value outside its range', async () => {
    const user = userEvent.setup()
    renderForm()
    await fillValid(user)
    await user.click(screen.getByText('Advanced settings'))
    const field = screen.getByLabelText('Pack-size tolerance across stores (%)')
    await user.clear(field)
    await user.type(field, '40')
    await user.click(screen.getByRole('button', { name: 'Make my plan' }))
    expect(await screen.findByText('Enter a number from 5 to 25.')).toBeInTheDocument()
    expect(posted).toEqual([])
  })
})

describe('submitting', () => {
  it('posts the request and goes to the run page', async () => {
    const user = userEvent.setup()
    renderForm()
    await fillValid(user)
    await user.type(screen.getByLabelText('Avoid (optional)'), 'nuts')
    await user.click(screen.getByRole('checkbox', { name: /Include pantry staples/ }))
    await user.click(screen.getByRole('button', { name: 'Make my plan' }))
    expect(await screen.findByText('Run page')).toBeInTheDocument()
    expect(posted[0]).toMatchObject({
      meals: [{ meal_name: 'Friday dinner', mains: ['fillet mignon'], sides: [], people: 2, days: 1, avoid: 'nuts' }],
      include_pantry_staples: true,
    })
  })

  it('maps server 422 field errors onto the right inputs', async () => {
    replies.create = {
      status: 422,
      body: {
        code: 'validation_error', message: 'The request is not valid.',
        fields: [
          { field: 'meals.0.avoid', message: 'String should have at most 100 characters' },
          { field: 'parameters.savings_threshold_dollars', message: 'Input should be greater than or equal to 3' },
        ],
      },
    }
    const user = userEvent.setup()
    renderForm()
    await fillValid(user)
    await user.click(screen.getByRole('button', { name: 'Make my plan' }))
    const avoid = await screen.findByLabelText('Avoid (optional)')
    await waitFor(() => expect(avoid).toHaveAttribute('aria-invalid', 'true'))
    expect(screen.getByText('String should have at most 100 characters')).toBeInTheDocument()
    expect(screen.getByLabelText('Savings threshold (dollars) ($)')).toHaveAttribute('aria-invalid', 'true')
    expect(screen.getByText('Input should be greater than or equal to 3')).toBeInTheDocument() // advanced opened
    expect(screen.getByText('Please fix the highlighted fields.')).toBeInTheDocument()
    expect(screen.queryByText('Run page')).not.toBeInTheDocument()
  })

  it('shows the API message when busy', async () => {
    replies.create = { status: 503, body: { code: 'busy', message: 'Too many plans are running right now. Please try again in a minute.' } }
    const user = userEvent.setup()
    renderForm()
    await fillValid(user)
    await user.click(screen.getByRole('button', { name: 'Make my plan' }))
    expect(await screen.findByText(/We are busy right now. Please try again in a minute: your answers are still here./)).toBeInTheDocument()
    expect(screen.getByLabelText('Meal name')).toHaveValue('Friday dinner') // nothing was lost
  })

  it('says when a limit resets', async () => {
    replies.create = { status: 429, body: { code: 'visitor_limit', message: "You have reached today's limit of plans. Please try again tomorrow." } }
    const user = userEvent.setup()
    renderForm()
    await fillValid(user)
    await waitFor(() => expect(fetch).toHaveBeenCalledWith('/api/availability', expect.anything()))
    await user.click(screen.getByRole('button', { name: 'Make my plan' }))
    expect(await screen.findByText(/limit of plans.*Limits reset at [A-Z][a-z]{2} \d{1,2} [A-Z][a-z]{2}, \d{1,2}:\d{2} (am|pm)/)).toBeInTheDocument()
  })

  it('swaps to the unavailable notice if the day closes while you fill the form', async () => {
    replies.create = {
      status: 503,
      body: { code: 'unavailable', message: 'used up', reset_at_utc: '2026-10-16T07:00:00+00:00', reset_at_sydney: '2026-10-16T18:00:00+11:00' },
    }
    const user = userEvent.setup()
    renderForm()
    await fillValid(user)
    await user.click(screen.getByRole('button', { name: 'Make my plan' }))
    expect(await screen.findByText("Today's free AI capacity is used up")).toBeInTheDocument()
  })
})

describe('availability', () => {
  it('replaces the form with the unavailable screen: reset time, why, and the sample run', async () => {
    replies.availability = { status: 200, body: { ...OPEN, available: false, reason: 'day_budget' } }
    renderForm()
    const notice = await screen.findByRole('status')
    expect(within(notice).getByRole('heading', { name: "Today's free AI capacity is used up" })).toBeInTheDocument()
    expect(within(notice).getByText(/free-tier demo/)).toBeInTheDocument()
    expect(within(notice).getByText(/\d{1,2}:\d{2} (am|pm)/)).toBeInTheDocument()
    expect(within(notice).getByRole('link', { name: /sample run/ })).toHaveAttribute('href', '/runs/sample-run')
    expect(screen.queryByRole('button', { name: 'Make my plan' })).not.toBeInTheDocument()
  })

  it('leaves out the sample link when there is none', async () => {
    replies.availability = { status: 200, body: { ...OPEN, available: false, sample_run_id: null } }
    renderForm()
    await screen.findByRole('status')
    expect(screen.queryByRole('link')).not.toBeInTheDocument()
  })
})

describe('examples', () => {
  it('fill the form with a ready-made plan that can then be edited and sent', async () => {
    const user = userEvent.setup()
    renderForm()
    expect(screen.getByRole('heading', { name: 'Try an example' })).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: /Lamb and pork weekend/ }))
    const names = screen.getAllByLabelText('Meal name')
    expect(names.map((n) => (n as HTMLInputElement).value)).toEqual(['Saturday lamb', 'Sunday pork'])
    expect(screen.getAllByLabelText('Mains 1').map((n) => (n as HTMLInputElement).value)).toEqual(['lamb chops', 'pork chops'])
    expect(screen.getAllByLabelText('People').map((n) => (n as HTMLInputElement).value)).toEqual(['4', '4'])
    await user.click(screen.getByRole('button', { name: /Big chicken BBQ/ }))
    expect(screen.getAllByLabelText('Meal name')).toHaveLength(1)
    expect(screen.getByLabelText('People')).toHaveValue('10')
    await user.click(screen.getByRole('button', { name: 'Make my plan' }))
    await waitFor(() => expect(posted).toHaveLength(1))
    expect(posted[0]).toMatchObject({
      meals: [{ meal_name: 'Big chicken BBQ', mains: ['grilled chicken breast'], people: 10, days: 4 }],
    })
  })

  it('every example is valid and says what it shows', async () => {
    const { PRESETS } = await import('../lib/presets')
    const { validate } = await import('../lib/validation')
    expect(PRESETS.map((p) => p.id)).toEqual(['close-call', 'one-store', 'split', 'special'])
    for (const preset of PRESETS) {
      expect(validate(preset.build())).toEqual({})
      expect(preset.outcome).toBeTruthy()
    }
  })
})
