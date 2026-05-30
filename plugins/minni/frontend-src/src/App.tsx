import { useEffect, useMemo, useState } from "react";
import {
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
  | "handoffs"
  | "vaults"
  | "audit"
  | "policy"
  | "settings";

export function App() {
  const [tweaks, setTweak] = useTweaks();
  const [active, setActive] = useState<ScreenId>("recall");
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

  const [railW, setRailW] = useLayoutSize("railW", 248);
  const [inspW, setInspW] = useLayoutSize("inspW", 384);
  const [activityW, setActivityW] = useLayoutSize("activityW", 320);

  const refreshStatus = async () => {
    setStatusLoading(true);
    setStatusError(null);
    try {
      const [s, h, a] = await Promise.all([
        getStatus().catch((err) => {
          throw err;
        }),
        getHealth().catch(() => null),
        getAuditTail(20).catch(() => null),
      ]);
      setStatus(s);
      setHealth(h);
      setAudit(a);
    } catch (err) {
      setStatusError(err instanceof Error ? err.message : String(err));
    } finally {
      setStatusLoading(false);
    }
  };

  useEffect(() => {
    void refreshStatus();
    const id = window.setInterval(() => void refreshStatus(), 30_000);
    return () => window.clearInterval(id);
  }, []);

  const focusSource = useMemo(
    () => evidence.find((r) => r.id === focusId),
    [focusId, evidence],
  );

  const effectiveInspector =
    tweaks.theme === "phosphor" ? "overlay" : tweaks.inspector;
  const showInspector = active === "recall";

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
            onRefresh={() => void refreshStatus()}
          />
        );
      default:
        return null;
    }
  };

  const isPhosphor = tweaks.theme === "phosphor";
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
              onClick={() => void refreshStatus()}
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
