// Layout 2 — NARRATIVE
// Tone: hybrid honest + confident. Three-act magazine feature.
// 820 × ~1900

function ReadmeNarrative() {
  return (
    <div style={{
      width: 820,
      background: P.bg,
      fontFamily: SANS,
      color: P.ink,
      padding: '44px 56px 48px',
      boxSizing: 'border-box',
    }}>
      <SharedStyles />

      {/* — Hero ————————————————————————— */}
      <div style={{ marginBottom: 44 }}>
        <Eyebrow style={{ marginBottom: 24 }}>A memory layer for agents · pre-v1 alpha</Eyebrow>
        <div style={{
          fontFamily: SERIF, fontSize: 62, fontWeight: 500, letterSpacing: -1.5,
          lineHeight: 1, marginBottom: 18,
        }}>
          Sovereign<br />Memory
        </div>
        <div style={{
          fontFamily: SERIF, fontSize: 22, lineHeight: 1.45, color: P.ink_2,
          maxWidth: 620, textWrap: 'balance', marginBottom: 24, fontStyle: 'italic',
        }}>
          Long-running agents forget. Sovereign Memory remembers — locally, with
          an audit trail, on your terms.
        </div>
        <div style={{
          display: 'flex', gap: 18, fontFamily: MONO, fontSize: 11,
          color: P.ink_3, letterSpacing: 0.8,
        }}>
          <span>454 TESTS PASSING</span>
          <span>·</span>
          <span>MIT</span>
          <span>·</span>
          <span>NO TELEMETRY</span>
          <span>·</span>
          <span>NO CLOUD SYNC</span>
        </div>
      </div>

      <Rule style={{ margin: '0 0 44px' }} />

      {/* — ACT I — The problem ————————————————————— */}
      <div style={{ marginBottom: 48 }}>
        <div style={{ display: 'flex', alignItems: 'baseline', gap: 14, marginBottom: 10 }}>
          <SectionNumber n={1} />
          <Eyebrow>The problem</Eyebrow>
        </div>
        <div style={{
          fontFamily: SERIF, fontSize: 36, fontWeight: 500, letterSpacing: -0.6,
          lineHeight: 1.1, marginBottom: 14, textWrap: 'balance',
        }}>
          Agent memory keeps failing in three predictable ways.
        </div>
        <div style={{
          fontFamily: SANS, fontSize: 14, lineHeight: 1.6, color: P.ink_2,
          maxWidth: 600, marginBottom: 28, textWrap: 'pretty',
        }}>
          Every long-running agent eventually hits the wall of "what did I learn
          last session?" The current answers all break in their own way.
        </div>

        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 14 }}>
          {[
            { name: 'Chat history',         good: 'simple · works out of the box', bad: 'opaque, bloated, hard to audit over time' },
            { name: 'RAG over files',       good: 'useful for lookup',              bad: 'rediscovers context · loses working state' },
            { name: 'Markdown / wiki notes', good: 'human-readable, durable',       bad: 'weak provenance · poor contradiction handling' },
          ].map((p) => (
            <div key={p.name} style={{
              background: P.surface,
              border: `1px solid ${P.hair}`,
              borderRadius: 16,
              padding: 18,
              minHeight: 200,
            }}>
              <div style={{ fontFamily: SERIF, fontSize: 18, fontWeight: 500, marginBottom: 16 }}>{p.name}</div>
              <Rule style={{ marginBottom: 12 }} />
              <div style={{ fontFamily: MONO, fontSize: 10, color: P.ink_3, letterSpacing: 1, marginBottom: 4 }}>WORKS</div>
              <div style={{ fontFamily: SANS, fontSize: 12.5, color: P.ink, lineHeight: 1.4, marginBottom: 14 }}>{p.good}</div>
              <div style={{ fontFamily: MONO, fontSize: 10, color: P.amber, letterSpacing: 1, marginBottom: 4 }}>BREAKS ON</div>
              <div style={{ fontFamily: SANS, fontSize: 12.5, color: P.ink, lineHeight: 1.4 }}>{p.bad}</div>
            </div>
          ))}
        </div>
      </div>

      {/* — ACT II — The bet ——————————————————————— */}
      <div style={{ marginBottom: 48 }}>
        <div style={{ display: 'flex', alignItems: 'baseline', gap: 14, marginBottom: 10 }}>
          <SectionNumber n={2} />
          <Eyebrow>The bet</Eyebrow>
        </div>
        <div style={{
          fontFamily: SERIF, fontSize: 36, fontWeight: 500, letterSpacing: -0.6,
          lineHeight: 1.1, marginBottom: 14, textWrap: 'balance',
        }}>
          Memory should be layered state, not one flat blob.
        </div>
        <div style={{
          fontFamily: SANS, fontSize: 14, lineHeight: 1.6, color: P.ink_2,
          maxWidth: 600, marginBottom: 28, textWrap: 'pretty',
        }}>
          Each layer carries its own load rule — what comes back every session
          versus what's retrieved only when needed. Identity is small and always
          present. Knowledge is large and comes in chunks, cited.
        </div>

        <Card padding={28}>
          <div style={{ display: 'grid', gridTemplateColumns: '170px 1fr 130px', rowGap: 16, columnGap: 18, alignItems: 'center' }}>
            {[
              ['Identity',            540, false, 'who · role · constraints'],
              ['Standing principles', 540, false, 'durable rules'],
              ['Project state',       360, false, 'compact packet'],
              ['Evidence',            220, true,  'source-backed facts'],
              ['Knowledge',           140, true,  'wiki / docs / history'],
            ].map(([label, w, dashed, desc]) => (
              <React.Fragment key={label}>
                <div>
                  <div style={{ fontFamily: SERIF, fontSize: 17, fontWeight: 500 }}>{label}</div>
                  <div style={{ fontFamily: MONO, fontSize: 10.5, color: P.ink_3 }}>{desc}</div>
                </div>
                <svg width="100%" height="10" viewBox="0 0 600 10" preserveAspectRatio="none">
                  <line x1="0" y1="5" x2={w * 600 / 540} y2="5"
                        stroke={dashed ? P.amber : P.ink}
                        strokeWidth="6"
                        strokeDasharray={dashed ? '3 5' : ''}
                        strokeLinecap="round" />
                </svg>
                <div style={{ fontFamily: MONO, fontSize: 11, color: dashed ? P.amber : P.ink_2, textAlign: 'right' }}>
                  {dashed ? 'retrieve' : 'load whole'}
                </div>
              </React.Fragment>
            ))}
          </div>
          <Rule style={{ margin: '20px 0 12px' }} />
          <div style={{ fontFamily: MONO, fontSize: 11, color: P.ink_3, textAlign: 'center' }}>
            solid: loaded every session  ·  dashed: pulled on demand, cited
          </div>
        </Card>
      </div>

      {/* — ACT III — The proof ————————————————————— */}
      <div style={{ marginBottom: 48 }}>
        <div style={{ display: 'flex', alignItems: 'baseline', gap: 14, marginBottom: 10 }}>
          <SectionNumber n={3} />
          <Eyebrow>The proof</Eyebrow>
        </div>
        <div style={{
          fontFamily: SERIF, fontSize: 36, fontWeight: 500, letterSpacing: -0.6,
          lineHeight: 1.1, marginBottom: 14, textWrap: 'balance',
        }}>
          What rehydration actually looks like.
        </div>
        <div style={{
          fontFamily: SANS, fontSize: 14, lineHeight: 1.6, color: P.ink_2,
          maxWidth: 600, marginBottom: 28, textWrap: 'pretty',
        }}>
          A resumed session doesn't retrieve documents. It produces the smallest
          packet that lets an agent resume safely — verified facts, plausible-
          but-unconfirmed state, open loops, the next concrete check, and an
          explicit "do not claim" list.
        </div>

        <Card padding={0}>
          {/* Card header */}
          <div style={{
            display: 'flex', justifyContent: 'space-between', alignItems: 'center',
            padding: '14px 22px',
            borderBottom: `1px solid ${P.hair}`,
            background: 'rgba(26,24,20,0.025)',
          }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
              <Dot kind="alpha" size={7} />
              <span style={{ fontFamily: MONO, fontSize: 11, letterSpacing: 1, color: P.ink_2 }}>
                SESSION REHYDRATE  ·  agent=codex  ·  vault=codex-vault
              </span>
            </div>
            <span style={{ fontFamily: MONO, fontSize: 11, color: P.ink_3 }}>68 tokens · 14ms</span>
          </div>

          {/* Time gap */}
          <div style={{
            padding: '10px 22px',
            fontFamily: MONO, fontSize: 11, color: P.ink_3,
            borderBottom: `1px solid ${P.hair}`,
            display: 'flex', justifyContent: 'space-between',
          }}>
            <span>last session ended  ·  3 days, 14:22:08 ago</span>
            <span>resume  →</span>
          </div>

          {/* Rehydration rows */}
          <div style={{ padding: '8px 22px 22px' }}>
            {[
              { k: 'verified_now',           v: 'auth.md · build-config.yml · deploy-target=staging',                    h: 'checked against current artifacts',  d: P.green },
              { k: 'remembered_unverified',  v: 'feature-flag rollout still pending review',                              h: 'plausible — needs confirmation',     d: P.blue  },
              { k: 'open_loops',             v: 'migration smoke-test · rotate audit token · finalize compile dry-run',  h: 'left incomplete last session',       d: P.amber },
              { k: 'first_verification',     v: '$ make verify   →   diff against current branch',                       h: 'next concrete check before acting',  d: P.amber },
              { k: 'do_not_claim',           v: 'old /v1 API surface (removed 2 sessions ago)',                           h: 'stale or contradicted',              d: P.slate },
            ].map((r, i, arr) => (
              <div key={r.k} style={{ padding: '12px 0', borderBottom: i < arr.length - 1 ? `1px solid ${P.hair}` : 'none' }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 4 }}>
                  <span style={{ width: 8, height: 8, borderRadius: '50%', background: r.d, flexShrink: 0 }} />
                  <span style={{ fontFamily: MONO, fontSize: 12, color: P.ink_2, minWidth: 180 }}>{r.k}</span>
                  <span style={{ fontFamily: MONO, fontSize: 12.5, color: P.ink }}>{r.v}</span>
                </div>
                <div style={{ fontFamily: MONO, fontSize: 10.5, color: P.ink_3, marginLeft: 20 }}>↳ {r.h}</div>
              </div>
            ))}
          </div>
        </Card>
        <div style={{ fontFamily: MONO, fontSize: 11, color: P.ink_3, marginTop: 12, textAlign: 'center' }}>
          fig. iii — the artifact, not the agent
        </div>
      </div>

      {/* — Try it ————————————————————————— */}
      <div style={{ marginBottom: 36 }}>
        <Eyebrow style={{ marginBottom: 8 }}>How to try it</Eyebrow>
        <div style={{
          fontFamily: SERIF, fontSize: 28, fontWeight: 500, letterSpacing: -0.4,
          marginBottom: 18, textWrap: 'balance',
        }}>
          Two terminals, about a minute.
        </div>
        <CodeBlock lines={[
          '# 1. start the daemon',
          '$ cd engine && python3 sovrd.py --socket ~/.sm/run/sovrd.sock',
          '',
          '# 2. install the plugin',
          '$ cd ../plugins/sovereign-memory && npm install && npm test',
          '',
          '# 3. verify',
          '$ python3 ../../engine/sovrd_client.py status',
        ]} />
        <div style={{
          marginTop: 16,
          padding: '12px 16px',
          background: P.tint_amber,
          border: `1px solid ${P.hair_2}`,
          borderRadius: 12,
          fontFamily: MONO, fontSize: 12, color: STATUS.alpha.fg, lineHeight: 1.5,
        }}>
          <strong style={{ letterSpacing: 0.5 }}>PRE-V1 ALPHA</strong>  ·  core subsystems work and are tested · integration depth varies · see the full status table for what's solid, what's early, what's stubbed.
        </div>
      </div>

      {/* — Read more ————————————————————————— */}
      <Rule style={{ margin: '24px 0 20px' }} />
      <Eyebrow style={{ marginBottom: 12 }}>Deeper reads</Eyebrow>
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 10 }}>
        {[
          ['Architecture',     'the daemon, the plugin, the storage layer'],
          ['Threat model',     'what local-first actually means'],
          ['Vault model',      'how the readable surface is organized'],
          ['MCP tool surface', '26 tools across recall, learning, handoff'],
          ['Eval methodology', 'how recovery quality is judged'],
          ['Roadmap to v1',    'what changes before stable'],
        ].map(([t, d]) => (
          <div key={t} style={{
            background: P.surface,
            border: `1px solid ${P.hair}`,
            borderRadius: 12,
            padding: '14px 16px',
          }}>
            <div style={{ fontFamily: SANS, fontSize: 13.5, fontWeight: 500, marginBottom: 2 }}>{t} →</div>
            <div style={{ fontFamily: MONO, fontSize: 10.5, color: P.ink_3, lineHeight: 1.4 }}>{d}</div>
          </div>
        ))}
      </div>
    </div>
  );
}

Object.assign(window, { ReadmeNarrative });
