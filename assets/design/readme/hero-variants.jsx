// Hero banner SVG variants for the Sovereign Memory README.
// Each is sized 1280×320 — the canonical hero strip dimension.
// All inline SVG so they can be exported as standalone files.

const SERIF = "'IBM Plex Serif', 'Iowan Old Style', Georgia, serif";
const MONO = "'IBM Plex Mono', ui-monospace, Menlo, monospace";

// — Palette ————————————————————————————————————————
// Warm neutral. accents share chroma+lightness, hue varies.
const CREAM  = '#f6f4ef';
const CREAM_2 = '#ece7da';
const INK    = '#1a1814';
const INK_2  = '#4a463d';
const INK_3  = '#8a8576';
const AMBER  = '#c97a2a';   // oklch(0.62 0.13 60)
const DARK_BG = '#0e0f0c';
const DARK_FG = '#efe9dc';
const DARK_FG_2 = '#aba596';
const DARK_RULE = '#2a2b25';

// — HERO 1 : Editorial broadsheet ———————————————————
function HeroEditorial() {
  return (
    <svg viewBox="0 0 1280 320" xmlns="http://www.w3.org/2000/svg" className="hero-frame" preserveAspectRatio="xMidYMid meet">
      <rect width="1280" height="320" fill={CREAM} />
      {/* Double rule top */}
      <line x1="56" y1="46" x2="1224" y2="46" stroke={INK} strokeWidth="1.5" />
      <line x1="56" y1="52" x2="1224" y2="52" stroke={INK} strokeWidth="0.5" />
      {/* Eyebrow */}
      <text x="56" y="78" fontFamily={MONO} fontSize="11" letterSpacing="2" fill={INK_2}>
        LOCAL-FIRST · PRE-V1 ALPHA · A MEMORY LAYER FOR AGENTS
      </text>
      {/* Wordmark */}
      <text x="54" y="180" fontFamily={SERIF} fontWeight="500" fontSize="92" fill={INK} letterSpacing="-1.5">
        Sovereign Memory
      </text>
      {/* Tagline */}
      <text x="58" y="222" fontFamily={MONO} fontSize="15" fill={AMBER}>
        Identity loads whole.<tspan fill={INK_2}>  </tspan><tspan fill={INK}>Knowledge loads chunked.</tspan>
      </text>

      {/* Colophon block, right side */}
      <g transform="translate(960, 90)">
        <text x="0" y="0" fontFamily={MONO} fontSize="10" letterSpacing="1.5" fill={INK_3}>VERSION</text>
        <text x="0" y="20" fontFamily={MONO} fontSize="13" fill={INK}>v0.9 · pre-v1</text>
        <text x="0" y="56" fontFamily={MONO} fontSize="10" letterSpacing="1.5" fill={INK_3}>TESTS</text>
        <text x="0" y="76" fontFamily={MONO} fontSize="13" fill={INK}>454 passing</text>
        <text x="0" y="112" fontFamily={MONO} fontSize="10" letterSpacing="1.5" fill={INK_3}>LICENSE</text>
        <text x="0" y="132" fontFamily={MONO} fontSize="13" fill={INK}>MIT · no telemetry</text>
      </g>

      {/* Bottom rule with section label */}
      <line x1="56" y1="270" x2="1224" y2="270" stroke={INK} strokeWidth="0.5" />
      <text x="56" y="294" fontFamily={MONO} fontSize="10" letterSpacing="2" fill={INK_2}>§ I</text>
      <text x="84" y="294" fontFamily={MONO} fontSize="10" letterSpacing="2" fill={INK_2}>HIGHLIGHTS  ·  STATUS  ·  ARCHITECTURE  ·  PLUGIN SURFACES  ·  EVALUATION</text>
      <text x="1224" y="294" textAnchor="end" fontFamily={MONO} fontSize="10" letterSpacing="2" fill={INK_2}>github.com/infektyd/sovereign-memory</text>
    </svg>
  );
}

function HeroEditorialDark() {
  return (
    <svg viewBox="0 0 1280 320" xmlns="http://www.w3.org/2000/svg" className="hero-frame" preserveAspectRatio="xMidYMid meet">
      <rect width="1280" height="320" fill={DARK_BG} />
      <line x1="56" y1="46" x2="1224" y2="46" stroke={DARK_FG} strokeWidth="1.5" />
      <line x1="56" y1="52" x2="1224" y2="52" stroke={DARK_FG} strokeWidth="0.5" />
      <text x="56" y="78" fontFamily={MONO} fontSize="11" letterSpacing="2" fill={DARK_FG_2}>
        LOCAL-FIRST · PRE-V1 ALPHA · A MEMORY LAYER FOR AGENTS
      </text>
      <text x="54" y="180" fontFamily={SERIF} fontWeight="500" fontSize="92" fill={DARK_FG} letterSpacing="-1.5">
        Sovereign Memory
      </text>
      <text x="58" y="222" fontFamily={MONO} fontSize="15" fill={AMBER}>
        Identity loads whole.<tspan fill={DARK_FG_2}>  </tspan><tspan fill={DARK_FG}>Knowledge loads chunked.</tspan>
      </text>
      <g transform="translate(960, 90)">
        <text x="0" y="0" fontFamily={MONO} fontSize="10" letterSpacing="1.5" fill={DARK_FG_2}>VERSION</text>
        <text x="0" y="20" fontFamily={MONO} fontSize="13" fill={DARK_FG}>v0.9 · pre-v1</text>
        <text x="0" y="56" fontFamily={MONO} fontSize="10" letterSpacing="1.5" fill={DARK_FG_2}>TESTS</text>
        <text x="0" y="76" fontFamily={MONO} fontSize="13" fill={DARK_FG}>454 passing</text>
        <text x="0" y="112" fontFamily={MONO} fontSize="10" letterSpacing="1.5" fill={DARK_FG_2}>LICENSE</text>
        <text x="0" y="132" fontFamily={MONO} fontSize="13" fill={DARK_FG}>MIT · no telemetry</text>
      </g>
      <line x1="56" y1="270" x2="1224" y2="270" stroke={DARK_RULE} strokeWidth="0.5" />
      <text x="56" y="294" fontFamily={MONO} fontSize="10" letterSpacing="2" fill={DARK_FG_2}>§ I</text>
      <text x="84" y="294" fontFamily={MONO} fontSize="10" letterSpacing="2" fill={DARK_FG_2}>HIGHLIGHTS  ·  STATUS  ·  ARCHITECTURE  ·  PLUGIN SURFACES  ·  EVALUATION</text>
      <text x="1224" y="294" textAnchor="end" fontFamily={MONO} fontSize="10" letterSpacing="2" fill={DARK_FG_2}>github.com/infektyd/sovereign-memory</text>
    </svg>
  );
}

// — HERO 2 : Memory layers ————————————————————————
function HeroLayers() {
  // Right side has 5 stacked bars; left has wordmark.
  const bars = [
    { label: 'Identity',          rule: 'load whole',         w: 540, dashed: false },
    { label: 'Standing principles', rule: 'load whole · pinned', w: 540, dashed: false },
    { label: 'Project state',     rule: 'compact packet',     w: 360, dashed: false },
    { label: 'Evidence',          rule: 'retrieve by need',   w: 220, dashed: true  },
    { label: 'Knowledge',         rule: 'retrieve chunked',   w: 140, dashed: true  },
  ];
  return (
    <svg viewBox="0 0 1280 320" xmlns="http://www.w3.org/2000/svg" className="hero-frame" preserveAspectRatio="xMidYMid meet">
      <rect width="1280" height="320" fill={CREAM} />
      {/* Wordmark left */}
      <text x="56" y="118" fontFamily={SERIF} fontWeight="500" fontSize="58" fill={INK} letterSpacing="-1">
        Sovereign
      </text>
      <text x="56" y="172" fontFamily={SERIF} fontWeight="500" fontSize="58" fill={INK} letterSpacing="-1">
        Memory
      </text>
      <text x="58" y="206" fontFamily={MONO} fontSize="12" fill={INK_2}>
        a memory + governance layer
      </text>
      <text x="58" y="222" fontFamily={MONO} fontSize="12" fill={INK_2}>
        for long-running agents
      </text>

      {/* Layers right */}
      <text x="540" y="64" fontFamily={MONO} fontSize="10" letterSpacing="2" fill={INK_3}>MEMORY LAYERS  ·  LOAD RULE</text>
      {bars.map((b, i) => {
        const y = 86 + i * 36;
        return (
          <g key={b.label}>
            <text x="540" y={y + 14} fontFamily={MONO} fontSize="13" fill={INK}>{b.label}</text>
            <line
              x1="700" y1={y + 9} x2={700 + b.w} y2={y + 9}
              stroke={i < 2 ? INK : (i === 2 ? INK : AMBER)}
              strokeWidth={i < 3 ? 3 : 2}
              strokeDasharray={b.dashed ? '2 4' : ''}
              strokeLinecap="round"
            />
            <text x={700 + b.w + 12} y={y + 14} fontFamily={MONO} fontSize="11" fill={INK_2}>{b.rule}</text>
          </g>
        );
      })}

      {/* Footer */}
      <line x1="540" y1="280" x2="1224" y2="280" stroke={INK} strokeWidth="0.5" opacity="0.3" />
      <text x="540" y="298" fontFamily={MONO} fontSize="10" letterSpacing="1.5" fill={INK_3}>
        ── solid: load whole      ┄ dashed: retrieve on demand
      </text>
    </svg>
  );
}

// — HERO 3 : Inspector readout ————————————————————
function HeroInspector() {
  return (
    <svg viewBox="0 0 1280 320" xmlns="http://www.w3.org/2000/svg" className="hero-frame" preserveAspectRatio="xMidYMid meet">
      <rect width="1280" height="320" fill={CREAM} />
      {/* Wordmark */}
      <text x="56" y="58" fontFamily={SERIF} fontWeight="500" fontSize="34" fill={INK} letterSpacing="-0.5">
        Sovereign Memory
      </text>
      <text x="56" y="80" fontFamily={MONO} fontSize="11" fill={INK_2}>
        identity loads whole · knowledge loads chunked
      </text>

      {/* Card */}
      <rect x="56" y="106" width="1168" height="184" rx="6" fill={CREAM_2} stroke={INK} strokeOpacity="0.18" />
      <rect x="56" y="106" width="1168" height="22" rx="6" fill={INK} fillOpacity="0.04" />
      <circle cx="72" cy="117" r="3" fill={AMBER} />
      <text x="84" y="121" fontFamily={MONO} fontSize="10" letterSpacing="1.5" fill={INK_2}>
        SESSION REHYDRATE  ·  agent=codex  ·  vault=codex-vault
      </text>
      <text x="1208" y="121" textAnchor="end" fontFamily={MONO} fontSize="10" letterSpacing="1.5" fill={INK_3}>
        06:42:18 PDT
      </text>

      {/* Rows */}
      {[
        { k: 'verified_now',         v: 'auth.md · build-config.yml · deploy-target=staging', dot: INK },
        { k: 'remembered_unverified', v: 'feature-flag rollout still pending review',          dot: INK_3 },
        { k: 'open_loops',            v: 'migration smoke-test · rotate audit token',          dot: AMBER },
        { k: 'first_verification',    v: '$ make verify  → diff against current branch',       dot: AMBER },
        { k: 'do_not_claim',          v: 'old /v1 API surface (removed 2 sessions ago)',       dot: INK_3 },
      ].map((r, i) => {
        const y = 156 + i * 26;
        return (
          <g key={r.k}>
            <circle cx="76" cy={y - 4} r="2.5" fill={r.dot} />
            <text x="88" y={y} fontFamily={MONO} fontSize="12" fill={INK_2}>{r.k.padEnd(22, ' ')}</text>
            <text x="350" y={y} fontFamily={MONO} fontSize="12" fill={INK}>{r.v}</text>
          </g>
        );
      })}
    </svg>
  );
}

// — HERO 4 : Sovereign seal (minimal) —————————————
function HeroSeal() {
  // Tiny concentric-square mark = layered memory
  return (
    <svg viewBox="0 0 1280 320" xmlns="http://www.w3.org/2000/svg" className="hero-frame" preserveAspectRatio="xMidYMid meet">
      <rect width="1280" height="320" fill={CREAM} />
      {/* Mark */}
      <g transform="translate(96, 132)">
        <rect x="0" y="0" width="56" height="56" fill="none" stroke={INK} strokeWidth="1.5" />
        <rect x="8" y="8" width="40" height="40" fill="none" stroke={INK} strokeWidth="1.5" />
        <rect x="16" y="16" width="24" height="24" fill="none" stroke={INK} strokeWidth="1.5" />
        <rect x="24" y="24" width="8" height="8" fill={AMBER} />
      </g>
      {/* Wordmark */}
      <text x="184" y="172" fontFamily={SERIF} fontWeight="500" fontSize="64" fill={INK} letterSpacing="-1">
        Sovereign Memory
      </text>
      <text x="186" y="206" fontFamily={MONO} fontSize="14" fill={INK_2} letterSpacing="0.2">
        identity loads whole · knowledge loads chunked
      </text>
      {/* Far-right tiny meta column */}
      <text x="1224" y="148" textAnchor="end" fontFamily={MONO} fontSize="11" fill={INK_3}>pre-v1 alpha</text>
      <text x="1224" y="166" textAnchor="end" fontFamily={MONO} fontSize="11" fill={INK_3}>454 tests passing</text>
      <text x="1224" y="184" textAnchor="end" fontFamily={MONO} fontSize="11" fill={INK_3}>MIT · local-only</text>
    </svg>
  );
}

// — HERO 5 : Architectural blueprint —————————————
function HeroBlueprint() {
  // Schematic of agent → plugin → daemon → storage on a tasteful grid.
  return (
    <svg viewBox="0 0 1280 320" xmlns="http://www.w3.org/2000/svg" className="hero-frame" preserveAspectRatio="xMidYMid meet">
      <rect width="1280" height="320" fill={CREAM} />
      {/* Grid */}
      <defs>
        <pattern id="grid" width="32" height="32" patternUnits="userSpaceOnUse">
          <path d="M 32 0 L 0 0 0 32" fill="none" stroke={INK} strokeWidth="0.4" strokeOpacity="0.08" />
        </pattern>
      </defs>
      <rect width="1280" height="320" fill="url(#grid)" />

      {/* Wordmark */}
      <text x="56" y="68" fontFamily={SERIF} fontWeight="500" fontSize="44" fill={INK} letterSpacing="-0.5">
        Sovereign Memory
      </text>
      <text x="58" y="92" fontFamily={MONO} fontSize="11" fill={INK_2}>
        identity loads whole · knowledge loads chunked
      </text>

      {/* Schematic boxes */}
      {[
        { x: 56,  label: 'agent', sub: 'codex · claude · gemini' },
        { x: 296, label: 'plugin', sub: 'MCP · 26 tools' },
        { x: 536, label: 'daemon', sub: 'sovrd · JSON-RPC' },
        { x: 776, label: 'identity', sub: 'principal · read-gate' },
        { x: 1016, label: 'storage', sub: 'sqlite · faiss · vaults' },
      ].map((b, i) => (
        <g key={b.label}>
          <rect x={b.x} y="156" width="208" height="80" fill={CREAM} stroke={INK} strokeWidth="1" />
          {i === 4 && <rect x={b.x + 6} y="162" width="196" height="68" fill="none" stroke={AMBER} strokeWidth="1" strokeDasharray="2 3" />}
          <text x={b.x + 16} y="184" fontFamily={MONO} fontSize="10" letterSpacing="1.5" fill={INK_3}>{`0${i+1}`}</text>
          <text x={b.x + 16} y="208" fontFamily={SERIF} fontSize="20" fill={INK}>{b.label}</text>
          <text x={b.x + 16} y="226" fontFamily={MONO} fontSize="10" fill={INK_2}>{b.sub}</text>
        </g>
      ))}
      {/* Connector arrows */}
      {[264, 504, 744, 984].map((x, i) => (
        <g key={i}>
          <line x1={x} y1="196" x2={x + 32} y2="196" stroke={INK} strokeWidth="1" />
          <path d={`M ${x + 28} 192 L ${x + 32} 196 L ${x + 28} 200`} fill="none" stroke={INK} strokeWidth="1" />
        </g>
      ))}

      {/* Footer scale label */}
      <text x="56" y="284" fontFamily={MONO} fontSize="10" letterSpacing="1.5" fill={INK_3}>fig.01 · request flow · all writes operator-gated</text>
    </svg>
  );
}

Object.assign(window, {
  HeroEditorial, HeroEditorialDark,
  HeroLayers, HeroInspector, HeroSeal, HeroBlueprint,
});
