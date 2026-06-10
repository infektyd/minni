/**
 * Minni — Safety / Injection Detector (TS mirror of engine/safety.py).
 *
 * SEC-010 (audit C3 / docs-F1): the contract's injection floor must hold on
 * the PLUGIN vault-search path, not only on the Python daemon path
 * (engine/retrieval.py G22). This module is the deterministic regex floor for
 * the `instruction_like` evidence flag: any snippet flagged here MUST be
 * treated as evidence about what a human or prior agent wrote, never as a
 * directive.
 *
 * Pattern set mirrors engine/safety.py `_PATTERNS` — keep the two in sync;
 * cross-language drift here re-opens the injection floor on one side.
 *
 * Design goals (same as the Python side):
 *   - Fast (regex, no model)
 *   - Zero false-negative rate on known injection patterns
 *   - Acceptable false-positive rate on normal knowledge-base content
 *   - Deterministic — same text always gives same answer
 */

const PATTERNS: RegExp[] = [
  // Classic prompt-injection openers
  /\bignore\s+(all\s+)?(previous|prior|above|earlier|your|all)\s+(instructions?|directives?|prompt|context|rules?|guidelines?|constraints?)\b/i,
  /\bdisregard\s+(all\s+)?(previous|prior|above|earlier|your|all)\s+(instructions?|directives?|prompt|context|rules?)/i,
  /\bforget\s+(all\s+)?(previous|prior|above|earlier|your|all)\s+(instructions?|directives?|prompt|context|rules?)/i,
  // Role hijack
  /\b(pretend|imagine|act|behave)\s+(you\s+are|as\s+if\s+you\s+are|that\s+you\s+are)\b/i,
  /\byou\s+are\s+now\s+(a|an)\b/i,
  /\bnew\s+(persona|identity|role|character|system\s+prompt)\b/i,
  /\boverride\s+(your|the|all)?\s*(instructions?|directives?|rules?|system)\b/i,
  /\bsystem\s+prompt\b/i,
  // Direct imperative commands targeting the model's core behaviour
  /\b(you\s+must|you\s+shall|you\s+will|you\s+need\s+to|you\s+are\s+required\s+to)\s+(always|never|not|only|immediately|now|instead)\b/i,
  /\b(do\s+not|don't|never)\s+(follow|obey|comply\s+with|adhere\s+to)\s+(your|the|any|these|those)\s+(instructions?|rules?|guidelines?|directives?)\b/i,
  // DAN / jailbreak openers (\bDAN\b is case-sensitive on the Python side too)
  /\bDAN\b/,
  /\bjailbreak\b/i,
  /\bdev(?:eloper)?\s+mode\b/i,
  // "from now on" override pattern
  /\bfrom\s+now\s+on\b[\s\S]*\b(you|your|always|never)\b/i,
  // Role-play directive to override safety
  /\b(disable|turn\s+off|remove|bypass|circumvent)\s+(your\s+)?(safety|safety\s+filter|content\s+filter|restrictions?|guardrails?|limits?)\b/i,
];

/**
 * True if `text` contains patterns consistent with prompt-injection or
 * role-hijacking attempts. False positives are acceptable; false negatives on
 * known patterns are not.
 */
export function isInstructionLike(text: string): boolean {
  if (!text) return false;
  return PATTERNS.some((pattern) => pattern.test(text));
}
