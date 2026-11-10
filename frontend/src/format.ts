// Pure formatting and date helpers. Days are ISO strings (YYYY-MM-DD) and always UTC.

import type { Day } from './api.ts'

/** GH Archive's collection volume dropped about 38% on this day (docs/SPEC.md §8a). */
export const VOLUME_SHIFT_DAY: Day = '2025-06-01'

const DAY_MS = 86_400_000

export function parseDay(day: Day): Date {
  return new Date(`${day}T00:00:00Z`)
}

export function toDay(date: Date): Day {
  return date.toISOString().slice(0, 10)
}

export function addDays(day: Day, days: number): Day {
  return toDay(new Date(parseDay(day).getTime() + days * DAY_MS))
}

/** The earliest as-of day that leaves room for both windows (SPEC §4). */
export function minAsOf(earliestDay: Day, windowDays: number): Day {
  return addDays(earliestDay, 2 * windowDays - 1)
}

/** True when the comparison spans the June 2025 volume drop, which makes it noisier. */
export function straddlesVolumeShift(previousStart: Day, asOf: Day): boolean {
  return previousStart < VOLUME_SHIFT_DAY && asOf >= VOLUME_SHIFT_DAY
}

const shortDate = new Intl.DateTimeFormat('en', { timeZone: 'UTC', month: 'short', day: 'numeric' })
const longDate = new Intl.DateTimeFormat('en', {
  timeZone: 'UTC',
  month: 'short',
  day: 'numeric',
  year: 'numeric',
})

/** "Sep 30" */
export function formatDay(day: Day): string {
  return shortDate.format(parseDay(day))
}

/** "Sep 30, 2025" */
export function formatDayLong(day: Day): string {
  return longDate.format(parseDay(day))
}

/** "Sep 1 – Sep 30, 2025" */
export function formatRange([start, end]: readonly [Day, Day]): string {
  return `${formatDay(start)} – ${formatDayLong(end)}`
}

/** A fraction as a percentage: 0.0108 -> "1.08%". Small shares get more digits. */
export function formatShare(fraction: number): string {
  const pct = fraction * 100
  const digits = pct >= 10 ? 1 : pct >= 0.1 ? 2 : 3
  return `${pct.toFixed(digits)}%`
}

/** A relative change with a sign: 0.294 -> "+29%", -0.05 -> "−5.0%". */
export function formatChange(fraction: number | null): string {
  if (fraction === null || !Number.isFinite(fraction)) return '–'
  const pct = fraction * 100
  const digits = Math.abs(pct) >= 10 ? 0 : 1
  const rounded = Number(pct.toFixed(digits))
  if (rounded === 0) return '0%'
  const sign = rounded > 0 ? '+' : '−'
  return `${sign}${Math.abs(rounded).toFixed(digits)}%`
}

const compact = new Intl.NumberFormat('en', { notation: 'compact', maximumFractionDigits: 1 })
const whole = new Intl.NumberFormat('en', { maximumFractionDigits: 0 })

/** In full under 10,000, compact above: 1,284 / 12.9K / 4.2M. */
export function formatCount(value: number): string {
  return Math.abs(value) < 10_000 ? whole.format(value) : compact.format(value)
}

/** A signed count: +1,938 / −420. */
export function formatDelta(value: number): string {
  const rounded = Math.round(value)
  if (rounded === 0) return '0'
  return `${rounded > 0 ? '+' : '−'}${formatCount(Math.abs(rounded))}`
}

/**
 * Trailing mean over up to `size` values ending at each index. The first few points average
 * over what is available, so the series keeps its length.
 */
export function rollingMean(values: readonly number[], size: number): number[] {
  const out: number[] = []
  let sum = 0
  for (let i = 0; i < values.length; i++) {
    sum += values[i]
    if (i >= size) sum -= values[i - size]
    out.push(sum / Math.min(i + 1, size))
  }
  return out
}
