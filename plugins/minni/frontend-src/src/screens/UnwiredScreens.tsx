import { ArchivalBand, StateBanner } from "../components/atoms";

export function HandoffsScreen() {
  return (
    <>
      <ArchivalBand
        eyebrow="HANDOFFS · INBOX / OUTBOX"
        title="Cross-agent packet ledger"
        meta={[
          { k: "INBOX", v: "—" },
          { k: "OUTBOX", v: "—" },
          { k: "POLICY", v: "policy.handoff.team@v4.2" },
          { k: "SCOPE", v: "this host" },
        ]}
      />
      <StateBanner state="empty">
        Handoffs is unwired in this alpha. The bridge has no <code>/api/handoffs</code> endpoint yet.
        Pending leases stay on MCP: <code>minni_list_pending_handoffs</code>,{" "}
        <code>minni_ack_handoff</code>, <code>minni_await_handoff</code>.
      </StateBanner>
    </>
  );
}

export function VaultsScreen() {
  return (
    <>
      <ArchivalBand
        eyebrow="VAULTS · OBSIDIAN-COMPATIBLE · LOCAL ONLY"
        title="Per-agent memory surfaces"
        meta={[
          { k: "VAULTS", v: "—" },
          { k: "TOTAL PAGES", v: "—" },
          { k: "DAEMON", v: "shared sovrd · 1 socket" },
          { k: "REMOTE SYNC", v: "off" },
        ]}
      />
      <StateBanner state="empty">
        Vaults is unwired in this alpha — no multi-agent catalogue API. Settings shows this
        console&apos;s active vault from <code>/api/status</code> (
        <code>MINNI_VAULT_PATH</code>).
      </StateBanner>
    </>
  );
}

export function PolicyScreen() {
  return (
    <>
      <ArchivalBand
        eyebrow="POLICY · CAPABILITIES · AFM"
        title="Local rule set & loop posture"
        meta={[
          { k: "POLICY VER", v: "v4.2" },
          { k: "AFM LOOP", v: "—" },
          { k: "BRIDGE", v: "available" },
          { k: "DRIFT", v: "—" },
        ]}
      />
      <StateBanner state="empty">
        Policy & AFM is unwired in this alpha — no policy read HTTP route. Hook/policy heuristics live
        in <code>plugins/minni/src/policy.ts</code>; AFM posture is on Settings via{" "}
        <code>/api/status</code>.
      </StateBanner>
    </>
  );
}
