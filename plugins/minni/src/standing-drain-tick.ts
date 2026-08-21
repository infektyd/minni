/**
 * Named standing drain trigger: minnid tick.
 *
 * Not an MCP stdio process. Not a second graph. Not G3. Not Thread SoT.
 * drainPendingWorkerWritesForVault stays the apply entry. This file only
 * lets minnid kick that apply when nobody boots MCP on the vault.
 * Standing drain must not apply a live start in the accept→reserve window
 * while the acceptor is live — that yield lives on the apply entry.
 */
import { drainPendingWorkerWritesForVault } from "./thread-worker.js";

export const STANDING_DRAIN_TRIGGER = "minnid tick" as const;

export async function minnidStandingDrainTick(
  vaultPath: string,
): Promise<{ planIds: string[] }> {
  return drainPendingWorkerWritesForVault(vaultPath);
}

function invokedAsCli(): boolean {
  const entry = process.argv[1];
  if (typeof entry !== "string" || entry.length === 0) return false;
  return (
    entry.endsWith("standing-drain-tick.js") ||
    entry.endsWith("standing-drain-tick.ts")
  );
}

if (invokedAsCli()) {
  const vault = process.env.MINNI_STANDING_DRAIN_VAULT ?? process.argv[2];
  if (typeof vault !== "string" || vault.length === 0) {
    process.stderr.write("minnid tick: vault path required\n");
    process.exit(2);
  }
  const result = await minnidStandingDrainTick(vault);
  process.stdout.write(`${JSON.stringify(result)}\n`);
}
