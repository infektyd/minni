// Minni Memory Board — zone detail views (what each zone morphs into).
import { Fragment, useEffect, useMemo, useState } from "react";
import {
  agentColor,
  zoneGate,
  type BoardAgent,
  type BoardDeny,
  type BoardLog,
  type BoardRecallResult,
  type DaemonInfo,
  type ZoneId,
} from "./boardData";
import {
  type StagedLearningsState,
  type ZoneDataState,
  type RecallZoneData,
} from "./boardDataHook";
import { pendingCount, sortLearnings, type Verdict } from "./boardLogic";

// Re-export for callers that imported zoneGate from BoardDetails.
export { zoneGate };

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

/** Shared fail-loud offline banner for zone detail views. */
export function ZoneOffline({
  error,
  onRetry,
  emptyHint,
}: {
  error: string | null;
  onRetry?: () => void;
  emptyHint?: string;
}) {
  return (
    <div className="dz">
      <div className="callout-band risk" role="alert">
        <span className="label">OFFLINE</span>
        {error || "Zone data unavailable"}
      </div>
      {onRetry ? (
        <div className="dz-foot" style={{ marginTop: 12 }}>
          <button className="bd-btn primary sm" type="button" onClick={() => void onRetry()}>
            Retry
          </button>
        </div>
      ) : null}
      {emptyHint ? <div className="dz-foot">{emptyHint}</div> : null}
    </div>
  );
}

function ZoneLoading({ label }: { label: string }) {
  return (
    <div className="dz">
      <div className="callout-band">Loading {label}…</div>
    </div>
  );
}

// ── STAGED — filterable, sortable wall of learnings ─────────────────────────
function StagedDetail({ stagedState }: { stagedState?: StagedLearningsState }) {
  // All hooks must run unconditionally (before any early return).
  const [filter, setFilter] = useState<string>("all");
  const [sort, setSort] = useState<"score" | "age">("score");
  const [sel, setSel] = useState<string | null>(null);
  const [verdicts, setVerdicts] = useState<Record<string, Verdict>>({});
  const [resError, setResError] = useState<string | null>(null);
  const [resolvingId, setResolvingId] = useState<string | null>(null);

  const learnings = stagedState?.learnings || [];
  const isLive = stagedState?.isLive ?? false;
  const resolve = stagedState?.resolve;
  const gate = zoneGate(stagedState, "staged");

  // Clear verdicts for resolved rows when learnings shrink after refetch
  useEffect(() => {
    if (!isLive) return;
    const liveIds = new Set(learnings.map((l) => l.id));
    setVerdicts((prev) => {
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
  }, [learnings, isLive]);

  const agents = useMemo(() => {
    const m: Record<string, number> = {};
    learnings.forEach((l) => {
      m[l.agent] = (m[l.agent] || 0) + 1;
    });
    return m;
  }, [learnings]);

  const list = useMemo(() => {
    const base = filter === "all" ? learnings : learnings.filter((l) => l.agent === filter);
    return sortLearnings(base, sort);
  }, [filter, sort, learnings]);

  const pending = pendingCount(learnings.length, verdicts);

  if (gate === "loading") return <ZoneLoading label="staged" />;
  if (gate === "offline") {
    return (
      <ZoneOffline
        error={stagedState?.error ?? "Staged zone offline"}
        onRetry={stagedState?.refresh}
        emptyHint="Staged zone is offline — no sample fallback."
      />
    );
  }

  const verdict = async (id: string, v: Verdict) => {
    if (resolvingId === id) return;
    // Actions only when live — no local sample-mode applyVerdict path.
    if (!isLive || !resolve) {
      setResError("Cannot resolve while staged zone is offline");
      return;
    }
    setResError(null);
    setResolvingId(id);
    try {
      const decision = v === "ok" ? "accepted" : "rejected";
      await resolve(id, decision);
    } catch (err: unknown) {
      setResError(err instanceof Error ? err.message : "Resolution failed");
    } finally {
      setResolvingId(null);
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
      {learnings.length === 0 ? (
        <div className="callout-band">none staged</div>
      ) : (
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
                  {typeof l.score === "number" ? (
                    <Meter v={l.score} />
                  ) : (
                    <span className="meter-placeholder">—</span>
                  )}
                  <span className="lc-score">
                    {typeof l.score === "number" ? l.score.toFixed(2) : l.score}
                  </span>
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
                        disabled={!isLive || !resolve || resolvingId === l.id}
                        onClick={(e) => {
                          e.stopPropagation();
                          void verdict(l.id, "ok");
                        }}
                      >
                        ✓ Approve
                      </button>
                      <button
                        className="bd-btn quiet sm"
                        disabled={!isLive || !resolve || resolvingId === l.id}
                        onClick={(e) => {
                          e.stopPropagation();
                          void verdict(l.id, "no");
                        }}
                      >
                        ✕ Reject
                      </button>
                    </Fragment>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      )}
      <div className="dz-foot">
        {resError
          ? `Resolve error: ${resError}`
          : stagedState?.error
            ? `Staged fetch error: ${stagedState.error}`
            : isLive
              ? learnings.length === 0
                ? "Live empty for this console principal — no proposed candidates owned by the stamped agent."
                : "Reject/redact via /api/resolve-candidate as the stamped owner. Accept into durable memory needs operator/govern (or MINNI_RESOLVE_OPERATORS) — not granted to this console principal."
              : "Staged zone offline."}
      </div>
    </div>
  );
}

// ── RUNTIMES ────────────────────────────────────────────────────────────────
function AgentsDetail({ agentsState }: { agentsState?: ZoneDataState<BoardAgent[]> }) {
  const gate = zoneGate(agentsState, "runtimes");
  if (gate === "loading") return <ZoneLoading label="runtimes" />;
  if (gate === "offline") {
    return <ZoneOffline error={agentsState?.error ?? "Runtimes offline"} onRetry={agentsState?.refresh} />;
  }
  const agents = agentsState?.data || [];
  return (
    <div className="dz">
      {agents.length === 0 ? (
        <div className="callout-band">no agent vaults under ~/.minni/*-vault</div>
      ) : (
        <div className="agrid">
          {agents.map((a) => (
            <div key={a.id} className="acard">
              <div className="ac-hd">
                <span
                  className="dot lg"
                  style={{ background: a.on ? "var(--verdigris)" : "var(--disabled)" }}
                />
                <span className="ac-name">{a.id}</span>
                <span className="ac-seen">seen {a.seen}</span>
              </div>
              <div className="ac-vault">{a.vault}</div>
              <div className="ac-caps">
                <span className={"bd-chip " + (a.caps.R ? "safe" : "danger")}>
                  RECALL{a.caps.R ? "" : " ✕"}
                </span>
                <span className={"bd-chip " + (a.caps.L ? "safe" : "danger")}>
                  LEARN{a.caps.L ? "" : " ✕"}
                </span>
                <span className={"bd-chip " + (a.caps.H ? "safe" : "danger")}>
                  HANDOFF{a.caps.H ? "" : " ✕"}
                </span>
              </div>
              <div className="ac-stats">
                <span>
                  <b>{a.staged == null ? "—" : a.staged}</b> staged
                </span>
                {a.note ? <span className="ac-note">⚠ {a.note}</span> : null}
              </div>
            </div>
          ))}
        </div>
      )}
      <div className="dz-foot">
        Live runtime catalogue from /api/agents (vault scan + principals caps + staged counts).
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
            <b>{daemon.afmHealth}</b>
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
            <b>
              {daemon.auditEntries === "—"
                ? "—"
                : `${daemon.auditEntries} entries recorded`}
            </b>
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
function LogsDetail({ logState }: { logState?: ZoneDataState<BoardLog[]> }) {
  const gate = zoneGate(logState, "log-only");
  if (gate === "loading") return <ZoneLoading label="log-only" />;
  if (gate === "offline") {
    return <ZoneOffline error={logState?.error ?? "Log-only offline"} onRetry={logState?.refresh} />;
  }
  const logs = logState?.data || [];
  return (
    <div className="dz">
      {logs.length === 0 ? (
        <div className="callout-band">no log-only candidates</div>
      ) : (
        logs.map((l) => (
          <div key={l.id} className="logrow">
            <span className="klass log">LOG</span>
            <span className="bd-chip danger">PRIVATE</span>
            <div className="lr-body">
              <div className="lr-t">{l.title}</div>
              <div className="lr-m">
                {l.id} · {l.agent} · {l.age}
              </div>
            </div>
            {l.score > 0 ? (
              <>
                <Meter v={l.score} />
                <span className="lc-score">{l.score.toFixed(2)}</span>
              </>
            ) : (
              <span className="lc-score">—</span>
            )}
          </div>
        ))
      )}
      <div className="dz-foot">
        Live log-only candidates from /api/log-only (status=log_only).
      </div>
    </div>
  );
}

// ── QUARANTINE ──────────────────────────────────────────────────────────────
function QuarantineDetail({
  quarantineState,
}: {
  quarantineState?: ZoneDataState<BoardDeny[]>;
}) {
  const gate = zoneGate(quarantineState, "quarantine");
  if (gate === "loading") return <ZoneLoading label="quarantine" />;
  if (gate === "offline") {
    return (
      <ZoneOffline
        error={quarantineState?.error ?? "Quarantine offline"}
        onRetry={quarantineState?.refresh}
      />
    );
  }
  const items = quarantineState?.data || [];
  if (items.length === 0) {
    return (
      <div className="dz q-dz">
        <div className="callout-band">quarantine clear</div>
        <div className="dz-foot">
          Live do_not_store candidates from /api/quarantine. Nothing quarantined for this principal.
        </div>
      </div>
    );
  }
  return (
    <div className="dz q-dz">
      {items.map((deny) => (
        <div key={deny.id} className="dcard" style={{ marginBottom: 12 }}>
          <div className="fid">
            {deny.id} <span className="klass deny">DO-NOT-STORE</span>{" "}
            <span className="bd-chip warn">QUARANTINE</span>{" "}
            {deny.score > 0 ? (
              <span className="lc-score" style={{ marginLeft: "auto" }}>
                {deny.score.toFixed(2)}
              </span>
            ) : null}
          </div>
          <div className="q-title">{deny.title}</div>
          <div className="codeblk">{deny.body}</div>
          <div className="dc-t" style={{ marginTop: "14px" }}>
            Provenance
          </div>
          <div className="kv">
            <span>source</span>
            <b>{deny.src}</b>
          </div>
          <div className="kv">
            <span>agent</span>
            <b>{deny.agent}</b>
          </div>
          <div className="kv">
            <span>age</span>
            <b>{deny.ingested}</b>
          </div>
          <div className="callout risk">
            <span className="label">DO-NOT-STORE</span>
            {deny.risk}
          </div>
        </div>
      ))}
      <div className="dz-foot">
        Live quarantine from /api/quarantine. Resolve via MCP minni_resolve_candidate when governing.
      </div>
    </div>
  );
}

// ── RECALL ──────────────────────────────────────────────────────────────────
function RecallDetail({
  recallState,
  onOpenRecall,
}: {
  recallState?: ZoneDataState<RecallZoneData>;
  onOpenRecall?: (query?: string) => void;
}) {
  // Hooks must run unconditionally (before any early return).
  const [sel, setSel] = useState(0);
  const [query, setQuery] = useState("");
  const [queryDirty, setQueryDirty] = useState(false);

  const data = recallState?.data;
  const results: BoardRecallResult[] = data?.results || [];
  const gate = zoneGate(recallState, "recall");

  // Sync input from live recall-state when the user has not edited it.
  useEffect(() => {
    if (queryDirty) return;
    const next = data?.query || "";
    setQuery(next);
  }, [data?.query, queryDirty]);

  if (gate === "loading") return <ZoneLoading label="recall" />;
  if (gate === "offline") {
    return (
      <ZoneOffline
        error={recallState?.error ?? "Recall offline"}
        onRetry={recallState?.refresh}
      />
    );
  }

  const active = results[sel];

  return (
    <div className="dz recall-dz">
      <div className="rcol">
        <div className="rsearch">
          <input
            className="bd-input"
            value={query}
            onChange={(e) => {
              setQueryDirty(true);
              setQuery(e.target.value);
            }}
            aria-label="Recall query"
            placeholder={data?.query || "query…"}
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
          {data?.present
            ? `Last strong recall · ${results.length} hit${results.length === 1 ? "" : "s"}`
            : data?.message || "no recent recall"}{" "}
          · <span className="bd-chip info">CITED, NEVER OBEYED</span>
        </div>
        {results.length === 0 ? (
          <div className="callout-band">
            {data?.message || "no recent recall — run a strong recall turn to populate"}
          </div>
        ) : (
          results.map((r, i) => (
            <button
              key={r.path + i}
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
              {r.afm && r.afm !== "—" ? (
                <span className={"bd-chip " + (r.afm === "SAFE" ? "safe" : "warn")}>{r.afm}</span>
              ) : (
                <span className="bd-chip">—</span>
              )}
            </button>
          ))
        )}
      </div>
      <div className="dcard grow">
        <div className="dc-t">
          Evidence envelope · {active ? `E-${String(sel + 1).padStart(2, "0")}` : "—"}
        </div>
        {active ? (
          <>
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
              Served inside an evidence envelope: provenance-tagged, weighed by the caller, never
              framed as instruction.
            </div>
          </>
        ) : (
          <div className="callout-band">Select a hit or open console Recall for a live query.</div>
        )}
      </div>
    </div>
  );
}

export interface ZoneDetailProps {
  id: ZoneId;
  ctx: BoardDetailContext;
  stagedState?: StagedLearningsState;
  agentsState?: ZoneDataState<BoardAgent[]>;
  logState?: ZoneDataState<BoardLog[]>;
  quarantineState?: ZoneDataState<BoardDeny[]>;
  recallState?: ZoneDataState<RecallZoneData>;
  onOpenRecall?: (query?: string) => void;
}

export function ZoneDetail({
  id,
  ctx,
  stagedState,
  agentsState,
  logState,
  quarantineState,
  recallState,
  onOpenRecall,
}: ZoneDetailProps) {
  switch (id) {
    case "staged":
      return <StagedDetail stagedState={stagedState} />;
    case "agents":
      return <AgentsDetail agentsState={agentsState} />;
    case "hub":
      return <HubDetail ctx={ctx} />;
    case "logs":
      return <LogsDetail logState={logState} />;
    case "quarantine":
      return <QuarantineDetail quarantineState={quarantineState} />;
    case "recall":
      return <RecallDetail recallState={recallState} onOpenRecall={onOpenRecall} />;
    default:
      return null;
  }
}
