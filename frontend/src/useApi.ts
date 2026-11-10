import { useCallback, useEffect, useState } from 'react'
import { ApiError, getJson } from './api.ts'

export interface ApiState<T> {
  /** The latest data. While a new URL loads, this stays the previous result. */
  data: T | null
  error: ApiError | null
  /** True while a request is in flight (first load or refetch). */
  loading: boolean
  retry: () => void
}

interface Settled<T> {
  url: string
  data: T | null
  error: ApiError | null
}

/**
 * Fetch JSON from `url` (null = don't fetch).
 *
 * When the URL changes, the previous data is kept until the new response arrives, so views
 * can dim instead of flashing empty. An error clears the data. Nothing is cached here: the
 * API keeps its own cache, so repeat requests come back in a few milliseconds.
 */
export function useApi<T>(url: string | null): ApiState<T> {
  const [settled, setSettled] = useState<Settled<T> | null>(null)
  const [attempt, setAttempt] = useState(0)

  useEffect(() => {
    if (url === null) return
    const controller = new AbortController()
    getJson<T>(url, controller.signal)
      .then((data) => setSettled({ url, data, error: null }))
      .catch((err: unknown) => {
        if (controller.signal.aborted) return
        const error =
          err instanceof ApiError ? err : new ApiError(0, 'Something went wrong loading data.')
        setSettled({ url, data: null, error })
      })
    return () => controller.abort()
  }, [url, attempt])

  const retry = useCallback(() => {
    setSettled(null)
    setAttempt((n) => n + 1)
  }, [])

  if (url === null) return { data: null, error: null, loading: false, retry }
  const current = settled?.url === url
  return {
    data: settled?.data ?? null,
    error: current ? (settled?.error ?? null) : null,
    loading: !current,
    retry,
  }
}
