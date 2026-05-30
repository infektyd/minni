import { useEffect, useState } from "react";
import type { EvidenceRow } from "../api";

function useTicker(intervalMs: number) {
  const [tick, setTick] = useState(0);
  useEffect(() => {
    const reduce = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
    if (reduce) return;
    const id = window.setInterval(() => setTick((t) => t + 1), intervalMs);
    return () => window.clearInterval(id);
  }, [intervalMs]);
  return tick;
}

function Sparkline({ values, color = "var(--verdigris)" }: { values: number[]; color?: string }) {
  const w = 120;
  const h = 28;
  const max = Math.max(...values);
  const min = Math.min(...values);
  const range = max - min || 1;
  const pts = values
    .map((v, i) => {
      const x = (i / (values.length - 1)) * w;
      const y = h - ((v - min) / range) * (h - 4) - 2;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
  const lastY = h - ((values[values.length - 1]! - min) / range) * (h - 4) - 2;
  return (
    <svg width={w} height={h} style={{ display: "block" }} aria-hidden="true">
      <polyline
        points={pts}
        fill="none"
        stroke={color}
        strokeWidth="1.4"
        strokeLinejoin="round"
        strokeLinecap="round"
      />
      <circle cx={w} cy={lastY} r="2" fill={color} />
    </svg>
  );
}

export function TelemetryRail({
  focusSource,
  auditCount,
  budgetTokens,
  usedTokens,
}: {
  focusSource: EvidenceRow | undefined;
  auditCount: number;
  budgetTokens: number;
  usedTokens: number;
}) {
  const tick = useTicker(1400);
  const series = (seed: number) =>
    Array.from(
      { length: 24 },
      (_, i) => 50 + Math.sin((tick + i + seed) * 0.4) * 18 + ((tick + i + seed) % 7) * 2,
    );

  const rows = [
    { label: "AUDIT · ENTRIES", v: String(auditCount), tone: "info" },
    { label: "BUDGET · TOKENS", v: budgetTokens ? `${usedTokens} / ${budgetTokens}` : "—", tone: "info" },
    { label: "BRIDGE", v: "127.0.0.1", tone: "ok" },
    { label: "MODE", v: "local-only", tone: "ok" },
  ];

  return (
    <footer className="telemetry">
      <div className="telemetry-mast">
        <span className="telemetry-mast-label">SOVRD</span>
        <span className="telemetry-mast-pid">local</span>
        <span className="telemetry-mast-uptime">v4.2</span>
      </div>
      <div className="telemetry-cells">
        {rows.map((r, i) => (
          <div className="telemetry-cell" key={r.label}>
            <div className="telemetry-cell-head">
              <span className="telemetry-cell-label">{r.label}</span>
              <span className="telemetry-cell-value">{r.v}</span>
            </div>
            <Sparkline values={series(i * 3)} />
          </div>
        ))}

        <div className="telemetry-cell telemetry-trace">
          <div className="telemetry-cell-head">
            <span className="telemetry-cell-label">ACTIVE TRACE</span>
            <span className="telemetry-cell-value">{focusSource ? focusSource.id : "—"}</span>
          </div>
          <div className="telemetry-trace-line">
            <span className="telemetry-trace-step is-done">recall</span>
            <span className="telemetry-trace-arrow">→</span>
            <span className="telemetry-trace-step is-done">rerank</span>
            <span className="telemetry-trace-arrow">→</span>
            <span className="telemetry-trace-step is-active">mmr</span>
            <span className="telemetry-trace-arrow">→</span>
            <span className="telemetry-trace-step">envelope</span>
          </div>
        </div>

        <div className="telemetry-cell telemetry-focus">
          <div className="telemetry-cell-head">
            <span className="telemetry-cell-label">FOCUSED SOURCE</span>
            <span
              className="telemetry-cell-value mono"
              style={{ color: "var(--verdigris)" }}
            >
              {focusSource ? focusSource.id : "—"}
            </span>
          </div>
          <div className="telemetry-focus-meta">
            {focusSource
              ? `${focusSource.cls} · ${focusSource.privacy} · ${focusSource.afm}`
              : "no selection"}
          </div>
        </div>
      </div>
    </footer>
  );
}

export function ActivityStream({ entries }: { entries: string[] }) {
  const tick = useTicker(2200);
  if (entries.length === 0) {
    return (
      <aside className="activity">
        <div className="activity-head">
          <span className="activity-head-title">ACTIVITY · IDLE</span>
        </div>
        <div className="activity-list">
          <div className="activity-row" style={{ opacity: 0.6 }}>
            <span className="activity-t">—</span>
            <span className="activity-src">—</span>
            <span className="activity-op">no audit yet</span>
            <span className="activity-note">run a recall to bind a trace</span>
          </div>
        </div>
      </aside>
    );
  }
  const offset = tick % entries.length;
  const rotated = [...entries.slice(offset), ...entries.slice(0, offset)];
  return (
    <aside className="activity">
      <div className="activity-head">
        <span className="activity-head-title">ACTIVITY · LIVE</span>
        <span className="activity-head-dot" />
      </div>
      <div className="activity-list">
        {rotated.slice(0, 12).map((line, i) => (
          <div className="activity-row" key={i} style={{ opacity: 1 - i * 0.05 }}>
            <span className="activity-t">+{i}s</span>
            <span className="activity-src">audit</span>
            <span className="activity-op">tail</span>
            <span className="activity-note" title={line}>
              {line.length > 40 ? line.slice(0, 40) + "…" : line}
            </span>
          </div>
        ))}
      </div>
    </aside>
  );
}
