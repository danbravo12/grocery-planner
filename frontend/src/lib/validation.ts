/** Client-side validation. It mirrors the API's limits (domain/request.py and config.py); the
 *  server is still the authority and its 422 field errors are mapped onto the same field paths. */

export const LIMITS = {
  maxMeals: 7,
  maxMains: 4,
  maxSides: 4,
  maxDishesTotal: 20,
  maxText: 60,
  maxAvoid: 100,
  people: [1, 12] as const,
  days: [1, 14] as const,
}

export interface MealForm {
  key: string
  meal_name: string
  mains: string[]
  sides: string[]
  people: string
  days: string
  avoid: string
}

export interface AdvancedForm {
  savings_threshold_dollars: string
  savings_threshold_pct: string // percent, as typed (8 means 8%)
  stop_not_found_pct: string
  stop_comparable_pct: string
  pack_size_tolerance: string
}

export interface PlanForm {
  meals: MealForm[]
  include_pantry_staples: boolean
  advanced: AdvancedForm
}

export type Errors = Record<string, string>

/** Defaults and clamped ranges from the design doc's configuration table (percent, not fractions). */
export const ADVANCED_FIELDS: {
  name: keyof AdvancedForm
  label: string
  unit: '$' | '%'
  default: string
  min: number
  max: number
}[] = [
  { name: 'savings_threshold_dollars', label: 'Savings threshold (dollars)', unit: '$', default: '8', min: 3, max: 25 },
  { name: 'savings_threshold_pct', label: 'Savings threshold (percent of basket)', unit: '%', default: '8', min: 3, max: 20 },
  { name: 'stop_not_found_pct', label: 'Stop if items not found at any store', unit: '%', default: '25', min: 10, max: 40 },
  { name: 'stop_comparable_pct', label: 'Stop if fewer items are comparable', unit: '%', default: '50', min: 40, max: 80 },
  { name: 'pack_size_tolerance', label: 'Pack-size tolerance across stores', unit: '%', default: '15', min: 5, max: 25 },
]

let counter = 0
export const newKey = () => `meal-${++counter}`

export function emptyMeal(): MealForm {
  return { key: newKey(), meal_name: '', mains: [''], sides: [], people: '2', days: '1', avoid: '' }
}

export function defaultForm(): PlanForm {
  return {
    meals: [emptyMeal()],
    include_pantry_staples: false,
    advanced: Object.fromEntries(ADVANCED_FIELDS.map((f) => [f.name, f.default])) as unknown as AdvancedForm,
  }
}

export function advancedChanged(a: AdvancedForm): boolean {
  return ADVANCED_FIELDS.some((f) => a[f.name].trim() !== f.default)
}

const isWholeNumber = (v: string) => /^\d+$/.test(v.trim())

export function validate(form: PlanForm): Errors {
  const errors: Errors = {}
  if (form.meals.length === 0) errors['meals'] = 'Add at least one meal.'
  if (form.meals.length > LIMITS.maxMeals) errors['meals'] = `You can plan at most ${LIMITS.maxMeals} meals.`

  let dishes = 0
  form.meals.forEach((meal, i) => {
    const p = `meals.${i}`
    const name = meal.meal_name.trim()
    if (!name) errors[`${p}.meal_name`] = 'Give the meal a name, for example "Friday dinner".'
    else if (name.length > LIMITS.maxText) errors[`${p}.meal_name`] = `Use at most ${LIMITS.maxText} characters.`

    const mains = meal.mains.map((m) => m.trim())
    if (mains.filter(Boolean).length === 0) errors[`${p}.mains`] = 'Add at least one main dish.'
    mains.forEach((m, j) => {
      if (!m && mains.length > 1) errors[`${p}.mains.${j}`] = 'Fill this in or remove it.'
      else if (m.length > LIMITS.maxText) errors[`${p}.mains.${j}`] = `Use at most ${LIMITS.maxText} characters.`
    })
    meal.sides.map((s) => s.trim()).forEach((s, j) => {
      if (!s) errors[`${p}.sides.${j}`] = 'Fill this in or remove it.'
      else if (s.length > LIMITS.maxText) errors[`${p}.sides.${j}`] = `Use at most ${LIMITS.maxText} characters.`
    })
    dishes += mains.filter(Boolean).length + meal.sides.filter((s) => s.trim()).length

    for (const [field, [lo, hi], label] of [
      ['people', LIMITS.people, 'People'],
      ['days', LIMITS.days, 'Days'],
    ] as const) {
      const v = meal[field]
      if (!isWholeNumber(v) || Number(v) < lo || Number(v) > hi) {
        errors[`${p}.${field}`] = `${label} must be a whole number from ${lo} to ${hi}.`
      }
    }
    if (meal.avoid.trim().length > LIMITS.maxAvoid) errors[`${p}.avoid`] = `Use at most ${LIMITS.maxAvoid} characters.`
  })
  if (dishes > LIMITS.maxDishesTotal && !errors['meals']) {
    errors['meals'] = `A plan can include at most ${LIMITS.maxDishesTotal} dishes in total (mains and sides across all meals); you have ${dishes}.`
  }

  for (const f of ADVANCED_FIELDS) {
    const raw = form.advanced[f.name].trim()
    const n = Number(raw)
    if (raw === '' || Number.isNaN(n) || n < f.min || n > f.max) {
      errors[`advanced.${f.name}`] = `Enter a number from ${f.min} to ${f.max}.`
    }
  }
  return errors
}

/** The POST body. Percentages become fractions, as strings so no float reaches the server's money maths. */
export function toRequest(form: PlanForm) {
  const fraction = (pct: string) => (Number(pct) / 100).toFixed(4).replace(/0+$/, '').replace(/\.$/, '')
  const a = form.advanced
  return {
    meals: form.meals.map((m) => ({
      meal_name: m.meal_name.trim(),
      mains: m.mains.map((x) => x.trim()).filter(Boolean),
      sides: m.sides.map((x) => x.trim()).filter(Boolean),
      people: Number(m.people),
      days: Number(m.days),
      avoid: m.avoid.trim() || null,
    })),
    include_pantry_staples: form.include_pantry_staples,
    parameters: {
      savings_threshold_dollars: String(Number(a.savings_threshold_dollars)),
      savings_threshold_pct: fraction(a.savings_threshold_pct),
      stop_not_found_pct: fraction(a.stop_not_found_pct),
      stop_comparable_pct: fraction(a.stop_comparable_pct),
      pack_size_tolerance: fraction(a.pack_size_tolerance),
    },
  }
}

const PARAM_MAP: Record<string, keyof AdvancedForm> = {
  savings_threshold_dollars: 'savings_threshold_dollars',
  savings_threshold_pct: 'savings_threshold_pct',
  stop_not_found_pct: 'stop_not_found_pct',
  stop_comparable_pct: 'stop_comparable_pct',
  pack_size_tolerance: 'pack_size_tolerance',
}

/** Server field paths ("meals.0.people", "parameters.pack_size_tolerance", "body") onto form paths. */
export function mapServerErrors(fields: { field: string; message: string }[]): Errors {
  const errors: Errors = {}
  for (const { field, message } of fields) {
    let path = field
    if (field.startsWith('parameters.')) {
      const name = PARAM_MAP[field.slice('parameters.'.length)]
      path = name ? `advanced.${name}` : 'form'
    } else if (field === 'body' || field === 'parameters') {
      path = 'form'
    } else if (field === 'include_pantry_staples') {
      path = 'form'
    }
    errors[path] ??= message
  }
  return errors
}
