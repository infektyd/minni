/** Catalogue labels from registration and structured audit facts only. */
const RUNTIME_NAMES: Record<string, string> = {
  codex: "Codex", cursor: "Cursor", "claude-code": "Claude Code",
  "claude-desktop": "Claude Desktop", gemini: "Gemini", antigravity: "Antigravity",
  "grok-build": "Grok", grok: "Grok", hermes: "Hermes", openclaw: "OpenClaw",
  kilocode: "Kilo Code", devin: "Devin", main: "Operator",
};

function opaqueIdentity(id: string): boolean {
  return /^[a-f0-9]{16,}$/i.test(id) || /^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/i.test(id);
}

/** Only a failed recall's structured error is evidence of this diagnosis.
 * A remembered phrase, summary, unrelated tool, or another identity is not.
 */
export function recordedUnknownIdentity(id: string, entries: readonly string[]): boolean {
  return entries.some(entry => {
    const header = /^## \[[^\]]+\]\s+([^|]+)\|/.exec(entry);
    if (header?.[1].trim() !== "minni_recall") return false;
    const block = /```json\r?\n([\s\S]*?)\r?\n```/.exec(entry);
    if (!block) return false;
    try {
      const details = JSON.parse(block[1]);
      if (!details || details.ok !== false || typeof details.error !== "string") return false;
      if (typeof details.agentId !== "string") return false;
      const normalize = (value: string) => opaqueIdentity(value) ? value.replaceAll("-", "").toLowerCase() : value;
      if (normalize(details.agentId) !== normalize(id)) return false;
      return /(?:^|\W)unknown_identity(?:\W|$)/.test(details.error);
    } catch { return false; }
  });
}

export function agentDisplayMetadata(input: {
  id: string; registered: boolean; registrationKnown: boolean; auditEntries?: readonly string[];
}) {
  const { id, registered, registrationKnown } = input;
  const shortId = id.length > 12 ? `${id.slice(0, 8)}…${id.slice(-4)}` : id;
  const identityLabel = registered ? "Registered identity" : registrationKnown ? "Unregistered identity" : "Identity";
  const displayName = RUNTIME_NAMES[id] ?? (opaqueIdentity(id) ? `${identityLabel} · ${shortId}` : id);
  const failedRecall = recordedUnknownIdentity(id, input.auditEntries ?? []);
  const registration = registered
    ? "Minni has a registration record for this agent."
    : registrationKnown ? "No agent is registered to this memory folder. Its permissions are unknown."
      : "Minni could not check this folder’s agent registration. Its permissions are unknown.";
  return {
    displayName,
    description: `${registration}${failedRecall ? " Past attempts to retrieve memories were rejected because this identity was not registered." : ""}`,
    registered, registrationKnown, capabilitiesKnown: registered,
    activityDescription: "Last seen records audit activity, not whether a process is running.",
    ...(failedRecall ? { recallFailure: "unknown_identity" as const } : {}),
  };
}
