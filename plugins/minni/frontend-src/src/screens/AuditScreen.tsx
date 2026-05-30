import { useEffect, useState } from "react";
import { getAuditTail, type AuditTailResult } from "../api";
import {
  ArchivalBand,
  PanelHeader,
  StateBanner,
} from "../components/atoms";

interface ParsedEntry {
  raw: string;
  time: string;
  actor: string;
  op: string;
  target: string;
  result: string;
}

// Vault audit entries arrive as markdown blocks like:
//   "## [2026-04-29T00:47:40.790Z] sovereign_status | socket=ok afm=ok\n\n```json\n{...}\n```"
// followed (sometimes) by another fenced JSON payload. Parse the header and
// fall back to JSON-on-a-line if the shape is different.
const HEADER_RE =
  /^##\s+\[(?<ts>[^\]]+)\]\s+(?<tool>[^|]+?)\s*(?:\|\s*(?<summary>.*))?$/;

function parseEntry(raw: string): ParsedEntry {
  const firstLine = raw.split(/\r?\n/, 1)[0] || raw;
  const m = HEADER_RE.exec(firstLine);
  if (m && m.groups) {
    const ts = m.groups.ts || "";
    const tool = (m.groups.tool || "").trim();
    const summary = (m.groups.summary || "").trim();
    return {
      raw,
      time: ts.includes("T") ? ts.slice(11, 19) : ts,
      actor: tool.startsWith("sovereign_") ? "sovrd" : tool || "—",
      op: tool || "—",
      target: summary || "—",
      result: "",
    };
  }
  try {
    const j = JSON.parse(raw) as Record<string, unknown>;
    const ts = (j.timestamp as string) || (j.time as string) || (j.ts as string) || "";
    return {
      raw,
      time: typeof ts === "string" ? ts.slice(11, 19) || ts : "",
      actor: String(j.actor || j.tool || j.agent || "—"),
      op: String(j.op || j.tool || "—"),
      target: String(j.target || j.summary || j.path || "—"),
      result: String(j.result || j.status || j.outcome || ""),
    };
  } catch {
    return {
      raw,
      time: "",
      actor: "—",
      op: "—",
      target: raw.length > 80 ? raw.slice(0, 80) + "…" : raw,
      result: "",
    };
  }
}

export function AuditScreen() {
  const [data, setData] = useState<AuditTailResult | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [limit, setLimit] = useState(20);

  const load = async (n: number) => {
    setLoading(true);
    setError(null);
    try {
      const result = await getAuditTail(n);
      setData(result);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void load(limit);
  }, [limit]);

  const entries: ParsedEntry[] = (data?.entries || []).map(parseEntry);

  return (
    <>
      <ArchivalBand
        eyebrow="AUDIT TRAIL · LIVE · LOCAL ONLY"
        title="Operations log"
        meta={[
          { k: "EVENTS", v: String(entries.length) },
          { k: "LIMIT", v: String(limit) },
          { k: "EXPORT", v: data ? `${(data.text.length / 1024).toFixed(1)} kB` : "—" },
          { k: "SCOPE", v: "this host" },
        ]}
      />

      <div className="panel">
        <PanelHeader
          title="Events"
          sub="newest first"
          actions={
            <>
              <select
                className="input"
                style={{ width: 100, height: 28, fontSize: 12 }}
                value={limit}
                onChange={(e) => setLimit(Number(e.target.value))}
                aria-label="Tail size"
              >
                <option value={10}>10</option>
                <option value={20}>20</option>
                <option value={50}>50</option>
                <option value={100}>100</option>
              </select>
              <button
                type="button"
                className="btn btn-secondary btn-sm"
                onClick={() => void load(limit)}
                disabled={loading}
              >
                {loading ? "…" : "Refresh"}
              </button>
            </>
          }
        />
        <div className="panel-body--flush">
          {loading && <StateBanner state="loading">Reading audit tail…</StateBanner>}
          {!loading && error && (
            <StateBanner state="error">audit-tail failed: {error}</StateBanner>
          )}
          {!loading && !error && entries.length === 0 && (
            <StateBanner state="empty">No audit entries yet.</StateBanner>
          )}
          {!loading && !error && entries.length > 0 && (
            <div className="audit-table">
              <div className="row head">
                <div>Time</div>
                <div>Actor</div>
                <div>Operation</div>
                <div>Target</div>
                <div style={{ justifyContent: "flex-end", display: "flex" }}>Result</div>
              </div>
              {entries.map((a, i) => (
                <div className="row" key={i} title={a.raw}>
                  <div className="audit-time">{a.time || "—"}</div>
                  <div className="audit-actor">{a.actor}</div>
                  <div className="mono" style={{ fontSize: 12 }}>
                    {a.op}
                  </div>
                  <div style={{ minWidth: 0 }}>
                    <span
                      className="mono"
                      style={{
                        fontSize: 11.5,
                        whiteSpace: "nowrap",
                        overflow: "hidden",
                        textOverflow: "ellipsis",
                        display: "block",
                      }}
                    >
                      {a.target}
                    </span>
                  </div>
                  <div style={{ justifyContent: "flex-end" }}>
                    <span className="mono muted" style={{ fontSize: 11.5 }}>
                      {a.result}
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
