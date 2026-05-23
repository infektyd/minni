// Sovereign Memory README — collapsible sections (§ 04 — § 10) + footer.

// ═══ § 04 — COMPONENT STATUS ════════════════════════
function SectionStatus({ theme: t }) {
  // Honest distribution: 1 stable / 7 beta / 5 alpha / 2 stub.
  // Each tier gets its own column with equal visual weight.
  const tiers = [
    {
      kind: 'stable',
      count: 3,
      blurb: 'tested, relied upon · breaking changes require migration',
      items: [
        { name: 'SQLite + storage runtime',  desc: '333 tests cover: schema + WAL · FTS5 indexes · additive migrations via PRAGMA user_version · vault indexer · hygiene' },
        { name: 'Local-first governance',    desc: 'docs/contracts/ · POLICY · THREAT_MODEL · AGENT · CAPABILITIES · VAULT · WORKFLOWS · PAGE_TYPES — settled specs' },
        { name: 'Test + verification harness', desc: '454 passing total (333 engine · 121 plugin) · pytest + node --test · release smoke gates' },
      ],
    },
    {
      kind: 'beta',
      count: 7,
      blurb: 'works and is tested · API or behavior may shift before v1',
      items: [
        { name: 'MCP plugin server',         desc: '26 tools across 6 categories · 121 tests · any MCP runtime' },
        { name: 'Hybrid retrieval',          desc: 'FTS5 + FAISS + RRF + rerank · HyDE · token-budget read gates' },
        { name: 'Proposal-first learning',   desc: 'stage → list → resolve · operator-gated · no silent writes' },
        { name: 'Identity + read policy',    desc: 'EffectivePrincipal · vault roots · centralized read gate' },
      ],
      moreCount: 3,
      more: 'Vault + handoff · Multi-agent propagation · Cross-vault search',
    },
    {
      kind: 'alpha',
      count: 6,
      blurb: 'functional but early · expect rough edges and limited real-world validation',
      items: [
        { name: 'AFM provider',              desc: 'macOS-only · bridge default · native via Foundation Models' },
        { name: 'Alt provider backends · in flight', desc: 'Ollama (local) · MLX (apple silicon) · Gemma 4 ⚠ Google OAuth · cloud, opt-in only — breaks local-only posture' },
        { name: 'Cross-agent ping contracts', desc: 'request → inbox → decide → status · vault-backed' },
        { name: 'Compile passes (AFM)',      desc: '5 passes · dry-run by default · review-only drafts' },
      ],
      moreCount: 2,
      more: 'Team coordination · Memory decay tuning',
    },
  ];

  return (
    <div style={{ padding: '14px 0 36px' }}>
      <div style={{
        fontFamily: ds_SANS, fontSize: 14.5, lineHeight: 1.55, color: t.graphite_2,
        maxWidth: 760, marginBottom: 22,
      }}>
        Pre-v1 means most surfaces are still hardening. Honest tiers below — the
        distribution is uneven, the visual signal is not. Each tier gets its own
        column; what's marquee shows, the rest is named below.
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 12 }}>
        {tiers.map((tier) => {
          const s = statusFor(t, tier.kind);
          return (
            <DsPanel key={tier.kind} theme={t} padding={0} style={{ display: 'flex', flexDirection: 'column' }}>
              {/* Column header */}
              <div style={{
                padding: '12px 16px',
                background: s.tint,
                borderBottom: `1px solid ${t.border}`,
              }}>
                <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 4 }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                    <DsDot theme={t} kind={tier.kind} size={8} />
                    <span style={{
                      fontFamily: ds_MONO, fontSize: 11, letterSpacing: 1.4, color: s.fg,
                      fontWeight: 700, textTransform: 'uppercase',
                    }}>{s.label}</span>
                  </div>
                  <span style={{
                    fontFamily: ds_MONO, fontSize: 13, color: s.fg, fontWeight: 700,
                  }}>
                    {String(tier.count).padStart(2, '0')}
                  </span>
                </div>
                <div style={{ fontFamily: ds_MONO, fontSize: 10, color: s.fg, opacity: 0.85, lineHeight: 1.5 }}>
                  {tier.blurb}
                </div>
              </div>

              {/* Items */}
              <div style={{ padding: '4px 16px', flexGrow: 1 }}>
                {tier.items.map((item, i, arr) => (
                  <div key={item.name} style={{
                    padding: '10px 0',
                    borderBottom: i < arr.length - 1 ? `1px solid ${t.border}` : 'none',
                  }}>
                    <div style={{
                      fontFamily: ds_SANS, fontSize: 13, fontWeight: 680, color: t.ink, marginBottom: 3,
                    }}>{item.name}</div>
                    <div style={{
                      fontFamily: ds_MONO, fontSize: 10.5, color: t.graphite_2, lineHeight: 1.45,
                    }}>{item.desc}</div>
                  </div>
                ))}
              </div>

              {/* "+N more" footer */}
              {tier.moreCount && (
                <div style={{
                  padding: '10px 16px',
                  borderTop: `1px solid ${t.border}`,
                  background: t.panel_2,
                }}>
                  <div style={{
                    fontFamily: ds_MONO, fontSize: 10, letterSpacing: 0.6, color: t.graphite_3, marginBottom: 3,
                  }}>+ {tier.moreCount} more</div>
                  <div style={{
                    fontFamily: ds_MONO, fontSize: 10.5, color: t.graphite_2, lineHeight: 1.5,
                  }}>{tier.more}</div>
                </div>
              )}
            </DsPanel>
          );
        })}
      </div>

      <div style={{
        marginTop: 14,
        fontFamily: ds_MONO, fontSize: 10.5, color: t.graphite_3, lineHeight: 1.65,
      }}>
        <span style={{ color: t.graphite_2, fontWeight: 600 }}>not in matrix · </span>
        Qdrant / Lance vector backends (stub · FAISS is the only active backend)  ·  comparative eval vs RAG / wiki-only baselines (planned · harness exists)
      </div>
    </div>
  );
}

// ═══ § 05 — PLUGIN SURFACES (26 TOOLS) ══════════════
function SectionPlugins({ theme: t }) {
  // Categorized from plugins/sovereign-memory/src/server.ts (verified).
  const groups = [
    { cat: 'RECALL',      col: t.blue,         tools: ['sovereign_status','sovereign_recall','sovereign_drill','sovereign_prepare_task','sovereign_prepare_outcome','sovereign_route','sovereign_export_pack'] },
    { cat: 'LEARNING',    col: t.verdigris,    tools: ['sovereign_learn','sovereign_learning_quality','sovereign_resolve_candidate','sovereign_vault_write'] },
    { cat: 'AUDIT',       col: t.mustard_dark, tools: ['sovereign_audit_report','sovereign_audit_tail','sovereign_subscribe_contradictions'] },
    { cat: 'COMPILE',     col: t.graphite_3,   tools: ['sovereign_compile_vault'] },
    { cat: 'HANDOFF',     col: t.blue,         tools: ['sovereign_negotiate_handoff','sovereign_ack_handoff','sovereign_list_pending_handoffs','sovereign_await_handoff'] },
    { cat: 'MULTI-AGENT', col: t.verdigris,    tools: ['sovereign_ping_agent_request','sovereign_ping_agent_inbox','sovereign_ping_agent_decide','sovereign_ping_agent_status','sovereign_team_runtime','sovereign_team_evidence','sovereign_team_promotion'] },
  ];
  const total = groups.reduce((n, g) => n + g.tools.length, 0);

  return (
    <div style={{ padding: '14px 0 36px' }}>
      <div style={{
        fontFamily: ds_SANS, fontSize: 14.5, lineHeight: 1.55, color: t.graphite_2,
        maxWidth: 760, marginBottom: 20,
      }}>
        The plugin implements the <em>Model Context Protocol</em> — any
        MCP-speaking agent can connect via <code style={{ fontFamily: ds_MONO, fontSize: 12, background: t.panel_2, padding: '1px 5px', borderRadius: 2 }}>.mcp.json</code>.
        Convenience manifests below register the same server with specific
        agent runtimes.
      </div>

      {/* Agents row */}
      <DsCollapsibleTable theme={t} label="Compatible agents" meta="6 runtimes" style={{ marginBottom: 18 }}>
        <div style={{ padding: 16, display: 'flex', gap: 8, flexWrap: 'wrap' }}>
          {[
            { n: 'Codex',          m: '.codex-plugin/' },
            { n: 'Claude Code',    m: '.claude-plugin/' },
            { n: 'Gemini',         m: '.gemini-plugin/' },
            { n: 'KiloCode',       m: '.kilocode-plugin/' },
            { n: 'Grok Build',     m: 'plugins/grok-sovereign-memory/' },
            { n: 'Any MCP client', m: '.mcp.json' },
          ].map((a, i) => (
            <div key={a.n} style={{
              padding: '6px 12px',
              borderRadius: 2,
              border: `1px solid ${t.border}`,
              background: i === 5 ? t.verdigris_soft : t.panel,
              fontFamily: ds_MONO, fontSize: 11.5,
              color: i === 5 ? t.verdigris_dark : t.ink,
              display: 'flex', alignItems: 'center', gap: 8,
            }}>
              <span style={{ fontWeight: 600 }}>{a.n}</span>
              <span style={{ color: t.graphite_3, fontSize: 10 }}>{a.m}</span>
            </div>
          ))}
        </div>
      </DsCollapsibleTable>

      {/* Tool surface · condensed */}
      <DsCollapsibleTable theme={t} label={`MCP tool surface · ${total} tools`} meta="plugins/sovereign-memory/src/server.ts" style={{ marginBottom: 14 }}>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 0 }}>
          {groups.map((g, i) => (
            <div key={g.cat} style={{
              padding: '12px 16px',
              borderRight: (i % 3 !== 2) ? `1px solid ${t.border}` : 'none',
              borderBottom: i < 3 ? `1px solid ${t.border}` : 'none',
            }}>
              <div style={{ display: 'flex', alignItems: 'baseline', justifyContent: 'space-between', marginBottom: 4 }}>
                <span style={{ fontFamily: ds_MONO, fontSize: 10, letterSpacing: 1.4, color: g.col, fontWeight: 700 }}>{g.cat}</span>
                <span style={{ fontFamily: ds_MONO, fontSize: 13, color: t.ink, fontWeight: 700 }}>{g.tools.length}</span>
              </div>
              <div style={{ fontFamily: ds_MONO, fontSize: 10.5, color: t.graphite_3, lineHeight: 1.45 }}>
                e.g. <span style={{ color: t.graphite_2 }}>{g.tools[0]}</span>
              </div>
            </div>
          ))}
        </div>
      </DsCollapsibleTable>

      {/* Lifecycle hooks — 4 standard events across 3 runtimes */}
      <DsCollapsibleTable theme={t} label="Lifecycle hooks · 4 events across 3 runtimes" meta="host-invoked · runs alongside MCP" style={{ marginBottom: 14 }}>
        {[
          { evt: 'SessionStart',     runs: ['Claude Code', 'Codex', 'Grok Build'], purpose: 'rehydrate identity + state · inject Layer 1 reminder' },
          { evt: 'UserPromptSubmit', runs: ['Claude Code', 'Codex', 'Grok Build'], purpose: 'intercept · classify intent · record audit (see slash commands below)' },
          { evt: 'PreCompact',       runs: ['Claude Code', 'Codex', 'Grok Build'], purpose: 'snapshot scar tissue before context window compaction' },
          { evt: 'Stop',             runs: ['Claude Code', 'Grok Build'],          purpose: 'end-of-session capture · close audit window' },
        ].map((h, i, arr) => (
          <div key={h.evt} style={{
            display: 'grid', gridTemplateColumns: '160px 240px 1fr',
            padding: '9px 18px', alignItems: 'center', columnGap: 14,
            borderBottom: i < arr.length - 1 ? `1px solid ${t.border}` : 'none',
          }}>
            <div style={{ fontFamily: ds_MONO, fontSize: 12, color: t.ink, fontWeight: 600 }}>{h.evt}</div>
            <div style={{ display: 'flex', gap: 5, flexWrap: 'wrap', alignItems: 'center' }}>
              {h.runs.map((r) => (
                <span key={r} style={{
                  fontFamily: ds_MONO, fontSize: 10,
                  padding: '2px 6px',
                  borderRadius: 2,
                  border: `1px solid ${t.border}`,
                  background: t.panel,
                  color: t.graphite_2,
                }}>{r}</span>
              ))}
            </div>
            <div style={{ fontFamily: ds_MONO, fontSize: 10.5, color: t.graphite_2 }}>{h.purpose}</div>
          </div>
        ))}
      </DsCollapsibleTable>

      {/* Grok-native slash commands — detected inside UserPromptSubmit */}
      <DsCollapsibleTable theme={t} label="Slash commands · Grok Build native" meta="detected in UserPromptSubmit · auto-draft prepare_outcome" style={{ marginBottom: 14 }}>
        {[
          { cmd: '/flush',   purpose: 'Grok-native flush → auto-draft prepare_outcome candidate to inbox' },
          { cmd: '/compact', purpose: 'Grok-native compact → auto-draft prepare_outcome candidate to inbox' },
          { cmd: '/dream',   purpose: 'Grok-native dream → auto-draft prepare_outcome candidate to inbox' },
        ].map((c, i, arr) => (
          <div key={c.cmd} style={{
            display: 'grid', gridTemplateColumns: '160px 1fr',
            padding: '9px 18px', alignItems: 'center', columnGap: 14,
            borderBottom: i < arr.length - 1 ? `1px solid ${t.border}` : 'none',
          }}>
            <div style={{ fontFamily: ds_MONO, fontSize: 12, color: t.ink, fontWeight: 600 }}>{c.cmd}</div>
            <div style={{ fontFamily: ds_MONO, fontSize: 10.5, color: t.graphite_2 }}>{c.purpose}</div>
          </div>
        ))}
      </DsCollapsibleTable>

      <div style={{
        marginTop: 16, padding: '12px 16px',
        background: t.mustard_soft,
        border: `1px solid ${t.border}`,
        borderRadius: 2,
        fontFamily: ds_SANS, fontSize: 12.5, color: t.mustard_dark, lineHeight: 1.5,
      }}>
        <strong style={{ letterSpacing: 0.4, fontWeight: 700 }}>BEHAVIOR ·</strong>{' '}
        Automatic activity is recall-only. Durable learning follows a
        proposal-first path — <code style={{ fontFamily: ds_MONO, fontSize: 11.5 }}>sovereign_learn</code> stages a candidate;
        only <code style={{ fontFamily: ds_MONO, fontSize: 11.5 }}>sovereign_resolve_candidate</code> (operator-gated) writes
        permanent memory. Cross-agent info sharing requires explicit
        vault-backed ping contracts.
      </div>
    </div>
  );
}

// ═══ § 06 — GETTING STARTED ═════════════════════════
function SectionInstall({ theme: t }) {
  return (
    <div style={{ padding: '14px 0 36px' }}>
      <DsCollapsibleTable theme={t} label="Prerequisites" meta="5 requirements" style={{ marginBottom: 22 }}>
        <div style={{ padding: 16, display: 'grid', gridTemplateColumns: '160px 1fr', rowGap: 6, columnGap: 14, fontFamily: ds_MONO, fontSize: 12 }}>
          <div style={{ color: t.graphite_3 }}>python</div>
          <div style={{ color: t.ink }}>3.11 or 3.12 (3.14 may produce partial installs)</div>
          <div style={{ color: t.graphite_3 }}>node</div>
          <div style={{ color: t.ink }}>20 (package.json requires &gt;= 20 &lt; 21)</div>
          <div style={{ color: t.graphite_3 }}>platform</div>
          <div style={{ color: t.ink }}>macOS for AFM features · linux for everything else</div>
          <div style={{ color: t.graphite_3 }}>storage</div>
          <div style={{ color: t.ink }}>~ 50 MB initial · grows with vault size</div>
          <div style={{ color: t.graphite_3 }}>network</div>
          <div style={{ color: t.ink }}>none required at runtime</div>
        </div>
      </DsCollapsibleTable>

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12, marginBottom: 16 }}>
        <DsCode theme={t} collapsible label="ENGINE · python daemon" lines={[
          '$ cd engine',
          '$ python3 -m pip install -r requirements.txt',
          '',
          '# start the daemon',
          '$ python3 sovrd.py \\',
          '    --socket ~/.sovereign-memory/run/sovrd.sock',
        ]} />
        <DsCode theme={t} collapsible label="PLUGIN · typescript MCP server" lines={[
          '$ cd plugins/sovereign-memory',
          '$ npm install',
          '$ npm test',
          '',
          '# optional: local console UI',
          '$ npm run console',
        ]} />
      </div>

      <DsCode theme={t} collapsible label="VERIFY · against the running daemon" style={{ marginBottom: 16 }} lines={[
        '$ cd engine',
        '$ python3 sovrd_client.py --socket ~/.sovereign-memory/run/sovrd.sock status',
        '$ python3 sovrd_client.py --socket ~/.sovereign-memory/run/sovrd.sock search "memory handoff"',
      ]} />

      <DsCollapsibleTable theme={t} label="Multi-agent install · sm-propagation" meta="7 platforms supported">
        <div style={{ padding: 16 }}>
          <div style={{ fontFamily: ds_SANS, fontSize: 13, color: t.graphite_2, lineHeight: 1.5, marginBottom: 10 }}>
            Per-agent setup runs through the <code style={{ fontFamily: ds_MONO, fontSize: 11.5 }}>sm-propagation</code> skill. Each hosted
            agent gets its own workspace envelope at <code style={{ fontFamily: ds_MONO, fontSize: 11.5 }}>~/.sovereign-memory/identities/&lt;agent&gt;/</code>
            and its own vault — no shared state between agents.
          </div>
          <div style={{ fontFamily: ds_MONO, fontSize: 11, color: t.graphite_3 }}>
            supported platforms: codex · claude-code · kilocode · gemini · grok-build · grok-beta (legacy) · all · generic
          </div>
        </div>
      </DsCollapsibleTable>
    </div>
  );
}

// ═══ § 07 — VAULT MODEL ═════════════════════════════
function SectionVault({ theme: t }) {
  return (
    <div style={{ padding: '14px 0 36px' }}>
      <div style={{
        fontFamily: ds_SANS, fontSize: 14.5, lineHeight: 1.55, color: t.graphite_2,
        maxWidth: 760, marginBottom: 18,
      }}>
        Each agent can have its own Obsidian vault while sharing the same
        daemon and database. The vault is the human-readable memory surface;
        SQLite remains runtime truth.
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
        <DsCode theme={t} collapsible label="VAULT LAYOUT" lines={[
          'vault/',
          '  index.md',
          '  log.md',
          '  logs/',
          '  raw/',
          '  wiki/',
          '  wiki/handoffs/',
          '  inbox/',
          '  outbox/',
          '  schema/',
        ]} />
        <DsCollapsibleTable theme={t} label="Write sections" meta="6 types">
          <div style={{ padding: 16 }}>
            <div style={{ fontFamily: ds_SANS, fontSize: 13, color: t.graphite_2, lineHeight: 1.55, marginBottom: 12 }}>
              The <code style={{ fontFamily: ds_MONO, fontSize: 11.5 }}>sovereign_vault_write</code> tool accepts six section types:
            </div>
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
              {['raw','entities','concepts','decisions','syntheses','sessions'].map((s) => (
                <span key={s} style={{
                  fontFamily: ds_MONO, fontSize: 11,
                  padding: '3px 8px', borderRadius: 2,
                  border: `1px solid ${t.border}`, background: t.panel,
                  color: t.graphite_2,
                }}>{s}</span>
              ))}
            </div>
            <DsRule theme={t} style={{ margin: '14px 0 10px' }} />
            <div style={{ fontFamily: ds_MONO, fontSize: 10.5, color: t.graphite_3, lineHeight: 1.5 }}>
              short, sourced wiki pages with frontmatter for durable knowledge ·
              raw/private logs stay local, out of public git
            </div>
          </div>
        </DsCollapsibleTable>
      </div>
    </div>
  );
}

// ═══ § 08 — LOCAL-FIRST SECURITY ════════════════════
function SectionSecurity({ theme: t }) {
  return (
    <div style={{ padding: '14px 0 36px' }}>
      <div style={{
        fontFamily: ds_SANS, fontSize: 14.5, lineHeight: 1.55, color: t.graphite_2,
        maxWidth: 760, marginBottom: 18,
      }}>
        Local-first is real only when four assumptions hold on the host:
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10, marginBottom: 18 }}>
        {[
          ['1', 'User account is the security perimeter',  'single-user host model'],
          ['2', 'FileVault is enabled',                     'database + vault encrypted at rest'],
          ['3', 'No cloud sync over vault or DB',           'iCloud · Dropbox · Drive · OneDrive'],
          ['4', 'Local-only transports',                    'no remote JSON-RPC fallback at v1'],
        ].map(([n, t1, t2]) => (
          <DsPanel key={n} theme={t} padding={14}>
            <div style={{ display: 'flex', alignItems: 'baseline', gap: 12 }}>
              <span style={{ fontFamily: ds_MONO, fontSize: 13, color: t.verdigris, fontWeight: 700, width: 18 }}>{n}.</span>
              <div>
                <div style={{ fontFamily: ds_SANS, fontSize: 13, fontWeight: 680, color: t.ink, marginBottom: 2 }}>{t1}</div>
                <div style={{ fontFamily: ds_MONO, fontSize: 10.5, color: t.graphite_3 }}>{t2}</div>
              </div>
            </div>
          </DsPanel>
        ))}
      </div>

      <DsCode theme={t} collapsible label="EXCLUDE FROM TIME MACHINE + SPOTLIGHT" lines={[
        '$ xattr -w com.apple.metadata:com_apple_backup_excludeItem true \\',
        '    ~/.sovereign-memory/sovereign_memory.db',
        '$ xattr -w com.apple.metadata:com_apple_backup_excludeItem true \\',
        '    ~/.sovereign-memory/codex-vault',
      ]} />

      <div style={{
        marginTop: 14, padding: '12px 16px',
        background: t.persimmon_soft,
        border: `1px solid ${t.border}`,
        borderRadius: 2,
        fontFamily: ds_SANS, fontSize: 12.5, color: t.persimmon_dark, lineHeight: 1.5,
      }}>
        <strong style={{ letterSpacing: 0.4 }}>NOTE ·</strong>{' '}
        Planned providers — Ollama, MLX, Gemma via Google OAuth — break the
        local-only posture if enabled. They are opt-in only and will be
        labeled in-product.
      </div>
    </div>
  );
}

// ═══ § 09 — VERIFICATION GATE ═══════════════════════
function SectionVerify({ theme: t }) {
  return (
    <div style={{ padding: '14px 0 36px' }}>
      <div style={{
        fontFamily: ds_SANS, fontSize: 14.5, lineHeight: 1.55, color: t.graphite_2,
        maxWidth: 760, marginBottom: 18,
      }}>
        Before pushing a release candidate, run the full gate:
      </div>
      <DsCode theme={t} collapsible label="RELEASE SMOKE" lines={[
        '$ cd engine && pytest -q                          # expect 333 passed',
        '$ cd ../plugins/sovereign-memory && npm test      # expect 121 passed',
        '$ npm run smoke:hook',
      ]} />
      <div style={{
        fontFamily: ds_SANS, fontSize: 13, lineHeight: 1.55, color: t.graphite_2,
        maxWidth: 760, marginTop: 14,
      }}>
        Then a temp-state live smoke: start <code style={{ fontFamily: ds_MONO, fontSize: 11.5 }}>sovrd.py</code> on a
        temporary Unix socket, call plugin helpers for status, recall,
        compile dry-run, and handoff, verify redaction and traceability, and
        confirm clean <code style={{ fontFamily: ds_MONO, fontSize: 11.5 }}>SIGTERM</code> shutdown. Migration safety is
        always run on a SQLite backup, never the live DB.
      </div>
    </div>
  );
}

// ═══ § 10 — REPOSITORY MAP ══════════════════════════
function SectionRepoMap({ theme: t }) {
  return (
    <div style={{ padding: '14px 0 36px' }}>
      <DsCollapsibleTable theme={t} label="Top-level directories" meta="6 paths" style={{ marginBottom: 16 }}>
        {[
          ['engine/',                  'python daemon · retrieval · migrations · compile passes · eval harness'],
          ['plugins/sovereign-memory/', 'agent-agnostic MCP plugin · 19 ts modules · 19 test files · console UI'],
          ['sovereign-memory/',        'delivery layer · sm-propagation skill · workflows · design docs'],
          ['openclaw-extension/',      'OpenClaw bridge and import tooling'],
          ['docs/contracts/',          'AGENT · CAPABILITIES · PAGE_TYPES · POLICY · THREAT_MODEL · VAULT · WORKFLOWS'],
          ['eval/',                    'recall fixtures and generated evaluation reports'],
        ].map(([dir, desc], i) => (
          <div key={dir} style={{
            display: 'grid', gridTemplateColumns: '260px 1fr',
            padding: '8px 18px',
            borderBottom: i < 5 ? `1px solid ${t.border}` : 'none',
            alignItems: 'baseline',
          }}>
            <div style={{ fontFamily: ds_MONO, fontSize: 11.5, color: t.ink, fontWeight: 600 }}>{dir}</div>
            <div style={{ fontFamily: ds_MONO, fontSize: 11, color: t.graphite_2 }}>{desc}</div>
          </div>
        ))}
      </DsCollapsibleTable>

      <DsCollapsibleTable theme={t} label="Heavyweight engine files" meta="6 files · by size">
        {[
          ['engine/sovrd.py',            '113 KB', 'local JSON-RPC daemon — the heart of the runtime'],
          ['engine/retrieval.py',        '86 KB',  'FTS5 + FAISS · rerank · HyDE · query expansion · token budgets · read gate'],
          ['engine/principal.py',        '18 KB',  'runtime identity · vault roots · capabilities · read authorization'],
          ['engine/sovereign_memory.py', '15 KB',  'CLI · indexing · stats · hygiene · vector status · compile dry-runs'],
          ['engine/db.py',               '14 KB',  'schema creation · additive migrations via PRAGMA user_version'],
          ['engine/afm_provider.py',     '9 KB',   'normalized AFM contracts · query expansion · neighborhood summary · HyDE'],
        ].map(([f, sz, desc], i, arr) => (
          <div key={f} style={{
            display: 'grid', gridTemplateColumns: '260px 70px 1fr',
            padding: '8px 18px',
            borderBottom: i < arr.length - 1 ? `1px solid ${t.border}` : 'none',
            alignItems: 'baseline',
          }}>
            <div style={{ fontFamily: ds_MONO, fontSize: 11.5, color: t.ink }}>{f}</div>
            <div style={{ fontFamily: ds_MONO, fontSize: 10.5, color: t.graphite_3 }}>{sz}</div>
            <div style={{ fontFamily: ds_MONO, fontSize: 11, color: t.graphite_2 }}>{desc}</div>
          </div>
        ))}
      </DsCollapsibleTable>

      <div style={{
        marginTop: 16, fontFamily: ds_MONO, fontSize: 11, color: t.graphite_3, lineHeight: 1.6,
      }}>
        also see · docs/CANONICAL-PATHS.md · docs/TROUBLESHOOTING.md · docs/ENGINEERING-REVIEW.md · docs/OBSERVED-USAGE.md
      </div>
    </div>
  );
}

// ═══ FOOTER ═════════════════════════════════════════
function ReadmeFooter({ theme: t }) {
  return (
    <div>
      <DsRule theme={t} strong style={{ margin: '32px 0 16px' }} />
      <div style={{
        display: 'flex', justifyContent: 'space-between', alignItems: 'center',
        fontFamily: ds_MONO, fontSize: 10.5, color: t.graphite_3, letterSpacing: 0.6,
      }}>
        <span>infektyd/sovereign-memory  ·  MIT  ·  pre-v1 alpha</span>
        <span>LOCAL-ONLY · NO TELEMETRY · NO CLOUD SYNC · NO REMOTE ENDPOINTS</span>
      </div>
    </div>
  );
}

Object.assign(window, {
  SectionStatus, SectionPlugins, SectionInstall,
  SectionVault, SectionSecurity, SectionVerify, SectionRepoMap,
  ReadmeFooter,
});
