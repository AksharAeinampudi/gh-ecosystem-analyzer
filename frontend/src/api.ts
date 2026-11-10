// API types and URL builders. These mirror docs/SPEC.md §5 and backend/app/schemas.py.

export type Dimension = 'language' | 'topic'
export type WindowDays = 30 | 60 | 90
export type Order = 'desc' | 'asc'

/** ISO date, YYYY-MM-DD (UTC day). */
export type Day = string

export interface Meta {
  earliest_day: Day
  latest_day: Day
  windows: number[]
  tracked_repos: number
  weights: { stars: number; forks: number; pushes: number }
  push_cap: number
  floor: number
  min_repos: number
  max_top_repo_share: number
}

/** Fields shared by every windowed response. Ranges are [first day, last day]. */
export interface WindowInfo {
  dimension: Dimension
  window: number
  as_of: Day
  current: [Day, Day]
  previous: [Day, Day]
}

export interface LeaderboardItem {
  rank: number
  name: string
  momentum: number
  share_current: number
  share_previous: number
  activity_current: number
  activity_previous: number
  active_repos: number
  top_repo_share: number
  concentrated: boolean
}

export interface Leaderboard extends WindowInfo {
  items: LeaderboardItem[]
}

export interface TimeseriesPoint {
  day: Day
  stars: number
  forks: number
  pushes: number
  pr_events: number
  issue_events: number
  activity: number
  share: number
}

export interface WindowSummary {
  activity_current: number
  activity_previous: number
  share_current: number
  share_previous: number
  momentum: number | null
}

export interface Timeseries extends WindowInfo {
  name: string
  summary: WindowSummary
  points: TimeseriesPoint[]
}

export interface TopRepo {
  full_name: string
  activity_current: number
  activity_previous: number
  delta: number
  stars_total: number | null
  url: string
}

export interface TopRepos extends WindowInfo {
  name: string
  items: TopRepo[]
}

export class ApiError extends Error {
  status: number

  constructor(status: number, message: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

const BASE: string = (import.meta.env.VITE_API_BASE as string | undefined) ?? ''

type ParamValue = string | number | boolean | null | undefined

/** Build an API URL; null and undefined params are left out. */
export function apiUrl(path: string, params: Record<string, ParamValue> = {}): string {
  const query = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value !== null && value !== undefined) query.set(key, String(value))
  }
  const qs = query.toString()
  return `${BASE}${path}${qs ? `?${qs}` : ''}`
}

interface WindowParams {
  dimension: Dimension
  window: WindowDays
  asOf: Day | null
}

export const urls = {
  meta: () => apiUrl('/api/v1/meta'),
  leaderboard: (p: WindowParams & { order: Order; limit: number; includeConcentrated: boolean }) =>
    apiUrl('/api/v1/leaderboard', {
      dimension: p.dimension,
      window: p.window,
      as_of: p.asOf,
      order: p.order,
      limit: p.limit,
      include_concentrated: p.includeConcentrated,
    }),
  timeseries: (p: WindowParams & { name: string }) =>
    apiUrl('/api/v1/timeseries', {
      dimension: p.dimension,
      window: p.window,
      as_of: p.asOf,
      name: p.name,
    }),
  topRepos: (p: WindowParams & { name: string; order: Order; limit: number }) =>
    apiUrl('/api/v1/top-repos', {
      dimension: p.dimension,
      window: p.window,
      as_of: p.asOf,
      name: p.name,
      order: p.order,
      limit: p.limit,
    }),
}

/** Turn a FastAPI error body into one readable sentence. */
export function errorDetail(body: unknown, fallback: string): string {
  if (typeof body !== 'object' || body === null || !('detail' in body)) return fallback
  const detail = (body as { detail: unknown }).detail
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) {
    const messages = detail
      .map((d: unknown) =>
        typeof d === 'object' && d !== null && 'msg' in d ? String((d as { msg: unknown }).msg) : '',
      )
      .filter(Boolean)
    if (messages.length > 0) return messages.join('; ')
  }
  return fallback
}

export async function getJson<T>(url: string, signal?: AbortSignal): Promise<T> {
  let response: Response
  try {
    response = await fetch(url, { signal, headers: { Accept: 'application/json' } })
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') throw err
    throw new ApiError(0, 'Could not reach the API. Is the server running?')
  }
  if (!response.ok) {
    const body: unknown = await response.json().catch(() => null)
    throw new ApiError(response.status, errorDetail(body, `Request failed (${response.status})`))
  }
  return (await response.json()) as T
}
