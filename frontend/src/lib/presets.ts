import { defaultForm, newKey, type PlanForm } from './validation'

export interface Preset {
  id: string
  label: string
  outcome: string
  build: () => PlanForm
}

function meals(...list: { name: string; mains: string[]; sides?: string[]; people: number; days: number }[]): PlanForm {
  const form = defaultForm()
  form.meals = list.map((m) => ({
    key: newKey(),
    meal_name: m.name,
    mains: m.mains,
    sides: m.sides ?? [],
    people: String(m.people),
    days: String(m.days),
    avoid: '',
  }))
  return form
}

/** Requests checked against the seeded prices to give each kind of answer (see DECISIONS.md, step 14). */
export const PRESETS: Preset[] = [
  {
    id: 'close-call',
    label: 'Friday dinner',
    outcome: 'A close call: no real difference',
    build: () => meals({ name: 'Friday dinner', mains: ['fillet mignon'], sides: ['potato salad', 'fries'], people: 2, days: 1 }),
  },
  {
    id: 'one-store',
    label: 'Sunday lamb',
    outcome: 'One store clearly wins',
    build: () => meals({ name: 'Sunday lamb', mains: ['lamb chops'], sides: ['steamed rice'], people: 4, days: 2 }),
  },
  {
    id: 'split',
    label: 'Lamb and pork weekend',
    outcome: 'Worth splitting the shop',
    build: () =>
      meals(
        { name: 'Saturday lamb', mains: ['lamb chops'], people: 4, days: 2 },
        { name: 'Sunday pork', mains: ['pork chops'], people: 4, days: 2 },
      ),
  },
  {
    id: 'special',
    label: 'Big chicken BBQ',
    outcome: 'Depends on a special that ends soon',
    build: () => meals({ name: 'Big chicken BBQ', mains: ['grilled chicken breast'], people: 10, days: 4 }),
  },
]
