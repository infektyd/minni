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
        Handoffs is unwired in this alpha. The bridge has no <code>/api/handoffs</code> endpoint yet — the
        screen will activate when one ships.
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
        Vaults is unwired in this alpha. The bridge does not expose a vault catalogue endpoint yet —
        Settings shows the active vault path read from <code>/api/status</code>.
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
        Policy & AFM is unwired in this alpha. The bridge does not expose a policy read endpoint yet
        — see <code>plugins/sovereign-memory/src/policy.ts</code> for the active rule set.
      </StateBanner>
    </>
  );
}
