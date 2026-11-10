import { useState } from 'react'
import type { Day, Dimension, LeaderboardItem, Order, Timeseries, TopRepos, WindowDays } from '../api.ts'
import { urls } from '../api.ts'
import { formatChange, formatCount, formatDelta, formatRange, formatShare } from '../format.ts'
import { useApi } from '../useApi.ts'
import { Empty, ErrorState, Loading } from './States.tsx'
import { TrendChart } from './TrendChart.tsx'

interface Props {
  dimension: Dimension
  window: WindowDays
  asOf: Day | null
  name: string
  /** The leaderboard row for this name, when one of the loaded lists has it. */
  item: LeaderboardItem | null
}

function Tile({ label, value, note }: { label: string; value: string; note: string }) {
  return (
    <div className="tile">
      <div className="tile-label">{label}</div>
      <div className="tile-value">{value}</div>
      <div className="tile-note">{note}</div>
    </div>
  )
}

/** Everything about one language or topic: headline figures, the trend, and its top repos. */
export function Detail({ dimension, window, asOf, name, item }: Props) {
  const [repoOrder, setRepoOrder] = useState<Order>('desc')
  const series = useApi<Timeseries>(urls.timeseries({ dimension, window, asOf, name }))
  const repos = useApi<TopRepos>(
    urls.topRepos({ dimension, window, asOf, name, order: repoOrder, limit: 10 }),
  )
  const noun = dimension === 'language' ? 'language' : 'topic'

  if (series.error) {
    return (
      <section className="card">
        <header className="card-head">
          <h2>{name}</h2>
        </header>
        {series.error.status === 404 ? (
          <Empty>
            No {noun} called “{name}” is in the data. Names are case-sensitive
            {dimension === 'topic' ? ', and a topic needs at least 20 tracked repos' : ''}.
          </Empty>
        ) : (
          <ErrorState error={series.error} onRetry={series.retry} />
        )}
      </section>
    )
  }

  const data = series.data
  if (!data) {
    return (
      <section className="card">
        <Loading label={`Loading ${name}…`} />
      </section>
    )
  }

  // While another name loads, `data` is still the previous one; label what is on screen.
  const shown = data.name
  const stale = series.loading
  const shownItem = item && item.name === shown && !stale ? item : null
  const s = data.summary

  return (
    <section className={`card detail ${stale ? 'stale' : ''}`} aria-busy={stale}>
      <header className="card-head">
        <h2>{shown}</h2>
        <p className="muted">
          {dimension === 'language' ? 'Language' : 'Topic'} · {formatRange(data.current)} compared
          with {formatRange(data.previous)}
        </p>
      </header>

      <div className="tiles">
        <Tile
          label="Momentum"
          value={formatChange(s.momentum)}
          note={`change in share vs the previous ${data.window} days`}
        />
        <Tile
          label="Share of activity"
          value={formatShare(s.share_current)}
          note={`was ${formatShare(s.share_previous)}`}
        />
        <Tile
          label="Active repos"
          value={shownItem ? formatCount(shownItem.active_repos) : '–'}
          note={shownItem ? 'tracked repos with activity' : 'shown for ranked entries'}
        />
        <Tile
          label="Largest repo"
          value={shownItem ? `${Math.round(shownItem.top_repo_share * 100)}%` : '–'}
          note={
            shownItem?.concentrated
              ? 'of activity: a single-repo spike'
              : shownItem
                ? 'of this window’s activity'
                : 'shown for ranked entries'
          }
        />
      </div>

      <h3>Trend</h3>
      <TrendChart series={data} />

      <div className="section-head">
        <h3>Repos behind the change</h3>
        <fieldset className="segmented small">
          <legend className="visually-hidden">Order</legend>
          {(['desc', 'asc'] as const).map((order) => (
            <label key={order} className={repoOrder === order ? 'on' : ''}>
              <input
                type="radio"
                name="repo-order"
                checked={repoOrder === order}
                onChange={() => setRepoOrder(order)}
              />
              {order === 'desc' ? 'Biggest gains' : 'Biggest drops'}
            </label>
          ))}
        </fieldset>
      </div>

      {repos.error ? (
        <ErrorState error={repos.error} onRetry={repos.retry} />
      ) : !repos.data ? (
        <Loading label="Loading repos…" />
      ) : repos.data.items.length === 0 ? (
        <Empty>No repos with activity in these windows.</Empty>
      ) : (
        <div className={`table-scroll ${repos.loading ? 'stale' : ''}`}>
          <table className="repos">
            <thead>
              <tr>
                <th scope="col">Repository</th>
                <th scope="col">Previous</th>
                <th scope="col">Current</th>
                <th scope="col">Change</th>
                <th scope="col">Stars today</th>
              </tr>
            </thead>
            <tbody>
              {repos.data.items.map((r) => (
                <tr key={r.full_name}>
                  <th scope="row">
                    <a href={r.url} target="_blank" rel="noreferrer">
                      {r.full_name}
                    </a>
                  </th>
                  <td>{formatCount(Math.round(r.activity_previous))}</td>
                  <td>{formatCount(Math.round(r.activity_current))}</td>
                  <td>{formatDelta(r.delta)}</td>
                  <td>{r.stars_total === null ? '–' : formatCount(r.stars_total)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="muted small-print">
            Previous, current and change are activity scores: stars + 2 × forks + 0.5 × pushes
            (pushes capped per day).
          </p>
        </div>
      )}
    </section>
  )
}
