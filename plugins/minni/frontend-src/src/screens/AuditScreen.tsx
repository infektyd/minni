import { useEffect, useRef, useState } from "react";
import {
  getAuditTail,
  getAuditTailFleet,
  getEvents,
  type EventRow,
} from "../api";
import {
  ArchivalBand,
  PanelHeader,
  StateBanner,
} from "../components/atoms";
import { Chip } from "../components/Chip";

interface ParsedEntry {
  raw: string;
  tsSort: number;
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
    const parsedTs = Date.parse(ts);
    return {
      raw,
      tsSort: Number.isNaN(parsedTs) ? 0 : parsedTs,
      time: ts.includes("T") ? ts.slice(11, 19) : ts,
      actor: tool.startsWith("minni_") || tool.startsWith("sovereign_") ? "minnid" : tool || "—",
      op: tool || "—",
      target: summary || "—",
      result: "",
    };
  }
  try {
    const j = JSON.parse(raw) as Record<string, unknown>;
    const ts = (j.timestamp as string) || (j.time as string) || (j.ts as string) || "";
    const parsedTs = Date.parse(ts);
    return {
      raw,
      tsSort: Number.isNaN(parsedTs) ? 0 : parsedTs,
      time: typeof ts === "string" ? ts.slice(11, 19) || ts : "",
      actor: String(j.actor || j.tool || j.agent || "—"),
      op: String(j.op || j.tool || "—"),
      target: String(j.target || j.summary || j.path || "—"),
      result: String(j.result || j.status || j.outcome || ""),
    };
  } catch {
    return {
      raw,
      tsSort: 0,
      time: "",
      actor: "—",
      op: "—",
      target: raw.length > 80 ? raw.slice(0, 80) + "…" : raw,
      result: "",
    };
  }
}

interface MergedRow {
  key: string;
  lane: "vault" | "daemon";
  /** In the fleet view, the owning agent for a vault row (badge shows `vault·<agent>`). */
  laneAgent?: string;
  tsSort: number;
  time: string;
  actor: string;
  op: string;
  target: string;
  result: string;
  raw: string;
}

/** Vault tail normalized so each entry can carry its owning agent (fleet view). */
interface VaultEntry {
  raw: string;
  agent?: string;
}

interface VaultTailState {
  entries: VaultEntry[];
  text: string;
}

function displayTime(ts: string | number): string {
  const s = String(ts);
  const parsed = Date.parse(s);
  if (!Number.isNaN(parsed)) {
    return new Date(parsed).toISOString().slice(11, 19);
  }
  return s.includes("T") ? s.slice(11, 19) : s;
}

function eventToRow(e: EventRow): MergedRow {
  const parsedTs = Date.parse(String(e.created_at));
  return {
    key: `d-${e.event_id}`,
    lane: "daemon",
    tsSort: Number.isNaN(parsedTs) ? e.event_id : parsedTs,
    time: displayTime(e.created_at),
    actor: e.agent_id || "—",
    op: e.event_type || "—",
    target: e.content || "—",
    result: e.thread_id ? `thread ${e.thread_id}` : "",
    raw: e.content,
  };
}

function vaultToRow(entry: ParsedEntry, idx: number, agent?: string): MergedRow {
  return {
    key: `v-${idx}-${entry.tsSort}`,
    lane: "vault",
    laneAgent: agent,
    tsSort: entry.tsSort,
    time: entry.time,
    actor: entry.actor,
    op: entry.op,
    target: entry.target,
    result: entry.result,
    raw: entry.raw,
  };
}

const POLL_MS = 5000;
const MAX_DAEMON_EVENTS = 200;
const isDaemonOffline = (err: unknown): boolean =>
  err instanceof Error && /^502\b/.test(err.message);

export function AuditScreen() {
  const [data, setData] = useState<VaultTailState | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [stale, setStale] = useState(false);
  const [limit, setLimit] = useState(20);
  const [agentFilter, setAgentFilter] = useState("");
  const [live, setLive] = useState(true);

  const [daemonEvents, setDaemonEvents] = useState<EventRow[]>([]);
  const [daemonOffline, setDaemonOffline] = useState(false);
  const [daemonError, setDaemonError] = useState<string | null>(null);

  const sinceIdRef = useRef(0);
  const liveRef = useRef(live);
  liveRef.current = live;
  const visibleRef = useRef(true);
  // Request generation: bumped on every scope reset (filter/limit change). Each
  // poll captures the generation it started under; a response whose generation
  // no longer matches belongs to a stale scope and is dropped — it must not
  // update state or advance the since_id cursor (which would append the wrong
  // agent's rows and skip the new scope's events).
  const genRef = useRef(0);

  const loadVault = async (n: number, agent: string, gen: number) => {
    setLoading(true);
    try {
      if (agent) {
        // Specific agent typed → single-vault tail (byte-identical to before).
        const result = await getAuditTail(n, agent);
        if (genRef.current !== gen) return;
        setData({ entries: result.entries.map((raw) => ({ raw })), text: result.text });
      } else {
        // No filter → fleet view: merge every vault, keep each entry's agent tag.
        const result = await getAuditTailFleet(n);
        if (genRef.current !== gen) return;
        const entries: VaultEntry[] = result.entries.map((e) => ({ raw: e.text, agent: e.agent }));
        setData({ entries, text: entries.map((e) => e.raw).join("\n\n") });
      }
      setError(null);
      setStale(false);
    } catch (err) {
      if (genRef.current !== gen) return;
      // Keep the last-known-good vault tail: a transient poll failure must
      // not blank the table between ticks.
      setError(err instanceof Error ? err.message : String(err));
      setStale(true);
    } finally {
      if (genRef.current === gen) setLoading(false);
    }
  };

  const loadDaemon = async (agent: string, gen: number) => {
    try {
      const result = await getEvents(sinceIdRef.current, agent || undefined);
      // Drop a response from a superseded scope: no state, no cursor advance.
      if (genRef.current !== gen) return;
      setDaemonOffline(false);
      setDaemonError(null);
      if (result.events.length > 0) {
        setDaemonEvents((prev) => {
          const merged = [...prev, ...result.events];
          const seen = new Set<number>();
          const deduped: EventRow[] = [];
          // Walk newest-first so dedupe keeps the latest copy of a repeated id.
          for (let i = merged.length - 1; i >= 0; i--) {
            const e = merged[i];
            if (seen.has(e.event_id)) continue;
            seen.add(e.event_id);
            deduped.push(e);
          }
          deduped.reverse();
          return deduped.slice(-MAX_DAEMON_EVENTS);
        });
      }
      if (typeof result.last_id === "number") {
        sinceIdRef.current = Math.max(sinceIdRef.current, result.last_id);
      }
    } catch (err) {
      if (genRef.current !== gen) return;
      if (isDaemonOffline(err)) {
        setDaemonOffline(true);
        setDaemonError("daemon offline");
      } else {
        setDaemonError(err instanceof Error ? err.message : String(err));
      }
    }
  };

  const loadAll = async (n: number, agent: string, gen: number) => {
    await Promise.allSettled([loadVault(n, agent, gen), loadDaemon(agent, gen)]);
  };

  // Reset the event cursor whenever the filter/limit combination changes so a
  // fresh scope doesn't silently inherit a stale since_id. Bump the generation
  // so any in-flight poll from the previous scope is discarded on arrival.
  useEffect(() => {
    genRef.current += 1;
    const gen = genRef.current;
    sinceIdRef.current = 0;
    setDaemonEvents([]);
    void loadAll(limit, agentFilter.trim(), gen);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [limit, agentFilter]);

  useEffect(() => {
    const onVisibility = () => {
      visibleRef.current = document.visibilityState === "visible";
    };
    document.addEventListener("visibilitychange", onVisibility);
    return () => document.removeEventListener("visibilitychange", onVisibility);
  }, []);

  useEffect(() => {
    const id = window.setInterval(() => {
      if (!liveRef.current) return;
      if (!visibleRef.current) return;
      void loadAll(limit, agentFilter.trim(), genRef.current);
    }, POLL_MS);
    return () => window.clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [limit, agentFilter]);

  const vaultEntries = (data?.entries || []).map((e) => ({ parsed: parseEntry(e.raw), agent: e.agent }));
  const merged: MergedRow[] = [
    ...vaultEntries.map(({ parsed, agent }, idx) => vaultToRow(parsed, idx, agent)),
    ...daemonEvents.map(eventToRow),
  ].sort((a, b) => b.tsSort - a.tsSort);

  const bothEmpty = merged.length === 0;

  return (
    <>
      <ArchivalBand
        eyebrow="AUDIT TRAIL · LIVE · LOCAL ONLY"
        title="Operations log"
        meta={[
          { k: "EVENTS", v: String(merged.length) },
          { k: "LIMIT", v: String(limit) },
          { k: "EXPORT", v: data ? `${(data.text.length / 1024).toFixed(1)} kB` : "—" },
          { k: "SCOPE", v: agentFilter.trim() || "this host" },
        ]}
      />

      <div className="panel">
        <PanelHeader
          title="Events"
          sub={live ? "live · newest first" : "paused · newest first"}
          actions={
            <>
              <input
                className="input"
                style={{ width: 130, height: 28, fontSize: 12 }}
                value={agentFilter}
                onChange={(e) => setAgentFilter(e.target.value)}
                placeholder="agent filter"
                aria-label="Filter by agent"
              />
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
                className={`btn btn-sm ${live ? "btn-primary" : "btn-secondary"}`}
                onClick={() => setLive((v) => !v)}
                aria-pressed={live}
              >
                {live ? "Live" : "Paused"}
              </button>
              <button
                type="button"
                className="btn btn-secondary btn-sm"
                onClick={() => void loadAll(limit, agentFilter.trim(), genRef.current)}
                disabled={loading}
              >
                {loading ? "…" : "Refresh"}
              </button>
            </>
          }
        />
        <div className="panel-body--flush">
          {stale && (
            <div className="mono muted" style={{ fontSize: 11, padding: "4px 10px" }}>
              showing last-known-good vault tail — most recent refresh failed
              {error ? `: ${error}` : ""}
            </div>
          )}
          {daemonError && (
            <div className="mono muted" style={{ fontSize: 11, padding: "4px 10px" }}>
              daemon lane: {daemonError}
              {daemonOffline ? " — vault lane unaffected" : ""}
            </div>
          )}
          {loading && !data && <StateBanner state="loading">Reading audit tail…</StateBanner>}
          {!loading && !data && error && (
            <StateBanner state="error">audit-tail failed: {error}</StateBanner>
          )}
          {!loading && bothEmpty && (
            <StateBanner state="empty">No audit entries yet.</StateBanner>
          )}
          {!bothEmpty && (
            <div className="audit-table">
              <div className="row head">
                <div>Lane</div>
                <div>Time</div>
                <div>Actor</div>
                <div>Operation</div>
                <div>Target</div>
                <div style={{ justifyContent: "flex-end", display: "flex" }}>Result</div>
              </div>
              {merged.map((a) => (
                <div className="row" key={a.key} title={a.raw}>
                  <div>
                    <Chip kind={a.lane === "vault" ? "info" : "system"}>
                      {a.lane === "vault"
                        ? a.laneAgent
                          ? `vault·${a.laneAgent}`
                          : "vault"
                        : "daemon"}
                    </Chip>
                  </div>
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
