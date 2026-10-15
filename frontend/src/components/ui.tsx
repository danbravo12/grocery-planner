import { useId, type InputHTMLAttributes, type ReactNode } from 'react'

export function Button({
  variant = 'primary',
  className = '',
  ...props
}: React.ButtonHTMLAttributes<HTMLButtonElement> & { variant?: 'primary' | 'secondary' | 'quiet' }) {
  const styles = {
    primary: 'bg-accent text-white hover:bg-accent-strong disabled:bg-stone-400',
    secondary: 'border border-stone-300 bg-white text-stone-800 hover:bg-stone-100 disabled:text-stone-400',
    quiet: 'text-accent-strong hover:bg-accent-soft disabled:text-stone-400',
  }[variant]
  return (
    <button
      type="button"
      {...props}
      className={`inline-flex min-h-11 items-center justify-center gap-2 rounded-lg px-4 text-sm font-semibold transition-colors disabled:cursor-not-allowed ${styles} ${className}`}
    />
  )
}

export function FieldError({ id, children }: { id: string; children?: ReactNode }) {
  if (!children) return null
  return (
    <p id={id} role="alert" className="mt-1 text-sm text-red-700">
      {children}
    </p>
  )
}

interface TextFieldProps extends Omit<InputHTMLAttributes<HTMLInputElement>, 'onChange' | 'value'> {
  label: string
  value: string
  onChange: (value: string) => void
  error?: string
  hint?: string
  hideLabel?: boolean
}

export function TextField({ label, value, onChange, error, hint, hideLabel, className = '', ...rest }: TextFieldProps) {
  const id = useId()
  const describedBy = [error ? `${id}-error` : '', hint ? `${id}-hint` : ''].filter(Boolean).join(' ')
  return (
    <div className={className}>
      <label htmlFor={id} className={hideLabel ? 'sr-only' : 'block text-sm font-medium text-stone-800'}>
        {label}
      </label>
      <input
        id={id}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        aria-invalid={error ? true : undefined}
        aria-describedby={describedBy || undefined}
        className={`mt-1 block min-h-11 w-full rounded-lg border bg-white px-3 text-base text-stone-900 placeholder:text-stone-400 ${
          error ? 'border-red-600' : 'border-stone-300'
        }`}
        {...rest}
      />
      {hint && (
        <p id={`${id}-hint`} className="mt-1 text-sm text-stone-600">
          {hint}
        </p>
      )}
      <FieldError id={`${id}-error`}>{error}</FieldError>
    </div>
  )
}

const BADGE = {
  neutral: 'bg-stone-100 text-stone-700 ring-stone-200',
  accent: 'bg-accent-soft text-accent-strong ring-teal-200',
  warn: 'bg-amber-50 text-amber-900 ring-amber-200',
  bad: 'bg-red-50 text-red-800 ring-red-200',
}

export function Badge({ tone = 'neutral', children }: { tone?: keyof typeof BADGE; children: ReactNode }) {
  return (
    <span className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ring-1 ring-inset ${BADGE[tone]}`}>
      {children}
    </span>
  )
}

export function Card({ children, className = '', ...rest }: React.HTMLAttributes<HTMLElement>) {
  return (
    <section {...rest} className={`rounded-xl border border-stone-200 bg-white p-4 shadow-sm sm:p-6 ${className}`}>
      {children}
    </section>
  )
}
