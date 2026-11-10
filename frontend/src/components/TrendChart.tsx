import { useMemo, useState } from 'react'
import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import type { Timeseries, TimeseriesPoint } from '../api.ts'
import { formatCount, formatDay, formatDayLong, rollingMean } from '../format.ts'

type MetricKey = 'share' | 'activity' | 'stars' | 'forks' | 'pushes' | 'pr_events' | 'issue_events'

interface Metric {
  key: MetricKey
  label: string
  value: (p: TimeseriesPoint) => number
  format: (v: number) => string
}

const percent = (v: number) => `${v.toFixed(v >= 10 ? 1 : 2)}%`

const METRICS: Metric[] = [
  { key: 'share', label: 'Share of activity', value: (p) => p.share * 100, format: percent },
  { key: 'activity', label: 'Activity score', value: (p) => p.activity, format: formatCount },
  { key: 'stars', label: 'Stars', value: (p) => p.stars, format: formatCount },
  { key: 'forks', label: 'Forks', value: (p) => p.forks, format: formatCount },
  { key: 'pushes', label: 'Pushes', value: (p) => p.pushes, format: formatCount },
  { key: 'pr_events', label: 'Pull request events', value: (p) => p.pr_events, format: formatCount },
  { key: 'issue_events', label: 'Issue events', value: (p) => p.issue_events, format: formatCount },
]

interface Row {
  day: string
  /** Value in the previous window, null in the current one (and the reverse for `cur`). */
  prev: number | null
  cur: number | null
}

interface TooltipProps {
  active?: boolean
  payload?: ReadonlyArray<{ payload?: Row }>
  metric: Metric
  smooth: boolean
}

function ChartTooltip({ active, payload, metric, smooth }: TooltipProps) {
  const row = payload?.[0]?.payload
  if (!active || !row) return null
  const inCurrent = row.cur !== null
  const value = row.cur ?? row.prev ?? 0
  return (
    <div className="tooltip">
      <div className="tooltip-title">{formatDayLong(row.day)}</div>
      <div className="tooltip-row">
        <span className={`key ${inCurrent ? 'key-cur' : 'key-prev'}`} aria-hidden="true" />
        <strong>{metric.format(value)}</strong>
        <span className="muted">
          {metric.label.toLowerCase()}
          {smooth ? ', 7-day average' : ''}
        </span>
      </div>
      <div className="muted">{inCurrent ? 'Current window' : 'Previous window'}</div>
    </div>
  )
}

/** Daily values across both windows: the previous window in gray, the current one in blue. */
export function TrendChart({ series }: { series: Timeseries }) {
  const [metricKey, setMetricKey] = useState<MetricKey>('share')
  const [smooth, setSmooth] = useState(true)
  const metric = METRICS.find((m) => m.key === metricKey) ?? METRICS[0]
  const currentStart = series.current[0]

  const rows = useMemo<Row[]>(() => {
    const raw = series.points.map(metric.value)
    const values = smooth ? rollingMean(raw, 7) : raw
    return series.points.map((p, i) => {
      const inCurrent = p.day >= currentStart
      // The first current day also ends the gray line, so the two lines meet.
      const joins = p.day === currentStart
      return {
        day: p.day,
        prev: inCurrent && !joins ? null : values[i],
        cur: inCurrent ? values[i] : null,
      }
    })
  }, [series.points, metric, smooth, currentStart])

  const days = series.window

  return (
    <div className="chart">
      <div className="chart-controls">
        <label className="field inline">
          <span className="field-label">Show</span>
          <select value={metricKey} onChange={(e) => setMetricKey(e.target.value as MetricKey)}>
            {METRICS.map((m) => (
              <option key={m.key} value={m.key}>
                {m.label}
              </option>
            ))}
          </select>
        </label>
        <label className="check">
          <input type="checkbox" checked={smooth} onChange={(e) => setSmooth(e.target.checked)} />
          7-day average
        </label>
        <ul className="legend" aria-label="Legend">
          <li>
            <span className="key key-prev" aria-hidden="true" />
            Previous {days} days
          </li>
          <li>
            <span className="key key-cur" aria-hidden="true" />
            Current {days} days
          </li>
        </ul>
      </div>

      <div
        className="chart-plot"
        role="img"
        aria-label={`${metric.label} for ${series.name}, ${formatDayLong(series.previous[0])} to ${formatDayLong(series.as_of)}. The data table below has every value.`}
      >
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={rows} margin={{ top: 8, right: 12, bottom: 0, left: 0 }}>
            <CartesianGrid vertical={false} stroke="var(--grid)" />
            <XAxis
              dataKey="day"
              tickFormatter={formatDay}
              tick={{ fill: 'var(--muted)', fontSize: 12 }}
              stroke="var(--axis)"
              tickLine={false}
              minTickGap={48}
            />
            <YAxis
              tickFormatter={metric.format}
              tick={{ fill: 'var(--muted)', fontSize: 12 }}
              axisLine={false}
              tickLine={false}
              width={56}
            />
            <Tooltip
              cursor={{ stroke: 'var(--axis)', strokeWidth: 1 }}
              content={(props) => (
                <ChartTooltip
                  active={props.active}
                  payload={props.payload as TooltipProps['payload']}
                  metric={metric}
                  smooth={smooth}
                />
              )}
            />
            <Line
              dataKey="prev"
              stroke="var(--deemph)"
              strokeWidth={2}
              strokeLinecap="round"
              strokeLinejoin="round"
              dot={false}
              activeDot={{ r: 4, stroke: 'var(--surface)', strokeWidth: 2 }}
              isAnimationActive={false}
            />
            <Line
              dataKey="cur"
              stroke="var(--series-1)"
              strokeWidth={2}
              strokeLinecap="round"
              strokeLinejoin="round"
              dot={false}
              activeDot={{ r: 4, stroke: 'var(--surface)', strokeWidth: 2 }}
              isAnimationActive={false}
            />
          </LineChart>
        </ResponsiveContainer>
      </div>

      <details className="table-view">
        <summary>Show the daily numbers as a table</summary>
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th scope="col">Day</th>
                <th scope="col">Share</th>
                <th scope="col">Stars</th>
                <th scope="col">Forks</th>
                <th scope="col">Pushes</th>
                <th scope="col">PR events</th>
                <th scope="col">Issue events</th>
              </tr>
            </thead>
            <tbody>
              {series.points.map((p) => (
                <tr key={p.day}>
                  <th scope="row">{formatDayLong(p.day)}</th>
                  <td>{percent(p.share * 100)}</td>
                  <td>{formatCount(p.stars)}</td>
                  <td>{formatCount(p.forks)}</td>
                  <td>{formatCount(p.pushes)}</td>
                  <td>{formatCount(p.pr_events)}</td>
                  <td>{formatCount(p.issue_events)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
    </div>
  )
}
