// Saves a cropped screenshot of a run's recommendation card (the README hero):
//   node scripts/hero-shot.mjs <base-url> <run-id> <out-file>
import { resolve } from 'node:path'
import { chromium } from '@playwright/test'

const [base, runId, out] = process.argv.slice(2)
if (!base || !runId || !out) {
  console.error('usage: hero-shot.mjs <base-url> <run-id> <out-file>')
  process.exit(1)
}
const browser = await chromium.launch({ channel: process.env.CI ? undefined : 'chrome' })
const page = await (await browser.newContext({ viewport: { width: 1000, height: 800 }, deviceScaleFactor: 2 })).newPage()
await page.goto(`${base}/runs/${runId}`)
const card = page.locator('section[aria-labelledby="verdict"]')
await card.waitFor()
await page.waitForTimeout(400)
await card.screenshot({ path: resolve(out) })
await browser.close()
