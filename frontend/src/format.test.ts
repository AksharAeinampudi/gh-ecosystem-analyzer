import { describe, expect, it } from 'vitest'
import { apiUrl, errorDetail } from './api.ts'
import {
  addDays,
  formatChange,
  formatCount,
  formatDelta,
  formatRange,
  formatShare,
  minAsOf,
  rollingMean,
  straddlesVolumeShift,
} from './format.ts'

describe('dates', () => {
  it('adds days across month ends in UTC', () => {
    expect(addDays('2025-02-27', 2)).toBe('2025-03-01')
    expect(addDays('2025-09-01', -1)).toBe('2025-08-31')
  })

  it('finds the earliest as-of day for each window', () => {
    // Matches the API: "as_of must be between 2025-03-01 and ..." for a 30-day window.
    expect(minAsOf('2025-01-01', 30)).toBe('2025-03-01')
    expect(minAsOf('2025-01-01', 90)).toBe('2025-06-29')
  })

  it('formats a range', () => {
    expect(formatRange(['2025-09-01', '2025-09-30'])).toBe('Sep 1 – Sep 30, 2025')
  })

  it('flags comparisons that span the June 2025 volume drop', () => {
    expect(straddlesVolumeShift('2025-05-03', '2025-07-01')).toBe(true)
    expect(straddlesVolumeShift('2025-05-31', '2025-06-01')).toBe(true)
    expect(straddlesVolumeShift('2025-06-01', '2025-07-30')).toBe(false)
    expect(straddlesVolumeShift('2025-03-01', '2025-05-31')).toBe(false)
  })
})

describe('numbers', () => {
  it('formats shares with more digits when small', () => {
    expect(formatShare(0.232)).toBe('23.2%')
    expect(formatShare(0.0108)).toBe('1.08%')
    expect(formatShare(0.0006)).toBe('0.060%')
  })

  it('formats changes with a sign and a real minus', () => {
    expect(formatChange(0.294)).toBe('+29%')
    expect(formatChange(-0.243)).toBe('−24%')
    expect(formatChange(-0.05)).toBe('−5.0%')
    expect(formatChange(0.0001)).toBe('0%')
    expect(formatChange(null)).toBe('–')
  })

  it('formats counts in full under 10,000 and compact above', () => {
    expect(formatCount(1284)).toBe('1,284')
    expect(formatCount(12_940)).toBe('12.9K')
    expect(formatDelta(1938.4)).toBe('+1,938')
    expect(formatDelta(-420)).toBe('−420')
    expect(formatDelta(0.2)).toBe('0')
  })

  it('computes a trailing mean that keeps the series length', () => {
    expect(rollingMean([2, 4, 6, 8], 2)).toEqual([2, 3, 5, 7])
    expect(rollingMean([3, 3, 3], 7)).toEqual([3, 3, 3])
    expect(rollingMean([], 7)).toEqual([])
  })
})

describe('api helpers', () => {
  it('builds URLs, encoding names and skipping empty params', () => {
    expect(apiUrl('/api/v1/timeseries', { name: 'C++', as_of: null, window: 30 })).toBe(
      '/api/v1/timeseries?name=C%2B%2B&window=30',
    )
    expect(apiUrl('/api/v1/meta')).toBe('/api/v1/meta')
  })

  it('reads FastAPI error bodies', () => {
    expect(errorDetail({ detail: "unknown language: 'Klingon'" }, 'x')).toBe(
      "unknown language: 'Klingon'",
    )
    expect(errorDetail({ detail: [{ msg: 'Input should be 30, 60 or 90' }] }, 'x')).toBe(
      'Input should be 30, 60 or 90',
    )
    expect(errorDetail(null, 'Request failed (500)')).toBe('Request failed (500)')
  })
})
