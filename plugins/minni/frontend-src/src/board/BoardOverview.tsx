// Minni Memory Board — overview layer: draggable zones, live links, flow pulses.
import { useEffect, useMemo, useRef, type ReactNode } from "react";
import {
  type BoardAgent,
  type BoardDeny,
  type BoardFlow,
  type BoardLog,
  type BoardRecallResult,
  type DaemonInfo,
  type FlowEvent,
  type ZoneDef,
  type ZoneId,
} from "./boardData";
import {
  type StagedLearningsState,
  type ZoneDataState,
  type RecallZoneData,
} from "./boardDataHook";
import {
  computeLinks,
  OVERVIEW_LAYOUT,
  stagedSlot,
  type Link,
} from "./boardLayout";
import { isDrag, type ZoneMode, type ZoneModes } from "./boardLogic";

// ── flow pulse layer ────────────────────────────────────────────────────────
interface Pulse {
  flow: BoardFlow;
  el: SVGCircleElement;
  step: number;
  start: number;
  pathEl: SVGPathElement | null;
}

function FlowLayer({
  links,
  running,
  reduced,
  feed,
  onEvent,
}: {
  links: Link[];
  running: boolean;
  reduced: boolean;
  /** Real traffic only: audit-tail entries mapped to flows. No synthetic ambient. */
  feed: { seq: number; flow: BoardFlow }[];
  onEvent: (e: FlowEvent) => void;
}) {
  const gRef = useRef<SVGGElement | null>(null);
  const pathRefs = useRef<Record<string, SVGPathElement | null>>({});
  const spawnRef = useRef<((flow: BoardFlow) => void) | null>(null);
  const lastSeq = useRef(0);

  useEffect(() => {
    const g = gRef.current;
    if (!running || reduced || !g) return;
    const paths = pathRefs.current;
    const pulses: Pulse[] = [];
    const liveRef = new Map<SVGPathElement, number>();
    let raf = 0;

    const enterSegment = (path: SVGPathElement, color: string) => {
      const next = (liveRef.get(path) || 0) + 1;
      liveRef.set(path, next);
      path.classList.add("live");
      path.setAttribute("data-live", "true");
      path.setAttribute("data-flow-color", color);
      path.style.stroke = color;
    };

    const leaveSegment = (path: SVGPathElement) => {
      const next = (liveRef.get(path) || 0) - 1;
      if (next <= 0) {
        liveRef.delete(path);
        path.classList.remove("live");
        path.setAttribute("data-live", "false");
        path.removeAttribute("data-flow-color");
        path.style.stroke = "";
      } else {
        liveRef.set(path, next);
      }
    };

    const spawnFlow = (flow: BoardFlow) => {
      const el = document.createElementNS("http://www.w3.org/2000/svg", "circle");
      el.setAttribute("r", "5");
      el.setAttribute("class", "pulse");
      el.setAttribute("data-testid", "board-flow-pulse");
      el.setAttribute("data-flow-label", flow.label);
      el.style.fill = flow.color;
      g.appendChild(el);
      pulses.push({ flow, el, step: 0, start: performance.now(), pathEl: null });
    };
    spawnRef.current = spawnFlow;

    const tick = (now: number) => {
      for (let i = pulses.length - 1; i >= 0; i--) {
        const p = pulses[i];
        const step = p.flow.steps[p.step];
        const path = paths[step.l];
        if (!path) {
          // No rendered link for this leg (e.g. an "ag-unknown" agent edge, or a
          // named agent absent from the live catalogue). Skip just this leg and
          // advance — the real hub legs must still animate, so we must not drop
          // the whole multi-leg pulse.
          if (p.pathEl) {
            leaveSegment(p.pathEl);
            p.pathEl = null;
          }
          p.step++;
          p.start = now;
          if (p.step >= p.flow.steps.length) {
            p.el.remove();
            pulses.splice(i, 1);
          }
          continue;
        }
        const len = path.getTotalLength();
        const dur = Math.max(550, len / 0.34);
        const t = (now - p.start) / dur;
        if (t >= 1) {
          if (p.pathEl) {
            leaveSegment(p.pathEl);
            p.pathEl = null;
          }
          p.step++;
          p.start = now;
          if (p.step >= p.flow.steps.length) {
            p.el.remove();
            pulses.splice(i, 1);
          }
          continue;
        }
        if (p.pathEl !== path) {
          if (p.pathEl) leaveSegment(p.pathEl);
          enterSegment(path, p.flow.color);
          p.pathEl = path;
        }
        p.el.setAttribute("data-flow-step", step.l);
        const pos = path.getPointAtLength((step.rev ? 1 - t : t) * len);
        p.el.setAttribute("cx", String(pos.x));
        p.el.setAttribute("cy", String(pos.y));
      }
      raf = requestAnimationFrame(tick);
    };

    raf = requestAnimationFrame(tick);
    return () => {
      spawnRef.current = null;
      cancelAnimationFrame(raf);
      pulses.forEach((p) => {
        if (p.pathEl) leaveSegment(p.pathEl);
        p.el.remove();
      });
      liveRef.clear();
      Object.values(paths).forEach((el) => {
        if (el) {
          el.classList.remove("live");
          el.setAttribute("data-live", "false");
          el.removeAttribute("data-flow-color");
          el.style.stroke = "";
        }
      });
    };
  }, [running, reduced, onEvent]);

  // Real traffic: each new audit-derived flow rides the board once and hits
  // the ticker. Ticker updates even under reduced motion (no pulse, still news).
  useEffect(() => {
    for (const item of feed) {
      if (item.seq <= lastSeq.current) continue;
      lastSeq.current = item.seq;
      onEvent({ n: item.seq, label: item.flow.label, color: item.flow.color });
      spawnRef.current?.(item.flow);
    }
  }, [feed, onEvent]);

  return (
    <svg className="bd-svg" aria-hidden="true">
      {links.map((l) => (
        <path
          key={l.id}
          className={l.cls}
          d={l.d}
          data-flow-id={l.id}
          data-live="false"
          ref={(el) => {
            pathRefs.current[l.id] = el;
          }}
        />
      ))}
      <g ref={gRef} />
    </svg>
  );
}

// ── draggable zone container ────────────────────────────────────────────────
interface DragState {
  sx: number;
  sy: number;
  x: number;
  y: number;
  moved: boolean;
}

function ZoneBox({
  id,
  z,
  scale,
  mode,
  focusable,
  onFocus,
  onMove,
  onResize,
  onModeChange,
  children,
}: {
  id: ZoneId;
  z: ZoneDef;
  scale: number;
  mode?: ZoneMode;
  focusable: boolean;
  onFocus: (id: ZoneId) => void;
  onMove: (id: ZoneId, x: number, y: number) => void;
  onResize: (id: ZoneId, w: number, h: number) => void;
  onModeChange: (id: ZoneId, mode: ZoneMode) => void;
  children: React.ReactNode;
}) {
  const drag = useRef<DragState | null>(null);
  const rdrag = useRef<{ sx: number; sy: number; w: number; h: number } | null>(null);
  const raf = useRef<number | undefined>(undefined);
  const pending = useRef<{ kind: "move" | "resize"; a: number; b: number } | null>(null);
  const isCustom = mode === "custom";

  const schedule = (kind: "move" | "resize", a: number, b: number) => {
    pending.current = { kind, a, b };
    if (raf.current !== undefined) return;
    raf.current = requestAnimationFrame(() => {
      raf.current = undefined;
      const p = pending.current;
      if (!p) return;
      if (p.kind === "move") onMove(id, p.a, p.b);
      else onResize(id, p.a, p.b);
    });
  };

  useEffect(
    () => () => {
      if (raf.current !== undefined) cancelAnimationFrame(raf.current);
    },
    [],
  );

  return (
    <div
      className={"zone" + (id === "quarantine" ? " qz" : "")}
      data-zone-id={id}
      data-status={z.status}
      style={{ left: z.x, top: z.y, width: z.w, height: z.h }}
      role="button"
      tabIndex={focusable ? 0 : -1}
      aria-hidden={!focusable}
      aria-label={"Zoom into " + z.title + " (drag to move)"}
      onKeyDown={(e) => {
        if (!focusable) return;
        if (e.key === "Enter") onFocus(id);
      }}
      onPointerDown={(e) => {
        e.preventDefault();
        e.currentTarget.setPointerCapture(e.pointerId);
        drag.current = { sx: e.clientX, sy: e.clientY, x: z.x, y: z.y, moved: false };
      }}
      onPointerMove={(e) => {
        const d = drag.current;
        if (!d) return;
        const screenDx = e.clientX - d.sx;
        const screenDy = e.clientY - d.sy;
        if (!d.moved && isDrag(screenDx, screenDy)) d.moved = true;
        if (d.moved) schedule("move", d.x + screenDx / scale, d.y + screenDy / scale);
      }}
      onPointerUp={(e) => {
        const d = drag.current;
        drag.current = null;
        if (raf.current !== undefined) {
          cancelAnimationFrame(raf.current);
          raf.current = undefined;
        }
        pending.current = null;
        try {
          e.currentTarget.releasePointerCapture(e.pointerId);
        } catch {
          /* already released */
        }
        if (d && !d.moved) onFocus(id);
        if (d && d.moved) {
          const screenDx = e.clientX - d.sx;
          const screenDy = e.clientY - d.sy;
          onMove(id, d.x + screenDx / scale, d.y + screenDy / scale);
        }
      }}
      onPointerCancel={() => {
        drag.current = null;
        pending.current = null;
      }}
    >
      <span className="zl">{z.label}</span>
      <div
        className="zmode"
        onPointerDown={(e) => e.stopPropagation()}
        onKeyDown={(e) => e.stopPropagation()}
      >
        <button
          type="button"
          className={!isCustom ? "on" : ""}
          onClick={(e) => {
            e.stopPropagation();
            onModeChange(id, "auto");
          }}
          aria-label="Automatic layout — snap this box back to its default place and size"
          title="Automatic layout"
        >
          auto
        </button>
        <button
          type="button"
          className={isCustom ? "on" : ""}
          onClick={(e) => {
            e.stopPropagation();
            onModeChange(id, "custom");
          }}
          aria-label="Custom layout — drag and resize this box freely"
          title="Custom layout: drag & resize freely"
        >
          custom
        </button>
      </div>
      <span className="zoom-hint">⤢</span>
      <div className="zc">{children}</div>
      <div
        className="zresize"
        aria-hidden="true"
        onPointerDown={(e) => {
          e.stopPropagation();
          e.currentTarget.setPointerCapture(e.pointerId);
          rdrag.current = { sx: e.clientX, sy: e.clientY, w: z.w, h: z.h };
        }}
        onPointerMove={(e) => {
          const r = rdrag.current;
          if (!r) return;
          schedule(
            "resize",
            r.w + (e.clientX - r.sx) / scale,
            r.h + (e.clientY - r.sy) / scale,
          );
        }}
        onPointerUp={(e) => {
          const r = rdrag.current;
          rdrag.current = null;
          if (raf.current !== undefined) {
            cancelAnimationFrame(raf.current);
            raf.current = undefined;
          }
          pending.current = null;
          try {
            e.currentTarget.releasePointerCapture(e.pointerId);
          } catch {
            /* already released */
          }
          if (r) {
            onResize(id, r.w + (e.clientX - r.sx) / scale, r.h + (e.clientY - r.sy) / scale);
          }
        }}
        onPointerCancel={() => {
          rdrag.current = null;
          pending.current = null;
        }}
      />
    </div>
  );
}

// ── overview card ───────────────────────────────────────────────────────────
function OvCard({
  x,
  y,
  w,
  klass,
  klassLabel,
  tag,
  tagCls,
  score,
  title,
  meta,
  deny,
}: {
  x: number;
  y: number;
  w?: number;
  klass?: string;
  klassLabel?: string;
  tag?: string;
  tagCls?: string;
  score?: string;
  title: string;
  meta: string;
  deny?: boolean;
}) {
  return (
    <div className={"card" + (deny ? " deny-card" : "")} style={{ left: x, top: y, width: w }}>
      <div className="cr">
        {klass ? <span className={"klass " + klass}>{klassLabel}</span> : null}
        {tag ? (
          <span className={"bd-chip " + (tagCls || "info")} style={{ fontSize: "8.5px" }}>
            {tag}
          </span>
        ) : null}
        {score ? <span className="sc">{score}</span> : null}
      </div>
      <div className="ct">{title}</div>
      <div className="cm">{meta}</div>
    </div>
  );
}

// ── overview root ───────────────────────────────────────────────────────────
export function BoardOverview({
  zones,
  scale,
  daemon,
  onFocus,
  onMove,
  onResize,
  zmode,
  onModeChange,
  zonesFocusable,
  flowsRunning,
  reducedMotion,
  flowFeed,
  onFlowEvent,
  stagedState,
  agentsState,
  logState,
  quarantineState,
  recallState,
}: {
  zones: Record<ZoneId, ZoneDef>;
  scale: number;
  daemon: DaemonInfo;
  onFocus: (id: ZoneId) => void;
  onMove: (id: ZoneId, x: number, y: number) => void;
  onResize: (id: ZoneId, w: number, h: number) => void;
  zmode: ZoneModes;
  onModeChange: (id: ZoneId, mode: ZoneMode) => void;
  zonesFocusable: boolean;
  flowsRunning: boolean;
  reducedMotion: boolean;
  flowFeed: { seq: number; flow: BoardFlow }[];
  onFlowEvent: (e: FlowEvent) => void;
  stagedState?: StagedLearningsState;
  agentsState?: ZoneDataState<BoardAgent[]>;
  logState?: ZoneDataState<BoardLog[]>;
  quarantineState?: ZoneDataState<BoardDeny[]>;
  recallState?: ZoneDataState<RecallZoneData>;
}) {
  const agents = agentsState?.data || [];
  // Empty catalogue → empty links (no invented "main" runtime for geometry).
  const agentIds = useMemo(() => agents.map((a) => a.id), [agents]);
  const links = useMemo(() => computeLinks(zones, agentIds), [zones, agentIds]);
  const learnings = stagedState?.learnings || [];
  const top4 = learnings.slice(0, 4);
  const logs = logState?.data || [];
  const denies = quarantineState?.data || [];
  const recallHits: BoardRecallResult[] = recallState?.data?.results || [];
  const L = OVERVIEW_LAYOUT;

  const offlineChip = (label: string) => (
    <div className="more-chip" style={{ left: 12, top: 40 }}>
      {label}
    </div>
  );
  /** Overview chip aligned with detail zoneGate: loading ≠ offline. `liveBody`
   * is lazy so an offline/empty zone never evaluates it — the live branches
   * dereference index 0, which would throw on the empty default-install state. */
  const zoneChip = (
    state: { isLive: boolean; loading: boolean; error: string | null } | undefined,
    offlineLabel: string,
    loadingLabel: string,
    emptyLabel: string,
    empty: boolean,
    liveBody: () => ReactNode,
  ) => {
    if (state && !state.isLive) {
      if (state.loading && !state.error) return offlineChip(loadingLabel);
      return offlineChip(offlineLabel);
    }
    if (empty) return offlineChip(emptyLabel);
    return liveBody();
  };

  return (
    <div className="ov-root">
      <FlowLayer
        links={links}
        running={flowsRunning}
        reduced={reducedMotion}
        feed={flowFeed}
        onEvent={onFlowEvent}
      />

      <ZoneBox id="agents" z={zones.agents} scale={scale} mode={zmode.agents} onResize={onResize} onModeChange={onModeChange} focusable={zonesFocusable} onFocus={onFocus} onMove={onMove}>
        {zoneChip(
          agentsState,
          "RUNTIMES · OFFLINE",
          "RUNTIMES · …",
          "no vaults",
          agents.length === 0,
          () => (
            <>
              {agents.slice(0, 6).map((a, i) => (
                <div
                  key={a.id}
                  className="node"
                  style={{ left: L.agents.nodeX, top: L.agents.nodeY0 + i * L.agents.nodeGap }}
                >
                  <div className="nn">
                    <span
                      className="dot"
                      style={{ background: a.on ? "var(--verdigris)" : "var(--disabled)" }}
                    />
                    {a.id}
                  </div>
                  <div className="nv">
                    {a.vault} · {a.seen}
                  </div>
                  <div className="ncaps">
                    <span className={"bd-chip " + (a.caps.R ? "ok" : "no")}>R</span>
                    <span className={"bd-chip " + (a.caps.L ? "ok" : "no")}>L</span>
                    <span className={"bd-chip " + (a.caps.H ? "ok" : "no")}>H</span>
                  </div>
                </div>
              ))}
              {agentsState?.isLive && agents.length > 6 ? (
                <div
                  className="more-chip"
                  style={{ left: L.agents.nodeX, top: L.agents.nodeY0 + 6 * L.agents.nodeGap }}
                >
                  + {agents.length - 6} more · click zone to expand
                </div>
              ) : null}
            </>
          ),
        )}
      </ZoneBox>

      <ZoneBox id="hub" z={zones.hub} scale={scale} mode={zmode.hub} onResize={onResize} onModeChange={onModeChange} focusable={zonesFocusable} onFocus={onFocus} onMove={onMove}>
        <div className="hub" style={{ left: L.hub.card.x, top: L.hub.card.y }}>
          <div className="hn">⬢ minnid</div>
          <div className="hv">
            {daemon.version} · {daemon.uptime}
            <br />
            {daemon.storeLine}
            <br />
            {daemon.doctorLine}
          </div>
        </div>
      </ZoneBox>

      <ZoneBox id="staged" z={zones.staged} scale={scale} mode={zmode.staged} onResize={onResize} onModeChange={onModeChange} focusable={zonesFocusable} onFocus={onFocus} onMove={onMove}>
        {zoneChip(
          stagedState,
          "STAGED · OFFLINE",
          "STAGED · …",
          "none staged",
          top4.length === 0,
          () => (
          <>
            {top4.map((l, i) => {
              const slot = stagedSlot(i);
              return (
                <OvCard
                  key={l.id}
                  x={slot.x}
                  y={slot.y}
                  w={slot.w}
                  klass="learn"
                  klassLabel="LEARN"
                  tag={l.tag}
                  score={typeof l.score === "number" ? l.score.toFixed(2) : String(l.score)}
                  title={l.title}
                  meta={l.id + " · " + l.agent}
                />
              );
            })}
            {stagedState?.isLive && learnings.length > 4 ? (
              <div
                className="more-chip"
                style={{ left: L.staged.moreChip.x, top: L.staged.moreChip.y }}
              >
                + {learnings.length - top4.length} more · click zone to expand
              </div>
            ) : null}
          </>
          ),
        )}
      </ZoneBox>

      <ZoneBox id="logs" z={zones.logs} scale={scale} mode={zmode.logs} onResize={onResize} onModeChange={onModeChange} focusable={zonesFocusable} onFocus={onFocus} onMove={onMove}>
        {zoneChip(
          logState,
          "LOG-ONLY · OFFLINE",
          "LOG-ONLY · …",
          "no log-only",
          logs.length === 0,
          () => (
          <>
            <OvCard
              x={L.logs.card.x}
              y={L.logs.card.y}
              w={L.logs.card.w}
              klass="log"
              klassLabel="LOG"
              tag="PRIVATE"
              tagCls="danger"
              score={logs[0].score > 0 ? logs[0].score.toFixed(2) : "—"}
              title={logs[0].title}
              meta={logs[0].id + " · " + logs[0].agent + " · personal leg"}
            />
            {logs.length > 1 ? (
              <div
                className="more-chip"
                style={{ left: L.logs.moreChip.x, top: L.logs.moreChip.y }}
              >
                + {logs.length - 1} more
              </div>
            ) : null}
          </>
          ),
        )}
      </ZoneBox>

      <ZoneBox id="quarantine" z={zones.quarantine} scale={scale} mode={zmode.quarantine} onResize={onResize} onModeChange={onModeChange} focusable={zonesFocusable} onFocus={onFocus} onMove={onMove}>
        {zoneChip(
          quarantineState,
          "QUARANTINE · OFFLINE",
          "QUARANTINE · …",
          "quarantine clear",
          denies.length === 0,
          () => (
            <OvCard
              x={L.quarantine.card.x}
              y={L.quarantine.card.y}
              w={L.quarantine.card.w}
              deny
              klass="deny"
              klassLabel="DENY"
              tag="DO-NOT-STORE"
              tagCls="warn"
              score={denies[0].score > 0 ? denies[0].score.toFixed(2) : "—"}
              title={denies[0].title}
              meta={denies[0].id + " · " + denies[0].agent}
            />
          ),
        )}
      </ZoneBox>

      <ZoneBox id="recall" z={zones.recall} scale={scale} mode={zmode.recall} onResize={onResize} onModeChange={onModeChange} focusable={zonesFocusable} onFocus={onFocus} onMove={onMove}>
        <svg className="bd-svg" aria-hidden="true">
          {L.recall.svgPaths.map((d) => (
            <path key={d} d={d} />
          ))}
        </svg>
        {zoneChip(
          recallState,
          "RECALL · OFFLINE",
          "RECALL · …",
          "",
          false,
          () => (
          <>
            <div className="qcard" style={{ left: L.recall.qcard.x, top: L.recall.qcard.y }}>
              <div className="ql">RECALL · LAST QUERY · CITED, NEVER OBEYED</div>
              <div className="qq">
                ▸ {recallState?.data?.query || recallState?.data?.message || "no recent recall"}
              </div>
              <div className="qn">
                {recallHits.length} result{recallHits.length === 1 ? "" : "s"}
                {recallState?.data?.present ? " · strong" : ""}
              </div>
            </div>
            {recallHits.slice(0, 3).map((r, i) => (
              <OvCard
                key={r.path + i}
                {...(L.recall.cards[i] || L.recall.cards[0])}
                tag={r.cls}
                tagCls={r.afm === "SAFE" ? "safe" : ""}
                score={r.score.toFixed(2)}
                title={r.sub}
                meta={r.path + " · " + r.age}
              />
            ))}
          </>
          ),
        )}
      </ZoneBox>
    </div>
  );
}
