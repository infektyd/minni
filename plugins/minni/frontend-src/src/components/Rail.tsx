export interface NavItem {
  id?: string;
  label?: string;
  glyph?: "square" | "dot" | "bar" | "cross" | "diamond";
  count?: string;
  kbd?: string;
  section?: string;
}

export const NAV: NavItem[] = [
  { section: "WORKSPACE" },
  { id: "recall", label: "Recall", glyph: "square" },
  { id: "packet", label: "Prepare Packet", glyph: "bar" },
  { id: "dryrun", label: "Dry-run Review", glyph: "diamond" },
  { section: "AGENTS" },
  { id: "handoffs", label: "Handoffs", glyph: "cross" },
  { id: "vaults", label: "Vaults", glyph: "dot" },
  { section: "OBSERVABILITY" },
  { id: "audit", label: "Audit Trail", glyph: "bar" },
  { id: "policy", label: "Policy & AFM", glyph: "diamond" },
  { id: "settings", label: "Settings", glyph: "cross", kbd: "," },
];

export function Rail({
  active,
  onSelect,
  counts,
}: {
  active: string;
  onSelect: (id: string) => void;
  counts: Partial<Record<string, string>>;
}) {
  return (
    <nav className="rail" aria-label="Primary">
      <div className="rail-brand">
        <div className="rail-brand-mark" />
        <div className="rail-brand-text">
          <div className="rail-brand-title">SOVEREIGN</div>
          <div className="rail-brand-sub">memory · v4.2</div>
        </div>
      </div>
      {NAV.map((n, i) => {
        if (n.section) {
          return (
            <div className="rail-section-label" key={`s-${i}`}>
              {n.section}
            </div>
          );
        }
        const isActive = active === n.id;
        const count = (n.id && counts[n.id]) || n.count || "";
        return (
          <button
            key={n.id}
            type="button"
            className={`rail-item ${isActive ? "is-active" : ""}`}
            onClick={() => n.id && onSelect(n.id)}
            aria-current={isActive ? "page" : undefined}
          >
            <span className="rail-item-glyph" data-shape={n.glyph} aria-hidden="true" />
            <span>{n.label}</span>
            {n.kbd ? (
              <span className="rail-item-kbd">{n.kbd}</span>
            ) : count ? (
              <span className="rail-item-count">{count}</span>
            ) : null}
          </button>
        );
      })}
      <div className="rail-foot">
        <div className="rail-meter-row">
          <span>VAULT</span>
          <span>{counts.__vault || "local"}</span>
        </div>
        <div className="rail-meter">
          <span style={{ width: "62%" }} />
        </div>
        <div className="rail-meter-row" style={{ marginTop: 6 }}>
          <span>AUDIT</span>
          <span>{counts.__audit || "—"}</span>
        </div>
      </div>
    </nav>
  );
}
