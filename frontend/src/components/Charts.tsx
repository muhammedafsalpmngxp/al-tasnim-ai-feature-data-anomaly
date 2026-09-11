import { useEffect, useRef } from 'react'
import * as echarts from 'echarts'
import type { Finding, RunRow } from '../types'

export const SEVERITY_COLOURS: Record<string, string> = {
  critical: '#c00000',
  high: '#e36c0a',
  medium: '#bf9000',
  low: '#808080',
}

/**
 * One ECharts instance per mount, disposed on unmount.
 *
 * ECharts attaches a resize listener and a canvas to the DOM node; without an explicit dispose
 * a tab switch leaks both, and after a few switches the page is rendering to detached canvases
 * it never frees.
 */
function useChart(option: echarts.EChartsOption, deps: unknown[]) {
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!ref.current) return
    const chart = echarts.init(ref.current)
    chart.setOption(option)
    const resize = () => chart.resize()
    window.addEventListener('resize', resize)
    return () => {
      window.removeEventListener('resize', resize)
      chart.dispose()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)
  return ref
}

export function SeverityDonut({ bySeverity }: { bySeverity: Record<string, number> }) {
  const data = Object.entries(bySeverity)
    .filter(([, n]) => n > 0)
    .map(([name, value]) => ({ name, value, itemStyle: { color: SEVERITY_COLOURS[name] } }))

  const ref = useChart(
    {
      tooltip: { trigger: 'item', formatter: '{b}: {c} rule(s)' },
      legend: { bottom: 0, textStyle: { color: '#555' } },
      series: [
        {
          type: 'pie',
          radius: ['52%', '76%'],
          avoidLabelOverlap: true,
          label: { show: false },
          data,
        },
      ],
    },
    [JSON.stringify(bySeverity)],
  )

  if (!data.length) return <p className="muted">No rule reported a finding.</p>
  return <div ref={ref} style={{ height: 240 }} />
}

export function CategoryBar({ findings }: { findings: Finding[] }) {
  const totals = new Map<string, number>()
  for (const f of findings) {
    totals.set(f.category, (totals.get(f.category) ?? 0) + f.anomaly_count)
  }
  // Largest at the top of a horizontal bar chart means ascending data order, because ECharts
  // draws a value axis upwards from the origin.
  const sorted = [...totals.entries()].sort((a, b) => a[1] - b[1]).slice(-8)

  const ref = useChart(
    {
      grid: { left: 8, right: 24, top: 8, bottom: 8, containLabel: true },
      tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
      xAxis: { type: 'value', axisLabel: { color: '#666' } },
      yAxis: {
        type: 'category',
        data: sorted.map(([name]) => name),
        axisLabel: { color: '#666', width: 150, overflow: 'truncate' },
      },
      series: [
        {
          type: 'bar',
          data: sorted.map(([, value]) => value),
          itemStyle: { color: '#1f3864', borderRadius: [0, 3, 3, 0] },
          barMaxWidth: 22,
        },
      ],
    },
    [JSON.stringify(sorted)],
  )

  if (!sorted.length) return <p className="muted">Nothing to break down yet.</p>
  return <div ref={ref} style={{ height: Math.max(180, sorted.length * 34) }} />
}

export function ScoreTrend({ runs }: { runs: RunRow[] }) {
  // Oldest first, so the line reads left to right as time passes.
  const ordered = [...runs].reverse()
  const ref = useChart(
    {
      grid: { left: 8, right: 16, top: 16, bottom: 8, containLabel: true },
      tooltip: { trigger: 'axis' },
      xAxis: {
        type: 'category',
        data: ordered.map((r) => r.started_at.slice(5, 16).replace('T', ' ')),
        axisLabel: { color: '#666', rotate: 30 },
      },
      // Pinned 0-100 on purpose: an auto-scaled axis turns a one-point wobble into a cliff and
      // makes every run look like a crisis.
      yAxis: { type: 'value', min: 0, max: 100, axisLabel: { color: '#666' } },
      series: [
        {
          type: 'line',
          smooth: true,
          symbolSize: 7,
          data: ordered.map((r) => r.score),
          lineStyle: { color: '#1f3864', width: 2 },
          itemStyle: { color: '#1f3864' },
          areaStyle: { color: 'rgba(31,56,100,0.08)' },
        },
      ],
    },
    [JSON.stringify(ordered.map((r) => [r.run_id, r.score]))],
  )

  if (ordered.length < 2) {
    return <p className="muted">At least two runs are needed before a trend means anything.</p>
  }
  return <div ref={ref} style={{ height: 220 }} />
}
