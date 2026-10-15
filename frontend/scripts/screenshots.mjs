// Drives the real app in a browser and saves screenshots of the form, the progress view and the
// result, on a desktop and a phone-sized viewport:
//   node scripts/screenshots.mjs http://localhost:8765 ../docs/screenshots
// It makes real plans (two runs), so use it against a local server you are happy to spend quota on.
import { mkdirSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { chromium } from '@playwright/test'

const base = process.argv[2] ?? 'http://localhost:8000'
const out = resolve(process.argv[3] ?? '../docs/screenshots')
mkdirSync(out, { recursive: true })

const browser = await chromium.launch({ channel: process.env.CI ? undefined : 'chrome' })
const sizes = [
  ['desktop', { width: 1280, height: 900 }],
  ['mobile', { width: 390, height: 844 }],
]

for (const [name, viewport] of sizes) {
  const page = await (await browser.newContext({ viewport, deviceScaleFactor: name === 'mobile' ? 2 : 1 })).newPage()
  await page.goto(base)
  await page.getByLabel('Meal name').fill('Friday dinner')
  await page.getByLabel('Mains 1').fill('fillet mignon')
  await page.getByRole('button', { name: '+ Add a side' }).click()
  await page.getByLabel('Sides 1').fill('potato salad')
  await page.getByRole('button', { name: '+ Add a side' }).click()
  await page.getByLabel('Sides 2').fill('fries')
  await page.screenshot({ path: join(out, `${name}-1-form.png`), fullPage: true })

  await page.getByRole('button', { name: 'Make my plan' }).click()
  await page.getByRole('heading', { name: 'Making your plan' }).waitFor({ timeout: 60_000 })
  await page.screenshot({ path: join(out, `${name}-2-progress.png`) })

  await page.getByRole('heading', { level: 1 }).filter({ hasNotText: 'Making your plan' }).waitFor({ timeout: 120_000 })
  await page.waitForTimeout(500)
  await page.screenshot({ path: join(out, `${name}-3-result.png`), fullPage: true })
  console.log(name, page.url())
}
await browser.close()
