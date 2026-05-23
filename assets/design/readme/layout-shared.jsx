// Shared primitives, palette, type system for the 4 README layout mockups.
// Each layout renders inside a DCArtboard at 920×variable, designed to read
// as "how this README would look on GitHub."

// — Type ——————————————————————————————————————————
const SERIF = "'IBM Plex Serif', 'Iowan Old Style', Georgia, serif";
const SANS  = "'Inter', 'Google Sans Text', system-ui, -apple-system, sans-serif";
const MONO  = "'IBM Plex Mono', ui-monospace, Menlo, monospace";

// — Palette ——————————————————————————————————————————
// Warm neutral. Off-white surfaces, deep ink, single chroma for accents.
const P = {
  bg:        '#faf8f3',   // page warm off-white
  surface:   '#fffefb',   // card surface — slightly brighter than bg
  surface_2: '#f4efe4',   // recessed surface (code, callouts)
  ink:       '#1a1814',   // primary text
  ink_2:     '#4a463d',   // secondary text
  ink_3:     '#8a8576',   // tertiary/labels
  hair:      'rgba(26,24,20,0.07)',  // hairline borders
  hair_2:    'rgba(26,24,20,0.12)',
  // accents (oklch ~ 0.62 chroma 0.13)
  amber:     '#c97a2a',
  green:     '#5a8a3a',
  blue:      '#3a6aa8',
  slate:     '#8a8576',
  // tinted backgrounds for status (very faint)
  tint_amber: '#f7eedd',
  tint_green: '#e8efde',
  tint_blue:  '#dde7f3',
  tint_slate: '#ece8de',
};

const STATUS = {
  stable: { dot: P.green, tint: P.tint_green, fg: '#1d4a26', label: 'stable' },
  beta:   { dot: P.blue,  tint: P.tint_blue,  fg: '#1c3a66', label: 'beta'   },
  alpha:  { dot: P.amber, tint: P.tint_amber, fg: '#5c3308', label: 'alpha'  },
  stub:   { dot: P.slate, tint: P.tint_slate, fg: '#3a3a3a', label: 'stub'   },
  todo:   { dot: P.slate, tint: P.tint_slate, fg: '#3a3a3a', label: 'planned'},
};

// — Reusable building blocks ————————————————————————

// A soft glass card — hairline border + faint vertical gradient + tiny inner highlight.
function Card({ children, style, padding = 28, tone, ...rest }) {
  const bg = tone === 'tint'
    ? `linear-gradient(180deg, ${P.surface} 0%, ${P.surface_2} 100%)`
    : `linear-gradient(180deg, ${P.surface} 0%, #fbf7ec 100%)`;
  return (
    <div
      style={{
        background: bg,
        border: `1px solid ${P.hair}`,
        borderRadius: 20,
        padding,
        boxShadow: '0 1px 0 rgba(255,255,255,0.7) inset, 0 1px 2px rgba(26,24,20,0.04), 0 8px 24px rgba(26,24,20,0.04)',
        ...style,
      }}
      {...rest}
    >
      {children}
    </div>
  );
}

// A small status dot — colored circle.
function Dot({ kind = 'stable', size = 8, animate = false, style }) {
  const s = STATUS[kind] ?? STATUS.stable;
  return (
    <span
      className={animate ? 'sm-dot-pulse' : ''}
      style={{
        display: 'inline-block',
        width: size, height: size, borderRadius: '50%',
        background: s.dot,
        boxShadow: `0 0 0 3px ${s.tint}`,
        flexShrink: 0,
        ...style,
      }}
    />
  );
}

// Inline status chip — small pill with dot + label.
function Chip({ kind = 'stable', children, style }) {
  const s = STATUS[kind] ?? STATUS.stable;
  return (
    <span style={{
      display: 'inline-flex', alignItems: 'center', gap: 8,
      padding: '4px 10px 4px 8px',
      borderRadius: 999,
      background: s.tint,
      color: s.fg,
      fontFamily: MONO,
      fontSize: 11,
      letterSpacing: 0.3,
      ...style,
    }}>
      <Dot kind={kind} size={6} style={{ boxShadow: 'none' }} />
      {children ?? s.label}
    </span>
  );
}

// Eyebrow label — small caps mono.
function Eyebrow({ children, style }) {
  return (
    <div style={{
      fontFamily: MONO, fontSize: 11, letterSpacing: 2,
      color: P.ink_3, textTransform: 'uppercase',
      ...style,
    }}>
      {children}
    </div>
  );
}

// Section number — large mono numeral.
function SectionNumber({ n }) {
  return (
    <span style={{
      fontFamily: MONO, fontSize: 13, letterSpacing: 2,
      color: P.amber, fontWeight: 500,
    }}>
      § {String(n).padStart(2, '0')}
    </span>
  );
}

// A hairline horizontal rule.
function Rule({ style, dashed }) {
  return (
    <div style={{
      height: 1,
      background: dashed
        ? `repeating-linear-gradient(90deg, ${P.hair_2} 0 4px, transparent 4px 8px)`
        : P.hair_2,
      ...style,
    }} />
  );
}

// A faux command block — looks like a styled <pre> in the README.
function CodeBlock({ lines, style, label }) {
  return (
    <div style={{
      background: P.surface_2,
      border: `1px solid ${P.hair}`,
      borderRadius: 14,
      overflow: 'hidden',
      ...style,
    }}>
      {label && (
        <div style={{
          padding: '8px 16px',
          fontFamily: MONO, fontSize: 10, letterSpacing: 1.5,
          color: P.ink_3,
          borderBottom: `1px solid ${P.hair}`,
        }}>{label}</div>
      )}
      <div style={{ padding: '14px 18px', fontFamily: MONO, fontSize: 12.5, lineHeight: 1.65, color: P.ink }}>
        {lines.map((l, i) => (
          <div key={i}>
            {l.startsWith('$') || l.startsWith('#') ? (
              <span style={{ color: P.ink_3 }}>{l[0]}</span>
            ) : null}
            <span>{l.startsWith('$') || l.startsWith('#') ? l.slice(1) : l}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

// — Global CSS for layouts ————————————————————————
const LAYOUT_CSS = `
  .sm-dot-pulse {
    animation: sm-pulse 2.4s ease-in-out infinite;
  }
  @keyframes sm-pulse {
    0%, 100% { opacity: 1; transform: scale(1); }
    50%      { opacity: 0.55; transform: scale(1.15); }
  }
`;

function SharedStyles() {
  return <style>{LAYOUT_CSS}</style>;
}

Object.assign(window, {
  SERIF, SANS, MONO, P, STATUS,
  Card, Dot, Chip, Eyebrow, SectionNumber, Rule, CodeBlock, SharedStyles,
});
