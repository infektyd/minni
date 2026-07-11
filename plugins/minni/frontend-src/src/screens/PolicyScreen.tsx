import { ArchivalBand, StateBanner } from "../components/atoms";
import { usePolicy } from "../board/boardDataHook";

export function PolicyScreen({
  tokenRefreshTrigger,
  onAuthRequired,
}: {
  tokenRefreshTrigger?: number;
  onAuthRequired?: () => void;
}) {
  const { data: policy, isLive, loading, error, refresh } = usePolicy(
    tokenRefreshTrigger,
    onAuthRequired,
  );

  const caps = policy?.caps;
  const intent = policy?.intentRouting;
  const afm = policy?.afm;

  return (
    <>
      <ArchivalBand
        eyebrow="POLICY · CAPABILITIES · AFM"
        title="Local rule set & loop posture"
        meta={[
          { k: "AGENT", v: isLive ? policy?.agentId || "—" : "—" },
          {
            k: "CAPS",
            v: isLive && caps ? `R${caps.R} L${caps.L} H${caps.H}` : "—",
          },
          {
            k: "AFM",
            v:
              isLive && afm
                ? String(
                    (afm as { ok?: boolean }).ok === true
                      ? "ok"
                      : (afm as { error?: string }).error || "—",
                  )
                : "—",
          },
          {
            k: "AUTO LEARN",
            v:
              isLive && typeof policy?.automaticLearning === "boolean"
                ? policy.automaticLearning
                  ? "on"
                  : "off"
                : "—",
          },
        ]}
      />

      {loading && !isLive && <StateBanner state="loading">Reading policy…</StateBanner>}
      {error && !isLive && (
        <StateBanner state="error">
          Policy offline: {error}{" "}
          <button type="button" className="btn btn-secondary btn-sm" onClick={() => void refresh()}>
            Retry
          </button>
        </StateBanner>
      )}

      {isLive && policy && (
        <div className="work-grid">
          <div className="panel">
            <div className="panel-body" style={{ display: "flex", flexDirection: "column", gap: 12 }}>
              <Row label="Console agent" value={policy.agentId || "—"} />
              <Row
                label="Stamped for candidates"
                value={policy.stampedForCandidates || "—"}
              />
              <Row
                label="Unknown agent"
                value={policy.unknownAgent ? "yes (default-deny risk)" : "no"}
              />
              <Row
                label="MINNI_RESOLVE_OPERATORS"
                value={policy.resolveOperatorsEnv ? "set" : "unset"}
              />
              <Row
                label="Caps (R/L/H)"
                value={
                  caps
                    ? `RECALL=${caps.R} LEARN=${caps.L} HANDOFF=${caps.H}`
                    : "—"
                }
              />
              <Row
                label="Principals known"
                value={(policy.principalsKnown || []).join(", ") || "—"}
              />
              <Row label="Source" value={policy.source || "—"} />
              <Row label="Policy module" value={policy.policyModule || "—"} />
              {intent ? (
                <>
                  <div className="section-h" style={{ marginTop: 8 }}>
                    <span className="section-h-title">Intent routing sample</span>
                  </div>
                  <Row label="Sample task" value={intent.sampleTask || "—"} />
                  <Row label="Action" value={intent.action || "—"} />
                  <Row
                    label="Confidence"
                    value={
                      typeof intent.confidence === "number"
                        ? intent.confidence.toFixed(2)
                        : "—"
                    }
                  />
                  <Row
                    label="Automatic allowed"
                    value={intent.automaticAllowed ? "yes" : "no"}
                  />
                  <Row label="Reason" value={intent.reason || "—"} />
                </>
              ) : null}
            </div>
          </div>
        </div>
      )}
    </>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div
      style={{
        display: "grid",
        gridTemplateColumns: "160px 1fr",
        gap: 8,
        fontSize: 13,
      }}
    >
      <span className="muted">{label}</span>
      <span style={{ wordBreak: "break-word" }}>{value}</span>
    </div>
  );
}
