import type { BoardAgent } from "../board/boardData";

export function agentDisplayName(agent: BoardAgent): string {
  if (agent.displayName?.trim()) return agent.displayName;
  const short = agent.id.length > 20 ? `${agent.id.slice(0, 8)}…${agent.id.slice(-6)}` : agent.id;
  if (agent.registered === true) return short;
  return `${agent.registrationKnown === true ? "Unregistered identity" : "Unknown identity"} · ${short}`;
}

function activityAge(value: string): string {
  const match = /^(\d+)(m|h|d)$/.exec(value);
  if (!match) return value === "—" ? "unknown" : value;
  const unit = { m: "minute", h: "hour", d: "day" }[match[2]];
  return `${match[1]} ${unit}${match[1] === "1" ? "" : "s"} ago`;
}

export function AgentSummary({ agent, compact = false }: { agent: BoardAgent; compact?: boolean }) {
  const known = agent.registered === true && agent.capabilitiesKnown === true;
  const pending = agent.staged == null || agent.stagedUnknown
    ? "Review count unavailable"
    : `${agent.staged}${agent.stagedAtLimit ? "+" : ""} ${agent.staged === 1 && !agent.stagedAtLimit ? "suggestion" : "suggestions"} awaiting review`;
  const registration = agent.registered === true
    ? "Registered memory identity."
    : agent.registrationKnown !== true ? "Registration status unknown."
      : "No registration record; this vault alone does not grant access.";
  return (
    <div style={{ fontSize: compact ? 10 : 12, lineHeight: compact ? "13px" : "1.5" }}>
      <div title={agentDisplayName(agent)} style={{ fontWeight: 650, fontSize: compact ? 12 : 14, overflowWrap: "anywhere", ...(compact ? { whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" } as const : {}) }}>{agentDisplayName(agent)}</div>
      {!compact && <p>{agent.description || registration}</p>}
      <div className={compact ? undefined : "muted"}>Last recorded memory activity: {activityAge(agent.seen)}</div>
      <div style={{ marginTop: 4 }}>
        {([["R", "Memory reading"], ["L", "Memory writing"], ["H", "Sharing / governance"]] as const).map(([key, label]) => (
          <div key={key}>{label}: {known ? agent.caps[key] ? "listed" : "not listed" : "unknown"}</div>
        ))}
      </div>
      <div>{pending}</div>
      {!compact && <>
        <details><summary>Identity and storage details</summary>
          <p className="muted">{registration} Activity records do not indicate a running process. These summarize registration records, not a live permission check.</p>
          {agent.note && <p>{agent.note}</p>}
          <div style={{ overflowWrap: "anywhere" }}>Identity: {agent.id}</div>
          <div style={{ overflowWrap: "anywhere" }}>Vault: {agent.vault}</div>
        </details>
      </>}
    </div>
  );
}
