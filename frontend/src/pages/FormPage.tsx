import { useEffect, useRef, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { api, ApiError, type Availability } from '../api/client'
import { AdvancedSettings } from '../components/AdvancedSettings'
import { MealCard } from '../components/MealCard'
import { Button, Card } from '../components/ui'
import { resetTime } from '../lib/format'
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

export function UnavailableNotice({ reset, sampleRunId }: { reset: string; sampleRunId?: string | null }) {
  return (
    <Card role="status" className="mx-auto max-w-xl text-center">
      <h1 className="text-xl font-semibold">The planner is resting for today</h1>
      <p className="mt-2 text-stone-700">
        It has used up today&apos;s AI capacity. It opens again at <strong>{resetTime(reset)}</strong> Sydney time.
      </p>
      {sampleRunId && (
        <p className="mt-4">
          <Link className="font-medium text-accent-strong underline" to={`/runs/${sampleRunId}`}>
            See a sample run in the meantime
          </Link>
        </p>
      )}
    </Card>
  )
}

/** The message to show for a refused submit: the API's own wording, plus when a limit resets. */
function submitMessage(error: ApiError, reset: string | null): string {
  if (error.code === 'visitor_limit' || error.code === 'ip_limit') {
    return reset ? `${error.message} Limits reset at ${resetTime(reset)} Sydney time.` : error.message
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
  const resetAt = availability?.reset_at_sydney ?? null

  if (availability && !availability.available) {
    return <UnavailableNotice reset={availability.reset_at_sydney} sampleRunId={availability.sample_run_id} />
  }
  if (unavailable) {
    return <UnavailableNotice reset={unavailable.reset} sampleRunId={availability?.sample_run_id} />
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
      } else if (e.code === 'unavailable' && typeof e.extra['reset_at_sydney'] === 'string') {
        setUnavailable({ reset: e.extra['reset_at_sydney'] })
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
