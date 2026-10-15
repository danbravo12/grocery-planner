import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { conditional, failed, oneStore, run, split, stopSteps, stopped, withFlags } from '../../test-fixtures'
import { ResultView } from './ResultView'

afterEach(() => vi.unstubAllGlobals())

describe('the recommendation card', () => {
  it('says no meaningful difference in plain words, with the code summary line', () => {
    render(<ResultView run={run()} steps={null} />)
    expect(screen.getByRole('heading', { level: 1, name: 'No meaningful difference' })).toBeInTheDocument()
    expect(screen.getByText(/The stores are too close to call/)).toBeInTheDocument()
    expect(screen.getByText(/Woolworths is \$1\.00 cheaper on 2 common items/)).toBeInTheDocument()
    expect(screen.getByText('Confidence: high')).toBeInTheDocument()
    expect(screen.queryByText(/Saving:/)).not.toBeInTheDocument()
  })

  it('names the store and the saving for one store', () => {
    render(<ResultView run={oneStore()} steps={null} />)
    expect(screen.getByRole('heading', { level: 1, name: 'Shop at Woolworths' })).toBeInTheDocument()
    expect(screen.getByText(/cheaper than Coles on the items both stores stock/)).toBeInTheDocument()
    expect(screen.getByText('$18.42')).toBeInTheDocument()
  })

  it('shows which items to buy elsewhere for a split', () => {
    render(<ResultView run={split()} steps={null} />)
    expect(screen.getByRole('heading', { level: 1, name: 'Split your shop' })).toBeInTheDocument()
    expect(screen.getByText(/Buy beef mince, eggs at the cheaper store/)).toBeInTheDocument()
    expect(screen.getByText('$10.00')).toBeInTheDocument()
  })

  it('explains a conditional verdict: until when it holds and what follows', () => {
    render(<ResultView run={conditional()} steps={null} />)
    expect(screen.getByRole('heading', { level: 1, name: 'Shop at Woolworths' })).toBeInTheDocument()
    const note = screen.getByText(/This depends on a special that ends Tue 20 Oct/).closest('div')!
    expect(within(note).getByText(/If you shop by Tue 20 Oct \(beef mince on special\)/)).toBeInTheDocument()
    expect(within(note).getByText(/After that: no meaningful difference \(the stores are \$1\.40 apart/)).toBeInTheDocument()
    expect(screen.getByText(/after that, no meaningful difference\./)).toBeInTheDocument() // the code line
  })

  it('says so when matches that are not like for like held the verdict back', () => {
    render(<ResultView run={withFlags()} steps={null} />)
    expect(screen.getByText(/held back because some matches are not like for like/)).toBeInTheDocument()
    expect(screen.getByText('Confidence: medium')).toBeInTheDocument()
  })
})

describe('the store by store comparison', () => {
  it('lists each item with both stores, packs, costs and which is cheaper', () => {
    render(<ResultView run={run()} steps={null} />)
    const table = screen.getByRole('table')
    const rows = within(table).getAllByRole('row')
    const eggs = rows.find((r) => within(r).queryByText('Eggs'))!
    expect(within(eggs).getByText('Coles Eggs 12 pack')).toBeInTheDocument()
    expect(within(eggs).getAllByText('$5.20').length).toBeGreaterThan(0)
    expect(within(eggs).getByText('Cheaper')).toBeInTheDocument()
    expect(within(table).getByText('Total on 2 common items')).toBeInTheDocument()
    expect(within(table).getByText('$13.00')).toBeInTheDocument()
    expect(within(table).getByText('$12.00')).toBeInTheDocument()
    expect(within(table).getByText('Lowest')).toBeInTheDocument()
  })

  it('renders every kind of flag: special with its end date, tier gap, pack gap, substitute, not found, low confidence', () => {
    render(<ResultView run={withFlags()} steps={null} />)
    const table = screen.getByRole('table')
    expect(within(table).getByText('On special until Tue 20 Oct')).toBeInTheDocument()
    expect(within(table).getByText(/regular \$7\.00/)).toBeInTheDocument()
    expect(within(table).getByText('Different quality tiers')).toBeInTheDocument()
    expect(within(table).getByText('Different pack sizes')).toBeInTheDocument()
    expect(within(table).getByText('Substitute, not in totals')).toBeInTheDocument()
    expect(within(table).getByText('Not found')).toBeInTheDocument()
    expect(within(table).getByText('Low confidence')).toBeInTheDocument()
    // items that cannot be compared are listed as left out
    const left = screen.getByText('Left out of the totals').closest('div')!
    expect(within(left).getByText(/Beef mince/)).toBeInTheDocument()
    expect(within(left).getByText(/Coles not found/)).toBeInTheDocument()
    expect(within(left).getByText(/only a substitute \(Woolworths Diced Beef\)/)).toBeInTheDocument()
  })

  it('lists planner flags, tips and assumptions in the notes', async () => {
    render(<ResultView run={withFlags()} steps={null} />)
    const notes = screen.getByRole('region', { name: 'Good to know' })
    expect(within(notes).getByText(/Bacon is cheaper at Coles while the special lasts/)).toBeInTheDocument()
    expect(within(notes).getByText('Quality tiers differ')).toBeInTheDocument()
    expect(within(notes).getByText('Pack sizes differ')).toBeInTheDocument()
    expect(within(notes).getByText('Missing')).toBeInTheDocument()
    await userEvent.click(within(notes).getByText(/Assumptions \(2\)/))
    expect(within(notes).getByText('Prices are as listed.')).toBeVisible()
  })
})

describe('the shopping list and the footer', () => {
  it('groups items by category with quantities and the meals that use them', () => {
    render(<ResultView run={run()} steps={null} />)
    const list = screen.getByRole('region', { name: 'Your shopping list' })
    expect(within(list).getByText('Meat')).toBeInTheDocument()
    expect(within(list).getByText('500 g')).toBeInTheDocument()
    expect(within(list).getByText('12 items')).toBeInTheDocument()
    expect(within(list).getByText('for Dinner, Brunch')).toBeInTheDocument()
  })

  it('shows the seeded-data notice, usage, and a working copy-link button', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText } })
    render(<ResultView run={run()} steps={null} />)
    expect(screen.getByText(/Prices are seeded and fictional/)).toBeInTheDocument()
    expect(screen.getByText(/data_source: seeded/)).toBeInTheDocument()
    expect(screen.getByText(/3 model requests · 11\.1 s · gemini-3\.1-flash-lite · would cost \$0\.0028/)).toBeInTheDocument()
    expect(screen.getByText(/Anyone with this link can view this run/)).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Copy link' }))
    expect(writeText).toHaveBeenCalledWith(window.location.href)
    expect(await screen.findByText('Link copied')).toBeInTheDocument()
  })
})

describe('stopped and failed runs', () => {
  it('shows the message, the ingredient list and which items failed and why', () => {
    render(<ResultView run={stopped()} steps={stopSteps()} />)
    expect(screen.getByRole('heading', { level: 1, name: "We couldn't compare the stores" })).toBeInTheDocument()
    expect(screen.getByText(/3 of 4 items have no usable match at any store/)).toBeInTheDocument()
    expect(screen.getByText(/Your shopping list was built/)).toBeInTheDocument()
    expect(screen.getByRole('region', { name: 'Your shopping list' })).toBeInTheDocument()
    const failedList = screen.getByRole('region', { name: /couldn't match/ })
    expect(within(failedList).getByText('Dragon fruit')).toBeInTheDocument()
    expect(within(failedList).getByText(/No dragon fruit in the catalogue/)).toBeInTheDocument()
    expect(within(failedList).getByText('Substitute')).toBeInTheDocument()
    expect(screen.queryByRole('table')).not.toBeInTheDocument()
  })

  it('shows a failed run with its message and no list when none was built', () => {
    render(<ResultView run={failed()} steps={null} />)
    expect(screen.getByRole('heading', { level: 1, name: "This plan didn't finish" })).toBeInTheDocument()
    expect(screen.getByText(/The AI service is unavailable right now/)).toBeInTheDocument()
    expect(screen.queryByRole('region', { name: 'Your shopping list' })).not.toBeInTheDocument()
  })
})
