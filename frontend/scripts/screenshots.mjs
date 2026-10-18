// Saves screenshots of the real app (served by FastAPI) for existing runs:
//   node scripts/screenshots.mjs <base-url> <out-dir> <conditional-run-id> <split-run-id>
// No new plans are made. The unavailable screen is shown by mocking /api/availability.
import { mkdirSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { chromium } from '@playwright/test'

const [base = 'http://localhost:8000', outArg = '../docs/screenshots', conditional, split] = process.argv.slice(2)
if (!conditional || !split) {
  console.error('usage: screenshots.mjs <base-url> <out-dir> <conditional-run-id> <split-run-id>')
  process.exit(1)
}
const out = resolve(outArg)
mkdirSync(out, { recursive: true })

// The real reset: the next midnight in America/Los_Angeles (07:00 or 08:00 UTC), as the server computes it.
const nextReset = () => {
  const pacific = (d) => new Intl.DateTimeFormat('en-US', { timeZone: 'America/Los_Angeles', hour: 'numeric', hourCycle: 'h23' }).format(d)
  const now = new Date()
  for (let day = 0; day < 3; day++) {
    for (const hour of [7, 8]) {
      const c = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate() + day, hour))
      if (c > now && Number(pacific(c)) === 0) return c.toISOString()
    }
  }
}
const unavailable = (route) =>
  route.fulfill({
    contentType: 'application/json',
    body: JSON.stringify({
      available: false, reason: 'day_budget', message: 'used up',
      reset_at_utc: nextReset(), reset_at_sydney: nextReset(), sample_run_id: split,
    }),
  })

const browser = await chromium.launch({ channel: process.env.CI ? undefined : 'chrome' })
const desktop = { width: 1280, height: 900 }
const phone = { width: 390, height: 844 }
const open = async (viewport, scale = 1) => (await browser.newContext({ viewport, deviceScaleFactor: scale })).newPage()
const shot = (page, name, options = {}) => page.screenshot({ path: join(out, name), ...options })

// the form with the examples
let page = await open(desktop)
await page.goto(base)
await page.getByRole('heading', { name: 'Try an example' }).waitFor()
await page.getByRole('button', { name: /Lamb and pork weekend/ }).click()
await shot(page, 'desktop-1-form.png', { fullPage: true })

// the result hero, conditional verdict, desktop and phone
page = await open(desktop)
await page.goto(`${base}/runs/${conditional}`)
await page.getByRole('heading', { level: 1 }).first().waitFor()
await page.waitForTimeout(400)
await shot(page, 'desktop-hero-conditional.png')
page = await open(phone, 2)
await page.goto(`${base}/runs/${conditional}`)
await page.getByRole('heading', { level: 1 }).first().waitFor()
await page.waitForTimeout(400)
await shot(page, 'mobile-hero-conditional.png')

// a split result, whole page, then the trace panel expanded
page = await open(desktop)
await page.goto(`${base}/runs/${split}`)
await page.getByRole('heading', { level: 1 }).first().waitFor()
await page.waitForTimeout(400)
await shot(page, 'desktop-split-result.png', { fullPage: true })
{
  const phoneSplit = await open(phone, 2)
  await phoneSplit.goto(`${base}/runs/${split}`)
  await phoneSplit.getByRole('heading', { level: 1 }).first().waitFor()
  await phoneSplit.waitForTimeout(400)
  await shot(phoneSplit, 'mobile-split-result.png')
}
const trace = page.locator('details', { has: page.getByText('How it worked', { exact: true }) }).first()
await page.getByText('How it worked', { exact: true }).click()
await page.getByText(/1\. Ingredient agent/).waitFor()
await page.getByText(/1\. Ingredient agent/).click()
await page.getByText(/2\. Price agent/).click()
await page.getByText(/3\. Planner/).click()
await page.waitForTimeout(300)
await trace.scrollIntoViewIfNeeded()
await trace.screenshot({ path: join(out, 'desktop-trace-panel.png') })

// the unavailable screen (availability mocked as used up)
page = await open(desktop)
await page.route('**/api/availability', unavailable)
await page.goto(base)
await page.getByRole('heading', { name: /free AI capacity is used up/ }).waitFor()
await shot(page, 'desktop-unavailable.png')
page = await open(phone, 2)
await page.route('**/api/availability', unavailable)
await page.goto(base)
await page.getByRole('heading', { name: /free AI capacity is used up/ }).waitFor()
await shot(page, 'mobile-unavailable.png')
await browser.close()
