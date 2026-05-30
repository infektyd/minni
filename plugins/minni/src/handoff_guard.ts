export type HandoffDeliveryPlan =
  | {
      kind: "direct";
      fromAgent: string;
      toAgent: string;
    }
  | {
      kind: "ping_required";
      toAgent: string;
      question: string;
      purpose: string;
      allowedTopics: string[];
    };

export interface PlanHandoffDeliveryInput {
  runtimeAgent: string;
  fromAgent: string;
  toAgent: string;
  task: string;
  openQuestions?: string[];
}

const INFORMATION_REQUEST_PATTERNS = [
  /\b(ask|query|ping|request)\b[\s\S]{0,80}\b(what|which|whether|why|how|when|where|share|send|provide|tell)\b/i,
  /\b(what|which|whether|why|how|when|where)\b[\s\S]{0,120}\b(your|its|their)\b[\s\S]{0,80}\b(memory|vault|recall|notes?|context|history|handoff|summary|learning|learnings)\b/i,
  /\b(share|send|provide|give|tell|return|export|import)\b[\s\S]{0,120}\b(your|its|their|latest|private)\b[\s\S]{0,80}\b(memory|vault|recall|notes?|context|history|handoff|summary|learning|learnings)\b/i,
  /\bfrom\b[\s\S]{0,80}\b(your|its|their|private)\b[\s\S]{0,80}\b(memory|vault|recall|notes?|context|history)\b/i,
];

function normalizeAgentId(value: string): string {
  return value.trim();
}

function firstInformationRequest(task: string, openQuestions: string[] | undefined): string | undefined {
  const candidates = [task, ...(openQuestions ?? [])].map((item) => item.replace(/\s+/g, " ").trim()).filter(Boolean);
  return candidates.find((candidate) => INFORMATION_REQUEST_PATTERNS.some((pattern) => pattern.test(candidate)));
}

export function planHandoffDelivery(input: PlanHandoffDeliveryInput): HandoffDeliveryPlan {
  const runtimeAgent = normalizeAgentId(input.runtimeAgent);
  const fromAgent = normalizeAgentId(input.fromAgent);
  const toAgent = normalizeAgentId(input.toAgent);
  if (!runtimeAgent) throw new Error("runtimeAgent is required.");
  if (!fromAgent) throw new Error("fromAgent is required.");
  if (!toAgent) throw new Error("toAgent is required.");
  if (fromAgent !== runtimeAgent) {
    throw new Error("Direct handoff cannot impersonate another agent; run as that agent or use sovereign_ping_agent_request.");
  }

  const informationRequest = firstInformationRequest(input.task, input.openQuestions);
  if (informationRequest) {
    return {
      kind: "ping_required",
      toAgent,
      question: informationRequest,
      purpose:
        "Routed from sovereign_negotiate_handoff because the request asks another agent for information; direct handoff is limited to work-transfer packets.",
      allowedTopics: ["handoff", "cross-agent information request"],
    };
  }

  return { kind: "direct", fromAgent, toAgent };
}
