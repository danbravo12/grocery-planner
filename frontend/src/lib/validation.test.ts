import { describe, expect, it } from 'vitest'
import { defaultForm, emptyMeal, mapServerErrors, toRequest, validate, type PlanForm } from './validation'

function valid(): PlanForm {
  const form = defaultForm()
  form.meals[0] = { ...form.meals[0]!, meal_name: 'Friday dinner', mains: ['fillet mignon'], sides: ['fries'] }
  return form
}

describe('validate', () => {
  it('accepts a good form', () => {
    expect(validate(valid())).toEqual({})
  })

  it('mirrors the API caps', () => {
    const form = valid()
    const meal = form.meals[0]!
    meal.meal_name = 'x'.repeat(61)
    meal.mains = ['a'.repeat(61)]
    meal.people = '13'
    meal.days = '0'
    meal.avoid = 'y'.repeat(101)
    const e = validate(form)
    expect(e['meals.0.meal_name']).toMatch(/at most 60/)
    expect(e['meals.0.mains.0']).toMatch(/at most 60/)
    expect(e['meals.0.people']).toMatch(/from 1 to 12/)
    expect(e['meals.0.days']).toMatch(/from 1 to 14/)
    expect(e['meals.0.avoid']).toMatch(/at most 100/)
  })

  it('needs a name and a main, and rejects blank dishes in a list', () => {
    const form = defaultForm()
    expect(validate(form)['meals.0.meal_name']).toBeTruthy()
    expect(validate(form)['meals.0.mains']).toMatch(/at least one main/)
    form.meals[0]!.mains = ['rice', ' ']
    form.meals[0]!.sides = ['']
    const e = validate({ ...form, meals: [{ ...form.meals[0]!, meal_name: 'x' }] })
    expect(e['meals.0.mains.1']).toBeTruthy()
    expect(e['meals.0.sides.0']).toBeTruthy()
  })

  it('limits meals and total dishes', () => {
    const form = valid()
    form.meals = Array.from({ length: 8 }, () => ({ ...emptyMeal(), meal_name: 'm', mains: ['a'] }))
    expect(validate(form)['meals']).toMatch(/at most 7 meals/)
    form.meals = Array.from({ length: 6 }, () => ({ ...emptyMeal(), meal_name: 'm', mains: ['a', 'b', 'c', 'd'] }))
    expect(validate(form)['meals']).toMatch(/at most 20 dishes in total.*you have 24/)
  })

  it('keeps advanced settings inside their ranges', () => {
    const form = valid()
    form.advanced.savings_threshold_dollars = '2'
    form.advanced.stop_comparable_pct = '90'
    form.advanced.pack_size_tolerance = ''
    const e = validate(form)
    expect(e['advanced.savings_threshold_dollars']).toMatch(/3 to 25/)
    expect(e['advanced.stop_comparable_pct']).toMatch(/40 to 80/)
    expect(e['advanced.pack_size_tolerance']).toBeTruthy()
  })
})

describe('toRequest', () => {
  it('trims, drops blanks, uses integers, and turns percentages into fraction strings', () => {
    const form = valid()
    form.meals[0]!.mains = ['  fillet mignon ']
    form.meals[0]!.avoid = '  '
    form.advanced.savings_threshold_pct = '8'
    form.advanced.pack_size_tolerance = '12.5'
    const body = toRequest(form)
    expect(body.meals[0]).toEqual({
      meal_name: 'Friday dinner',
      mains: ['fillet mignon'],
      sides: ['fries'],
      people: 2,
      days: 1,
      avoid: null,
    })
    expect(body.parameters).toEqual({
      savings_threshold_dollars: '8',
      savings_threshold_pct: '0.08',
      stop_not_found_pct: '0.25',
      stop_comparable_pct: '0.5',
      pack_size_tolerance: '0.125',
    })
  })
})

describe('mapServerErrors', () => {
  it('puts server field paths onto form paths', () => {
    expect(
      mapServerErrors([
        { field: 'meals.0.people', message: 'too many' },
        { field: 'parameters.pack_size_tolerance', message: 'out of range' },
        { field: 'body', message: 'A plan can include at most 20 dishes' },
        { field: 'extra', message: 'Extra inputs are not permitted' },
      ]),
    ).toEqual({
      'meals.0.people': 'too many',
      'advanced.pack_size_tolerance': 'out of range',
      form: 'A plan can include at most 20 dishes',
      extra: 'Extra inputs are not permitted',
    })
  })
})
