// Minni Memory Board — infinite-canvas prezi shell (camera, morph, chrome).
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
} from "react";
import {
  AuthRequiredError,
  getAuditTail,
  type AuditTailResult,
  type HealthReport,
  type StatusReport,
} from "../api";
import {
  useAgents,
  useLogOnly,
  useQuarantine,
  useRecallState,
  useStagedLearnings,
} from "../board/boardDataHook";
import { BoardOverview } from "../board/BoardOverview";
import { ZoneDetail } from "../board/BoardDetails";
import {
  BOARD_ORDER,
  BOARD_ZONES,
  WORLD,
  zoneLabel,
  type BoardFlow,
  type DaemonInfo,
  type FlowEvent,
  type ZoneDef,
  type ZoneId,
  type ZoneStatus,
} from "../board/boardData";
import {
  useElementSize,
  usePersistentJSON,
  usePrefersReducedMotion,
} from "../board/boardHooks";
import {
  type Cam,
  classifyWheel,
  clampZoneWH,
  clampZoneXY,
  deriveDaemonInfo,
  dragPan,
  flowForAuditEntry,
  panByWheel,
  sanitizeZoneModes,
  sanitizeZonePositions,
  zoomToward,
  type ZoneMode,
  type ZoneModes,
  type ZonePositions,
} from "../board/boardLogic";

type ZonePos = ZonePositions;

const K_FOCUS = "minni-board-focus";
const K_CAM = "minni-board-cam";
const K_ZPOS = "minni-board-zonepos";
const K_ZMODE = "minni-board-zonemode";
// Obsolete freeform-text store from the previous design revision.
const K_ZCUSTOM_LEGACY = "minni-board-zonecustom";

function isZoneId(v: unknown): v is ZoneId {
  return typeof v === "string" && Object.prototype.hasOwnProperty.call(BOARD_ZONES, v);
}

// Turn a raw audit entry (markdown block or JSON) into a compact ticker line.
function summarizeAudit(raw: string): string {
  const first = raw.split(/\r?\n/, 1)[0] || raw;
  const m = /^##\s+\[(?<ts>[^\]]+)\]\s+(?<tool>[^|]+?)\s*(?:\|\s*(?<summary>.*))?$/.exec(first);
  if (m && m.groups) {
    const time = (m.groups.ts || "").includes("T") ? m.groups.ts.slice(11, 19) : m.groups.ts;
    const tool = (m.groups.tool || "").trim();
    const summary = (m.groups.summary || "").trim();
    return `${time} · ${tool}${summary ? " · " + summary : ""}`.toUpperCase();
  }
  return (first.length > 72 ? first.slice(0, 72) + "…" : first).toUpperCase();
}

export function BoardScreen({
  status,
  health,
  audit,
  onOpenConsole,
  onOpenRecall,
  onAuthRequired,
  tokenRefreshTrigger,
  theme,
  onThemeChange,
}: {
  status: StatusReport | null;
  health: HealthReport | null;
  audit: AuditTailResult | null;
  /** Switch back to the v1 console shell. */
  onOpenConsole?: () => void;
  /** Deep-link into console Recall with an optional query (board Recall zone). */
  onOpenRecall?: (query?: string) => void;
  /** The console token was rejected — raise the token gate now. */
  onAuthRequired?: () => void;
  tokenRefreshTrigger?: number;
  /** Active console theme; enables the dark/light chrome toggle when set. */
  theme?: "paper" | "phosphor";
  onThemeChange?: (theme: "paper" | "phosphor") => void;
}) {
  const [stageRef, vp] = useElementSize();
  const reducedMotion = usePrefersReducedMotion();

  const [focus, setFocus] = usePersistentJSON<ZoneId | null>(K_FOCUS, null, (v) =>
    isZoneId(v) ? v : null,
  );
  const [cam, setCam] = usePersistentJSON<Cam | null>(K_CAM, null, (v) => {
    if (v && typeof v === "object" && "s" in v) return v as Cam;
    return null;
  });
  const [zpos, setZpos] = usePersistentJSON<ZonePos>(K_ZPOS, {}, (v) =>
    sanitizeZonePositions(v, BOARD_ZONES, WORLD) || {},
  );
  const [zmode, setZmode] = usePersistentJSON<ZoneModes>(K_ZMODE, {}, (v) =>
    sanitizeZoneModes(v, BOARD_ZONES) || {},
  );
  const [evt, setEvt] = useState<FlowEvent | null>(null);
  const [instant, setInstant] = useState(false);

  // The freeform-text store from the previous revision is gone for good.
  useEffect(() => {
    try {
      localStorage.removeItem(K_ZCUSTOM_LEGACY);
    } catch {
      /* private mode */
    }
  }, []);

  // ── live zone data (fail-loud; no sample fallbacks) ──
  // AuthRequiredError from any zone raises TokenGate via onAuthRequired.
  const stagedState = useStagedLearnings(tokenRefreshTrigger, onAuthRequired);
  const agentsState = useAgents(tokenRefreshTrigger, onAuthRequired);
  const logState = useLogOnly(tokenRefreshTrigger, onAuthRequired);
  const quarantineState = useQuarantine(tokenRefreshTrigger, onAuthRequired);
  const recallState = useRecallState(tokenRefreshTrigger, onAuthRequired);

  const zones = useMemo(() => {
    const z = {} as Record<ZoneId, ZoneDef>;
    // Danger only when truly offline (not initial loading).
    const offlineStatus = (
      live: boolean,
      loading: boolean,
      error: string | null,
    ): ZoneStatus | undefined => {
      if (live) return undefined;
      if (loading && !error) return undefined;
      return "danger";
    };
    const titleFor = (base: string, live: boolean, loading: boolean, error: string | null) => {
      if (live) return base;
      if (loading && !error) return `${base} · loading`;
      return `${base} · offline`;
    };

    BOARD_ORDER.forEach((id) => {
      z[id] = { ...BOARD_ZONES[id], ...(zpos[id] || {}) };
    });

    z.agents = {
      ...z.agents,
      label: zoneLabel("RUNTIMES", {
        isLive: agentsState.isLive,
        count: agentsState.data.length,
        loading: agentsState.loading,
        error: agentsState.error,
      }),
      title: titleFor("Runtimes", agentsState.isLive, agentsState.loading, agentsState.error),
      status:
        offlineStatus(agentsState.isLive, agentsState.loading, agentsState.error) ??
        z.agents.status,
    };
    z.staged = {
      ...z.staged,
      label: zoneLabel("STAGED · LEARN CANDIDATES", {
        isLive: stagedState.isLive,
        count: stagedState.learnings.length,
        loading: stagedState.loading,
        error: stagedState.error,
      }),
      title: titleFor(
        "Staged learnings",
        stagedState.isLive,
        stagedState.loading,
        stagedState.error,
      ),
      status:
        offlineStatus(stagedState.isLive, stagedState.loading, stagedState.error) ??
        z.staged.status,
    };
    z.logs = {
      ...z.logs,
      label: zoneLabel("LOG-ONLY · PERSONAL", {
        isLive: logState.isLive,
        count: logState.data.length,
        loading: logState.loading,
        error: logState.error,
      }),
      title: titleFor("Log-only", logState.isLive, logState.loading, logState.error),
      status: offlineStatus(logState.isLive, logState.loading, logState.error) ?? z.logs.status,
    };
    z.quarantine = {
      ...z.quarantine,
      label: zoneLabel("QUARANTINE · DO-NOT-STORE", {
        isLive: quarantineState.isLive,
        count: quarantineState.data.length,
        loading: quarantineState.loading,
        error: quarantineState.error,
      }),
      title: titleFor(
        "Quarantine",
        quarantineState.isLive,
        quarantineState.loading,
        quarantineState.error,
      ),
      // Quarantine zone is always danger-colored for the frame; content still fail-loud.
      status: "danger",
    };
    z.recall = {
      ...z.recall,
      label: zoneLabel("RECALL · LAST QUERY", {
        isLive: recallState.isLive,
        count: recallState.data.results.length,
        loading: recallState.loading,
        error: recallState.error,
      }),
      title: titleFor("Recall", recallState.isLive, recallState.loading, recallState.error),
      status:
        offlineStatus(recallState.isLive, recallState.loading, recallState.error) ??
        z.recall.status,
    };

    return z;
  }, [
    zpos,
    agentsState.isLive,
    agentsState.loading,
    agentsState.error,
    agentsState.data.length,
    stagedState.isLive,
    stagedState.loading,
    stagedState.error,
    stagedState.learnings.length,
    logState.isLive,
    logState.loading,
    logState.error,
    logState.data.length,
    quarantineState.isLive,
    quarantineState.loading,
    quarantineState.error,
    quarantineState.data.length,
    recallState.isLive,
    recallState.loading,
    recallState.error,
    recallState.data.results.length,
  ]);

  // Any drag or resize flips the box to custom (free) layout.
  const moveZone = useCallback(
    (id: ZoneId, x: number, y: number) => {
      setZmode((m) => (m[id] === "custom" ? m : { ...m, [id]: "custom" }));
      setZpos((p) => {
        const cur = { ...BOARD_ZONES[id], ...(p[id] || {}) };
        return { ...p, [id]: { ...cur, ...clampZoneXY(cur.w, cur.h, x, y, WORLD) } };
      });
    },
    [setZpos, setZmode],
  );

  const resizeZone = useCallback(
    (id: ZoneId, w: number, h: number) => {
      setZmode((m) => (m[id] === "custom" ? m : { ...m, [id]: "custom" }));
      setZpos((p) => {
        const cur = { ...BOARD_ZONES[id], ...(p[id] || {}) };
        return { ...p, [id]: { ...cur, ...clampZoneWH(w, h, WORLD) } };
      });
    },
    [setZpos, setZmode],
  );

  // auto = automatic layout (clears this box's override); custom = free layout.
  const setZoneMode = useCallback(
    (id: ZoneId, mode: ZoneMode) => {
      setZmode((p) => ({ ...p, [id]: mode }));
      if (mode === "auto")
        setZpos((p) => {
          if (!(id in p)) return p;
          const n = { ...p };
          delete n[id];
          return n;
        });
    },
    [setZpos, setZmode],
  );

  // ── base fit + effective camera ──
  const s0 = Math.min(vp.w / WORLD.w, vp.h / WORLD.h) * 0.96;
  const ox = (vp.w - WORLD.w * s0) / 2;
  const oy = (vp.h - WORLD.h * s0) / 2;
  const camEff: Cam = cam || { x: ox, y: oy, s: s0 };

  const camRef = useRef(camEff);
  camRef.current = camEff;
  const focusRef = useRef(focus);
  focusRef.current = focus;
  const idleT = useRef<number | undefined>(undefined);

  const bump = useCallback(() => {
    setInstant(true);
    window.clearTimeout(idleT.current);
    idleT.current = window.setTimeout(() => setInstant(false), 220);
  }, []);

  // ── wheel = zoom toward cursor / trackpad pan (native listener) ──
  useEffect(() => {
    const el = stageRef.current;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      if (focusRef.current) return; // let detail views scroll normally
      e.preventDefault();
      const c = camRef.current;
      const rect = el.getBoundingClientRect();
      const px = e.clientX - rect.left;
      const py = e.clientY - rect.top;
      // trackpad two-finger scroll → pan; mouse wheel or pinch (ctrl) → zoom.
      bump();
      if (classifyWheel(e) === "pan") {
        setCam(panByWheel(c, e.deltaX, e.deltaY));
        return;
      }
      setCam(zoomToward(c, px, py, e.deltaY, e.ctrlKey, s0));
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, [s0, bump, setCam, stageRef]);

  // ── drag empty canvas to pan ──
  const pan = useRef<{ sx: number; sy: number; x: number; y: number } | null>(null);
  const onStageDown = (e: React.PointerEvent<HTMLDivElement>) => {
    if (focus) return;
    if ((e.target as HTMLElement).closest(".zone, .chrome, .zoverlay, .ticker")) return;
    e.currentTarget.setPointerCapture(e.pointerId);
    const c = camRef.current;
    pan.current = { sx: e.clientX, sy: e.clientY, x: c.x, y: c.y };
  };
  const onStageMove = (e: React.PointerEvent<HTMLDivElement>) => {
    const p = pan.current;
    if (!p) return;
    bump();
    setCam(dragPan(p, e.clientX, e.clientY, camRef.current.s));
  };
  const onStageUp = () => {
    pan.current = null;
  };

  // ── world transform (overview pan/zoom or focus morph) ──
  const worldStyle: CSSProperties = useMemo(() => {
    const base: CSSProperties = instant ? { transition: "none" } : {};
    if (!focus) {
      return {
        ...base,
        transform: `translate(${camEff.x}px, ${camEff.y}px) scale(${camEff.s})`,
        opacity: 1,
      };
    }
    const z = zones[focus];
    const s1 = Math.min(vp.w / z.w, vp.h / z.h) * 0.94;
    const tx = (vp.w - z.w * s1) / 2 - z.x * s1;
    const ty = (vp.h - z.h * s1) / 2 - z.y * s1;
    return { ...base, transform: `translate(${tx}px, ${ty}px) scale(${s1})`, opacity: 0 };
  }, [focus, zones, vp.w, vp.h, camEff.x, camEff.y, camEff.s, instant]);

  // ── per-zone overlay morph (from zone screen rect to fullscreen) ──
  const overlayStyle = useCallback(
    (id: ZoneId): CSSProperties => {
      const z = zones[id];
      if (focus === id) {
        return { opacity: 1, pointerEvents: "auto", transform: "translate(0px, 0px) scale(1, 1)" };
      }
      const sx = camEff.x + z.x * camEff.s;
      const sy = camEff.y + z.y * camEff.s;
      return {
        opacity: 0,
        pointerEvents: "none",
        transform: `translate(${sx}px, ${sy}px) scale(${(z.w * camEff.s) / vp.w}, ${
          (z.h * camEff.s) / vp.h
        })`,
      };
    },
    [focus, zones, camEff.x, camEff.y, camEff.s, vp.w, vp.h],
  );

  // ── keyboard: ESC exits, ←/→ tours zones in BOARD_ORDER ──
  useEffect(() => {
    const on = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement | null;
      const typing =
        target &&
        (target.tagName === "INPUT" ||
          target.tagName === "TEXTAREA" ||
          target.isContentEditable);
      if (typing) return;
      if (e.key === "Escape") {
        setFocus(null);
        return;
      }
      if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
      if (!focus) {
        setFocus(BOARD_ORDER[0]);
        return;
      }
      const i = BOARD_ORDER.indexOf(focus);
      const n =
        e.key === "ArrowRight"
          ? (i + 1) % BOARD_ORDER.length
          : (i - 1 + BOARD_ORDER.length) % BOARD_ORDER.length;
      setFocus(BOARD_ORDER[n]);
    };
    window.addEventListener("keydown", on);
    return () => window.removeEventListener("keydown", on);
  }, [focus, setFocus]);

  // ── real daemon summary: never backfills missing API fields with samples ──
  const daemon: DaemonInfo = useMemo(() => deriveDaemonInfo(status, health), [status, health]);

  const auditSummaries = useMemo(
    () => (audit?.entries || []).map(summarizeAudit).reverse(),
    [audit],
  );

  // Seed the ticker with a real recent audit op before live pulses take over.
  const seededRef = useRef(false);
  useEffect(() => {
    if (seededRef.current) return;
    const latest = auditSummaries[0];
    if (latest) {
      seededRef.current = true;
      setEvt({ n: 0, label: latest, color: "var(--blue)" });
    }
  }, [auditSummaries]);

  // ── live traffic: poll the audit tail and turn NEW entries into pulses ──
  // No synthetic ambient flows — quiet board when audit is empty/unreachable.
  const [flowFeed, setFlowFeed] = useState<{ seq: number; flow: BoardFlow }[]>([]);
  const [auditLive, setAuditLive] = useState<boolean | null>(null);
  const seenRef = useRef<Set<string> | null>(null);
  const seqRef = useRef(0);
  useEffect(() => {
    let stopped = false;
    const poll = async () => {
      try {
        const a = await getAuditTail(20);
        if (stopped) return;
        setAuditLive(true);
        const entries = a.entries || [];
        if (!seenRef.current) {
          // First poll: everything is history, not news — no pulse flood.
          seenRef.current = new Set(entries);
          return;
        }
        const seen = seenRef.current;
        const fresh = entries.filter((e) => !seen.has(e));
        if (!fresh.length) return;
        fresh.forEach((e) => seen.add(e));
        if (seen.size > 400) seenRef.current = new Set(entries);
        setFlowFeed((f) =>
          [...f, ...fresh.map((e) => ({ seq: ++seqRef.current, flow: flowForAuditEntry(e) }))].slice(
            -24,
          ),
        );
      } catch (err) {
        if (stopped) return;
        setAuditLive(false);
        if (err instanceof AuthRequiredError) onAuthRequired?.();
      }
    };
    void poll();
    const id = window.setInterval(() => void poll(), 8000);
    return () => {
      stopped = true;
      window.clearInterval(id);
    };
  }, [onAuthRequired]);

  const hasLayout = Object.keys(zpos).length > 0 || Object.keys(zmode).length > 0;

  return (
    <div
      className={"board stage" + (!focus ? " pannable" : "")}
      ref={stageRef}
      onPointerDown={onStageDown}
      onPointerMove={onStageMove}
      onPointerUp={onStageUp}
      onPointerCancel={onStageUp}
      onDoubleClick={(e) => {
        if (!focus && !(e.target as HTMLElement).closest(".zone, .chrome, .zoverlay")) {
          setCam(null);
        }
      }}
    >
      <div className="world" style={worldStyle} aria-hidden={!!focus}>
        <BoardOverview
          zones={zones}
          scale={camEff.s}
          daemon={daemon}
          onFocus={setFocus}
          onMove={moveZone}
          onResize={resizeZone}
          zmode={zmode}
          onModeChange={setZoneMode}
          zonesFocusable={!focus}
          flowsRunning={!focus}
          reducedMotion={reducedMotion}
          flowFeed={flowFeed}
          onFlowEvent={setEvt}
          stagedState={stagedState}
          agentsState={agentsState}
          logState={logState}
          quarantineState={quarantineState}
          recallState={recallState}
        />
      </div>

      {/* fixed chrome */}
      <div className="chrome">
        <button className="bd-title" onClick={() => setFocus(null)} type="button">
          <span className="rune">⬢</span>
          <span className="bn">minni</span>
          <span className="bm">
            {auditLive === false
              ? "memory board · audit offline"
              : auditLive
                ? "memory board · live"
                : "memory board · connecting"}
          </span>
        </button>
        <span className={"bd-chip " + (daemon.online ? "safe" : "danger")}>
          <span className="dot" />
          {daemon.online ? "DAEMON ONLINE" : "DAEMON OFFLINE"}
        </span>
        <span
          className={
            "bd-chip " +
            (stagedState.isLive
              ? ""
              : stagedState.loading && !stagedState.error
                ? "warn"
                : "danger")
          }
        >
          {stagedState.isLive
            ? `${stagedState.learnings.length} STAGED`
            : stagedState.loading && !stagedState.error
              ? "STAGED · …"
              : "STAGED · OFFLINE"}
        </span>
        {hasLayout ? (
          <button
            className="fchip"
            onClick={() => {
              setZpos({});
              setZmode({});
            }}
            type="button"
          >
            reset layout
          </button>
        ) : null}
        {cam && !focus ? (
          <button className="fchip" onClick={() => setCam(null)} type="button">
            fit view
          </button>
        ) : null}
        {theme && onThemeChange ? (
          <button
            className="fchip"
            onClick={() => onThemeChange(theme === "phosphor" ? "paper" : "phosphor")}
            type="button"
            aria-label={theme === "phosphor" ? "Switch to light theme" : "Switch to dark theme"}
          >
            {theme === "phosphor" ? "☀ light" : "☾ dark"}
          </button>
        ) : null}
        {onOpenConsole ? (
          <button className="fchip" onClick={onOpenConsole} type="button">
            console v1
          </button>
        ) : null}
        {!focus ? (
          <span className="hint">
            drag/resize zones · click to zoom · scroll to zoom · drag canvas to pan · ←/→ tour
          </span>
        ) : null}
      </div>

      {/* live traffic ticker */}
      {!focus && evt ? (
        <div className="ticker" key={evt.n}>
          <span className="dot" style={{ background: evt.color }} />
          {evt.label}
        </div>
      ) : null}

      {/* zone overlays — morph out of their zone rect */}
      {BOARD_ORDER.map((id) => {
        const open = focus === id;
        return (
          <div
            key={id}
            className={"zoverlay" + (open ? " open" : "")}
            style={overlayStyle(id)}
            aria-hidden={!open}
          >
            {open ? (
              <>
                <div className="zo-hd">
                  <button className="bd-btn quiet" onClick={() => setFocus(null)} type="button">
                    ← Board <kbd>ESC</kbd>
                  </button>
                  <span className="zo-title">{zones[id].title}</span>
                  <div className="zo-pills">
                    {BOARD_ORDER.filter((o) => o !== id).map((o) => (
                      <button key={o} className="fchip" onClick={() => setFocus(o)} type="button">
                        {zones[o].title}
                      </button>
                    ))}
                  </div>
                </div>
                <div className="zo-body">
                  <ZoneDetail
                    id={id}
                    ctx={{ daemon, recentAudit: auditSummaries }}
                    stagedState={stagedState}
                    agentsState={agentsState}
                    logState={logState}
                    quarantineState={quarantineState}
                    recallState={recallState}
                    onOpenRecall={onOpenRecall}
                  />
                </div>
              </>
            ) : (
              <div className="zo-body" />
            )}
          </div>
        );
      })}
    </div>
  );
}
