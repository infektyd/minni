import type { HealthReport, StatusReport } from "../api";
import {
  ArchivalBand,
  PanelHeader,
  StateBanner,
} from "../components/atoms";
import { Chip } from "../components/Chip";

interface Props {
  status: StatusReport | null;
  health: HealthReport | null;
  loading: boolean;
  error: string | null;
  onRefresh: () => void;
}

function dot(ok: boolean | undefined) {
  return (
    <span
      style={{
        width: 8,
        height: 8,
        borderRadius: 8,
        background: ok ? "var(--verdigris)" : "var(--mustard-dark)",
        display: "inline-block",
      }}
    />
  );
}

export function SettingsScreen({ status, health, loading, error, onRefresh }: Props) {
  return (
    <>
      <ArchivalBand
        eyebrow="SETTINGS · HEALTH · LOCAL"
        title="Daemon, vault, and bridge"
        meta={[
          { k: "BRIDGE", v: health?.ok ? `:${health.port ?? "?"}` : "offline" },
          { k: "VAULT", v: status?.vault.exists ? "ready" : "missing" },
          { k: "AUDIT", v: status ? `${status.audit.entries} entries` : "—" },
          { k: "SCOPE", v: "this host" },
        ]}
      />

      {loading && <StateBanner state="loading">Reading status…</StateBanner>}
      {error && <StateBanner state="error">status failed: {error}</StateBanner>}

      <div className="work-grid">
        <div className="panel">
          <PanelHeader
            title="Daemon & vault"
            sub="GET /api/status"
            actions={
              <button
                type="button"
                className="btn btn-secondary btn-sm"
                onClick={onRefresh}
                disabled={loading}
              >
                {loading ? "…" : "Refresh"}
              </button>
            }
          />
          <div className="panel-body" style={{ display: "flex", flexDirection: "column", gap: 14 }}>
            <Row label="Vault path" value={status?.vault.path || "—"} />
            <Row
              label="Vault exists"
              value={
                <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                  {dot(status?.vault.exists)}
                  {status?.vault.exists ? "yes" : "no"}
                </span>
              }
            />
            <Row
              label="Daemon socket"
              value={
                <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                  {dot(status?.socket.ok)}
                  {status?.socket.ok ? "ok" : status?.socket.error || "offline"}
                </span>
              }
            />
            <Row
              label="AFM adapter"
              value={
                <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                  {dot(status?.afm.ok)}
                  {status?.afm.ok ? "ok" : status?.afm.error || "—"}
                </span>
              }
            />
            <Row label="Audit entries" value={String(status?.audit.entries ?? 0)} />
            {status?.audit.latest && (
              <Row label="Audit latest" value={status.audit.latest} />
            )}
          </div>
        </div>

        <div className="panel">
          <PanelHeader title="Bridge" sub="GET /api/health" />
          <div className="panel-body" style={{ display: "flex", flexDirection: "column", gap: 14 }}>
            <Row label="Bind" value={health?.host || "—"} />
            <Row label="Port" value={String(health?.port ?? "—")} />
            <Row
              label="Status"
              value={
                <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                  {dot(health?.ok)}
                  {health?.ok ? "ok" : "offline"}
                </span>
              }
            />
            <div>
              <div
                className="mono muted"
                style={{
                  fontSize: 10,
                  letterSpacing: "0.12em",
                  textTransform: "uppercase",
                  marginBottom: 6,
                }}
              >
                Tools
              </div>
              <div style={{ display: "flex", flexWrap: "wrap", gap: 4 }}>
                {(health?.tools || []).length === 0 ? (
                  <span className="muted mono" style={{ fontSize: 12 }}>
                    none
                  </span>
                ) : (
                  (health?.tools || []).map((t) => (
                    <Chip key={t} kind="default">
                      {t}
                    </Chip>
                  ))
                )}
              </div>
            </div>
          </div>
        </div>
      </div>
    </>
  );
}

function Row({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div style={{ display: "flex", alignItems: "baseline", gap: 12 }}>
      <span
        className="mono muted"
        style={{
          fontSize: 10,
          letterSpacing: "0.12em",
          textTransform: "uppercase",
          width: 130,
          flex: "0 0 130px",
        }}
      >
        {label}
      </span>
      <span className="mono" style={{ fontSize: 12.5, wordBreak: "break-all" }}>
        {value}
      </span>
    </div>
  );
}
