import type { EvidenceRow } from "../api";
import { AfmChip, AuthorityChip, PrivacyChip } from "./Chip";
import { RiskCallout } from "./atoms";

interface Props {
  source: EvidenceRow | undefined;
  mode: "right" | "bottom" | "overlay";
  onClose?: () => void;
}

export function Inspector({ source, mode, onClose }: Props) {
  if (!source) {
    return (
      <aside className="inspector">
        <div className="insp-header">
          <div className="insp-eyebrow">Inspector</div>
          <div className="insp-title">No source selected</div>
          <div className="muted" style={{ fontSize: 13 }}>
            Click an evidence row to inspect its metadata, excerpt, and recommended handling.
          </div>
        </div>
      </aside>
    );
  }
  return (
    <aside className={mode === "overlay" ? "inspector-overlay" : "inspector"}>
      <div className="insp-header">
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <span className="insp-eyebrow">SOURCE · {source.cls.toUpperCase()}</span>
          <span className="spacer" />
          {mode === "overlay" && (
            <button className="btn-icon" onClick={onClose} aria-label="Close inspector">
              ×
            </button>
          )}
        </div>
        <div className="insp-title">{source.title}</div>
        <div className="insp-path">{source.path}</div>
        <div style={{ display: "flex", gap: 4, flexWrap: "wrap", marginTop: 6 }}>
          <PrivacyChip value={source.privacy} />
          <AuthorityChip value={source.authority} />
          <AfmChip value={source.afm} />
        </div>
      </div>

      <div className="insp-section">
        <h3 className="insp-section-title">Metadata</h3>
        <dl className="kv-grid">
          <dt>source_id</dt>
          <dd>{source.id}</dd>
          <dt>collection</dt>
          <dd>{source.collection}</dd>
          <dt>locality</dt>
          <dd>{source.locality}</dd>
          <dt>score</dt>
          <dd>{source.score.toFixed(3)}</dd>
          {source.size && (
            <>
              <dt>size</dt>
              <dd>{source.size}</dd>
            </>
          )}
          {source.ingested && (
            <>
              <dt>ingested</dt>
              <dd>{source.ingested}</dd>
            </>
          )}
          {source.modified && (
            <>
              <dt>modified</dt>
              <dd>{source.modified}</dd>
            </>
          )}
          {source.hash && (
            <>
              <dt>hash</dt>
              <dd>{source.hash}</dd>
            </>
          )}
          {source.tags.length > 0 && (
            <>
              <dt>tags</dt>
              <dd>{source.tags.join(", ")}</dd>
            </>
          )}
        </dl>
      </div>

      <div className="insp-section">
        <h3 className="insp-section-title">Excerpt</h3>
        <div
          style={{
            fontFamily: "var(--font-archive)",
            fontSize: 14.5,
            lineHeight: 1.55,
            fontStyle: "italic",
            color: "var(--graphite-2)",
            borderLeft: "2px solid var(--border-strong)",
            paddingLeft: 12,
          }}
        >
          {source.excerpt || <span className="muted">No excerpt available.</span>}
        </div>
      </div>

      {source.reason && (
        <div className="insp-section">
          <h3 className="insp-section-title">Inclusion reason</h3>
          <div style={{ fontSize: 13 }}>{source.reason}</div>
        </div>
      )}

      {source.afm !== "safe" && (
        <div className="insp-section">
          <h3 className="insp-section-title">Recommended handling</h3>
          <RiskCallout
            title={
              source.afm === "log"
                ? "LOG-ONLY"
                : source.afm === "dns"
                  ? "DO-NOT-STORE"
                  : "REVIEW"
            }
          >
            {source.afm === "log" &&
              "Include in recall packet, but do not promote to vault until a corroborating signal arrives."}
            {source.afm === "dns" &&
              "Exclude from any compile pass. Confirm redaction at envelope-build time."}
            {source.afm === "learn" &&
              "Marked as a learn candidate by the dry-run pass. Approve from the Dry-run Review screen."}
          </RiskCallout>
        </div>
      )}

      <div className="insp-section" style={{ borderBottom: 0 }}>
        <h3 className="insp-section-title">Actions</h3>
        <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
          <button className="btn btn-primary btn-sm" type="button" disabled>
            Include in packet
          </button>
          <button className="btn btn-secondary btn-sm" type="button" disabled>
            Open in vault
          </button>
        </div>
        <div
          className="muted"
          style={{
            fontSize: 11.5,
            marginTop: 10,
            fontFamily: "var(--font-mono)",
          }}
        >
          Inspecting a source does not store, learn, export, or publish.
        </div>
      </div>
    </aside>
  );
}
