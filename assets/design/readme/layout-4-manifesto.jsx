// Layout 4 — MANIFESTO
// Tone: confident, opinionated, principle-driven.
// 820 × ~1620

function ReadmeManifesto() {
  return (
    <div style={{
      width: 820,
      background: P.bg,
      fontFamily: SANS,
      color: P.ink,
      padding: '56px 64px 56px',
      boxSizing: 'border-box',
    }}>
      <SharedStyles />

      {/* — Hero ————————————————————————— */}
      <div style={{ marginBottom: 72 }}>
        <Eyebrow style={{ marginBottom: 28 }}>Sovereign Memory · a manifesto + a project</Eyebrow>
        <div style={{
          fontFamily: SERIF, fontSize: 88, fontWeight: 500, letterSpacing: -2.4,
          lineHeight: 0.95, color: P.ink, textWrap: 'balance',
        }}>
          Memory should<br />be sovereign.
        </div>
        <div style={{
          fontFamily: SERIF, fontSize: 26, fontStyle: 'italic',
          color: P.ink_2, marginTop: 22, letterSpacing: -0.2,
        }}>
          Yours. Verifiable. Local.
        </div>
        <Rule style={{ margin: '40px 0 0', background: P.ink, height: 1.5 }} />
      </div>

      {/* — Pitch para ———————————————————————— */}
      <div style={{
        fontFamily: SERIF, fontSize: 22, lineHeight: 1.45,
        color: P.ink, maxWidth: 640, textWrap: 'pretty', marginBottom: 72,
      }}>
        Long-running agent work deserves a memory layer that does not lie, does
        not bloat, does not phone home, and does not surrender provenance.
        Sovereign Memory is the spine: identity, working state, retrieval,
        evidence, handoffs, and audit trails — kept on your machine, inspectable
        at every step.
      </div>

      {/* — Five principles —————————————————— */}
      <div style={{ marginBottom: 64 }}>
        <Eyebrow style={{ marginBottom: 20 }}>Five non-negotiables</Eyebrow>

        {[
          {
            n: '01',
            t: 'Identity loads whole. Knowledge loads chunked.',
            d: 'The agent\u2019s self-model is small enough to ship intact every session. Everything else is retrieved, cited, and validated — never assumed.',
          },
          {
            n: '02',
            t: 'No silent writes.',
            d: 'Every durable memory change is proposed first, listed, and resolved by an operator. The system cannot quietly learn its way into a different state.',
          },
          {
            n: '03',
            t: 'SQLite is runtime truth.',
            d: 'Vault pages, graph exports, FAISS indices, context packs, and compile drafts are review surfaces — derivable, replaceable, and never the source of authority.',
          },
          {
            n: '04',
            t: 'Local-first, or nothing.',
            d: 'No telemetry. No cloud sync. No remote endpoints at v1. The host machine is the security perimeter, and the audit trail is yours to keep.',
          },
          {
            n: '05',
            t: 'If a simpler model wins, delete complexity.',
            d: 'This project is judged by recovery quality, not by how elaborate the machinery looks. The smallest packet that lets an agent resume safely is the right packet.',
          },
        ].map((p, i, arr) => (
          <div key={p.n} style={{
            display: 'grid', gridTemplateColumns: '88px 1fr',
            columnGap: 24,
            padding: '24px 0',
            borderTop: `1px solid ${P.hair_2}`,
            borderBottom: i === arr.length - 1 ? `1px solid ${P.hair_2}` : 'none',
          }}>
            <div style={{
              fontFamily: SERIF, fontSize: 36, fontWeight: 500,
              color: P.amber, lineHeight: 1, letterSpacing: -1,
            }}>{p.n}</div>
            <div>
              <div style={{
                fontFamily: SERIF, fontSize: 28, fontWeight: 500,
                letterSpacing: -0.5, lineHeight: 1.15,
                marginBottom: 10, textWrap: 'balance',
              }}>{p.t}</div>
              <div style={{
                fontFamily: SANS, fontSize: 14.5, lineHeight: 1.55,
                color: P.ink_2, maxWidth: 580, textWrap: 'pretty',
              }}>{p.d}</div>
            </div>
          </div>
        ))}
      </div>

      {/* — What works ————————————————————— */}
      <div style={{ marginBottom: 64 }}>
        <Eyebrow style={{ marginBottom: 8 }}>Where we are</Eyebrow>
        <div style={{
          fontFamily: SERIF, fontSize: 30, fontWeight: 500, letterSpacing: -0.5,
          marginBottom: 22, textWrap: 'balance',
        }}>
          Honest about what works.
        </div>

        {/* Maturity bar */}
        <div style={{
          display: 'flex', height: 38,
          borderRadius: 19,
          overflow: 'hidden',
          border: `1px solid ${P.hair}`,
          marginBottom: 14,
        }}>
          {[
            ['stable', 6.7,  '1 stable'],
            ['beta',   53.3, '8 beta'],
            ['alpha',  33.3, '5 alpha'],
            ['stub',   6.7,  '1 stub'],
          ].map(([k, pct, lbl]) => (
            <div key={k} style={{
              flex: pct,
              background: STATUS[k].tint,
              borderRight: k === 'stub' ? 'none' : `1px solid ${P.hair_2}`,
              display: 'flex', alignItems: 'center', justifyContent: 'center',
              gap: 8,
              fontFamily: MONO, fontSize: 11, color: STATUS[k].fg,
            }}>
              <Dot kind={k} size={6} style={{ boxShadow: 'none' }} />
              <span>{lbl}</span>
            </div>
          ))}
        </div>

        <div style={{
          fontFamily: SANS, fontSize: 14, lineHeight: 1.6, color: P.ink_2,
          maxWidth: 620, textWrap: 'pretty',
        }}>
          One stable subsystem (the SQLite runtime). Eight in <b style={{ color: STATUS.beta.fg }}>beta</b> —
          retrieval, the MCP plugin, learning, vault model, identity, handoff,
          propagation. Five in <b style={{ color: STATUS.alpha.fg }}>alpha</b> —
          cross-agent contracts, AFM, compile, team coordination, decay. One
          stub. Full matrix linked below.
        </div>
      </div>

      {/* — Try it ————————————————————— */}
      <div style={{ marginBottom: 56 }}>
        <Eyebrow style={{ marginBottom: 8 }}>Try it</Eyebrow>
        <div style={{
          fontFamily: SERIF, fontSize: 30, fontWeight: 500, letterSpacing: -0.5,
          marginBottom: 22,
        }}>
          Three commands.
        </div>

        {[
          { t: 'Install',  desc: 'engine + plugin',           c: '$ git clone … && cd sovereign-memory && make install' },
          { t: 'Resume',   desc: 'rehydrate an agent session', c: '$ sm resume --agent codex --vault codex-vault' },
          { t: 'Audit',    desc: 'tail every durable write',   c: '$ sm audit tail --since 1d' },
        ].map((a) => (
          <div key={a.t} style={{
            display: 'grid', gridTemplateColumns: '160px 1fr',
            padding: '14px 0', columnGap: 20,
            borderTop: `1px solid ${P.hair_2}`,
            alignItems: 'center',
          }}>
            <div>
              <div style={{ fontFamily: SERIF, fontSize: 22, fontWeight: 500, letterSpacing: -0.3 }}>{a.t}</div>
              <div style={{ fontFamily: MONO, fontSize: 10.5, color: P.ink_3 }}>{a.desc}</div>
            </div>
            <div style={{
              fontFamily: MONO, fontSize: 12.5, color: P.ink,
              background: P.surface_2,
              border: `1px solid ${P.hair}`,
              borderRadius: 10,
              padding: '10px 14px',
            }}>{a.c}</div>
          </div>
        ))}
      </div>

      {/* — Deeper —————————————————————— */}
      <div style={{
        background: P.surface_2,
        border: `1px solid ${P.hair_2}`,
        borderRadius: 18,
        padding: '28px 32px',
        marginBottom: 32,
      }}>
        <Eyebrow style={{ marginBottom: 10 }}>The deeper read</Eyebrow>
        <div style={{ fontFamily: SERIF, fontSize: 22, fontWeight: 500, marginBottom: 16, letterSpacing: -0.3 }}>
          For the curious, the skeptical, the operator.
        </div>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          {[
            ['→ the design',          'docs/DESIGN-sovereign-delivery-layer.md'],
            ['→ the threat model',    'docs/contracts/threat-model.md'],
            ['→ the component matrix', 'docs/STATUS.md'],
            ['→ the source',          'engine/  ·  plugins/sovereign-memory/'],
          ].map(([t, h]) => (
            <div key={t} style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline' }}>
              <span style={{ fontFamily: SERIF, fontSize: 17, color: P.ink }}>{t}</span>
              <span style={{ fontFamily: MONO, fontSize: 11, color: P.ink_3 }}>{h}</span>
            </div>
          ))}
        </div>
      </div>

      {/* — Footer ————————————————————— */}
      <div style={{
        fontFamily: MONO, fontSize: 11, color: P.ink_3,
        textAlign: 'center', letterSpacing: 1, marginTop: 24,
      }}>
        LOCAL-FIRST  ·  NO TELEMETRY  ·  NO CLOUD SYNC  ·  NO REMOTE ENDPOINTS  ·  EVER
      </div>
    </div>
  );
}

Object.assign(window, { ReadmeManifesto });
