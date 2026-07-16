import { useEffect, useState } from "react";
import { AuthRequiredError, getSessions, type SessionRow } from "../api";
import { ArchivalBand, PanelHeader, StateBanner } from "../components/atoms";

function formatTime(ts: string | null): string {
  if (!ts) return "—";
  // ISO timestamps: keep the date+time, drop sub-second noise.
  return ts.includes("T") ? ts.slice(0, 19).replace("T", " ") : ts;
}

export function SessionsScreen({
  tokenRefreshTrigger,
  onAuthRequired,
}: {
  tokenRefreshTrigger?: number;
  onAuthRequired?: () => void;
}) {
  const [sessions, setSessions] = useState<SessionRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [agentFilter, setAgentFilter] = useState("");

  const load = async () => {
    setLoading(true);
    setError(null);
    try {
      const result = await getSessions(20, agentFilter.trim() || undefined);
      setSessions(result.sessions);
    } catch (err) {
      if (err instanceof AuthRequiredError) {
        onAuthRequired?.();
      }
      setError(err instanceof Error ? err.message : String(err));
      // Drop the previous scope's rows: the error banner replaces the table, so
      // another agent's receipts must not linger beside it after a filter/auth
      // change failed.
      setSessions([]);
    } finally {
      setLoading(false);
    }
  };

  // Reload whenever the agent filter changes (debounced ~300ms) so the table
  // and the header SCOPE meta always describe the same query — matching the
  // Audit screen, which refetches on filter change. Also covers the initial
  // mount load. tokenRefreshTrigger is a dependency so a re-auth via the token
  // gate refetches (clearing stale error/data) without a manual refresh —
  // matching how HandoffsScreen consumes it.
  useEffect(() => {
    const t = window.setTimeout(() => {
      void load();
    }, 300);
    return () => window.clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agentFilter, tokenRefreshTrigger]);

  return (
    <>
      <ArchivalBand
        eyebrow="SESSIONS · ROLLING LOG · ALL VAULTS"
        title="Per-session receipts"
        meta={[
          { k: "SESSIONS", v: String(sessions.length) },
          { k: "SOURCE", v: "/api/sessions" },
          { k: "SCOPE", v: agentFilter.trim() || "all agents" },
        ]}
      />

      <div className="panel">
        <PanelHeader
          title="Sessions"
          sub="newest first"
          actions={
            <>
              <input
                className="input"
                style={{ width: 140, height: 28, fontSize: 12 }}
                value={agentFilter}
                onChange={(e) => setAgentFilter(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") void load();
                }}
                placeholder="agent filter"
                aria-label="Filter by agent"
              />
              <button
                type="button"
                className="btn btn-secondary btn-sm"
                onClick={() => void load()}
                disabled={loading}
              >
                {loading ? "…" : "Refresh"}
              </button>
            </>
          }
        />
        <div className="panel-body--flush">
          {loading && sessions.length === 0 && (
            <StateBanner state="loading">Reading session receipts…</StateBanner>
          )}
          {!loading && error && (
            <StateBanner state="error">
              sessions failed: {error}{" "}
              <button type="button" className="btn btn-secondary btn-sm" onClick={() => void load()}>
                Retry
              </button>
            </StateBanner>
          )}
          {!loading && !error && sessions.length === 0 && (
            <StateBanner state="empty">No sessions in the rolling log yet.</StateBanner>
          )}
          {!loading && !error && sessions.length > 0 && (
            <div className="sessions-table">
              <div className="row head">
                <div>Agent</div>
                <div>Session</div>
                <div>Started</div>
                <div>Status</div>
                <div>Recalls</div>
                <div>Guards</div>
                <div>Learns</div>
                <div style={{ justifyContent: "flex-end", display: "flex" }}>Staged</div>
              </div>
              {sessions.map((s) => (
                <div className="row" key={`${s.agent}:${s.session_id}`} title={s.receipt_line}>
                  <div className="mono">{s.agent}</div>
                  <div className="mono" style={{ fontSize: 11.5 }}>
                    {s.session_id}
                  </div>
                  <div className="audit-time">{formatTime(s.boot_at)}</div>
                  <div>
                    {s.open ? (
                      <span className="bd-chip warn">OPEN</span>
                    ) : (
                      <span className="mono muted" style={{ fontSize: 11.5 }}>
                        {formatTime(s.stop_at)}
                      </span>
                    )}
                  </div>
                  <div className="mono" style={{ fontSize: 12 }}>
                    {s.receipt.recalls_strong}/{s.receipt.recalls_weak}
                  </div>
                  <div className="mono" style={{ fontSize: 12 }}>
                    {s.receipt.guard_denied}
                  </div>
                  <div className="mono" style={{ fontSize: 12 }}>
                    {s.receipt.learns}
                  </div>
                  <div style={{ justifyContent: "flex-end", display: "flex" }}>
                    <span className="mono muted" style={{ fontSize: 11.5 }}>
                      {s.receipt.candidates_drafted}
                    </span>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </>
  );
}
