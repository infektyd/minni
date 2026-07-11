import { ArchivalBand, StateBanner } from "../components/atoms";
import { useHandoffs } from "../board/boardDataHook";

export function HandoffsScreen({
  tokenRefreshTrigger,
  onAuthRequired,
}: {
  tokenRefreshTrigger?: number;
  onAuthRequired?: () => void;
}) {
  const { data: handoffs, isLive, loading, error, refresh } = useHandoffs(
    tokenRefreshTrigger,
    onAuthRequired,
  );

  return (
    <>
      <ArchivalBand
        eyebrow="HANDOFFS · INBOX / OUTBOX"
        title="Cross-agent packet ledger"
        meta={[
          { k: "PENDING", v: isLive ? String(handoffs.length) : "—" },
          { k: "SOURCE", v: "/api/handoffs" },
          { k: "RPC", v: "minni_list_pending_handoffs" },
          { k: "SCOPE", v: "this principal" },
        ]}
      />

      {loading && !isLive && <StateBanner state="loading">Loading handoffs…</StateBanner>}
      {error && !isLive && (
        <StateBanner state="error">
          Handoffs offline: {error}{" "}
          <button type="button" className="btn btn-secondary btn-sm" onClick={() => void refresh()}>
            Retry
          </button>
        </StateBanner>
      )}
      {isLive && handoffs.length === 0 && (
        <StateBanner state="empty">no pending handoffs for this principal</StateBanner>
      )}

      {isLive && handoffs.length > 0 && (
        <div className="work-grid">
          <div className="panel">
            <div className="panel-body" style={{ display: "flex", flexDirection: "column", gap: 10 }}>
              {handoffs.map((h, i) => (
                <div
                  key={h.lease_id || h.path || String(i)}
                  className="dcard"
                  style={{ padding: 12 }}
                >
                  <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 6 }}>
                    <span className="bd-chip warn">PENDING</span>
                    <span className="mono" style={{ fontSize: 12 }}>
                      {h.lease_id || "—"}
                    </span>
                  </div>
                  <div style={{ fontWeight: 600, marginBottom: 4 }}>{h.task || "(no task)"}</div>
                  <div className="muted" style={{ fontSize: 12 }}>
                    {h.from_agent || "?"} → {h.to_agent || "?"}
                    {h.expires_at ? ` · expires ${h.expires_at}` : ""}
                  </div>
                  {h.path ? (
                    <div className="mono muted" style={{ fontSize: 11, marginTop: 4 }}>
                      {h.path}
                    </div>
                  ) : null}
                </div>
              ))}
            </div>
          </div>
        </div>
      )}
    </>
  );
}
