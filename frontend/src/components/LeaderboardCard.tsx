import type { Leaderboard } from '../api.ts'
import { formatChange, formatCount, formatShare } from '../format.ts'
import type { ApiState } from '../useApi.ts'
import { Empty, ErrorState, Loading } from './States.tsx'

interface Props {
  title: string
  subtitle: string
  /** 'gain' draws blue bars, 'loss' red ones. The sign and the heading say the same thing. */
  tone: 'gain' | 'loss'
  state: ApiState<Leaderboard>
  noun: string
  selected: string | null
  onSelect: (name: string) => void
}

/** A ranked list with one thin bar per row. Each row is a button that selects that entry. */
export function LeaderboardCard({ title, subtitle, tone, state, noun, selected, onSelect }: Props) {
  const { data, error, loading, retry } = state
  // Only entries moving in this card's direction belong here.
  const items = (data?.items ?? []).filter((i) => (tone === 'gain' ? i.momentum > 0 : i.momentum < 0))
  const largest = Math.max(...items.map((i) => Math.abs(i.momentum)), 0)

  return (
    <section className="card" aria-busy={loading}>
      <header className="card-head">
        <h2>{title}</h2>
        <p className="muted">{subtitle}</p>
      </header>

      {error ? (
        <ErrorState error={error} onRetry={retry} />
      ) : !data ? (
        <Loading label="Loading leaderboard…" />
      ) : items.length === 0 ? (
        <Empty>
          No {noun}s {tone === 'gain' ? 'gained' : 'lost'} share in this window.
        </Empty>
      ) : (
        <ol className={`board ${loading ? 'stale' : ''}`}>
          {items.map((item) => (
            <li key={item.name}>
              <button
                type="button"
                className={`row ${selected === item.name ? 'selected' : ''}`}
                aria-pressed={selected === item.name}
                onClick={() => onSelect(item.name)}
              >
                <span className="row-rank">{item.rank}</span>
                <span className="row-name">
                  {item.name}
                  {item.concentrated && (
                    <span className="badge" title="One repo is more than half of this activity">
                      1 repo
                    </span>
                  )}
                </span>
                <span className="row-value">{formatChange(item.momentum)}</span>
                <span className="row-bar" aria-hidden="true">
                  <span
                    className={`bar bar-${tone}`}
                    style={{ width: `${largest ? (Math.abs(item.momentum) / largest) * 100 : 0}%` }}
                  />
                </span>
                <span className="row-meta">
                  {formatShare(item.share_previous)} → {formatShare(item.share_current)} of
                  activity · {formatCount(item.active_repos)} repos
                </span>
              </button>
            </li>
          ))}
        </ol>
      )}
    </section>
  )
}
