// Harness-envelope rules (recall-to-use rev5, 5.2.1): typed constants only.
//
// Host-injected text reaches the prompt hooks next to what the human typed:
// system reminders, task notifications, relayed messages, scheduled check-ins.
// This module is the single place that says which shapes are which. The
// classifier in harness-envelope.ts reads it; the Python mirror
// (src/minni/afm_passes/harness_envelopes.py) repeats the TEXT rules below with
// the same `rule_id`s.
//
// Why a typed module and not a shared JSON file: a bare JSON import fails
// typecheck under NodeNext, and a dist/data/ file would fail stage_payload.py.
// Parity is instead enforced by a shared case file
// (tests/fixtures/harness_envelope_cases.json) that BOTH sides run, and each side
// asserts every one of its rule ids has a case. Precedent: inbox_ingest.is_audit_echo.
//
// Rule order matters: arrays are tried top to bottom and the first match wins.
// Matching is case sensitive. Every character class a rule depends on is spelled
// out (space, tab, CR, LF, FF, VT), never `\s`, because JS and Python disagree
// about what `\s` and trim() cover.
//
// Record-only: nothing in the hooks calls the classifier yet (slice S1a-3).

export const HARNESS_ENVELOPE_RULES = {
  version: 1,

  /**
   * Blocks stripped anywhere in the text, including nested ones. A block that is
   * never closed is left alone: an unterminated tag is not evidence of a block.
   * The open tag may carry attributes (at most 256 chars before `>`).
   */
  blockTags: [{ rule_id: "block.system_reminder", tag: "system-reminder" }],

  /**
   * Prompts whose stripped text STARTS with the prefix are machine output: no
   * prompt recall, no harvest. Prefixes are open ended on the right on purpose
   * (`<task-notification` also covers `<task-notification id="…">`).
   */
  skip: [
    { rule_id: "skip.task_notification", prefix: "<task-notification", subkind: "task_notification" },
    { rule_id: "skip.side_sessions_event", prefix: "<side-sessions-event>", subkind: "side_sessions_event" },
    { rule_id: "skip.ci_monitor_event", prefix: "<ci-monitor-event>", subkind: "ci_monitor_event" },
    { rule_id: "skip.command_name", prefix: "<command-name>", subkind: "slash_command" },
    { rule_id: "skip.command_message", prefix: "<command-message>", subkind: "slash_command" },
    { rule_id: "skip.local_command_stdout", prefix: "<local-command-stdout>", subkind: "local_command_output" },
    { rule_id: "skip.local_command_caveat", prefix: "<local-command-caveat>", subkind: "local_command_output" },
    { rule_id: "skip.bash_input", prefix: "<bash-input>", subkind: "human_shell" },
  ],

  /**
   * Wrappers are stripped and the body is kept as the payload. They are tried at
   * the start of the text and repeated, so a lead line followed by a tag pair
   * unwraps fully (at most `maxWrapperDepth` passes). The first rule that matched
   * names the result.
   *
   * - `tag_pair`: `<tag …>` … `</tag>`. The inner text is kept; a missing close
   *   tag keeps everything after the open tag.
   * - `lead_line`: the whole first line is dropped.
   * - `header`: the bracketed `[Prefix …]` part is dropped, the rest of its line
   *   stays. A header with no `]` on its first line is not a header.
   */
  wrappers: [
    {
      rule_id: "wrap.cross_session_message",
      form: "tag_pair",
      tag: "cross-session-message",
      subkind: "cross_session_message",
    },
    {
      rule_id: "wrap.another_session_lead",
      form: "lead_line",
      prefix: "Another Claude session sent a message",
      subkind: "cross_session_lead",
    },
    {
      rule_id: "wrap.workflow_harness_header",
      form: "header",
      prefix: "[Workflow harness",
      subkind: "workflow_harness",
    },
    {
      rule_id: "wrap.artifact_comment_header",
      form: "header",
      prefix: "[Artifact comment",
      subkind: "artifact_comment",
    },
  ],

  maxWrapperDepth: 4,
  /** Longest run of attribute text accepted inside an open tag, and inside a `[Header …]`. */
  maxOpenTagChars: 512,
  maxBlockOpenTagChars: 256,
  maxHeaderChars: 256,

  /**
   * A text that survives every rule as `human` but starts like harness output is
   * flagged (`suspicious_prefix`) so a new harness shape shows up in the audit
   * instead of passing silently. It only flags; it never changes the class.
   */
  suspiciousPrefixPattern: "^(?:<[A-Za-z]|\\[[A-Z])",

  /**
   * The scheduled-template rule. TypeScript only: the daemon never sees the
   * counts file, so it has no Python mirror and no shared case.
   */
  templateRule: {
    rule_id: "scheduled.template",
    /** Only prompts of at least this many chars (after stripping) are eligible. */
    minChars: 80,
    /** A prompt over this length is hashed on its first N normalized chars. */
    hashInputChars: 2000,
    /** Prior occurrences needed (ts < at, a different pid). */
    minPrior: 3,
    /** Either this many distinct sessions first seen before `at`, … */
    minSessions: 2,
    /** … or `at - first_seen` of at least this long (2 h). */
    minSpanMs: 2 * 60 * 60 * 1000,
    /** Counts file bounds. */
    ringMax: 16,
    sessionsMax: 8,
    entriesMax: 500,
    /** Entries idle for MORE than this are pruned (14 days). */
    idleMaxMs: 14 * 24 * 60 * 60 * 1000,
    /** The counts file's own schema version. */
    fileVersion: 1,
  },
} as const;

/** The characters trimmed from both ends of every stripped prompt. */
export const HARNESS_TRIM_CHARS = " \t\r\n\f\v";

/**
 * The rule ids shared with the Python mirror, in rule order. The template rule is
 * deliberately absent: it is TypeScript only.
 */
export function harnessEnvelopeRuleIds(): string[] {
  return [
    ...HARNESS_ENVELOPE_RULES.blockTags.map((r) => r.rule_id),
    ...HARNESS_ENVELOPE_RULES.skip.map((r) => r.rule_id),
    ...HARNESS_ENVELOPE_RULES.wrappers.map((r) => r.rule_id),
  ];
}
