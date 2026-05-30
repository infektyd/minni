import type { ReactNode } from "react";

export function ScoreBar({ value }: { value: number }) {
  const pct = Math.max(0, Math.min(100, Math.round(value * 100)));
  return (
    <span className="score-bar">
      <span className="mono">{value.toFixed(2)}</span>
      <span className="score-bar-fill">
        <span style={{ width: `${pct}%` }} />
      </span>
    </span>
  );
}

export function Checkbox({
  on,
  onChange,
}: {
  on: boolean;
  onChange?: (next: boolean) => void;
}) {
  return (
    <span
      className={`ledger-checkbox ${on ? "is-on" : ""}`}
      role="checkbox"
      aria-checked={on}
      tabIndex={0}
      onClick={(e) => {
        e.stopPropagation();
        onChange?.(!on);
      }}
      onKeyDown={(e) => {
        if (e.key === " " || e.key === "Enter") {
          e.preventDefault();
          onChange?.(!on);
        }
      }}
    />
  );
}

export function PanelHeader({
  title,
  sub,
  actions,
}: {
  title: ReactNode;
  sub?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <div className="panel-header">
      <span className="panel-header-title">{title}</span>
      {sub && <span className="panel-header-sub">{sub}</span>}
      {actions && <div className="panel-header-actions">{actions}</div>}
    </div>
  );
}

export function SectionHeading({
  title,
  sub,
  actions,
}: {
  title: ReactNode;
  sub?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <div className="section-h">
      <span className="section-h-title">{title}</span>
      {sub && <span className="section-h-sub">{sub}</span>}
      <span className="section-h-rule" />
      {actions && <div className="section-h-actions">{actions}</div>}
    </div>
  );
}

export function CodeBlock({ children }: { children: ReactNode }) {
  return <pre className="code-block">{children}</pre>;
}

export function FilterPill({
  on,
  label,
  count,
  onClick,
}: {
  on: boolean;
  label: string;
  count?: number | string;
  onClick?: () => void;
}) {
  return (
    <button
      type="button"
      className={`filter-pill ${on ? "is-on" : ""}`}
      onClick={onClick}
    >
      <span>{label}</span>
      {count != null && <span className="filter-pill-count">{count}</span>}
    </button>
  );
}

export function RiskCallout({
  title,
  children,
}: {
  title: string;
  children: ReactNode;
}) {
  return (
    <div className="risk-callout">
      <div style={{ flex: 1 }}>
        <div className="risk-callout-title">{title}</div>
        <div style={{ marginTop: 4 }}>{children}</div>
      </div>
    </div>
  );
}

export function ArchivalBand({
  eyebrow,
  title,
  meta,
}: {
  eyebrow: string;
  title: string;
  meta: { k: string; v: string }[];
}) {
  return (
    <div className="archival-band">
      <div>
        <div className="archival-band-eyebrow">{eyebrow}</div>
        <div className="archival-band-title">{title}</div>
        <div className="archival-band-meta">
          {meta.map((m) => (
            <span key={m.k}>
              <b className="muted">{m.k}</b> {m.v}
            </span>
          ))}
        </div>
      </div>
    </div>
  );
}

export function StateBanner({
  state,
  children,
}: {
  state: "loading" | "error" | "empty";
  children: ReactNode;
}) {
  return (
    <div
      className={`state-banner ${state === "loading" ? "is-loading" : ""} ${
        state === "error" ? "is-error" : ""
      }`}
    >
      <span>{children}</span>
    </div>
  );
}
