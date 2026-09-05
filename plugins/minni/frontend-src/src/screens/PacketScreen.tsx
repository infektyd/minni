import { evidenceFromSource, type EvidenceRow, type PreparedTaskPacket } from "../api";
import { AfmChip, AuthorityChip, PrivacyChip } from "../components/Chip";
import {
  ArchivalBand,
  CodeBlock,
  PanelHeader,
  RiskCallout,
  ScoreBar,
  StateBanner,
} from "../components/atoms";

interface Props {
  packet: PreparedTaskPacket | null;
  evidence: EvidenceRow[];
  selected: Set<string>;
}

export function PacketScreen({ packet }: Props) {
  if (!packet) {
    return (
      <>
        <ArchivalBand
          eyebrow="PREPARE PACKET"
          title="No packet built yet"
          meta={[
            { k: "TASK", v: "—" },
            { k: "MODE", v: "—" },
            { k: "BUDGET", v: "—" },
            { k: "AFM", v: "—" },
          ]}
        />
        <StateBanner state="empty">
          Run a query on the <b>Recall</b> screen first. The packet preview comes from the
          same prepare-task response.
        </StateBanner>
      </>
    );
  }

  const allowance = packet.budget?.tokens ?? packet.budgetTokens ?? 0;
  const inclSources = packet.relevantSources.map((source) => evidenceFromSource(source, packet.outcomeDraft));

  const envelopeJson = packet.contextMarkdown
    ? packet.contextMarkdown
    : JSON.stringify(
        {
          task: packet.task,
          profile: packet.profile,
          mode: packet.mode,
          budgetTokens: packet.budgetTokens,
          intent: packet.intent,
          relevantSources: packet.relevantSources.map((s) => ({
            title: s.title,
            relativePath: s.relativePath,
            score: s.score,
            authority: s.authority,
            privacyLevel: s.privacyLevel,
          })),
        },
        null,
        2,
      );

  return (
    <>
      <ArchivalBand
        eyebrow={`PREPARE PACKET · ${packet.mode.toUpperCase()} ENVELOPE`}
        title={`Packet — ${packet.task}`}
        meta={[
          { k: "PROFILE", v: packet.profile },
          { k: "MODE", v: packet.mode },
          { k: "DAEMON", v: packet.recall.daemonOk ? "ok" : packet.recall.error || "—" },
          { k: "AFM", v: packet.afm.used ? "used" : "off" },
        ]}
      />

      <div className="packet-meta">
        <div className="packet-meta-cell">
          <div className="packet-meta-label">Token allowance</div>
          <div className="packet-meta-value">
            {allowance.toLocaleString()} tokens
          </div>
        </div>
        <div className="packet-meta-cell">
          <div className="packet-meta-label">Sources included</div>
          <div className="packet-meta-value">
            {inclSources.length}
          </div>
        </div>
        <div className="packet-meta-cell">
          <div className="packet-meta-label">Risks</div>
          <div className="packet-meta-value">
            {packet.risks.length} flagged
          </div>
        </div>
        <div className="packet-meta-cell">
          <div className="packet-meta-label">Inspection only</div>
          <div className="packet-meta-value" style={{ color: "var(--verdigris-dark)" }}>
            NOTHING STORED
          </div>
        </div>
      </div>

      <div className="work-grid">
        <div className="panel">
          <PanelHeader
            title="<minni:context> envelope"
            sub={packet.contextMarkdown ? "prepared markdown · read-only" : "prepared json preview"}
            actions={
              <>
                <button
                  className="btn btn-ghost btn-sm"
                  type="button"
                  onClick={() => {
                    void navigator.clipboard
                      .writeText(envelopeJson)
                      .catch(() => undefined);
                  }}
                >
                  Copy
                </button>
              </>
            }
          />
          <div className="panel-body">
            <p className="muted">This is the prepared response. Recall selections are inspection marks and do not change this preview or the copied context.</p>
            <CodeBlock>{envelopeJson}</CodeBlock>
            {packet.risks.length > 0 && (
              <div style={{ marginTop: 12 }}>
                <RiskCallout title={`REVIEW · ${packet.risks.length} risk${packet.risks.length === 1 ? "" : "s"}`}>
                  <ul style={{ margin: 0, paddingLeft: 18 }}>
                    {packet.risks.map((r, i) => (
                      <li key={i}>{r}</li>
                    ))}
                  </ul>
                </RiskCallout>
              </div>
            )}
          </div>
        </div>

        <div className="panel">
          <PanelHeader title="Prepared sources" sub={`${inclSources.length} ranked`} />
          <div>
            {inclSources.map((e, i) => (
              <div
                key={e.id}
                style={{
                  padding: "10px 14px",
                  borderBottom: "1px solid var(--border)",
                  display: "flex",
                  flexDirection: "column",
                  gap: 6,
                }}
              >
                <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  <span className="mono muted" style={{ fontSize: 10 }}>
                    {String(i + 1).padStart(2, "0")}
                  </span>
                  <span className="mono" style={{ fontSize: 11.5 }}>
                    {e.id}
                  </span>
                  <span className="spacer" />
                  <ScoreBar value={e.score} />
                </div>
                <div style={{ fontSize: 13.5, fontWeight: 600 }}>{e.title}</div>
                <div className="mono muted" style={{ fontSize: 10.5, wordBreak: "break-all" }}>
                  {e.path}
                </div>
                <div style={{ display: "flex", gap: 4, flexWrap: "wrap" }}>
                  <PrivacyChip value={e.privacy} />
                  <AuthorityChip value={e.authority} />
                  <AfmChip value={e.afm} />
                </div>
              </div>
            ))}
            {inclSources.length === 0 && (
              <div style={{ padding: 20 }} className="muted mono">
                No ranked sources in this prepared response.
              </div>
            )}
          </div>
        </div>
      </div>
    </>
  );
}
