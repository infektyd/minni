// Supporting visuals: full memory-layering diagram, session rehydration card,
// custom status chips.

const dSERIF = "'IBM Plex Serif', Georgia, serif";
const dMONO = "'IBM Plex Mono', ui-monospace, Menlo, monospace";

const dCREAM = '#f6f4ef';
const dCREAM2 = '#ece7da';
const dINK = '#1a1814';
const dINK2 = '#4a463d';
const dINK3 = '#8a8576';
const dAMBER = '#c97a2a';
const dGREEN = '#5a8a3a';   // approx oklch(0.62 0.13 130)
const dBLUE  = '#3a6aa8';   // approx oklch(0.62 0.13 250)

const dDARK = '#0e0f0c';
const dDARKFG = '#efe9dc';
const dDARKFG2 = '#aba596';

// — Full memory-layering diagram ————————————————
function LayerDiagram() {
  const rows = [
    { label: 'Identity',           rule: 'load whole',          width: 700, dashed: false, accent: dINK,  desc: 'agent identity · role · constraints · standing operating rules' },
    { label: 'Standing principles', rule: 'load whole · pinned', width: 700, dashed: false, accent: dINK,  desc: 'durable rules that guide behavior across sessions' },
    { label: 'Project state',      rule: 'compact packet',      width: 460, dashed: false, accent: dINK2, desc: 'active branch · status · blockers · recent decisions · next checks' },
    { label: 'Evidence',           rule: 'retrieve by need',    width: 280, dashed: true,  accent: dAMBER, desc: 'source-backed facts · artifacts · logs · traces · citations' },
    { label: 'Knowledge',          rule: 'retrieve chunked',    width: 180, dashed: true,  accent: dAMBER, desc: 'larger wiki/docs/history — cited and validated, never assumed' },
  ];
  return (
    <svg viewBox="0 0 1280 520" xmlns="http://www.w3.org/2000/svg" className="diag-frame" preserveAspectRatio="xMidYMid meet">
      <rect width="1280" height="520" fill={dCREAM} />
      {/* Title */}
      <text x="56" y="68" fontFamily={dSERIF} fontWeight="500" fontSize="36" fill={dINK} letterSpacing="-0.5">
        Memory is layered state, not one flat blob.
      </text>
      <text x="58" y="94" fontFamily={dMONO} fontSize="12" fill={dINK2}>
        each layer has its own load rule — solid bars load whole, dashed bars retrieve on demand
      </text>

      {/* Column headers */}
      <text x="56" y="148" fontFamily={dMONO} fontSize="10" letterSpacing="2" fill={dINK3}>LAYER</text>
      <text x="280" y="148" fontFamily={dMONO} fontSize="10" letterSpacing="2" fill={dINK3}>LOAD RULE</text>
      <text x="500" y="148" fontFamily={dMONO} fontSize="10" letterSpacing="2" fill={dINK3}>VISUAL SIZE  →  COST AT REHYDRATE</text>
      <line x1="56" y1="158" x2="1224" y2="158" stroke={dINK} strokeWidth="0.5" opacity="0.3" />

      {rows.map((r, i) => {
        const y = 184 + i * 62;
        return (
          <g key={r.label}>
            <text x="56" y={y} fontFamily={dSERIF} fontSize="22" fill={dINK}>{r.label}</text>
            <text x="56" y={y + 22} fontFamily={dMONO} fontSize="11" fill={dINK3}>{r.desc}</text>

            <text x="280" y={y} fontFamily={dMONO} fontSize="13" fill={dINK2}>{r.rule}</text>

            <line
              x1="500" y1={y - 6} x2={500 + r.width} y2={y - 6}
              stroke={r.accent}
              strokeWidth="6"
              strokeDasharray={r.dashed ? '3 5' : ''}
              strokeLinecap="round"
            />
            {/* Numerical hint */}
            <text x={500 + r.width + 12} y={y - 2} fontFamily={dMONO} fontSize="11" fill={dINK3}>
              {r.dashed ? '~ on demand' : 'every session'}
            </text>

            {i < rows.length - 1 && (
              <line x1="56" y1={y + 38} x2="1224" y2={y + 38} stroke={dINK} strokeWidth="0.3" opacity="0.15" />
            )}
          </g>
        );
      })}

      {/* Footer */}
      <line x1="56" y1="488" x2="1224" y2="488" stroke={dINK} strokeWidth="0.5" opacity="0.3" />
      <text x="56" y="506" fontFamily={dMONO} fontSize="10" letterSpacing="1.5" fill={dINK3}>
        the smallest packet that lets an agent resume safely  ·  if a simpler model achieves the same recovery quality, delete complexity
      </text>
    </svg>
  );
}

function LayerDiagramDark() {
  // Dark-mode pair — same layout, recolored.
  const rows = [
    { label: 'Identity',           rule: 'load whole',          width: 700, dashed: false, accent: dDARKFG },
    { label: 'Standing principles', rule: 'load whole · pinned', width: 700, dashed: false, accent: dDARKFG },
    { label: 'Project state',      rule: 'compact packet',      width: 460, dashed: false, accent: dDARKFG2 },
    { label: 'Evidence',           rule: 'retrieve by need',    width: 280, dashed: true,  accent: dAMBER },
    { label: 'Knowledge',          rule: 'retrieve chunked',    width: 180, dashed: true,  accent: dAMBER },
  ];
  const descs = [
    'agent identity · role · constraints · standing operating rules',
    'durable rules that guide behavior across sessions',
    'active branch · status · blockers · recent decisions · next checks',
    'source-backed facts · artifacts · logs · traces · citations',
    'larger wiki/docs/history — cited and validated, never assumed',
  ];
  return (
    <svg viewBox="0 0 1280 520" xmlns="http://www.w3.org/2000/svg" className="diag-frame" preserveAspectRatio="xMidYMid meet">
      <rect width="1280" height="520" fill={dDARK} />
      <text x="56" y="68" fontFamily={dSERIF} fontWeight="500" fontSize="36" fill={dDARKFG} letterSpacing="-0.5">
        Memory is layered state, not one flat blob.
      </text>
      <text x="58" y="94" fontFamily={dMONO} fontSize="12" fill={dDARKFG2}>
        each layer has its own load rule — solid bars load whole, dashed bars retrieve on demand
      </text>
      <text x="56" y="148" fontFamily={dMONO} fontSize="10" letterSpacing="2" fill={dDARKFG2}>LAYER</text>
      <text x="280" y="148" fontFamily={dMONO} fontSize="10" letterSpacing="2" fill={dDARKFG2}>LOAD RULE</text>
      <text x="500" y="148" fontFamily={dMONO} fontSize="10" letterSpacing="2" fill={dDARKFG2}>VISUAL SIZE  →  COST AT REHYDRATE</text>
      <line x1="56" y1="158" x2="1224" y2="158" stroke={dDARKFG} strokeWidth="0.5" opacity="0.25" />
      {rows.map((r, i) => {
        const y = 184 + i * 62;
        return (
          <g key={r.label}>
            <text x="56" y={y} fontFamily={dSERIF} fontSize="22" fill={dDARKFG}>{r.label}</text>
            <text x="56" y={y + 22} fontFamily={dMONO} fontSize="11" fill={dDARKFG2}>{descs[i]}</text>
            <text x="280" y={y} fontFamily={dMONO} fontSize="13" fill={dDARKFG2}>{r.rule}</text>
            <line x1="500" y1={y - 6} x2={500 + r.width} y2={y - 6} stroke={r.accent} strokeWidth="6" strokeDasharray={r.dashed ? '3 5' : ''} strokeLinecap="round" />
            <text x={500 + r.width + 12} y={y - 2} fontFamily={dMONO} fontSize="11" fill={dDARKFG2}>
              {r.dashed ? '~ on demand' : 'every session'}
            </text>
            {i < rows.length - 1 && (
              <line x1="56" y1={y + 38} x2="1224" y2={y + 38} stroke={dDARKFG} strokeWidth="0.3" opacity="0.1" />
            )}
          </g>
        );
      })}
      <line x1="56" y1="488" x2="1224" y2="488" stroke={dDARKFG} strokeWidth="0.5" opacity="0.25" />
      <text x="56" y="506" fontFamily={dMONO} fontSize="10" letterSpacing="1.5" fill={dDARKFG2}>
        the smallest packet that lets an agent resume safely  ·  if a simpler model achieves the same recovery quality, delete complexity
      </text>
    </svg>
  );
}

// — Session rehydration card —————————————————
function RehydrationCard() {
  const rows = [
    { k: 'verified_now',           v: 'auth.md · build-config.yml · deploy-target=staging',                       hint: 'checked against current artifacts',  dot: dGREEN },
    { k: 'remembered_unverified',  v: 'feature-flag rollout still pending review',                                 hint: 'plausible — needs confirmation',     dot: dBLUE  },
    { k: 'open_loops',             v: 'migration smoke-test ·  rotate audit token  ·  finalize compile dry-run',  hint: 'left incomplete last session',       dot: dAMBER },
    { k: 'first_verification',     v: '$ make verify   →   diff against current branch',                          hint: 'next concrete check before acting',  dot: dAMBER },
    { k: 'do_not_claim',           v: 'old /v1 API surface (removed 2 sessions ago)',                              hint: 'stale, contradicted, or unsupported', dot: dINK3  },
  ];
  return (
    <svg viewBox="0 0 1280 480" xmlns="http://www.w3.org/2000/svg" className="diag-frame" preserveAspectRatio="xMidYMid meet">
      <rect width="1280" height="480" fill={dCREAM} />
      <text x="56" y="60" fontFamily={dSERIF} fontWeight="500" fontSize="32" fill={dINK} letterSpacing="-0.5">
        A resumed session doesn't just retrieve documents.
      </text>
      <text x="58" y="86" fontFamily={dMONO} fontSize="12" fill={dINK2}>
        it produces the smallest packet that lets an agent resume safely:
      </text>

      <rect x="56" y="116" width="1168" height="316" rx="8" fill={dCREAM2} stroke={dINK} strokeOpacity="0.15" />
      <rect x="56" y="116" width="1168" height="32" rx="8" fill={dINK} fillOpacity="0.04" />
      <circle cx="76" cy="132" r="4" fill={dAMBER} />
      <text x="92" y="137" fontFamily={dMONO} fontSize="11" letterSpacing="1.5" fill={dINK2}>
        SESSION REHYDRATE  ·  agent=codex  ·  vault=codex-vault  ·  policy=local-only
      </text>
      <text x="1208" y="137" textAnchor="end" fontFamily={dMONO} fontSize="11" letterSpacing="1.5" fill={dINK3}>
        06:42:18 PDT  ·  68 tokens
      </text>

      {rows.map((r, i) => {
        const y = 188 + i * 46;
        return (
          <g key={r.k}>
            <circle cx="80" cy={y - 4} r="3" fill={r.dot} />
            <text x="96" y={y} fontFamily={dMONO} fontSize="13" fill={dINK2}>{r.k}</text>
            <text x="340" y={y} fontFamily={dMONO} fontSize="13" fill={dINK}>{r.v}</text>
            <text x="96" y={y + 17} fontFamily={dMONO} fontSize="10" fill={dINK3}>↳ {r.hint}</text>
            {i < rows.length - 1 && (
              <line x1="96" y1={y + 28} x2="1184" y2={y + 28} stroke={dINK} strokeWidth="0.3" opacity="0.15" />
            )}
          </g>
        );
      })}
    </svg>
  );
}

// — Custom status chips ————————————————————————
function StatusChips() {
  const chips = [
    { label: 'stable',      fg: '#1d4a26', bg: '#dfeede', dot: dGREEN, desc: 'tested, relied upon · breaking changes require migration' },
    { label: 'beta',        fg: '#1c3a66', bg: '#dde7f4', dot: dBLUE,  desc: 'works and is tested · API may shift before v1' },
    { label: 'alpha',       fg: '#5c3308', bg: '#f4e4cf', dot: dAMBER, desc: 'functional but early · limited real-world validation' },
    { label: 'stub',        fg: '#3a3a3a', bg: '#e4e1d8', dot: dINK3,  desc: 'interface exists · implementation is placeholder only' },
    { label: 'not started', fg: '#3a3a3a', bg: '#e4e1d8', dot: dINK3,  desc: 'planned · no work yet' },
  ];
  return (
    <svg viewBox="0 0 1280 220" xmlns="http://www.w3.org/2000/svg" preserveAspectRatio="xMidYMid meet">
      <rect width="1280" height="220" fill={dCREAM} />
      <text x="56" y="48" fontFamily={dSERIF} fontWeight="500" fontSize="24" fill={dINK}>Component status chips</text>
      <text x="58" y="68" fontFamily={dMONO} fontSize="11" fill={dINK3}>replaces shields.io badges in the status table — unified palette, single dot indicator</text>

      {chips.map((c, i) => {
        const y = 110 + i * 22;
        return (
          <g key={c.label}>
            {/* Chip */}
            <rect x="56" y={y - 12} width="120" height="22" rx="11" fill={c.bg} />
            <circle cx="72" cy={y - 1} r="3" fill={c.dot} />
            <text x="84" y={y + 3} fontFamily={dMONO} fontSize="11" fill={c.fg} letterSpacing="0.4">{c.label}</text>
            {/* Description */}
            <text x="196" y={y + 3} fontFamily={dMONO} fontSize="11" fill={dINK2}>{c.desc}</text>
          </g>
        );
      })}
    </svg>
  );
}

Object.assign(window, { LayerDiagram, LayerDiagramDark, RehydrationCard, StatusChips });
