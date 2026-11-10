import type { ReactNode } from 'react'
import type { ApiError } from '../api.ts'

/** Shown the first time a view loads. Refetches dim the old content instead. */
export function Loading({ label }: { label: string }) {
  return (
    <div className="state" role="status">
      <span className="spinner" aria-hidden="true" />
      {label}
    </div>
  )
}

export function ErrorState({ error, onRetry }: { error: ApiError; onRetry: () => void }) {
  return (
    <div className="state state-error" role="alert">
      <p>
        <strong>Couldn’t load this.</strong> {error.message}
      </p>
      <button type="button" className="button" onClick={onRetry}>
        Try again
      </button>
    </div>
  )
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="state">{children}</div>
}
