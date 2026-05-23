// Sovereign Memory README — full Narrative direction, v2.
// Aligned to the project's own DESIGN.md (bone + verdigris + persimmon + mustard).
// All content verified against actual source files (server.ts, package.json, requirements.txt).
//
// Usage: <ReadmeFinal theme="light" /> or theme="dark"
// Width: 1000px. Height varies with which sections are open (default: all open ~ 4400px).

const ALL_OPEN = { s04: true, s05: true, s06: true, s07: true, s08: true, s09: true, s10: true };

function ReadmeFinal({ theme: themeKey = 'light' }) {
  const t = themeKey === 'dark' ? DARK : LIGHT;
  const [open, setOpen] = React.useState(ALL_OPEN);
  const toggle = (k) => setOpen((p) => ({ ...p, [k]: !p[k] }));
  const setAll = (v) => setOpen({ s04: v, s05: v, s06: v, s07: v, s08: v, s09: v, s10: v });

  return (
    <div style={{
      width: 1000,
      background: t.bg,
      fontFamily: ds_SANS,
      color: t.ink,
      padding: '36px 40px 48px',
      boxSizing: 'border-box',
    }}>
      {/* ——————————————————————————————————————— HERO ——— */}
      <ReadmeHero theme={t} />

      {/* ——————————————————————————————————————— CONTENTS NAV ——— */}
      <ReadmeContents theme={t} open={open} toggle={toggle} setAll={setAll} />

      {/* ——————————————————————————————————————— ACT I ——— */}
      <ActI theme={t} />

      {/* ——————————————————————————————————————— ACT II ——— */}
      <ActII theme={t} />

      {/* ——————————————————————————————————————— ACT III ——— */}
      <ActIII theme={t} />

      {/* ——————————————————————————————————————— § 04 STATUS ——— */}
      <DsCollapsibleHeader theme={t} n={4} title="Component status" anchor="s04"
        open={open.s04} onToggle={() => toggle('s04')} />
      {open.s04 && <SectionStatus theme={t} />}

      {/* ——————————————————————————————————————— § 05 PLUGIN ——— */}
      <DsCollapsibleHeader theme={t} n={5} title="Plugin surfaces · 26 MCP tools" anchor="s05"
        open={open.s05} onToggle={() => toggle('s05')} />
      {open.s05 && <SectionPlugins theme={t} />}

      {/* ——————————————————————————————————————— § 06 INSTALL ——— */}
      <DsCollapsibleHeader theme={t} n={6} title="Getting started" anchor="s06"
        open={open.s06} onToggle={() => toggle('s06')} />
      {open.s06 && <SectionInstall theme={t} />}

      {/* ——————————————————————————————————————— § 07 VAULT ——— */}
      <DsCollapsibleHeader theme={t} n={7} title="Vault model" anchor="s07"
        open={open.s07} onToggle={() => toggle('s07')} />
      {open.s07 && <SectionVault theme={t} />}

      {/* ——————————————————————————————————————— § 08 SECURITY ——— */}
      <DsCollapsibleHeader theme={t} n={8} title="Local-first security" anchor="s08"
        open={open.s08} onToggle={() => toggle('s08')} />
      {open.s08 && <SectionSecurity theme={t} />}

      {/* ——————————————————————————————————————— § 09 VERIFY ——— */}
      <DsCollapsibleHeader theme={t} n={9} title="Verification gate" anchor="s09"
        open={open.s09} onToggle={() => toggle('s09')} />
      {open.s09 && <SectionVerify theme={t} />}

      {/* ——————————————————————————————————————— § 10 REPO MAP ——— */}
      <DsCollapsibleHeader theme={t} n={10} title="Repository map" anchor="s10"
        open={open.s10} onToggle={() => toggle('s10')} />
      {open.s10 && <SectionRepoMap theme={t} />}

      {/* ——————————————————————————————————————— FOOTER ——— */}
      <ReadmeFooter theme={t} />
    </div>
  );
}

// ═══ HERO ═══════════════════════════════════════════
function ReadmeHero({ theme: t }) {
  return (
    <div style={{ marginBottom: 36 }}>
      <DsLabel theme={t} style={{ marginBottom: 20 }}>
        a local-first memory + governance layer for AI agents · pre-v1 alpha
      </DsLabel>
      <div style={{
        fontFamily: ds_SANS, fontSize: 56, fontWeight: 720, letterSpacing: -1.2,
        lineHeight: 1.02, color: t.ink, marginBottom: 18,
      }}>
        Sovereign Memory
      </div>
      <div style={{
        fontFamily: ds_SANS, fontSize: 19, lineHeight: 1.5, color: t.graphite_2,
        maxWidth: 700, marginBottom: 24, fontStyle: 'italic', fontWeight: 420,
      }}>
        Long-running agents forget. Sovereign Memory remembers — locally, with
        an audit trail, on your terms.
      </div>
      <div style={{
        display: 'flex', gap: 14, alignItems: 'center', flexWrap: 'wrap',
        fontFamily: ds_MONO, fontSize: 11, color: t.graphite_3, letterSpacing: 0.6,
      }}>
        <span>454 TESTS PASSING</span>
        <span style={{ color: t.border_strong }}>·</span>
        <span>26 MCP TOOLS</span>
        <span style={{ color: t.border_strong }}>·</span>
        <span>MIT</span>
        <span style={{ color: t.border_strong }}>·</span>
        <span>NO TELEMETRY</span>
        <span style={{ color: t.border_strong }}>·</span>
        <span>NO CLOUD SYNC</span>
      </div>
      <DsRule theme={t} strong style={{ marginTop: 28 }} />
    </div>
  );
}

// ═══ CONTENTS NAV ═══════════════════════════════════
function ReadmeContents({ theme: t, open, toggle, setAll }) {
  const sections = [
    { n: 'I',   id: 's01', title: 'The problem',           static: true },
    { n: 'II',  id: 's02', title: 'The bet',               static: true },
    { n: 'III', id: 's03', title: 'The proof',             static: true },
    { n: '04',  id: 's04', title: 'Component status',      key: 's04', status: 'beta' },
    { n: '05',  id: 's05', title: 'Plugin surfaces',       key: 's05' },
    { n: '06',  id: 's06', title: 'Getting started',       key: 's06' },
    { n: '07',  id: 's07', title: 'Vault model',           key: 's07' },
    { n: '08',  id: 's08', title: 'Local-first security',  key: 's08' },
    { n: '09',  id: 's09', title: 'Verification gate',     key: 's09' },
    { n: '10',  id: 's10', title: 'Repository map',        key: 's10' },
  ];
  return (
    <DsPanel theme={t} style={{ marginBottom: 44 }} padding={18}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
        <DsLabel theme={t}>Contents</DsLabel>
        <div style={{ display: 'flex', gap: 14, fontFamily: ds_MONO, fontSize: 10.5, color: t.graphite_3 }}>
          <span onClick={() => setAll(true)}  style={{ cursor: 'pointer' }}>expand all</span>
          <span style={{ color: t.border }}>·</span>
          <span onClick={() => setAll(false)} style={{ cursor: 'pointer' }}>collapse all</span>
        </div>
      </div>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(2, 1fr)', columnGap: 16, rowGap: 4 }}>
        {sections.map((s) => {
          const isOpen = s.static ? true : open[s.key];
          return (
            <a key={s.id} href={`#${s.id}`}
               onClick={(e) => { if (!s.static) { e.preventDefault(); toggle(s.key); } }}
               style={{
                 display: 'flex', alignItems: 'center', gap: 10,
                 padding: '6px 0', textDecoration: 'none',
                 cursor: 'pointer',
               }}>
              <span style={{ fontFamily: ds_MONO, fontSize: 10.5, color: t.verdigris, width: 36, fontWeight: 600, flexShrink: 0 }}>§ {s.n}</span>
              <span style={{ fontFamily: ds_SANS, fontSize: 13, color: t.ink, flexGrow: 1 }}>{s.title}</span>
              {!s.static && (
                <span style={{ fontFamily: ds_MONO, fontSize: 9.5, color: t.graphite_3, flexShrink: 0, whiteSpace: 'nowrap' }}>
                  {isOpen ? '▾' : '▸'}
                </span>
              )}
            </a>
          );
        })}
      </div>
    </DsPanel>
  );
}

// ═══ ACT I — THE PROBLEM ════════════════════════════
function ActI({ theme: t }) {
  return (
    <div id="s01" style={{ marginBottom: 44 }}>
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 14, marginBottom: 12 }}>
        <span style={{ fontFamily: ds_MONO, fontSize: 11, color: t.verdigris, fontWeight: 600, letterSpacing: 1.4 }}>§ I</span>
        <DsLabel theme={t}>The problem</DsLabel>
      </div>
      <div style={{
        fontFamily: ds_SANS, fontSize: 32, fontWeight: 680, letterSpacing: -0.5,
        lineHeight: 1.15, marginBottom: 12, color: t.ink, maxWidth: 820,
      }}>
        Agent memory keeps failing in three predictable ways.
      </div>
      <div style={{
        fontFamily: ds_SANS, fontSize: 15, lineHeight: 1.55, color: t.graphite_2,
        maxWidth: 720, marginBottom: 22,
      }}>
        Every long-running agent eventually hits the wall of "what did I learn
        last session?" The current answers all break in their own way.
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 12 }}>
        {[
          { name: 'Chat history',         good: 'simple · works out of the box',  bad: 'opaque, bloated, hard to audit over time' },
          { name: 'RAG over files',       good: 'useful for lookup',              bad: 'rediscovers context · loses working state' },
          { name: 'Wiki / markdown notes', good: 'human-readable, durable',        bad: 'weak provenance, poor contradiction handling' },
        ].map((p) => (
          <DsPanel key={p.name} theme={t} padding={16}>
            <div style={{ fontFamily: ds_SANS, fontSize: 15, fontWeight: 680, color: t.ink, marginBottom: 12 }}>{p.name}</div>
            <DsRule theme={t} style={{ marginBottom: 10 }} />
            <DsLabel theme={t} style={{ color: t.verdigris_dark, marginBottom: 4 }}>Works</DsLabel>
            <div style={{ fontFamily: ds_SANS, fontSize: 12.5, color: t.ink, lineHeight: 1.45, marginBottom: 12 }}>{p.good}</div>
            <DsLabel theme={t} style={{ color: t.persimmon_dark, marginBottom: 4 }}>Breaks on</DsLabel>
            <div style={{ fontFamily: ds_SANS, fontSize: 12.5, color: t.ink, lineHeight: 1.45 }}>{p.bad}</div>
          </DsPanel>
        ))}
      </div>
    </div>
  );
}

// ═══ ACT II — THE BET ═══════════════════════════════
function ActII({ theme: t }) {
  const rows = [
    ['Identity',            'who · role · constraints',         540, false],
    ['Standing principles', 'durable rules across sessions',    540, false],
    ['Project state',       'active branch · open loops · next', 360, false],
    ['Evidence',            'source-backed facts · citations',  220, true],
    ['Knowledge',           'wiki · docs · history',            140, true],
  ];
  return (
    <div id="s02" style={{ marginBottom: 44 }}>
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 14, marginBottom: 12 }}>
        <span style={{ fontFamily: ds_MONO, fontSize: 11, color: t.verdigris, fontWeight: 600, letterSpacing: 1.4 }}>§ II</span>
        <DsLabel theme={t}>The bet</DsLabel>
      </div>
      <div style={{
        fontFamily: ds_SANS, fontSize: 32, fontWeight: 680, letterSpacing: -0.5,
        lineHeight: 1.15, marginBottom: 12, color: t.ink, maxWidth: 820,
      }}>
        Memory should be layered state, not one flat blob.
      </div>
      <div style={{
        fontFamily: ds_SANS, fontSize: 15, lineHeight: 1.55, color: t.graphite_2,
        maxWidth: 720, marginBottom: 22,
      }}>
        Each layer carries its own load rule — what comes back every session
        versus what's retrieved only when needed. Identity is small and always
        present. Knowledge is large and arrives in chunks, cited.
      </div>

      <DsPanel theme={t} padding={22}>
        <div style={{ display: 'grid', gridTemplateColumns: '190px 1fr 110px', rowGap: 14, columnGap: 18, alignItems: 'center' }}>
          {rows.map(([label, desc, w, dashed]) => (
            <React.Fragment key={label}>
              <div>
                <div style={{ fontFamily: ds_SANS, fontSize: 14, fontWeight: 680, color: t.ink }}>{label}</div>
                <div style={{ fontFamily: ds_MONO, fontSize: 10.5, color: t.graphite_3, marginTop: 1 }}>{desc}</div>
              </div>
              <svg width="100%" height="8" viewBox="0 0 600 8" preserveAspectRatio="none">
                <line x1="0" y1="4" x2={w * 600 / 540} y2="4"
                      stroke={dashed ? t.mustard_dark : t.verdigris}
                      strokeWidth="6"
                      strokeDasharray={dashed ? '3 5' : ''}
                      strokeLinecap="square" />
              </svg>
              <div style={{ fontFamily: ds_MONO, fontSize: 10.5, color: dashed ? t.mustard_dark : t.verdigris_dark, textAlign: 'right', fontWeight: 600, letterSpacing: 0.4 }}>
                {dashed ? 'RETRIEVE' : 'LOAD WHOLE'}
              </div>
            </React.Fragment>
          ))}
        </div>
        <DsRule theme={t} style={{ margin: '18px 0 12px' }} />
        <div style={{ fontFamily: ds_MONO, fontSize: 10.5, color: t.graphite_3, textAlign: 'center', letterSpacing: 0.4 }}>
          solid: loaded every session · dashed: pulled on demand, cited, never assumed in context
        </div>
      </DsPanel>
    </div>
  );
}

// ═══ ACT III — THE PROOF ════════════════════════════
function ActIII({ theme: t }) {
  const rows = [
    { k: 'verified_now',          v: 'auth.md · build-config.yml · deploy-target=staging',                  h: 'checked against current artifacts',  kind: 'stable' },
    { k: 'remembered_unverified', v: 'feature-flag rollout still pending review',                            h: 'plausible — needs confirmation',    kind: 'beta' },
    { k: 'open_loops',            v: 'migration smoke-test · rotate audit token · finalize compile dry-run', h: 'left incomplete last session',      kind: 'alpha' },
    { k: 'first_verification',    v: '$ make verify   →   diff against current branch',                      h: 'next concrete check before acting', kind: 'alpha' },
    { k: 'do_not_claim',          v: 'old /v1 API surface (removed 2 sessions ago)',                          h: 'stale or contradicted',             kind: 'stub' },
  ];
  return (
    <div id="s03" style={{ marginBottom: 48 }}>
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 14, marginBottom: 12 }}>
        <span style={{ fontFamily: ds_MONO, fontSize: 11, color: t.verdigris, fontWeight: 600, letterSpacing: 1.4 }}>§ III</span>
        <DsLabel theme={t}>The proof</DsLabel>
      </div>
      <div style={{
        fontFamily: ds_SANS, fontSize: 32, fontWeight: 680, letterSpacing: -0.5,
        lineHeight: 1.15, marginBottom: 12, color: t.ink, maxWidth: 820,
      }}>
        What a session rehydration actually produces.
      </div>
      <div style={{
        fontFamily: ds_SANS, fontSize: 15, lineHeight: 1.55, color: t.graphite_2,
        maxWidth: 720, marginBottom: 22,
      }}>
        A resumed session doesn't retrieve documents — it produces the smallest
        packet that lets an agent resume safely. Verified facts, plausible-but-
        unconfirmed state, open loops, the next concrete check, and an explicit
        "do not claim" list.
      </div>

      <DsPanel theme={t} padding={0}>
        {/* card header */}
        <div style={{
          display: 'flex', justifyContent: 'space-between', alignItems: 'center',
          padding: '12px 18px',
          borderBottom: `1px solid ${t.border}`,
          background: t.bg_2,
        }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            <DsDot theme={t} kind="alpha" size={6} />
            <span style={{ fontFamily: ds_MONO, fontSize: 10.5, letterSpacing: 1, color: t.graphite_2, fontWeight: 600 }}>
              SESSION REHYDRATE  ·  agent=codex  ·  vault=codex-vault  ·  policy=local-only
            </span>
          </div>
          <span style={{ fontFamily: ds_MONO, fontSize: 10.5, color: t.graphite_3 }}>68 tokens · 14ms</span>
        </div>
        {/* time gap */}
        <div style={{
          padding: '8px 18px',
          fontFamily: ds_MONO, fontSize: 11, color: t.graphite_3,
          borderBottom: `1px solid ${t.border}`,
          display: 'flex', justifyContent: 'space-between',
        }}>
          <span>last session ended  ·  3 days, 14:22:08 ago</span>
          <span>resume  →</span>
        </div>
        {/* rows */}
        <div style={{ padding: '6px 18px 18px' }}>
          {rows.map((r, i, arr) => (
            <div key={r.k} style={{ padding: '11px 0', borderBottom: i < arr.length - 1 ? `1px solid ${t.border}` : 'none' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 3 }}>
                <DsDot theme={t} kind={r.kind} size={6} />
                <span style={{ fontFamily: ds_MONO, fontSize: 11.5, color: t.graphite_2, minWidth: 200, fontWeight: 600 }}>{r.k}</span>
                <span style={{ fontFamily: ds_MONO, fontSize: 12.5, color: t.ink }}>{r.v}</span>
              </div>
              <div style={{ fontFamily: ds_MONO, fontSize: 10.5, color: t.graphite_3, marginLeft: 18 }}>↳ {r.h}</div>
            </div>
          ))}
        </div>
      </DsPanel>
      <div style={{ fontFamily: ds_MONO, fontSize: 10.5, color: t.graphite_3, marginTop: 10, textAlign: 'center', letterSpacing: 0.4 }}>
        fig. iii — the artifact, not the agent
      </div>
    </div>
  );
}

Object.assign(window, { ReadmeFinal });
