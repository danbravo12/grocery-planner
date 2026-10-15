import { ADVANCED_FIELDS, type AdvancedForm, type Errors } from '../lib/validation'
import { TextField } from './ui'

export function AdvancedSettings({
  value,
  errors,
  onChange,
}: {
  value: AdvancedForm
  errors: Errors
  onChange: (value: AdvancedForm) => void
}) {
  const hasError = ADVANCED_FIELDS.some((f) => errors[`advanced.${f.name}`])
  return (
    <details className="rounded-xl border border-stone-200 bg-white p-4 sm:p-6" open={hasError || undefined}>
      <summary className="cursor-pointer text-base font-semibold">Advanced settings</summary>
      <p className="mt-2 text-sm text-stone-600">
        The defaults suit most plans. Each value is kept within the range shown.
      </p>
      <div className="mt-4 grid gap-4 sm:grid-cols-2">
        {ADVANCED_FIELDS.map((f) => (
          <TextField
            key={f.name}
            label={`${f.label} (${f.unit})`}
            inputMode="decimal"
            value={value[f.name]}
            hint={`Default ${f.default}${f.unit}, range ${f.min} to ${f.max}${f.unit}`}
            error={errors[`advanced.${f.name}`]}
            onChange={(v) => onChange({ ...value, [f.name]: v })}
          />
        ))}
      </div>
    </details>
  )
}
