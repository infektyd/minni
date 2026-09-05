import type { ReactNode } from "react";
import type {
  EvidenceAfm,
  EvidenceAuthority,
  EvidencePrivacy,
} from "../api";

export type ChipKind =
  | "default"
  | "safe"
  | "warn"
  | "danger"
  | "info"
  | "private"
  | "team"
  | "public"
  | "owner"
  | "system"
  | "afm"
  | "learn"
  | "log"
  | "dns";

interface ChipProps {
  kind?: ChipKind;
  children: ReactNode;
  dot?: boolean;
  square?: boolean;
}

export function Chip({ kind = "default", children, dot, square }: ChipProps) {
  return (
    <span className={`chip chip-${kind}`}>
      {dot && <span className="chip-dot" />}
      {square && <span className="chip-square" />}
      {children}
    </span>
  );
}

export function PrivacyChip({ value }: { value: EvidencePrivacy }) {
  const map: Record<EvidencePrivacy, { kind: ChipKind; label: string }> = {
    private: { kind: "private", label: "PRIVATE" },
    blocked: { kind: "danger", label: "BLOCKED" },
    "local-only": { kind: "info", label: "LOCAL-ONLY" },
    safe: { kind: "safe", label: "SAFE" },
    unknown: { kind: "default", label: "UNKNOWN" },
  };
  const m = map[value] || { kind: "default", label: String(value).toUpperCase() };
  return <Chip kind={m.kind}>{m.label}</Chip>;
}

export function AuthorityChip({ value }: { value: EvidenceAuthority }) {
  const map: Record<EvidenceAuthority, { kind: ChipKind; label: string }> = {
    schema: { kind: "info", label: "SCHEMA" },
    handoff: { kind: "info", label: "HANDOFF" },
    decision: { kind: "info", label: "DECISION" },
    session: { kind: "default", label: "SESSION" },
    concept: { kind: "default", label: "CONCEPT" },
    daemon: { kind: "default", label: "DAEMON" },
    vault: { kind: "default", label: "VAULT" },
    unknown: { kind: "default", label: "UNKNOWN" },
  };
  const m = map[value] || { kind: "default", label: String(value).toUpperCase() };
  return <Chip kind={m.kind}>{m.label}</Chip>;
}

export function AfmChip({ value }: { value: EvidenceAfm }) {
  const map: Record<EvidenceAfm, { kind: ChipKind; label: string }> = {
    unclassified: { kind: "default", label: "UNCLASSIFIED" },
    safe: { kind: "afm", label: "AFM-SAFE" },
    learn: { kind: "learn", label: "LEARN CANDIDATE" },
    log: { kind: "log", label: "LOG-ONLY" },
    dns: { kind: "dns", label: "DO-NOT-STORE" },
  };
  const m = map[value] || { kind: "default", label: String(value) };
  return <Chip kind={m.kind}>{m.label}</Chip>;
}
