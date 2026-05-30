export type MemoryIntentAction = "recall" | "learn" | "vault_write" | "audit" | "status" | "none";

export interface MemoryIntent {
  action: MemoryIntentAction;
  confidence: number;
  automaticAllowed: boolean;
  reason: string;
  suggestedTool?: string;
  suggestedQuery?: string;
}

export interface LearningQualityReport {
  ok: boolean;
  score: number;
  warnings: string[];
  summary: string;
}

const RECALL_TERMS = [
  "remember",
  "recall",
  "memory",
  "prior",
  "previous",
  "context",
  "where did we leave",
  "what did we decide",
];

const LEARN_TERMS = ["learn", "remember this", "save to memory", "store this", "keep this", "make a note"];
const VAULT_TERMS = ["vault note", "obsidian", "write note", "wiki page", "source note"];
const AUDIT_TERMS = ["audit", "logs", "log tail", "transparency"];
const STATUS_TERMS = ["status", "health", "daemon", "afm"];

function includesAny(text: string, terms: string[]): boolean {
  return terms.some((term) => text.includes(term));
}

function clampScore(score: number): number {
  return Math.max(0, Math.min(1, Number(score.toFixed(2))));
}

function conciseQuery(task: string): string {
  return task.replace(/\s+/g, " ").trim().slice(0, 180);
}

export function routeMemoryIntent(task: string): MemoryIntent {
  const text = task.toLowerCase();
  if (includesAny(text, LEARN_TERMS)) {
    return {
      action: "learn",
      confidence: 0.92,
      automaticAllowed: false,
      reason: "The task explicitly asks for durable memory or learning.",
      suggestedTool: "sovereign_learn",
      suggestedQuery: conciseQuery(task),
    };
  }
  if (includesAny(text, VAULT_TERMS)) {
    return {
      action: "vault_write",
      confidence: 0.88,
      automaticAllowed: false,
      reason: "The task asks for a visible Obsidian/wiki note.",
      suggestedTool: "sovereign_vault_write",
      suggestedQuery: conciseQuery(task),
    };
  }
  if (includesAny(text, AUDIT_TERMS)) {
    return {
      action: "audit",
      confidence: 0.84,
      automaticAllowed: true,
      reason: "The task asks for transparent memory logs or audit state.",
      suggestedTool: "sovereign_audit_tail",
      suggestedQuery: conciseQuery(task),
    };
  }
  if (includesAny(text, STATUS_TERMS)) {
    return {
      action: "status",
      confidence: 0.8,
      automaticAllowed: true,
      reason: "The task asks about local service health or plugin status.",
      suggestedTool: "sovereign_status",
      suggestedQuery: conciseQuery(task),
    };
  }
  if (includesAny(text, RECALL_TERMS) || /continue|resume|pick up|integrat|debug|test|build/.test(text)) {
    return {
      action: "recall",
      confidence: 0.72,
      automaticAllowed: true,
      reason: "The task likely benefits from prior local project context; recall-only is allowed automatically.",
      suggestedTool: "sovereign_recall",
      suggestedQuery: conciseQuery(task),
    };
  }
  return {
    action: "none",
    confidence: 0.35,
    automaticAllowed: true,
    reason: "No memory action appears necessary from the task wording.",
  };
}

export function assessLearningQuality(input: {
  title: string;
  content: string;
  category?: string;
  source?: string;
}): LearningQualityReport {
  const warnings: string[] = [];
  let score = 0.35;
  const content = input.content.trim();
  const wordCount = content.split(/\s+/).filter(Boolean).length;

  if (input.title.trim().length >= 8) score += 0.15;
  else warnings.push("Title is very short; use a durable, searchable title.");

  if (wordCount >= 12) score += 0.2;
  else warnings.push("Content is short; durable memory works best with a complete fact, decision, or procedure.");

  if (input.category) score += 0.1;
  else warnings.push("Category is missing; defaulting to general.");

  if (input.source) score += 0.1;
  else warnings.push("Source is missing; add one when this came from a session, file, or user instruction.");

  if (/\b(todo|maybe|later|stuff|thing)\b/i.test(content)) {
    score -= 0.12;
    warnings.push("Content has vague wording; prefer specific facts and decisions.");
  }

  if (/secret|password|token|api[_ -]?key|private key/i.test(content)) {
    score -= 0.3;
    warnings.push("Content may contain sensitive material; avoid storing secrets in memory.");
  }

  const normalized = clampScore(score);
  return {
    ok: normalized >= 0.6 && !warnings.some((warning) => warning.includes("sensitive material")),
    score: normalized,
    warnings,
    summary: warnings.length === 0 ? "Learning looks durable and specific." : warnings.join(" "),
  };
}
