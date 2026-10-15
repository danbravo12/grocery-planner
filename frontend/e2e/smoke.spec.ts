import { expect, test, type Route } from '@playwright/test'
import { oneStore } from '../src/test-fixtures'

const RUN_ID = '3a1f9a52-0000-4000-8000-000000000001'
const OPEN = {
  available: true, reason: null, message: 'available',
  reset_at_utc: '2026-10-16T07:00:00+00:00', reset_at_sydney: '2026-10-16T18:00:00+11:00', sample_run_id: null,
}

interface Ev {
  seq: number
  type: string
}
const sse = (events: Ev[]) =>
  events.map((e) => `event: ${e.type}\nid: ${e.seq}\ndata: ${JSON.stringify(e)}\n\n`).join('')

const event = (seq: number, type: string, name: string | null, summary: string | null = null) => ({
  seq, type, run_id: RUN_ID, step: name ? 1 : null, name, attempt: name ? 1 : null, summary, data: {},
})

test('fill the form, see progress, see the result (all /api calls mocked)', async ({ page }) => {
  let finish!: () => void
  const finished = new Promise<void>((resolve) => (finish = resolve))
  let posted: unknown = null
  let over = false
  const json = (route: Route, body: unknown, status = 200) =>
    route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })

  await page.route('**/api/availability', (route) => json(route, OPEN))
  await page.route('**/api/runs', async (route) => {
    posted = route.request().postDataJSON()
    await json(route, { run_id: RUN_ID, run_url: `/runs/${RUN_ID}`, events_url: `/api/runs/${RUN_ID}/events` }, 202)
  })
  await page.route(`**/api/runs/${RUN_ID}`, (route) => {
    // still running until the stream has said it is done (React StrictMode fetches twice in dev)
    return json(route, over ? oneStore() : { ...oneStore(), status: 'running', recommendation: null, comparison: [] })
  })
  await page.route(`**/api/runs/${RUN_ID}/events*`, async (route) => {
    await finished // hold the stream so the progress view can be seen
    over = true
    await route.fulfill({
      status: 200,
      contentType: 'text/event-stream',
      body: sse([
        event(1, 'step_started', 'ingredient'),
        event(2, 'step_completed', 'ingredient', '5 items from 3 dishes'),
        event(3, 'step_started', 'price'),
        event(4, 'step_completed', 'price', 'priced 5 items'),
        event(5, 'step_started', 'planner'),
        event(6, 'step_completed', 'planner', 'done'),
        event(7, 'run_completed', null, 'Shop at Woolworths'),
      ]),
    })
  })

  await page.goto('/')
  await expect(page.getByRole('heading', { name: "Plan your week's shop" })).toBeVisible()
  await page.getByLabel('Meal name').fill('Friday dinner')
  await page.getByLabel('Mains 1').fill('fillet mignon')
  await page.getByRole('button', { name: '+ Add a side' }).click()
  await page.getByLabel('Sides 1').fill('potato salad')
  await page.getByRole('button', { name: 'Make my plan' }).click()

  // progress
  await expect(page).toHaveURL(new RegExp(`/runs/${RUN_ID}$`))
  await expect(page.getByRole('heading', { name: 'Making your plan' })).toBeVisible()
  await expect(page.getByRole('list', { name: 'Progress' })).toBeVisible()
  expect(posted).toMatchObject({
    meals: [{ meal_name: 'Friday dinner', mains: ['fillet mignon'], sides: ['potato salad'], people: 2, days: 1 }],
  })

  // result
  finish()
  await expect(page.getByRole('heading', { level: 1, name: 'Shop at Woolworths' })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Store by store' })).toBeVisible()
  await expect(page.getByRole('table')).toContainText('Beef mince')
  await expect(page.getByText(/data_source: seeded/)).toBeVisible()
  await expect(page.getByRole('button', { name: 'Copy link' })).toBeVisible()
})

test('an unknown run shows the not-found page', async ({ page }) => {
  await page.route('**/api/runs/nope', (route) =>
    route.fulfill({ status: 404, contentType: 'application/json', body: JSON.stringify({ code: 'not_found', message: 'No such run.' }) }),
  )
  await page.goto('/runs/nope')
  await expect(page.getByRole('heading', { name: "We can't find that page" })).toBeVisible()
})
