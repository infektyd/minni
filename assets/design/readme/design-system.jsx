// Sovereign Memory README — design system primitives.
// Palette + type taken verbatim from the project's DESIGN.md.
// Tokens for the dark variant added to mirror the light system.

// — Type ———————————————————————————————————————
const ds_SANS = "'Inter', system-ui, -apple-system, 'Segoe UI', sans-serif";
const ds_MONO = "'IBM Plex Mono', ui-monospace, Menlo, monospace";

// — Palettes ——————————————————————————————————
// LIGHT — warm paper first, graphite second, semantic always.
const LIGHT = {
  bg:        '#F4F1EA',  // bone (page)
  bg_2:      '#E8E2D0',  // bone-2 (recessed)
  panel:     '#FFFEFA',  // panel (card)
  panel_2:   '#F8F5EE',  // panel-muted

  ink:       '#181613',  // primary text
  graphite:  '#242320',  // secondary
  graphite_2:'#33312C',
  graphite_3:'#4E4A42',  // tertiary / labels
  muted:     '#7C817B',  // disabled / loading text

  border:        '#D6D0C3',
  border_strong: '#AFA696',

  // Semantic accents (from DESIGN.md)
  verdigris:        '#2F7D68', // primary / healthy / AFM-safe / learn
  verdigris_dark:   '#1E5E4C',
  verdigris_soft:   '#D9ECE5',
  persimmon:        '#D2603A', // danger / exclude / do-not-store
  persimmon_dark:   '#A4483F',
  persimmon_soft:   '#F7DED6',
  mustard:          '#C9A961', // warning / log-only / review
  mustard_dark:     '#8C681F',
  mustard_soft:     '#F4E8C7',
  blue:             '#3D6F95', // info
  blue_soft:        '#DCEAF2',
};

// DARK — same hues, control-room palette. Derived to keep contrast and semantic roles intact.
const DARK = {
  bg:        '#161512',
  bg_2:      '#1c1b17',
  panel:     '#1f1e1a',
  panel_2:   '#272620',

  ink:       '#F4F1EA',
  graphite:  '#E8E2D0',
  graphite_2:'#cfc9ba',
  graphite_3:'#a8a294',
  muted:     '#7C817B',

  border:        '#3a382f',
  border_strong: '#544f44',

  verdigris:        '#4ea890',
  verdigris_dark:   '#7cc4ad',
  verdigris_soft:   '#1f3a32',
  persimmon:        '#e08056',
  persimmon_dark:   '#eb9b78',
  persimmon_soft:   '#3e251c',
  mustard:          '#d6b87a',
  mustard_dark:     '#e8cf94',
  mustard_soft:     '#3b3018',
  blue:             '#6b96bd',
  blue_soft:        '#1f303e',
};

// — Status mapping (matches DESIGN.md semantic roles) —
function statusFor(theme, kind) {
  const t = theme;
  switch (kind) {
    case 'stable': return { dot: t.verdigris,    tint: t.verdigris_soft, fg: t.verdigris_dark,   label: 'stable' };
    case 'beta':   return { dot: t.blue,         tint: t.blue_soft,      fg: t.blue,             label: 'beta' };
    case 'alpha':  return { dot: t.mustard_dark, tint: t.mustard_soft,   fg: t.mustard_dark,     label: 'alpha' };
    case 'stub':   return { dot: t.graphite_3,   tint: t.panel_2,        fg: t.graphite_3,       label: 'stub' };
    case 'todo':   return { dot: t.muted,        tint: t.panel_2,        fg: t.muted,            label: 'planned' };
    default:       return { dot: t.graphite_3,   tint: t.panel_2,        fg: t.graphite_3,       label: kind };
  }
}

// — Primitives ————————————————————————————————

function DsDot({ theme, kind = 'stable', size = 8, style }) {
  const s = statusFor(theme, kind);
  return (
    <span style={{
      display: 'inline-block',
      width: size, height: size, borderRadius: '50%',
      background: s.dot, flexShrink: 0, ...style,
    }} />
  );
}

function DsChip({ theme, kind = 'stable', children, style }) {
  const s = statusFor(theme, kind);
  return (
    <span style={{
      display: 'inline-flex', alignItems: 'center', gap: 6,
      padding: '3px 8px 3px 7px',
      borderRadius: 4,
      background: s.tint,
      color: s.fg,
      fontFamily: ds_MONO, fontSize: 10.5, letterSpacing: 0.4,
      textTransform: 'uppercase', fontWeight: 600,
      ...style,
    }}>
      <DsDot theme={theme} kind={kind} size={6} />
      {children ?? s.label}
    </span>
  );
}

function DsPanel({ theme, children, style, padding = 20, muted = false }) {
  return (
    <div style={{
      background: muted ? theme.panel_2 : theme.panel,
      border: `1px solid ${theme.border}`,
      borderRadius: 2,
      padding,
      ...style,
    }}>{children}</div>
  );
}

function DsLabel({ theme, children, style }) {
  return (
    <div style={{
      fontFamily: ds_MONO, fontSize: 10.5, letterSpacing: 1.4,
      color: theme.graphite_3, textTransform: 'uppercase', fontWeight: 600,
      ...style,
    }}>{children}</div>
  );
}

function DsRule({ theme, style, strong = false }) {
  return <div style={{ height: 1, background: strong ? theme.border_strong : theme.border, ...style }} />;
}

function DsCode({ theme, lines, label, style, collapsible = false, defaultOpen = true }) {
  const [open, setOpen] = React.useState(defaultOpen);
  const isOpen = collapsible ? open : true;
  return (
    <div style={{
      background: theme.panel_2,
      border: `1px solid ${theme.border}`,
      borderRadius: 2,
      overflow: 'hidden',
      ...style,
    }}>
      {label && (
        <div
          onClick={collapsible ? () => setOpen((v) => !v) : undefined}
          style={{
            padding: '8px 14px',
            fontFamily: ds_MONO, fontSize: 9.5, letterSpacing: 1.4,
            color: theme.graphite_3, fontWeight: 600,
            borderBottom: isOpen ? `1px solid ${theme.border}` : 'none',
            background: theme.bg_2,
            display: 'flex', alignItems: 'center', gap: 10,
            cursor: collapsible ? 'pointer' : 'default', userSelect: 'none',
          }}
        >
          {collapsible && (
            <svg width="9" height="9" viewBox="0 0 11 11" style={{
              transform: isOpen ? 'rotate(90deg)' : 'rotate(0deg)',
              transition: 'transform 140ms ease',
              flexShrink: 0,
            }}>
              <path d="M3 1.5 L7.5 5.5 L3 9.5" fill="none" stroke={theme.graphite_3} strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          )}
          <span>{label}</span>
        </div>
      )}
      {isOpen && (
        <div style={{ padding: '12px 14px', fontFamily: ds_MONO, fontSize: 12, lineHeight: 1.6, color: theme.ink, whiteSpace: 'pre' }}>
          {lines.map((l, i) => (
            <div key={i}>
              {l.startsWith('$') || l.startsWith('#') ? <span style={{ color: theme.graphite_3 }}>{l[0]}</span> : null}
              <span>{l.startsWith('$') || l.startsWith('#') ? l.slice(1) : l}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// Collapsible wrapper for any table/panel. Renders the standard "header strip"
// (background bg_2, label + optional meta) and toggles its children below.
function DsCollapsibleTable({ theme, label, meta, defaultOpen = true, style, children }) {
  const [open, setOpen] = React.useState(defaultOpen);
  return (
    <div style={{
      background: theme.panel,
      border: `1px solid ${theme.border}`,
      borderRadius: 2,
      overflow: 'hidden',
      ...style,
    }}>
      <div
        onClick={() => setOpen((v) => !v)}
        style={{
          padding: '10px 18px',
          background: theme.bg_2,
          borderBottom: open ? `1px solid ${theme.border}` : 'none',
          display: 'flex', justifyContent: 'space-between', alignItems: 'center',
          cursor: 'pointer', userSelect: 'none',
          gap: 14,
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <svg width="9" height="9" viewBox="0 0 11 11" style={{
            transform: open ? 'rotate(90deg)' : 'rotate(0deg)',
            transition: 'transform 140ms ease',
            flexShrink: 0,
          }}>
            <path d="M3 1.5 L7.5 5.5 L3 9.5" fill="none" stroke={theme.graphite_3} strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
          <span style={{
            fontFamily: ds_MONO, fontSize: 10.5, letterSpacing: 1.4,
            color: theme.graphite_3, textTransform: 'uppercase', fontWeight: 600,
          }}>{label}</span>
        </div>
        {meta && (
          <span style={{ fontFamily: ds_MONO, fontSize: 10.5, color: theme.graphite_3 }}>{meta}</span>
        )}
      </div>
      {open && children}
    </div>
  );
}

// Collapsible section header — chevron + title + anchor + status pill.
function DsCollapsibleHeader({ theme, n, title, status, open, onToggle, anchor }) {
  return (
    <div
      id={anchor}
      onClick={onToggle}
      style={{
        display: 'flex', alignItems: 'center', gap: 14,
        padding: '14px 0',
        borderTop: `1px solid ${theme.border_strong}`,
        cursor: 'pointer',
        userSelect: 'none',
      }}
    >
      <span style={{
        fontFamily: ds_MONO, fontSize: 11, letterSpacing: 1.4,
        color: theme.verdigris, fontWeight: 600, width: 32,
      }}>§ {String(n).padStart(2, '0')}</span>

      <svg width="11" height="11" viewBox="0 0 11 11" style={{
        transform: open ? 'rotate(90deg)' : 'rotate(0deg)',
        transition: 'transform 160ms ease',
        flexShrink: 0,
      }}>
        <path d="M3 1.5 L7.5 5.5 L3 9.5" fill="none" stroke={theme.graphite_3} strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" />
      </svg>

      <span style={{
        fontFamily: ds_SANS, fontSize: 19, fontWeight: 600, color: theme.ink,
        letterSpacing: -0.2, flexGrow: 1,
      }}>{title}</span>

      {status && <DsChip theme={theme} kind={status} />}

      <span style={{ fontFamily: ds_MONO, fontSize: 10.5, color: theme.graphite_3 }}>
        {open ? 'collapse' : 'expand'}
      </span>
    </div>
  );
}

Object.assign(window, {
  ds_SANS, ds_MONO, LIGHT, DARK, statusFor,
  DsDot, DsChip, DsPanel, DsLabel, DsRule, DsCode, DsCollapsibleHeader, DsCollapsibleTable,
});
