import { describe, expect, it } from 'vitest'
import { localTime, money, quantity, shortDate } from './format'

describe('format', () => {
  it('writes dates and money the way the server does', () => {
    expect(shortDate('2026-10-20')).toBe('Tue 20 Oct')
    expect(money('11.4')).toBe('$11.40')
    expect(quantity('1200', 'g')).toBe('1.2 kg')
    expect(quantity('12', 'each')).toBe('12 items')
  })

  it('shows a reset time in the given (viewer) time zone', () => {
    expect(localTime('2026-10-16T07:00:00+00:00', 'Australia/Sydney')).toMatch(/^Fri 16 Oct, 6:00 pm (AEDT|GMT\+11)$/)
    expect(localTime('2026-10-16T07:00:00+00:00', 'UTC')).toMatch(/^Fri 16 Oct, 7:00 am (UTC|GMT)$/)
    expect(localTime('2026-10-16T07:00:00+00:00', 'America/Los_Angeles')).toMatch(/^Fri 16 Oct, 12:00 am /)
  })
})

describe('localTime for the Sydney reset', () => {
  // The quota resets at midnight Pacific: 07:00 UTC in PDT (Mar-Nov) and 08:00 UTC in PST.
  it('is 6:00 pm AEDT in daylight saving', () => {
    expect(localTime('2026-10-16T07:00:00Z', 'Australia/Sydney')).toBe('Fri 16 Oct, 6:00 pm AEDT')
  })
  it('is 5:00 pm AEST outside daylight saving', () => {
    expect(localTime('2026-07-16T07:00:00Z', 'Australia/Sydney')).toBe('Thu 16 Jul, 5:00 pm AEST')
  })
})
