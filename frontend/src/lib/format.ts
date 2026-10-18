const DAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

/** "Tue 20 Oct" from an ISO date (2026-10-20), the same wording the server uses. */
export function shortDate(iso: string): string {
  const [y, m, d] = iso.slice(0, 10).split('-').map(Number)
  if (!y || !m || !d) return iso
  const date = new Date(Date.UTC(y, m - 1, d))
  return `${DAYS[date.getUTCDay()]} ${d} ${MONTHS[m - 1]}`
}

/** "$11.40" from a decimal string. Display only: no arithmetic is done on money here. */
export function money(value: string | null | undefined): string {
  if (value === null || value === undefined || value === '') return '-'
  const n = Number(value)
  if (Number.isNaN(n)) return value
  return `${n < 0 ? '-' : ''}$${Math.abs(n).toLocaleString('en-AU', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
}

/** "400 g", "1.2 kg", "2 L", "6 each" from a quantity string and a unit. */
export function quantity(value: string | null | undefined, unit: string | null | undefined): string {
  if (!value) return ''
  const n = Number(value)
  if (Number.isNaN(n)) return `${value} ${unit ?? ''}`.trim()
  const trim = (x: number) => String(Number(x.toFixed(2)))
  if (unit === 'g' && n >= 1000) return `${trim(n / 1000)} kg`
  if (unit === 'ml' && n >= 1000) return `${trim(n / 1000)} L`
  if (unit === 'each') return `${trim(n)} ${n === 1 ? 'item' : 'items'}`
  return `${trim(n)} ${unit ?? ''}`.trim()
}

export function title(text: string): string {
  return text.charAt(0).toUpperCase() + text.slice(1)
}

export function seconds(ms: number | null | undefined): string {
  return ms === null || ms === undefined ? '-' : `${(ms / 1000).toFixed(1)} s`
}

/** A reset time for people in Sydney, e.g. "Thu 15 Oct, 6:00 pm" (the server gives the offset). */
export function resetTime(iso: string): string {
  const m = /^(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2})/.exec(iso)
  if (!m) return iso
  const [, date, hh, mm] = m
  const hour = Number(hh)
  const suffix = hour >= 12 ? 'pm' : 'am'
  const h12 = hour % 12 === 0 ? 12 : hour % 12
  return `${shortDate(date!)}, ${h12}:${mm} ${suffix}`
}

/** A reset time in the viewer's own time zone, e.g. "Fri 16 Oct, 6:00 pm AEDT". */
export function localTime(iso: string, timeZone?: string): string {
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return iso
  const parts = new Intl.DateTimeFormat('en-AU', {
    weekday: 'short',
    day: 'numeric',
    month: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
    hour12: true,
    timeZoneName: 'short',
    timeZone,
  }).formatToParts(date)
  const get = (type: string) => parts.find((p) => p.type === type)?.value ?? ''
  const pm = get('dayPeriod').toLowerCase()
  return `${get('weekday')} ${get('day')} ${MONTHS[Number(get('month')) - 1] ?? ''}, ${get('hour')}:${get('minute')} ${pm} ${get('timeZoneName')}`
}
