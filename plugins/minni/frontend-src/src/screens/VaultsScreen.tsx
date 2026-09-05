import { ArchivalBand, StateBanner } from "../components/atoms";
import { useAgents } from "../board/boardDataHook";
import { AgentSummary } from "../components/AgentSummary";

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
        eyebrow="STORED CONTEXT"
        title="Memory by agent"
        meta={[
          { k: "FOLDERS", v: isLive ? String(agents.length) : "—" },
          {
            k: "AWAITING REVIEW",
            v: (() => {
              if (!isLive) return "—";
              // Fail-loud: any unknown staged count → header "—" (not sum-as-0).
              if (agents.some((a) => a.staged == null || a.stagedUnknown)) return "—";
              const sum = agents.reduce((n, a) => n + (a.staged as number), 0);
              // Any at-limit count makes the sum a floor, not an exact total.
              return agents.some((a) => a.stagedAtLimit) ? `${sum}+` : String(sum);
            })(),
          },
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
                  <AgentSummary agent={a} />
                </div>
              ))}
            </div>
          </div>
        </div>
      )}
    </>
  );
}
