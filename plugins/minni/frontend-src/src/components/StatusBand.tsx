import type { ReactNode } from "react";
import type { HealthReport, StatusReport } from "../api";

export type Tone = "ok" | "warn" | "danger" | "info";

export interface BandStat {
  label: string;
  value: string;
  tone: Tone;
}

export function deriveStatusStats(
  status: StatusReport | null,
  health: HealthReport | null,
): BandStat[] {
  const stats: BandStat[] = [];
  stats.push({
    label: "Daemon",
    value: status?.socket?.ok ? "sovrd · ready" : "sovrd · offline",
    tone: status?.socket?.ok ? "ok" : "warn",
  });
  stats.push({
    label: "AFM",
    value: status?.afm?.ok ? "loop · on (dry)" : "loop · idle",
    tone: status?.afm?.ok ? "ok" : "info",
  });
  stats.push({
    label: "Vault",
    value: status?.vault?.exists ? "ready" : "not initialized",
    tone: status?.vault?.exists ? "ok" : "warn",
  });
  stats.push({
    label: "Privacy",
    value: "local-only",
    tone: "ok",
  });
  stats.push({
    label: "Audit",
    value: status ? `${status.audit.entries} entries` : "—",
    tone: "info",
  });
  stats.push({
    label: "Bridge",
    value: health?.ok ? `:${health.port ?? "?"}` : "offline",
    tone: health?.ok ? "ok" : "danger",
  });
  return stats;
}

export function StatusBand({
  stats,
  actions,
}: {
  stats: BandStat[];
  actions?: ReactNode;
}) {
  return (
    <div className="band">
      <div className="band-stats">
        {stats.map((s) => (
          <div className="band-stat" key={s.label}>
            <span className="band-stat-label">{s.label}</span>
            <span className="band-stat-value">
              <span className="band-dot" data-tone={s.tone === "ok" ? undefined : s.tone} />
              {s.value}
            </span>
          </div>
        ))}
      </div>
      <div className="band-actions">{actions}</div>
    </div>
  );
}
