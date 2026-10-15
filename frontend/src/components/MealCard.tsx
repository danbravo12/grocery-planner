import { LIMITS, type Errors, type MealForm } from '../lib/validation'
import { Button, Card, FieldError, TextField } from './ui'

interface Props {
  meal: MealForm
  index: number
  errors: Errors
  canRemove: boolean
  canAddDish: boolean
  onChange: (meal: MealForm) => void
  onRemove: () => void
}

function DishList({
  label,
  addLabel,
  items,
  max,
  min,
  path,
  errors,
  canAddDish,
  onChange,
}: {
  label: string
  addLabel: string
  items: string[]
  max: number
  min: number
  path: string
  errors: Errors
  canAddDish: boolean
  onChange: (items: string[]) => void
}) {
  const listError = errors[path]
  return (
    <fieldset className="mt-4">
      <legend className="text-sm font-medium text-stone-800">{label}</legend>
      <ul className="mt-1 space-y-2">
        {items.map((dish, j) => (
          <li key={j} className="flex items-start gap-2">
            <TextField
              className="flex-1"
              label={`${label} ${j + 1}`}
              hideLabel
              value={dish}
              maxLength={LIMITS.maxText + 20}
              placeholder={label === 'Mains' ? 'e.g. fillet mignon' : 'e.g. potato salad'}
              error={errors[`${path}.${j}`]}
              onChange={(v) => onChange(items.map((x, k) => (k === j ? v : x)))}
            />
            {items.length > min && (
              <Button
                variant="quiet"
                className="mt-1"
                aria-label={`Remove ${label.toLowerCase().replace(/s$/, '')} ${j + 1}`}
                onClick={() => onChange(items.filter((_, k) => k !== j))}
              >
                Remove
              </Button>
            )}
          </li>
        ))}
      </ul>
      <FieldError id={`${path}-error`}>{listError}</FieldError>
      {items.length < max && (
        <Button variant="quiet" className="mt-1 -ml-4" disabled={!canAddDish} onClick={() => onChange([...items, ''])}>
          + {addLabel}
        </Button>
      )}
    </fieldset>
  )
}

export function MealCard({ meal, index, errors, canRemove, canAddDish, onChange, onRemove }: Props) {
  const p = `meals.${index}`
  const set = <K extends keyof MealForm>(key: K, value: MealForm[K]) => onChange({ ...meal, [key]: value })
  return (
    <Card aria-labelledby={`${meal.key}-title`}>
      <div className="flex items-center justify-between">
        <h2 id={`${meal.key}-title`} className="text-lg font-semibold">
          Meal {index + 1}
        </h2>
        {canRemove && (
          <Button variant="quiet" aria-label={`Remove meal ${index + 1}`} onClick={onRemove}>
            Remove meal
          </Button>
        )}
      </div>
      <TextField
        className="mt-3"
        label="Meal name"
        value={meal.meal_name}
        placeholder="e.g. Friday dinner"
        error={errors[`${p}.meal_name`]}
        onChange={(v) => set('meal_name', v)}
      />
      <DishList
        label="Mains"
        addLabel="Add a main"
        items={meal.mains}
        max={LIMITS.maxMains}
        min={1}
        path={`${p}.mains`}
        errors={errors}
        canAddDish={canAddDish}
        onChange={(v) => set('mains', v)}
      />
      <DishList
        label="Sides"
        addLabel="Add a side"
        items={meal.sides}
        max={LIMITS.maxSides}
        min={0}
        path={`${p}.sides`}
        errors={errors}
        canAddDish={canAddDish}
        onChange={(v) => set('sides', v)}
      />
      <div className="mt-4 grid grid-cols-2 gap-3">
        <TextField
          label="People"
          inputMode="numeric"
          value={meal.people}
          hint={`${LIMITS.people[0]} to ${LIMITS.people[1]}`}
          error={errors[`${p}.people`]}
          onChange={(v) => set('people', v)}
        />
        <TextField
          label="Days"
          inputMode="numeric"
          value={meal.days}
          hint={`${LIMITS.days[0]} to ${LIMITS.days[1]}`}
          error={errors[`${p}.days`]}
          onChange={(v) => set('days', v)}
        />
      </div>
      <TextField
        className="mt-4"
        label="Avoid (optional)"
        value={meal.avoid}
        placeholder="e.g. nuts, dairy"
        hint="Please don't enter anything personal. Runs can be viewed by anyone with the link."
        error={errors[`${p}.avoid`]}
        onChange={(v) => set('avoid', v)}
      />
    </Card>
  )
}
