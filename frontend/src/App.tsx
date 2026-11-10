import { useRef, useState } from 'react'
import type { Leaderboard, LeaderboardItem, Meta } from './api.ts'
import { urls } from './api.ts'
import { Detail } from './components/Detail.tsx'
import { Filters, type FilterValues } from './components/Filters.tsx'
import { LeaderboardCard } from './components/LeaderboardCard.tsx'
import { ErrorState, Loading } from './components/States.tsx'
import { formatDayLong, formatRange, minAsOf, straddlesVolumeShift } from './format.ts'
import { useApi } from './useApi.ts'

const TOP_N = 10

export default function App() {
  const meta = useApi<Meta>(urls.meta())
  const [filters, setFilters] = useState<FilterValues>({
    dimension: 'language',
    window: 30,
    asOf: null,
    includeConcentrated: false,
  })
  // What the user picked. Until they pick, the top gainer is shown.
  const [picked, setPicked] = useState<string | null>(null)
  const detailRef = useRef<HTMLDivElement>(null)

  // On a one-column layout the detail panel sits below both lists, so bring it into view.
  const select = (name: string) => {
    setPicked(name)
    if (globalThis.matchMedia('(max-width: 900px)').matches) {
      detailRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' })
    }
  }

  const { dimension, window, includeConcentrated } = filters
  const ready = meta.data !== null
  // A longer window needs a later start; move the date forward if it no longer fits.
  const earliestAsOf = meta.data ? minAsOf(meta.data.earliest_day, window) : null
  const asOf = filters.asOf && earliestAsOf && filters.asOf < earliestAsOf ? earliestAsOf : filters.asOf

  const base = { dimension, window, asOf, includeConcentrated }
  const gainers = useApi<Leaderboard>(
    ready ? urls.leaderboard({ ...base, order: 'desc', limit: TOP_N }) : null,
  )
  const decliners = useApi<Leaderboard>(
    ready ? urls.leaderboard({ ...base, order: 'asc', limit: TOP_N }) : null,
  )
  // Up to 100 entries, spikes included: lookup suggestions, and figures for entries outside
  // the top 10. This covers every ranked language; for topics it is the top 100 gainers.
  const everyone = useApi<Leaderboard>(
    ready
      ? urls.leaderboard({ ...base, includeConcentrated: true, order: 'desc', limit: 100 })
      : null,
  )

  if (meta.error) {
    return (
      <main className="page">
        <PageHeader />
        <ErrorState error={meta.error} onRetry={meta.retry} />
      </main>
    )
  }
  if (!meta.data || !earliestAsOf) {
    return (
      <main className="page">
        <PageHeader />
        <Loading label="Loading the dataset…" />
      </main>
    )
  }

  const windowInfo = gainers.data ?? decliners.data
  // While a new ranking loads, the lists still hold the old one (dimmed). Names from the other
  // dimension must not be used to pick or describe an entry in this one.
  const lists = [everyone.data, decliners.data, gainers.data].filter(
    (list) => list !== null && list.dimension === dimension,
  )
  const firstOf = (list: Leaderboard | null) =>
    list && list.dimension === dimension ? (list.items[0]?.name ?? null) : null
  const selected = picked ?? firstOf(gainers.data) ?? firstOf(decliners.data)
  const noun = dimension === 'language' ? 'language' : 'topic'

  const known = new Map<string, LeaderboardItem>()
  for (const list of lists) {
    for (const item of list?.items ?? []) known.set(item.name, item)
  }
  // Figures from a list that is being replaced belong to the old window.
  const listsLoading = gainers.loading || decliners.loading || everyone.loading
  const suggestions = [...known.keys()].sort((a, b) => a.localeCompare(b))

  return (
    <main className="page">
      <PageHeader meta={meta.data} />

      <Filters
        values={{ ...filters, asOf }}
        minDay={earliestAsOf}
        maxDay={meta.data.latest_day}
        suggestions={suggestions}
        onLookup={select}
        onChange={(next) => {
          // A language name means nothing as a topic, so start over when the ranking changes.
          if (next.dimension !== dimension) setPicked(null)
          setFilters(next)
        }}
      />

      {windowInfo && (
        <p className="comparison">
          Comparing <strong>{formatRange(windowInfo.current)}</strong> with{' '}
          <strong>{formatRange(windowInfo.previous)}</strong>.
          {!includeConcentrated && (
            <>
              {' '}
              {noun === 'language' ? 'Languages' : 'Topics'} where one repo is more than{' '}
              {Math.round(meta.data.max_top_repo_share * 100)}% of the activity are hidden.
            </>
          )}
        </p>
      )}

      {windowInfo && straddlesVolumeShift(windowInfo.previous[0], windowInfo.as_of) && (
        <p className="notice" role="note">
          <span aria-hidden="true">⚠</span>
          <span>
            <strong>This comparison is noisier than usual.</strong> It spans June 1, 2025, when the
            source data’s volume dropped by about 38%. Shares cancel most of that, but they moved
            about twice as much as in a normal month.
          </span>
        </p>
      )}

      <div className="layout">
        <div className="boards">
          <LeaderboardCard
            title="Gaining share"
            subtitle={`Top ${noun}s by momentum`}
            tone="gain"
            state={gainers}
            noun={noun}
            selected={selected}
            onSelect={select}
          />
          <LeaderboardCard
            title="Losing share"
            subtitle={`Bottom ${noun}s by momentum`}
            tone="loss"
            state={decliners}
            noun={noun}
            selected={selected}
            onSelect={select}
          />
        </div>

        <div ref={detailRef} className="detail-slot">
        {selected ? (
          <Detail
            key={dimension}
            dimension={dimension}
            window={window}
            asOf={asOf}
            name={selected}
            item={listsLoading ? null : (known.get(selected) ?? null)}
          />
        ) : (
          <section className="card">
            {gainers.error || decliners.error ? (
              <p className="state">Pick a {noun} once the leaderboard loads.</p>
            ) : (
              <Loading label="Loading…" />
            )}
          </section>
        )}
        </div>
      </div>

      <footer className="footer">
        <p>
          Momentum is the change in a {noun}’s share of all tracked activity between two windows.
          Activity = stars + 2 × forks + 0.5 × pushes, with pushes capped at {meta.data.push_cap}{' '}
          per repo per day. Based on the {meta.data.tracked_repos.toLocaleString('en')} most active
          public repos in GH Archive. Languages and topics are the ones GitHub lists today.
        </p>
      </footer>
    </main>
  )
}

function PageHeader({ meta }: { meta?: Meta }) {
  return (
    <header className="page-head">
      <h1>GitHub Ecosystem Analyzer</h1>
      <p>
        Which languages and topics are gaining or losing share of activity on GitHub.
        {meta && (
          <>
            {' '}
            A fixed snapshot: {formatDayLong(meta.earliest_day)} to {formatDayLong(meta.latest_day)}.
          </>
        )}
      </p>
      {meta && (
        <details className="why">
          <summary>Why does the data stop in 2025?</summary>
          <p>
            The source, GH Archive, stopped recording most stars and forks from late 2025, and its
            2026 data is erratic. January to September 2025 is the stretch where the data is
            complete enough to compare.
          </p>
        </details>
      )}
    </header>
  )
}
