// Layout 1 — DASHBOARD
// Tone: honest-engineering. The status grid is the centerpiece.
// 920 × ~1620

function ReadmeDashboard({ animate = false }) {
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

      {/* — Hero strip ————————————————————————— */}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 24, marginBottom: 20 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 18 }}>
          {/* Concentric-square mark */}
          <svg width="56" height="56" viewBox="0 0 56 56">
            <rect x="0.5" y="0.5" width="55" height="55" fill="none" stroke={P.ink} strokeWidth="1.2" />
            <rect x="8.5" y="8.5" width="39" height="39" fill="none" stroke={P.ink} strokeWidth="1.2" />
            <rect x="16.5" y="16.5" width="23" height="23" fill="none" stroke={P.ink} strokeWidth="1.2" />
            <rect x="24" y="24" width="8" height="8" fill={P.amber} />
          </svg>
          <div>
            <div style={{ fontFamily: SERIF, fontWeight: 500, fontSize: 32, letterSpacing: -0.5, lineHeight: 1.05 }}>
              Sovereign Memory
            </div>
            <div style={{ fontFamily: MONO, fontSize: 12, color: P.ink_2, marginTop: 2 }}>
              identity loads whole · knowledge loads chunked
            </div>
          </div>
        </div>
        <div style={{ display: 'flex', gap: 10 }}>
          {[
            ['454', 'tests passing'],
            ['pre-v1', 'alpha'],
            ['MIT', 'no telemetry'],
          ].map(([n, l]) => (
            <Card key={l} padding={12} style={{ minWidth: 96, textAlign: 'center' }}>
              <div style={{ fontFamily: SERIF, fontSize: 20, fontWeight: 500, lineHeight: 1 }}>{n}</div>
              <div style={{ fontFamily: MONO, fontSize: 10, color: P.ink_3, marginTop: 6, letterSpacing: 0.5 }}>{l}</div>
            </Card>
          ))}
        </div>
      </div>

      <Rule style={{ margin: '4px 0 24px' }} />

      {/* — Pitch card ————————————————————————— */}
      <Card style={{ marginBottom: 28 }} padding={28}>
        <Eyebrow style={{ marginBottom: 12 }}>What this is</Eyebrow>
        <div style={{ fontFamily: SERIF, fontSize: 22, lineHeight: 1.35, color: P.ink, marginBottom: 10, textWrap: 'pretty' }}>
          A local-first memory and governance layer for AI agents — durable
          identity, working state, retrieval, evidence, handoffs, and audit
          trails that stay inspectable on the host machine.
        </div>
        <div style={{ fontFamily: SANS, fontSize: 13.5, lineHeight: 1.55, color: P.ink_2, textWrap: 'pretty' }}>
          Sits between chat-history-as-memory and pure RAG: agents resume with
          typed state, verified evidence, open loops, and a clear next action —
          instead of rediscovering context from scratch.
        </div>
      </Card>

      {/* — Status grid ————————————————————————— */}
      <div style={{ marginBottom: 12, display: 'flex', justifyContent: 'space-between', alignItems: 'baseline' }}>
        <div>
          <Eyebrow>What works right now</Eyebrow>
          <div style={{ fontFamily: SERIF, fontSize: 26, fontWeight: 500, letterSpacing: -0.3, marginTop: 4 }}>
            Component status
          </div>
        </div>
        <div style={{ display: 'flex', gap: 10 }}>
          <Chip kind="stable">stable</Chip>
          <Chip kind="beta">beta</Chip>
          <Chip kind="alpha">alpha</Chip>
          <Chip kind="stub">stub</Chip>
        </div>
      </div>

      <div style={{
        display: 'grid',
        gridTemplateColumns: 'repeat(4, 1fr)',
        gap: 14,
        marginBottom: 28,
      }}>
        {[
          ['stable', 'SQLite runtime',       '333 engine tests · WAL · additive migrations'],
          ['beta',   'Hybrid retrieval',     'FTS5 + FAISS + RRF + rerank · HyDE'],
          ['beta',   'MCP plugin server',    '26 tools · 121 tests · agent-agnostic'],
          ['beta',   'Proposal-first learning', 'stage → list → resolve · operator-gated'],
          ['beta',   'Vault + wiki indexer', 'WikiIndexer → SQLite/FAISS pipeline'],
          ['beta',   'Identity + read policy', 'EffectivePrincipal · centralized read gate'],
          ['beta',   'Handoff (inbox/outbox)', 'vault-backed pages · ack + await'],
          ['beta',   'Multi-agent propagation', 'seed · bootstrap · update · verify'],
          ['alpha',  'Cross-agent ping contracts', 'request → inbox → decide → status'],
          ['alpha',  'AFM provider',         'macOS-only · bridge default · native opt-in'],
          ['alpha',  'Compile passes (AFM)', '5 passes · dry-run only by default'],
          ['alpha',  'Memory decay + scoring', 'exponential decay · access reinforcement'],
        ].map(([kind, name, desc]) => (
          <div key={name} style={{
            background: P.surface,
            border: `1px solid ${P.hair}`,
            borderRadius: 14,
            padding: '14px 14px 12px',
            display: 'flex', flexDirection: 'column', gap: 8,
            minHeight: 108,
          }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <Dot kind={kind} size={7} animate={animate && kind === 'alpha'} />
              <span style={{ fontFamily: MONO, fontSize: 10, color: STATUS[kind].fg, letterSpacing: 0.6, textTransform: 'uppercase' }}>
                {STATUS[kind].label}
              </span>
            </div>
            <div style={{ fontFamily: SANS, fontSize: 13.5, fontWeight: 500, color: P.ink, lineHeight: 1.25 }}>{name}</div>
            <div style={{ fontFamily: MONO, fontSize: 10.5, color: P.ink_3, lineHeight: 1.5 }}>{desc}</div>
          </div>
        ))}
      </div>
      <div style={{ fontFamily: MONO, fontSize: 11, color: P.ink_3, marginBottom: 32, textAlign: 'right' }}>
        + 3 more · team coordination · alt vector backends · comparative eval
      </div>

      {/* — Memory layers viz ————————————————————— */}
      <Card style={{ marginBottom: 28 }}>
        <Eyebrow style={{ marginBottom: 6 }}>How it works</Eyebrow>
        <div style={{ fontFamily: SERIF, fontSize: 24, fontWeight: 500, letterSpacing: -0.3, marginBottom: 18 }}>
          Memory is layered state — not one flat blob.
        </div>
        <div style={{ display: 'grid', gridTemplateColumns: '160px 1fr 110px', rowGap: 12, columnGap: 16, alignItems: 'center' }}>
          {[
            ['Identity',            'load whole',        540, false],
            ['Standing principles', 'load whole · pinned', 540, false],
            ['Project state',       'compact packet',    360, false],
            ['Evidence',            'retrieve by need',  220, true],
            ['Knowledge',           'retrieve chunked',  140, true],
          ].map(([label, rule, w, dashed]) => (
            <React.Fragment key={label}>
              <div style={{ fontFamily: SANS, fontSize: 13.5, fontWeight: 500, color: P.ink }}>{label}</div>
              <div>
                <svg width="100%" height="10" viewBox={`0 0 600 10`} preserveAspectRatio="none">
                  <line x1="0" y1="5" x2={w * 600 / 540} y2="5"
                        stroke={dashed ? P.amber : P.ink}
                        strokeWidth="5"
                        strokeDasharray={dashed ? '3 5' : ''}
                        strokeLinecap="round" />
                </svg>
              </div>
              <div style={{ fontFamily: MONO, fontSize: 11, color: P.ink_3 }}>{rule}</div>
            </React.Fragment>
          ))}
        </div>
      </Card>

      {/* — Quickstart ————————————————————————— */}
      <div style={{ marginBottom: 14, display: 'flex', justifyContent: 'space-between', alignItems: 'baseline' }}>
        <div>
          <Eyebrow>Try it</Eyebrow>
          <div style={{ fontFamily: SERIF, fontSize: 24, fontWeight: 500, letterSpacing: -0.3, marginTop: 4 }}>
            Up in about 60 seconds
          </div>
        </div>
        <div style={{ fontFamily: MONO, fontSize: 11, color: P.ink_3 }}>requires python 3.11+ · node 18+</div>
      </div>
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 14, marginBottom: 28 }}>
        <CodeBlock label="ENGINE" lines={[
          '$ cd engine',
          '$ python3 -m pip install -r requirements.txt',
          '$ python3 sovrd.py --socket ~/.sm/run/sovrd.sock',
        ]} />
        <CodeBlock label="PLUGIN" lines={[
          '$ cd plugins/sovereign-memory',
          '$ npm install && npm test',
          '$ npm run console',
        ]} />
      </div>

      {/* — Agents row ————————————————————————— */}
      <Card style={{ marginBottom: 28 }} padding={22}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 14 }}>
          <div>
            <Eyebrow>Plugin surfaces</Eyebrow>
            <div style={{ fontFamily: SERIF, fontSize: 20, fontWeight: 500, marginTop: 4 }}>
              Works with any MCP-speaking agent
            </div>
          </div>
          <div style={{ fontFamily: MONO, fontSize: 11, color: P.ink_3 }}>
            convenience manifests for each →
          </div>
        </div>
        <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
          {['Codex', 'Claude Code', 'Gemini', 'KiloCode', 'Grok', 'Any MCP client'].map((a, i) => (
            <span key={a} style={{
              padding: '8px 14px',
              borderRadius: 999,
              border: `1px solid ${P.hair_2}`,
              background: i === 5 ? P.tint_amber : P.surface,
              fontFamily: MONO, fontSize: 12,
              color: i === 5 ? STATUS.alpha.fg : P.ink,
            }}>
              {a}
            </span>
          ))}
        </div>
      </Card>

      {/* — Footer ————————————————————————— */}
      <Rule style={{ margin: '8px 0 14px' }} />
      <div style={{ display: 'flex', justifyContent: 'space-between', fontFamily: MONO, fontSize: 11, color: P.ink_3 }}>
        <div style={{ display: 'flex', gap: 18 }}>
          <span>architecture →</span>
          <span>plugin surfaces →</span>
          <span>vault model →</span>
          <span>eval →</span>
          <span>security →</span>
        </div>
        <div>local-only · no telemetry · no cloud sync</div>
      </div>
    </div>
  );
}

Object.assign(window, { ReadmeDashboard });
