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
  getAuditTail,
  type AuditTailResult,
  type HealthReport,
  type StatusReport,
} from "../api";
import { BoardOverview } from "../board/BoardOverview";
import { ZoneDetail } from "../board/BoardDetails";
import {
  BOARD_ORDER,
  BOARD_ZONES,
  SAMPLE_LEARNINGS,
  WORLD,
  type BoardFlow,
  type DaemonInfo,
  type FlowEvent,
  type ZoneDef,
  type ZoneId,
} from "../board/boardData";
import {
  useElementSize,
  usePersistentJSON,
  usePrefersReducedMotion,
} from "../board/boardHooks";
import {
  type Cam,
  classifyWheel,
  clampZonePosition,
  deriveDaemonInfo,
  dragPan,
  flowForAuditEntry,
  panByWheel,
  sanitizeZonePositions,
  zoomToward,
  type ZonePositions,
} from "../board/boardLogic";

type ZonePos = ZonePositions;

const K_FOCUS = "minni-board-focus";
const K_CAM = "minni-board-cam";
const K_ZPOS = "minni-board-zonepos";

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
}: {
  status: StatusReport | null;
  health: HealthReport | null;
  audit: AuditTailResult | null;
  /** Switch back to the v1 console shell. */
  onOpenConsole?: () => void;
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
  const [evt, setEvt] = useState<FlowEvent | null>(null);
  const [instant, setInstant] = useState(false);

  // ── live zone rects = defaults + dragged offsets ──
  const zones = useMemo(() => {
    const z = {} as Record<ZoneId, ZoneDef>;
    BOARD_ORDER.forEach((id) => {
      z[id] = { ...BOARD_ZONES[id], ...(zpos[id] || {}) };
    });
    return z;
  }, [zpos]);

  const moveZone = useCallback(
    (id: ZoneId, x: number, y: number) => {
      const next = clampZonePosition(id, x, y, BOARD_ZONES, WORLD);
      setZpos((p) => ({
        ...p,
        [id]: next,
      }));
    },
    [setZpos],
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
      if (e.key === "Escape") {
        setFocus(null);
        return;
      }
      if (typing) return;
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
  // The pulses on the links are real events (an agent pinging/recalling/
  // learning shows up as a dot travelling its line). Sample ambience only
  // runs as a fallback while the audit tail is unreachable.
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
          [...f, ...fresh.map((e) => ({ seq: ++seqRef.current, flow: flowForAuditEntry(e) }))].slice(-24),
        );
      } catch {
        if (!stopped) setAuditLive(false);
      }
    };
    void poll();
    const id = window.setInterval(() => void poll(), 8000);
    return () => {
      stopped = true;
      window.clearInterval(id);
    };
  }, []);

  const hasLayout = Object.keys(zpos).length > 0;

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
          zonesFocusable={!focus}
          flowsRunning={!focus}
          reducedMotion={reducedMotion}
          ambientFlows={auditLive === false}
          flowFeed={flowFeed}
          onFlowEvent={setEvt}
        />
      </div>

      {/* fixed chrome */}
      <div className="chrome">
        <button className="bd-title" onClick={() => setFocus(null)} type="button">
          <span className="rune">⬢</span>
          <span className="bn">minni</span>
          <span className="bm">
            {auditLive ? "memory board · live" : "memory board · dry-run"}
          </span>
        </button>
        <span className={"bd-chip " + (daemon.online ? "safe" : "danger")}>
          <span className="dot" />
          {daemon.online ? "DAEMON ONLINE" : "DAEMON OFFLINE"}
        </span>
        <span className="bd-chip warn">SAMPLE · {SAMPLE_LEARNINGS.length} STAGED</span>
        {hasLayout ? (
          <button className="fchip" onClick={() => setZpos({})} type="button">
            reset layout
          </button>
        ) : null}
        {cam && !focus ? (
          <button className="fchip" onClick={() => setCam(null)} type="button">
            fit view
          </button>
        ) : null}
        {onOpenConsole ? (
          <button className="fchip" onClick={onOpenConsole} type="button">
            console v1
          </button>
        ) : null}
        {!focus ? (
          <span className="hint">
            drag zones · click to zoom · scroll to zoom · drag canvas to pan · ←/→ tour
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
                  <span className="zo-title">{BOARD_ZONES[id].title}</span>
                  <div className="zo-pills">
                    {BOARD_ORDER.filter((o) => o !== id).map((o) => (
                      <button key={o} className="fchip" onClick={() => setFocus(o)} type="button">
                        {BOARD_ZONES[o].title}
                      </button>
                    ))}
                  </div>
                </div>
                <div className="zo-body">
                  <ZoneDetail id={id} ctx={{ daemon, recentAudit: auditSummaries }} />
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
