import { useId, useState } from 'react'
import type { Day, Dimension, WindowDays } from '../api.ts'

export interface FilterValues {
  dimension: Dimension
  window: WindowDays
  /** null = the latest day in the data. */
  asOf: Day | null
  includeConcentrated: boolean
}

interface Props {
  values: FilterValues
  onChange: (next: FilterValues) => void
  /** Allowed range for the as-of date, given the current window. */
  minDay: Day
  maxDay: Day
  /** Names offered as suggestions in the lookup box. */
  suggestions: string[]
  onLookup: (name: string) => void
}

const DIMENSIONS: { value: Dimension; label: string }[] = [
  { value: 'language', label: 'Languages' },
  { value: 'topic', label: 'Topics' },
]
const WINDOWS: WindowDays[] = [30, 60, 90]

/** The one filter row. Everything below it is scoped by these values. */
export function Filters({ values, onChange, minDay, maxDay, suggestions, onLookup }: Props) {
  const listId = useId()
  const [query, setQuery] = useState('')
  const noun = values.dimension === 'language' ? 'language' : 'topic'

  return (
    <form
      className="filters"
      aria-label="Filters"
      onSubmit={(e) => {
        e.preventDefault()
        const name = query.trim()
        if (name) {
          onLookup(name)
          setQuery('')
        }
      }}
    >
      <fieldset className="segmented">
        <legend className="field-label">Rank</legend>
        {DIMENSIONS.map((d) => (
          <label key={d.value} className={values.dimension === d.value ? 'on' : ''}>
            <input
              type="radio"
              name="dimension"
              checked={values.dimension === d.value}
              onChange={() => onChange({ ...values, dimension: d.value })}
            />
            {d.label}
          </label>
        ))}
      </fieldset>

      <fieldset className="segmented">
        <legend className="field-label">Window</legend>
        {WINDOWS.map((w) => (
          <label key={w} className={values.window === w ? 'on' : ''}>
            <input
              type="radio"
              name="window"
              checked={values.window === w}
              onChange={() => onChange({ ...values, window: w })}
            />
            {w} days
          </label>
        ))}
      </fieldset>

      <label className="field">
        <span className="field-label">Ending on</span>
        <input
          type="date"
          value={values.asOf ?? maxDay}
          min={minDay}
          max={maxDay}
          onChange={(e) => {
            const day = e.target.value
            // Ignore cleared or out-of-range input; the API would reject it anyway.
            if (!day || day < minDay || day > maxDay) return
            onChange({ ...values, asOf: day === maxDay ? null : day })
          }}
        />
      </label>

      <label className="field">
        <span className="field-label">Look up a {noun}</span>
        <span className="lookup">
          <input
            type="search"
            value={query}
            list={listId}
            placeholder={values.dimension === 'language' ? 'e.g. Python' : 'e.g. llm'}
            onChange={(e) => setQuery(e.target.value)}
          />
          <button type="submit" className="button">
            Show
          </button>
        </span>
        <datalist id={listId}>
          {suggestions.map((name) => (
            <option key={name} value={name} />
          ))}
        </datalist>
      </label>

      <label className="check">
        <input
          type="checkbox"
          checked={values.includeConcentrated}
          onChange={(e) => onChange({ ...values, includeConcentrated: e.target.checked })}
        />
        Include single-repo spikes
      </label>
    </form>
  )
}
