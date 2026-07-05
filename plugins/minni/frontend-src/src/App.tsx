import { useEffect, useMemo, useState } from "react";
import {
  AuthRequiredError,
  getAuditTail,
  getHealth,
  getStatus,
  type AuditTailResult,
  type EvidenceRow,
  type HealthReport,
  type PreparedOutcomePacket,
  type PreparedTaskPacket,
  type StatusReport,
  type TaskProfile,
} from "./api";
import { Inspector } from "./components/Inspector";
import { Rail } from "./components/Rail";
import { ResizeHandle } from "./components/ResizeHandle";
import { StatusBand, deriveStatusStats } from "./components/StatusBand";
import { TokenGate } from "./components/TokenGate";
import {
  ActivityStream,
  TelemetryRail,
} from "./components/PhosphorOperator";
import {
  TweakButton,
  TweakRadio,
  TweakSection,
  TweaksPanel,
} from "./components/TweaksPanel";
import { useLayoutSize, resetAllLayout } from "./hooks/useLayoutSize";
import { useTweaks } from "./hooks/useTweaks";
import { AuditScreen } from "./screens/AuditScreen";
import { BoardScreen } from "./screens/BoardScreen";
import { DryrunScreen } from "./screens/DryrunScreen";
import { PacketScreen } from "./screens/PacketScreen";
import { RecallScreen } from "./screens/RecallScreen";
import { SettingsScreen } from "./screens/SettingsScreen";
import {
  HandoffsScreen,
  PolicyScreen,
  VaultsScreen,
} from "./screens/UnwiredScreens";

type ScreenId =
  | "recall"
  | "packet"
  | "dryrun"
  | "board"
  | "handoffs"
  | "vaults"
  | "audit"
  | "policy"
  | "settings";

export function App() {
  const [tweaks, setTweak] = useTweaks();
  // The Memory Board is the frontend: it is the landing view. The v1 console
  // shell stays intact underneath — reachable via the board's "console v1"
  // chip, and the rail's Board entry leads back here.
  const [active, setActive] = useState<ScreenId>("board");
  const [query, setQuery] = useState("");
  const [profile, setProfile] = useState<TaskProfile>("standard");
  const [packet, setPacket] = useState<PreparedTaskPacket | null>(null);
  const [evidence, setEvidence] = useState<EvidenceRow[]>([]);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [focusId, setFocusId] = useState<string | null>(null);
  const [outcome, setOutcome] = useState<PreparedOutcomePacket | null>(null);

  const [status, setStatus] = useState<StatusReport | null>(null);
  const [health, setHealth] = useState<HealthReport | null>(null);
  const [audit, setAudit] = useState<AuditTailResult | null>(null);
  const [statusLoading, setStatusLoading] = useState(true);
  const [statusError, setStatusError] = useState<string | null>(null);
  const [authRequired, setAuthRequired] = useState(false);
  const [tokenRefreshTrigger, setTokenRefreshTrigger] = useState(0);

  const [railW, setRailW] = useLayoutSize("railW", 248);
  const [inspW, setInspW] = useLayoutSize("inspW", 384);
  const [activityW, setActivityW] = useLayoutSize("activityW", 320);

  const refreshStatus = async (mounted: () => boolean) => {
    if (!mounted()) return;
    setStatusLoading(true);
    setStatusError(null);
    const [s, h, a] = await Promise.allSettled([
      getStatus(),
      getHealth(),
      getAuditTail(20),
    ]);
    if (!mounted()) return;
    if (s.status === "fulfilled") {
      setStatus(s.value);
      setAuthRequired(false);
    } else {
      setStatus(null);
      setStatusError(s.reason instanceof Error ? s.reason.message : String(s.reason));
      if (s.reason instanceof AuthRequiredError) setAuthRequired(true);
    }
    setHealth(h.status === "fulfilled" ? h.value : null);
    setAudit(a.status === "fulfilled" ? a.value : null);
    setStatusLoading(false);
  };

  useEffect(() => {
    let alive = true;
    const mounted = () => alive;
    void refreshStatus(mounted);
    const id = window.setInterval(() => void refreshStatus(mounted), 30_000);
    return () => {
      alive = false;
      window.clearInterval(id);
    };
  }, []);

  const focusSource = useMemo(
    () => evidence.find((r) => r.id === focusId),
    [focusId, evidence],
  );

  const effectiveInspector =
    tweaks.theme === "phosphor" ? "overlay" : tweaks.inspector;
  const showInspector = active === "recall";
  const isPhosphor = tweaks.theme === "phosphor";

  const counts: Record<string, string> = {
    recall: evidence.length ? String(evidence.length) : "",
    packet: selected.size ? `${selected.size} incl` : "",
    dryrun: outcome ? String(
      outcome.outcomeDraft.learnCandidates.length +
        outcome.outcomeDraft.logOnly.length +
        outcome.outcomeDraft.expires.length +
        outcome.outcomeDraft.doNotStore.length,
    ) : "",
    audit: audit ? String(audit.entries.length) : "",
    __vault: status?.vault.exists ? "ready" : "—",
    __audit: status ? `${status.audit.entries}` : "—",
  };

  const stats = deriveStatusStats(status, health);

  const renderScreen = () => {
    switch (active) {
      case "recall":
        return (
          <RecallScreen
            selected={selected}
            setSelected={setSelected}
            focusId={focusId}
            setFocusId={setFocusId}
            query={query}
            setQuery={setQuery}
            profile={profile}
            setProfile={setProfile}
            packet={packet}
            setPacket={setPacket}
            evidence={evidence}
            setEvidence={setEvidence}
          />
        );
      case "packet":
        return <PacketScreen packet={packet} evidence={evidence} selected={selected} />;
      case "dryrun":
        return (
          <DryrunScreen
            layout={tweaks.dryrunLayout}
            outcome={outcome}
            setOutcome={setOutcome}
            defaultTask={query || (packet ? packet.task : "")}
          />
        );
      case "handoffs":
        return <HandoffsScreen />;
      case "vaults":
        return <VaultsScreen />;
      case "audit":
        return <AuditScreen />;
      case "policy":
        return <PolicyScreen />;
      case "settings":
        return (
          <SettingsScreen
            status={status}
            health={health}
            loading={statusLoading}
            error={statusError}
            theme={tweaks.theme}
            onThemeChange={(theme) => setTweak("theme", theme)}
            onRefresh={() => void refreshStatus(() => true)}
          />
        );
      default:
        return null;
    }
  };

  // Auth gate: the static shell loads without a token, but the API is locked.
  // Nothing meaningful renders until a valid token is stored.
  if (authRequired) {
    return (
      <div className="app board-app" data-screen="token-gate" data-theme-layout="default">
        <TokenGate onSubmit={() => {
        void refreshStatus(() => true);
        setTokenRefreshTrigger(prev => prev + 1);
      }} />
      </div>
    );
  }

  // Board mode: full-viewport, no rail/band chrome — the board IS the UI.
  if (active === "board") {
    return (
      <div
        className="app board-app"
        data-screen="board"
        data-density={tweaks.density}
        data-theme-layout={isPhosphor ? "operator" : "default"}
      >
        <BoardScreen
          status={status}
          health={health}
          audit={audit}
          onOpenConsole={() => setActive("recall")}
          tokenRefreshTrigger={tokenRefreshTrigger}
        />
      </div>
    );
  }

  const gridStyle: React.CSSProperties = {
    "--rail-w": railW + "px",
    "--insp-w": inspW + "px",
  } as React.CSSProperties;

  return (
    <div
      className="app"
      data-density={tweaks.density}
      data-inspector={effectiveInspector}
      data-band={tweaks.band}
      data-theme-layout={isPhosphor ? "operator" : "default"}
      data-screen={active}
      style={gridStyle}
    >
      <Rail active={active} onSelect={(id) => setActive(id as ScreenId)} counts={counts} />

      <StatusBand
        stats={stats}
        actions={
          <>
            <button
              className="btn btn-secondary btn-sm"
              type="button"
              onClick={() => void refreshStatus(() => true)}
              disabled={statusLoading}
            >
              {statusLoading ? "…" : "Refresh"}
            </button>
            <button
              className="btn btn-primary btn-sm"
              type="button"
              onClick={() => setActive("recall")}
            >
              Recall
            </button>
          </>
        }
      />

      <ResizeHandle
        axis="x"
        side="right"
        value={railW}
        onChange={(v) => setRailW(v ?? 248)}
        min={180}
        max={400}
        className="resize-rail"
      />

      <main className="main">
        {isPhosphor && active === "recall" ? (
          <div className="operator-grid" style={{ gridTemplateColumns: `1fr ${activityW}px` }}>
            <div className="operator-main">{renderScreen()}</div>
            <div style={{ position: "relative" }}>
              <ResizeHandle
                axis="x"
                side="left"
                value={activityW}
                onChange={(v) => setActivityW(v ?? 320)}
                min={220}
                max={520}
              />
              <ActivityStream entries={audit?.entries || []} />
            </div>
          </div>
        ) : (
          renderScreen()
        )}
      </main>

      {isPhosphor && (
        <TelemetryRail
          focusSource={focusSource}
          auditCount={status?.audit.entries ?? 0}
          budgetTokens={packet?.budget.tokens ?? 0}
          usedTokens={packet?.budgetTokens ?? 0}
        />
      )}

      {showInspector && effectiveInspector === "right" && (
        <>
          <ResizeHandle
            axis="x"
            side="left"
            value={inspW}
            onChange={(v) => setInspW(v ?? 384)}
            min={280}
            max={640}
            className="resize-insp"
          />
          <Inspector source={focusSource} mode="right" />
        </>
      )}
      {showInspector && effectiveInspector === "bottom" && (
        <Inspector source={focusSource} mode="bottom" />
      )}
      {showInspector && effectiveInspector === "overlay" && focusSource && (
        <Inspector
          source={focusSource}
          mode="overlay"
          onClose={() => setFocusId(null)}
        />
      )}

      <TweaksPanel title="Tweaks">
        <TweakSection label="Theme">
          <TweakRadio<typeof tweaks.theme>
            label="Theme"
            value={tweaks.theme}
            options={[
              { value: "paper", label: "Paper" },
              { value: "phosphor", label: "Phosphor" },
            ]}
            onChange={(v) => setTweak("theme", v)}
          />
        </TweakSection>
        <TweakSection label="Layout">
          <TweakRadio<typeof tweaks.density>
            label="Density"
            value={tweaks.density}
            options={[
              { value: "comfortable", label: "Comfortable" },
              { value: "compact", label: "Compact" },
            ]}
            onChange={(v) => setTweak("density", v)}
          />
          <TweakRadio<typeof tweaks.inspector>
            label="Inspector"
            value={tweaks.inspector}
            options={[
              { value: "right", label: "Right" },
              { value: "bottom", label: "Bottom" },
              { value: "overlay", label: "Overlay" },
            ]}
            onChange={(v) => setTweak("inspector", v)}
          />
          <TweakRadio<typeof tweaks.band>
            label="Status band"
            value={tweaks.band}
            options={[
              { value: "paper", label: "Paper" },
              { value: "graphite", label: "Graphite" },
            ]}
            onChange={(v) => setTweak("band", v)}
          />
          <TweakButton onClick={resetAllLayout}>Reset panel sizes</TweakButton>
        </TweakSection>
        <TweakSection label="Dry-run review">
          <TweakRadio<typeof tweaks.dryrunLayout>
            label="Layout"
            value={tweaks.dryrunLayout}
            options={[
              { value: "columns", label: "3 columns" },
              { value: "accordion", label: "Stacked" },
              { value: "tray", label: "Tray" },
            ]}
            onChange={(v) => setTweak("dryrunLayout", v)}
          />
        </TweakSection>
      </TweaksPanel>
    </div>
  );
}
