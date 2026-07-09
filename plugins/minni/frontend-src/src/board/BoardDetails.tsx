// Minni Memory Board — zone detail views (what each zone morphs into).
import { Fragment, useMemo, useState } from "react";
import {
  agentColor,
  SAMPLE_AGENTS,
  SAMPLE_DENY,
  SAMPLE_LEARNINGS,
  SAMPLE_LOGS,
  SAMPLE_RECALL,
  type DaemonInfo,
  type ZoneId,
} from "./boardData";
import { type StagedLearningsState } from "./boardDataHook";
import { applyVerdict, pendingCount, sortLearnings, type Verdict } from "./boardLogic";

export interface BoardDetailContext {
  daemon: DaemonInfo;
  recentAudit: string[];
}

function Meter({ v }: { v: number }) {
  return (
    <span className="bd-meter">
      <i style={{ width: Math.round(v * 100) + "%" }} />
    </span>
  );
}

// ── STAGED — filterable, sortable wall of learnings ─────────────────────────
function StagedDetail({ stagedState }: { stagedState?: StagedLearningsState }) {
  const [filter, setFilter] = useState<string>("all");
  const [sort, setSort] = useState<"score" | "age">("score");
  const [sel, setSel] = useState<string | null>(null);
  const [verdicts, setVerdicts] = useState<Record<string, Verdict>>({});
  const [resError, setResError] = useState<string | null>(null);
  const [resolvingId, setResolvingId] = useState<string | null>(null);

  const learnings = stagedState?.learnings || SAMPLE_LEARNINGS;
  const isLive = stagedState?.isLive ?? false;
  const resolve = stagedState?.resolve;

  // DEFECT 6: Clear verdicts for resolved rows when learnings shrink after refetch
  useMemo(() => {
    if (isLive) {
      const liveIds = new Set(learnings.map(l => l.id));
      setVerdicts(prev => {
        const cleaned = { ...prev };
        let changed = false;
        for (const id of Object.keys(cleaned)) {
          if (!liveIds.has(id)) {
            delete cleaned[id];
            changed = true;
          }
        }
        return changed ? cleaned : prev;
      });
    }
  }, [learnings, isLive]);

  const agents = useMemo(() => {
    const m: Record<string, number> = {};
    learnings.forEach((l) => {
      m[l.agent] = (m[l.agent] || 0) + 1;
    });
    return m;
  }, [learnings]);

  const list = useMemo(() => {
    const base =
      filter === "all"
        ? learnings
        : learnings.filter((l) => l.agent === filter);
    // "recency" is a real sort on the chronological `order` (0 = newest), not a
    // no-op relying on array order — see sortLearnings in boardLogic.
    return sortLearnings(base, sort);
  }, [filter, sort, learnings]);

  const pending = pendingCount(learnings.length, verdicts);

  // Toggling a verdict off DELETES the key (not `undefined`) so PENDING
  // recovers — see applyVerdict in boardLogic.
  const verdict = async (id: string, v: Verdict) => {
    // DEFECT 7: Guard against double-click / in-flight race
    if (resolvingId === id) return;
    
    if (isLive && resolve) {
      // Call API resolution when live
      setResError(null);
      setResolvingId(id);
      try {
        const decision = v === "ok" ? "accepted" : "rejected";
        await resolve(id, decision);
      } catch (err: any) {
        setResError(err?.message || "Resolution failed");
      } finally {
        setResolvingId(null);
      }
    } else {
      // Local verdict tracking in sample mode
      setVerdicts((prev) => applyVerdict(prev, id, v));
    }
  };

  return (
    <div className="dz">
      <div className="dz-bar">
        <div className="fchips" role="tablist">
          <button
            className={"fchip" + (filter === "all" ? " on" : "")}
            onClick={() => setFilter("all")}
          >
            All · {learnings.length}
          </button>
          {Object.keys(agents).map((a) => (
            <button
              key={a}
              className={"fchip" + (filter === a ? " on" : "")}
              onClick={() => setFilter(a)}
            >
              <span className="dot" style={{ background: agentColor(a) }} />
              {a} · {agents[a]}
            </button>
          ))}
        </div>
        <div className="dz-bar-r">
          <span className="bd-chip warn">{pending} PENDING</span>
          <button
            className={"fchip" + (sort === "score" ? " on" : "")}
            onClick={() => setSort(sort === "score" ? "age" : "score")}
          >
            sort: {sort === "score" ? "score ↓" : "recency"}
          </button>
        </div>
      </div>
      <div className="lgrid">
        {list.map((l) => {
          const v = verdicts[l.id];
          return (
            <div
              key={l.id}
              className={
                "lcard" +
                (sel === l.id ? " sel" : "") +
                (v === "ok" ? " ok" : "") +
                (v === "no" ? " no" : "")
              }
              onClick={() => setSel(sel === l.id ? null : l.id)}
            >
              <div className="lc-top">
                <span className="dot" style={{ background: agentColor(l.agent) }} />
                <span className="lc-agent">{l.agent}</span>
                {typeof l.score === 'number' ? <Meter v={l.score} /> : <span className="meter-placeholder">—</span>}
                <span className="lc-score">{typeof l.score === 'number' ? l.score.toFixed(2) : l.score}</span>
              </div>
              <div className="lc-title">{l.title}</div>
              <div className="lc-meta">
                {l.id} · {l.src} · {l.age}
                {l.tag ? " · " : ""}
                {l.tag ? (
                  <span className="bd-chip info" style={{ fontSize: "8.5px" }}>
                    {l.tag}
                  </span>
                ) : null}
              </div>
              <div className="lc-actions">
                {v === "ok" ? (
                  <span className="bd-chip safe">✓ APPROVED</span>
                ) : v === "no" ? (
                  <span className="bd-chip danger">✕ REJECTED</span>
                ) : (
                  <Fragment>
                    <button
                      className="bd-btn primary sm"
                      disabled={resolvingId === l.id}
                      onClick={(e) => {
                        e.stopPropagation();
                        verdict(l.id, "ok");
                      }}
                    >
                      ✓ Approve
                    </button>
                    <button
                      className="bd-btn quiet sm"
                      disabled={resolvingId === l.id}
                      onClick={(e) => {
                        e.stopPropagation();
                        verdict(l.id, "no");
                      }}
                    >
                      ✕ Reject
                    </button>
                  </Fragment>
                )}
                {v ? (
                  <button
                    className="bd-btn quiet sm"
                    onClick={(e) => {
                      e.stopPropagation();
                      verdict(l.id, v);
                    }}
                  >
                    Undo
                  </button>
                ) : null}
              </div>
            </div>
          );
        })}
      </div>
      <div className="dz-foot">
        {stagedState?.error
          ? `Staged fetch error: ${stagedState.error}`
          : isLive
            ? learnings.length === 0
              ? "Live empty for this console principal — no proposed candidates owned by the stamped agent."
              : "Reject/redact via /api/resolve-candidate as the stamped owner. Accept into durable memory needs operator/govern (or MINNI_RESOLVE_OPERATORS) — not granted to this console principal."
            : "Sample candidates only. Nothing is stored from this board pass."}
      </div>
    </div>
  );
}

// ── RUNTIMES ────────────────────────────────────────────────────────────────
function AgentsDetail() {
  return (
    <div className="dz">
      <div className="agrid">
        {SAMPLE_AGENTS.map((a) => (
          <div key={a.id} className="acard">
            <div className="ac-hd">
              <span className="dot lg" style={{ background: a.on ? "var(--verdigris)" : "var(--disabled)" }} />
              <span className="ac-name">{a.id}</span>
              <span className="ac-seen">seen {a.seen} ago</span>
            </div>
            <div className="ac-vault">{a.vault}</div>
            <div className="ac-caps">
              <span className={"bd-chip " + (a.caps.R ? "safe" : "danger")}>RECALL{a.caps.R ? "" : " ✕"}</span>
              <span className={"bd-chip " + (a.caps.L ? "safe" : "danger")}>LEARN{a.caps.L ? "" : " ✕"}</span>
              <span className={"bd-chip " + (a.caps.H ? "safe" : "danger")}>HANDOFF{a.caps.H ? "" : " ✕"}</span>
            </div>
            <div className="ac-stats">
              <span>
                <b>{a.staged}</b> staged this week
              </span>
              {a.note ? <span className="ac-note">⚠ {a.note}</span> : null}
            </div>
          </div>
        ))}
        <div className="acard lease-card">
          <div className="ac-hd">
            <span className="bd-chip warn">AWAITING-ACK</span>
            <span className="ac-name" style={{ fontSize: "14px" }}>
              Open lease
            </span>
          </div>
          <div className="ac-vault">
            LS-2231 · claude-code → codex · “Port membench scorecard diff to CI” · 42m · TTL 15m, renewable
            once
          </div>
          <div className="ac-caps">
            <span className="bd-chip warn">SAMPLE · no lease API in console</span>
          </div>
        </div>
      </div>
      <div className="dz-foot">
        Sample runtime fixture. Capabilities shown here are not live grants. Lease
        revoke/nudge stay MCP-only until <code>/api/handoffs</code> ships.
      </div>
    </div>
  );
}

// ── DAEMON — wired from real status/health/audit where available ────────────
function HubDetail({ ctx }: { ctx: BoardDetailContext }) {
  const { daemon, recentAudit } = ctx;
  return (
    <div className="dz hub-dz">
      <div className="hcol">
        <div className="dcard">
          <div className="dc-t">Status</div>
          <div className="kv">
            <span>daemon</span>
            <b className={daemon.online ? "ok-t" : ""}>
              {daemon.online ? "online" : "offline"} · {daemon.version} · {daemon.uptime}
            </b>
          </div>
          <div className="kv">
            <span>socket</span>
            <b>{daemon.socket}</b>
          </div>
          <div className="kv">
            <span>bridge</span>
            <b>{daemon.bridge}</b>
          </div>
          <div className="kv">
            <span>extractor</span>
            <b>{daemon.mode}</b>
          </div>
          <div className="kv">
            <span>AFM</span>
            <b>
              {daemon.afmHealth}
            </b>
          </div>
        </div>
        <div className="dcard">
          <div className="dc-t">Store</div>
          <div className="kv">
            <span>learnings</span>
            <b>{daemon.storeLine}</b>
          </div>
          <div className="kv">
            <span>vault</span>
            <b>{daemon.vaultPath}</b>
          </div>
          <div className="kv">
            <span>vault exists</span>
            <b>{daemon.vaultExists}</b>
          </div>
          <div className="kv">
            <span>audit</span>
            <b>{daemon.auditEntries === "—" ? "—" : `${daemon.auditEntries} entries recorded`}</b>
          </div>
        </div>
      </div>
      <div className="dcard grow">
        <div className="dc-t">Recent audit · live</div>
        {recentAudit.length > 0 ? (
          <div className="audit-strip">
            {recentAudit.slice(0, 8).map((line, i) => (
              <div key={i} className="audit-line">
                {line}
              </div>
            ))}
          </div>
        ) : (
          <div className="callout-band">—</div>
        )}
      </div>
    </div>
  );
}

// ── LOG-ONLY ────────────────────────────────────────────────────────────────
function LogsDetail() {
  return (
    <div className="dz">
      <div className="callout-band">
        Sample log-only entries. These are not live personal-leg records.
      </div>
      {SAMPLE_LOGS.map((l) => (
        <div key={l.id} className="logrow">
          <span className="klass log">LOG</span>
          <span className="bd-chip danger">PRIVATE</span>
          <div className="lr-body">
            <div className="lr-t">{l.title}</div>
            <div className="lr-m">
              {l.id} · {l.agent} · {l.age}
            </div>
          </div>
          <Meter v={l.score} />
          <span className="lc-score">{l.score.toFixed(2)}</span>
        </div>
      ))}
      <div className="dz-foot">
        Sample log-only entries. Forget is not available in the console bridge.
      </div>
    </div>
  );
}

// ── QUARANTINE ──────────────────────────────────────────────────────────────
function QuarantineDetail() {
  return (
    <div className="dz q-dz">
      <div className="dcard">
        <div className="fid">
          {SAMPLE_DENY.id} <span className="klass deny">DO-NOT-STORE</span>{" "}
          <span className="bd-chip info">SAMPLE</span>{" "}
          <span className="bd-chip warn">AFM: DEFUSED</span>{" "}
          <span className="lc-score" style={{ marginLeft: "auto" }}>
            {SAMPLE_DENY.score.toFixed(2)}
          </span>
        </div>
        <div className="q-title">{SAMPLE_DENY.title}</div>
        <div className="codeblk">{SAMPLE_DENY.body}</div>
        <div className="dc-t" style={{ marginTop: "14px" }}>
          Provenance
        </div>
        <div className="kv">
          <span>source</span>
          <b>{SAMPLE_DENY.src}</b>
        </div>
        <div className="kv">
          <span>ingested</span>
          <b>{SAMPLE_DENY.ingested}</b>
        </div>
        <div className="kv">
          <span>hash</span>
          <b>{SAMPLE_DENY.hash}</b>
        </div>
        <div className="callout risk">
          <span className="label">INSTRUCTION-LIKE · DEFUSED</span>
          {SAMPLE_DENY.risk}
        </div>
        <div className="dz-foot" style={{ marginTop: 12 }}>
          Sample quarantine fixture. Confirm / redact / view-raw are not wired in
          the console — use MCP <code>minni_resolve_candidate</code> with a real
          candidate id when governing live rows.
        </div>
      </div>
    </div>
  );
}

// ── RECALL ──────────────────────────────────────────────────────────────────
function RecallDetail({
  onOpenRecall,
}: {
  onOpenRecall?: (query?: string) => void;
}) {
  const [sel, setSel] = useState(0);
  const [query, setQuery] = useState("handoff leases");
  const active = SAMPLE_RECALL[sel];
  return (
    <div className="dz recall-dz">
      <div className="rcol">
        <div className="rsearch">
          <input
            className="bd-input"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            aria-label="Recall query"
          />
          <button
            className="bd-btn primary"
            type="button"
            onClick={() => onOpenRecall?.(query.trim() || undefined)}
            disabled={!onOpenRecall}
          >
            Open in console Recall
          </button>
        </div>
        <div className="r-meta">
          Sample preview only · use console Recall for live <code>/api/prepare-task</code> ·{" "}
          <span className="bd-chip info">CITED, NEVER OBEYED</span>
        </div>
        {SAMPLE_RECALL.map((r, i) => (
          <button
            key={r.path}
            className={"rrow" + (sel === i ? " sel" : "")}
            onClick={() => setSel(i)}
            type="button"
          >
            <Meter v={r.score} />
            <span className="lc-score">{r.score.toFixed(2)}</span>
            <div className="lr-body">
              <div className="lr-t mono-t">{r.path}</div>
              <div className="lr-m">
                {r.sub} · {r.cls} · {r.priv} · {r.age}
              </div>
            </div>
            <span className={"bd-chip " + (r.afm === "SAFE" ? "safe" : "warn")}>{r.afm}</span>
          </button>
        ))}
      </div>
      <div className="dcard grow">
        <div className="dc-t">Evidence envelope · E-{String(sel + 1).padStart(2, "0")}</div>
        <div className="codeblk">{active.body}</div>
        <div className="kv">
          <span>source</span>
          <b>{active.path}</b>
        </div>
        <div className="kv">
          <span>authority</span>
          <b>{active.auth}</b>
        </div>
        <div className="kv">
          <span>privacy</span>
          <b>{active.priv}</b>
        </div>
        <div className="callout safe">
          Served inside an evidence envelope: provenance-tagged, weighed by the caller, never framed as
          instruction.
        </div>
      </div>
    </div>
  );
}

export function ZoneDetail({
  id,
  ctx,
  stagedState,
  onOpenRecall,
}: {
  id: ZoneId;
  ctx: BoardDetailContext;
  stagedState?: StagedLearningsState;
  onOpenRecall?: (query?: string) => void;
}) {
  switch (id) {
    case "staged":
      return <StagedDetail stagedState={stagedState} />;
    case "agents":
      return <AgentsDetail />;
    case "hub":
      return <HubDetail ctx={ctx} />;
    case "logs":
      return <LogsDetail />;
    case "quarantine":
      return <QuarantineDetail />;
    case "recall":
      return <RecallDetail onOpenRecall={onOpenRecall} />;
    default:
      return null;
  }
}
