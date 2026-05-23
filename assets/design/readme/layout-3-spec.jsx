// Layout 3 — SPEC SHEET
// Tone: honest-engineering, dense. Hardware-datasheet aesthetic.
// 920 × ~1740

function ReadmeSpec() {
  return (
    <div style={{
      width: 920,
      background: P.bg,
      fontFamily: SANS,
      color: P.ink,
      padding: '32px 36px 40px',
      boxSizing: 'border-box',
    }}>
      <SharedStyles />

      {/* — Header ————————————————————————— */}
      <div style={{ marginBottom: 22 }}>
        <div style={{
          display: 'flex', justifyContent: 'space-between', alignItems: 'flex-end',
          paddingBottom: 14, borderBottom: `1px solid ${P.ink}`,
        }}>
          <div>
            <div style={{ fontFamily: MONO, fontSize: 11, letterSpacing: 2, color: P.ink_3 }}>DATASHEET · REV 0.9</div>
            <div style={{ fontFamily: SERIF, fontSize: 38, fontWeight: 500, letterSpacing: -0.7, lineHeight: 1, marginTop: 6 }}>
              Sovereign Memory
            </div>
            <div style={{ fontFamily: SANS, fontSize: 13.5, color: P.ink_2, marginTop: 8 }}>
              Local-first memory and governance layer for AI agents.
            </div>
          </div>
          <div style={{ display: 'grid', gridTemplateColumns: 'auto auto auto auto', columnGap: 22, rowGap: 4 }}>
            {[
              ['VERSION', 'v0.9 · pre-v1 alpha'],
              ['TESTS',   '454 passing'],
              ['LICENSE', 'MIT'],
              ['PLATFORM', 'macOS · linux†'],
            ].map(([k, v]) => (
              <React.Fragment key={k}>
                <div style={{ fontFamily: MONO, fontSize: 9.5, letterSpacing: 1.5, color: P.ink_3 }}>{k}</div>
                <div style={{ fontFamily: MONO, fontSize: 11.5, color: P.ink }}>{v}</div>
              </React.Fragment>
            ))}
          </div>
        </div>
        <div style={{ fontFamily: MONO, fontSize: 10, color: P.ink_3, marginTop: 4 }}>
          † linux works for everything except AFM-bound passes (macOS-only). Ollama / MLX / Gemma providers planned.
        </div>
      </div>

      {/* — Quick facts grid ——————————————————————— */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(5, 1fr)', gap: 10, marginBottom: 28 }}>
        {[
          ['LANGUAGE',  'Python 3.11+\nTypeScript / Node 18+'],
          ['STORAGE',   'SQLite (WAL)\nFAISS (active)'],
          ['RETRIEVAL', 'FTS5 + FAISS\nRRF + rerank + HyDE'],
          ['PROTOCOL',  'MCP (standard)\nlocal JSON-RPC'],
          ['AGENTS',    'Codex · Claude Code\nGemini · KiloCode · Grok'],
        ].map(([k, v]) => (
          <div key={k} style={{
            background: P.surface,
            border: `1px solid ${P.hair}`,
            borderRadius: 10,
            padding: '12px 14px',
          }}>
            <div style={{ fontFamily: MONO, fontSize: 9, letterSpacing: 1.5, color: P.ink_3, marginBottom: 6 }}>{k}</div>
            <div style={{ fontFamily: MONO, fontSize: 11.5, color: P.ink, lineHeight: 1.45, whiteSpace: 'pre-line' }}>{v}</div>
          </div>
        ))}
      </div>

      {/* — Component matrix ——————————————————————— */}
      <div style={{ marginBottom: 10, display: 'flex', justifyContent: 'space-between', alignItems: 'baseline' }}>
        <div>
          <Eyebrow>§ I · component matrix</Eyebrow>
          <div style={{ fontFamily: SERIF, fontSize: 22, fontWeight: 500, letterSpacing: -0.3, marginTop: 4 }}>
            Maturity by subsystem
          </div>
        </div>
        <div style={{ fontFamily: MONO, fontSize: 10, color: P.ink_3 }}>
          stable → beta → alpha → stub
        </div>
      </div>

      <div style={{
        background: P.surface, border: `1px solid ${P.hair}`, borderRadius: 12, overflow: 'hidden',
        marginBottom: 28,
      }}>
        {/* Header row */}
        <div style={{
          display: 'grid', gridTemplateColumns: '110px 1.1fr 130px 1.4fr',
          padding: '10px 18px', background: 'rgba(26,24,20,0.03)',
          fontFamily: MONO, fontSize: 9.5, letterSpacing: 1.5, color: P.ink_3,
          borderBottom: `1px solid ${P.hair}`,
        }}>
          <div>STATUS</div>
          <div>COMPONENT</div>
          <div>MATURITY</div>
          <div>NOTES</div>
        </div>
        {[
          ['stable', 'SQLite runtime + migrations',     1.0, '333 engine tests · WAL · additive `PRAGMA user_version`'],
          ['beta',   'Hybrid retrieval (FTS5 + FAISS)', 0.7, 'FTS5 → FAISS → RRF → rerank · HyDE · query expansion'],
          ['beta',   'MCP plugin server',               0.7, '26 tools · 121 tests · agent-agnostic'],
          ['beta',   'Proposal-first learning',         0.7, 'stage → list → resolve · operator-gated writes'],
          ['beta',   'Vault model + wiki indexer',      0.7, 'WikiIndexer → VaultIndexer → SQLite/FAISS'],
          ['beta',   'Identity + read policy',          0.7, 'EffectivePrincipal · centralized read gate'],
          ['beta',   'Handoff (inbox / outbox)',        0.7, 'vault-backed pages · ack and await flows'],
          ['beta',   'Multi-agent propagation',         0.65, 'seed-hosted · bootstrap-vault · update · verify'],
          ['alpha',  'Cross-agent ping contracts',      0.4, 'protocol works · limited real-world testing'],
          ['alpha',  'AFM provider (native / bridge)',  0.4, 'macOS-only · bridge default · native opt-in'],
          ['alpha',  'Compile passes (AFM)',            0.4, '5 passes · dry-run only by default'],
          ['alpha',  'Team coordination',               0.35, '3 tools registered · multi-agent untested'],
          ['alpha',  'Memory decay + scoring',          0.35, 'exponential decay · needs tuning'],
          ['stub',   'Qdrant / Lance backends',         0.05, 'non-functional · FAISS is the only active backend'],
          ['stub',   'Comparative eval vs baselines',   0.0, 'harness exists · no head-to-head runs yet'],
        ].map(([kind, name, m, notes], i) => (
          <div key={name} style={{
            display: 'grid', gridTemplateColumns: '110px 1.1fr 130px 1.4fr', alignItems: 'center',
            padding: '10px 18px',
            borderBottom: i < 14 ? `1px solid ${P.hair}` : 'none',
            background: i % 2 === 1 ? 'rgba(26,24,20,0.012)' : 'transparent',
          }}>
            <div><Chip kind={kind} /></div>
            <div style={{ fontFamily: SANS, fontSize: 12.5, color: P.ink, fontWeight: 500 }}>{name}</div>
            <div>
              <svg width="100" height="6">
                <rect x="0" y="0" width="100" height="6" rx="3" fill={STATUS[kind].tint} />
                <rect x="0" y="0" width={100 * m} height="6" rx="3" fill={STATUS[kind].dot} />
              </svg>
            </div>
            <div style={{ fontFamily: MONO, fontSize: 11, color: P.ink_2 }}>{notes}</div>
          </div>
        ))}
      </div>

      {/* — Tool surface ——————————————————————— */}
      <div style={{ marginBottom: 10 }}>
        <Eyebrow>§ II · plugin tool surface</Eyebrow>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', marginTop: 4 }}>
          <div style={{ fontFamily: SERIF, fontSize: 22, fontWeight: 500, letterSpacing: -0.3 }}>
            26 MCP tools, six categories
          </div>
          <div style={{ fontFamily: MONO, fontSize: 11, color: P.ink_3 }}>
            implements model context protocol · any MCP client can connect
          </div>
        </div>
      </div>

      <div style={{ marginBottom: 28 }}>
        {[
          { cat: 'RECALL',     col: P.blue,  tools: ['sovereign_status', 'sovereign_recall', 'sovereign_drill', 'sovereign_prepare_task', 'sovereign_prepare_outcome', 'sovereign_route', 'sovereign_export_pack'] },
          { cat: 'LEARNING',   col: P.amber, tools: ['sovereign_learn', 'sovereign_learning_quality', 'sovereign_resolve_candidate', 'sovereign_vault_write'] },
          { cat: 'AUDIT',      col: P.green, tools: ['sovereign_audit_report', 'sovereign_audit_tail', 'sovereign_subscribe_contradictions'] },
          { cat: 'COMPILE',    col: P.slate, tools: ['sovereign_compile_vault'] },
          { cat: 'HANDOFF',    col: P.blue,  tools: ['sovereign_negotiate_handoff', 'sovereign_ack_handoff', 'sovereign_list_pending_handoffs', 'sovereign_await_handoff'] },
          { cat: 'MULTI-AGENT', col: P.amber, tools: ['sovereign_ping_agent_request', 'sovereign_ping_agent_inbox', 'sovereign_ping_agent_decide', 'sovereign_ping_agent_status', 'sovereign_team_runtime', 'sovereign_team_evidence', 'sovereign_team_promotion'] },
        ].map((g) => (
          <div key={g.cat} style={{ display: 'flex', gap: 14, padding: '8px 0', borderBottom: `1px solid ${P.hair}`, alignItems: 'center' }}>
            <div style={{
              fontFamily: MONO, fontSize: 9.5, letterSpacing: 1.5,
              color: g.col, width: 100, flexShrink: 0,
            }}>{g.cat}</div>
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
              {g.tools.map((t) => (
                <span key={t} style={{
                  fontFamily: MONO, fontSize: 11,
                  padding: '3px 9px',
                  borderRadius: 999,
                  border: `1px solid ${P.hair_2}`,
                  background: P.surface,
                  color: P.ink_2,
                }}>{t}</span>
              ))}
            </div>
          </div>
        ))}
      </div>

      {/* — Requirements ————————————————————— */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 14, marginBottom: 28 }}>
        <div style={{ background: P.surface, border: `1px solid ${P.hair}`, borderRadius: 12, padding: 18 }}>
          <Eyebrow style={{ marginBottom: 12 }}>Currently requires</Eyebrow>
          {[
            ['python',  '3.11+'],
            ['node',    '18+'],
            ['platform', 'macOS for AFM features · linux for the rest'],
            ['storage', '~50 MB initial · grows with vault'],
            ['network', 'none required at runtime'],
          ].map(([k, v]) => (
            <div key={k} style={{ display: 'flex', justifyContent: 'space-between', padding: '6px 0', borderTop: `1px solid ${P.hair}` }}>
              <span style={{ fontFamily: MONO, fontSize: 11, color: P.ink_3 }}>{k}</span>
              <span style={{ fontFamily: MONO, fontSize: 11, color: P.ink }}>{v}</span>
            </div>
          ))}
        </div>
        <div style={{ background: P.tint_amber, border: `1px solid ${P.hair_2}`, borderRadius: 12, padding: 18 }}>
          <Eyebrow style={{ marginBottom: 12, color: STATUS.alpha.fg }}>Coming · planned providers</Eyebrow>
          {[
            ['ollama',          'local llm runtime'],
            ['mlx',             'apple silicon native'],
            ['gemma (cloud)',   'google oauth · opt-in · breaks local-only'],
            ['linux-native AFM', 'pending upstream'],
          ].map(([k, v]) => (
            <div key={k} style={{ display: 'flex', justifyContent: 'space-between', padding: '6px 0', borderTop: `1px solid rgba(92,51,8,0.15)` }}>
              <span style={{ fontFamily: MONO, fontSize: 11, color: STATUS.alpha.fg }}>{k}</span>
              <span style={{ fontFamily: MONO, fontSize: 11, color: STATUS.alpha.fg, opacity: 0.75 }}>{v}</span>
            </div>
          ))}
        </div>
      </div>

      {/* — Footer ————————————————————— */}
      <Rule style={{ margin: '4px 0 14px' }} />
      <div style={{ display: 'flex', justifyContent: 'space-between', fontFamily: MONO, fontSize: 10, color: P.ink_3, letterSpacing: 0.5 }}>
        <span>docs/architecture · docs/contracts · docs/CANONICAL-PATHS · docs/TROUBLESHOOTING</span>
        <span>local-only · no telemetry · no cloud sync</span>
      </div>
    </div>
  );
}

Object.assign(window, { ReadmeSpec });
