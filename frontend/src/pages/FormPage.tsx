import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api, ApiError, type Availability } from '../api/client'
import { AdvancedSettings } from '../components/AdvancedSettings'
import { MealCard } from '../components/MealCard'
import { UnavailableScreen } from '../components/UnavailableScreen'
import { Button, Card } from '../components/ui'
import { localTime } from '../lib/format'
import { PRESETS } from '../lib/presets'
import {
  LIMITS,
  advancedChanged,
  defaultForm,
  emptyMeal,
  mapServerErrors,
  toRequest,
  validate,
  type Errors,
  type PlanForm,
} from '../lib/validation'

/** The message to show for a refused submit: the API's own wording, plus when a limit resets. */
function submitMessage(error: ApiError, reset: string | null): string {
  if (error.code === 'busy') return 'We are busy right now. Please try again in a minute: your answers are still here.'
  if (error.code === 'visitor_limit' || error.code === 'ip_limit') {
    return reset ? `${error.message} Limits reset at ${localTime(reset)}.` : error.message
  }
  return error.message
}

export function FormPage() {
  const navigate = useNavigate()
  const [form, setForm] = useState<PlanForm>(defaultForm)
  const [errors, setErrors] = useState<Errors>({})
  const [banner, setBanner] = useState<string | null>(null)
  const [availability, setAvailability] = useState<Availability | null>(null)
  const [unavailable, setUnavailable] = useState<{ reset: string } | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const bannerRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    let live = true
    api
      .availability()
      .then((a) => live && setAvailability(a))
      .catch(() => undefined) // the form still works; the POST will say if something is wrong
    return () => {
      live = false
    }
  }, [])

  useEffect(() => {
    if (banner) bannerRef.current?.focus()
  }, [banner])

  const dishes = form.meals.reduce(
    (n, m) => n + m.mains.filter((x) => x.trim()).length + m.sides.filter((x) => x.trim()).length,
    0,
  )
  const canAddDish = dishes < LIMITS.maxDishesTotal
  const resetAt = availability?.reset_at_utc ?? null

  if (availability && !availability.available) {
    return <UnavailableScreen resetUtc={availability.reset_at_utc} sampleRunId={availability.sample_run_id} />
  }
  if (unavailable) {
    return <UnavailableScreen resetUtc={unavailable.reset} sampleRunId={availability?.sample_run_id} />
  }

  async function submit(event: React.FormEvent) {
    event.preventDefault()
    setBanner(null)
    const found = validate(form)
    setErrors(found)
    if (Object.keys(found).length > 0) {
      setBanner('Please fix the highlighted fields.')
      return
    }
    setSubmitting(true)
    try {
      const accepted = await api.createRun(toRequest(form))
      navigate(`/runs/${accepted.run_id}`)
    } catch (e) {
      if (!(e instanceof ApiError)) throw e
      if (e.status === 422) {
        setErrors(mapServerErrors(e.fields))
        setBanner('Please fix the highlighted fields.')
      } else if (e.code === 'unavailable' && typeof e.extra['reset_at_utc'] === 'string') {
        setUnavailable({ reset: e.extra['reset_at_utc'] })
      } else {
        setBanner(submitMessage(e, resetAt))
      }
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <form onSubmit={submit} noValidate className="mx-auto max-w-2xl space-y-5">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight sm:text-3xl">Plan your week&apos;s shop</h1>
        <p className="mt-2 text-stone-700">
          Tell us what you&apos;re cooking. We&apos;ll build the shopping list, price it at Coles and Woolworths, and say
          whether it&apos;s worth going to one store or splitting the shop.
        </p>
      </div>

      <Card aria-labelledby="examples-title">
        <h2 id="examples-title" className="text-base font-semibold">
          Try an example
        </h2>
        <p className="mt-1 text-sm text-stone-600">Fills the form with a ready-made plan. You can change anything before sending it.</p>
        <ul className="mt-3 grid gap-2 sm:grid-cols-2">
          {PRESETS.map((preset) => (
            <li key={preset.id}>
              <button
                type="button"
                onClick={() => {
                  setForm(preset.build())
                  setErrors({})
                  setBanner(null)
                }}
                className="min-h-11 w-full rounded-lg border border-stone-300 bg-white px-3 py-2 text-left hover:border-accent hover:bg-accent-soft"
              >
                <span className="block text-sm font-semibold">{preset.label}</span>
                <span className="block text-sm text-stone-600">{preset.outcome}</span>
              </button>
            </li>
          ))}
        </ul>
      </Card>

      {banner && (
        <div
          ref={bannerRef}
          tabIndex={-1}
          role="alert"
          className="rounded-lg border border-red-300 bg-red-50 p-3 text-sm text-red-900"
        >
          {banner}
        </div>
      )}
      {errors['form'] && (
        <p role="alert" className="text-sm text-red-700">
          {errors['form']}
        </p>
      )}

      {form.meals.map((meal, i) => (
        <MealCard
          key={meal.key}
          meal={meal}
          index={i}
          errors={errors}
          canRemove={form.meals.length > 1}
          canAddDish={canAddDish}
          onChange={(m) => setForm({ ...form, meals: form.meals.map((x, k) => (k === i ? m : x)) })}
          onRemove={() => setForm({ ...form, meals: form.meals.filter((_, k) => k !== i) })}
        />
      ))}
      {errors['meals'] && (
        <p role="alert" className="text-sm text-red-700">
          {errors['meals']}
        </p>
      )}
      <div className="flex flex-wrap items-center gap-3">
        <Button
          variant="secondary"
          disabled={form.meals.length >= LIMITS.maxMeals}
          onClick={() => setForm({ ...form, meals: [...form.meals, emptyMeal()] })}
        >
          + Add another meal
        </Button>
        <span className="text-sm text-stone-600">
          {form.meals.length} of {LIMITS.maxMeals} meals · {dishes} of {LIMITS.maxDishesTotal} dishes
        </span>
      </div>

      <Card>
        <label className="flex items-start gap-3">
          <input
            type="checkbox"
            className="mt-1 size-5 accent-[#0f766e]"
            checked={form.include_pantry_staples}
            onChange={(e) => setForm({ ...form, include_pantry_staples: e.target.checked })}
          />
          <span>
            <span className="block font-medium">Include pantry staples</span>
            <span className="block text-sm text-stone-600">
              Salt, oil, pepper and similar are assumed to be at home unless you tick this.
            </span>
          </span>
        </label>
      </Card>

      <AdvancedSettings
        value={form.advanced}
        errors={errors}
        onChange={(advanced) => setForm({ ...form, advanced })}
      />
      {advancedChanged(form.advanced) && (
        <p className="text-sm text-stone-600">Custom settings are saved with your run so the result can be reproduced.</p>
      )}

      <Button type="submit" className="w-full sm:w-auto" disabled={submitting}>
        {submitting ? 'Starting…' : 'Make my plan'}
      </Button>
    </form>
  )
}
