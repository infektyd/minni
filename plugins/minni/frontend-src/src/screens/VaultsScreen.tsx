import { ArchivalBand, StateBanner } from "../components/atoms";
import { useAgents } from "../board/boardDataHook";
import { agentColor } from "../board/boardData";

export function VaultsScreen({
  tokenRefreshTrigger,
  onAuthRequired,
}: {
  tokenRefreshTrigger?: number;
  onAuthRequired?: () => void;
}) {
  const { data: agents, isLive, loading, error, refresh } = useAgents(
    tokenRefreshTrigger,
    onAuthRequired,
  );

  return (
    <>
      <ArchivalBand
        eyebrow="VAULTS · OBSIDIAN-COMPATIBLE · LOCAL ONLY"
        title="Per-agent memory surfaces"
        meta={[
          { k: "VAULTS", v: isLive ? String(agents.length) : "—" },
          {
            k: "STAGED",
            v: (() => {
              if (!isLive) return "—";
              // Fail-loud: any unknown staged count → header "—" (not sum-as-0).
              if (agents.some((a) => a.staged == null || a.stagedUnknown)) return "—";
              const sum = agents.reduce((n, a) => n + (a.staged as number), 0);
              // Any at-limit count makes the sum a floor, not an exact total.
              return agents.some((a) => a.stagedAtLimit) ? `${sum}+` : String(sum);
            })(),
          },
          { k: "SOURCE", v: "/api/agents" },
          { k: "REMOTE SYNC", v: "off" },
        ]}
      />

      {loading && !isLive && <StateBanner state="loading">Scanning vaults…</StateBanner>}
      {error && !isLive && (
        <StateBanner state="error">
          Vaults offline: {error}{" "}
          <button type="button" className="btn btn-secondary btn-sm" onClick={() => void refresh()}>
            Retry
          </button>
        </StateBanner>
      )}
      {isLive && agents.length === 0 && (
        <StateBanner state="empty">no *-vault directories under MINNI_HOME</StateBanner>
      )}

      {isLive && agents.length > 0 && (
        <div className="work-grid">
          <div className="panel">
            <div className="panel-body" style={{ display: "flex", flexDirection: "column", gap: 10 }}>
              {agents.map((a) => (
                <div key={a.id} className="dcard" style={{ padding: 12 }}>
                  <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 6 }}>
                    <span
                      className="dot"
                      style={{
                        width: 10,
                        height: 10,
                        borderRadius: 10,
                        background: a.on ? agentColor(a.id) : "var(--disabled)",
                        display: "inline-block",
                      }}
                    />
                    <span style={{ fontWeight: 600 }}>{a.id}</span>
                    <span className="muted" style={{ fontSize: 12 }}>
                      seen {a.seen}
                    </span>
                    <span className="bd-chip info" style={{ marginLeft: "auto" }}>
                      {a.staged == null ? "— staged" : `${a.staged}${a.stagedAtLimit ? "+" : ""} staged`}
                    </span>
                  </div>
                  <div className="mono" style={{ fontSize: 12, marginBottom: 8 }}>
                    {a.vault}
                  </div>
                  <div style={{ display: "flex", gap: 6 }}>
                    <span className={"bd-chip " + (a.caps.R ? "safe" : "danger")}>R</span>
                    <span className={"bd-chip " + (a.caps.L ? "safe" : "danger")}>L</span>
                    <span className={"bd-chip " + (a.caps.H ? "safe" : "danger")}>H</span>
                  </div>
                </div>
              ))}
            </div>
          </div>
        </div>
      )}
    </>
  );
}
