#!/usr/bin/env node
/**
 * Grok Build custom minimal Sovereign Memory hook.
 * Own implementation — not copied from any other agent's *-hook.js.
 *
 * Provides lightweight SessionStart spine + scar tissue / decision drafting
 * for PreCompact/Stop. Uses only stdlib + direct FS reads of the vault inbox
 * (no extra deps, no assumption that the full TS hook logic is loaded).
 *
 * Outputs either a systemMessage (for passive events) or hookSpecificOutput
 * so Grok's runner can inject it as additional context.
 */

const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');

const HOME = os.homedir();
const VAULT = process.env.SOVEREIGN_VAULT_PATH || path.join(HOME, '.sovereign-memory/grok-build-vault');
const INBOX = path.join(VAULT, 'inbox');
const LOGS = path.join(VAULT, 'logs');
const AGENT = 'grok-build';

// Sovereign Distill V1 (Grok-specific delivery only; real Layer 1 (identity:grok-build + layer1/) now installed via sm-propagation; this plugin provides Grok-specific V1 delivery/ritual on top).
// Toggle + gauges live in vault/distill/ per DESIGN. Light enhancement: keyword fallback + injection
// of gauges/mode on SessionStart (now on top of real Layer 1) and UserPromptSubmit. Preserves 100% of flush/compact/dream paths.
const DISTILL_DIR = path.join(VAULT, 'distill');
const DISTILL_MODE = path.join(DISTILL_DIR, 'mode');
const DISTILL_GAUGES = path.join(DISTILL_DIR, 'gauges.md');

/**
 * Minimal stdin reader (sync, stdlib only; defensive for Grok event shapes).
 * Enables UserPromptSubmit detection of /flush etc + richer PreCompact/Stop payloads.
 * See DESIGN-flush-integration.md for rationale (automatic zero-reminder participation).
 */
function readStdinSync() {
  try {
    if (process.stdin.isTTY) return {};
    const data = fs.readFileSync(0, 'utf8');
    if (!data || !data.trim()) return {};
    return JSON.parse(data);
  } catch {
    return {};
  }
}

function ensureDir(p) {
  if (!fs.existsSync(p)) fs.mkdirSync(p, { recursive: true });
}

function countInbox() {
  try {
    ensureDir(INBOX);
    const files = fs.readdirSync(INBOX).filter(f => f.endsWith('.md') || f.endsWith('.json'));
    return files.length;
  } catch {
    return 0;
  }
}

function recentLogSnippet(limitLines = 8) {
  try {
    const today = new Date().toISOString().slice(0, 10);
    const logPath = path.join(LOGS, `${today}.md`);
    if (!fs.existsSync(logPath)) return '';
    const lines = fs.readFileSync(logPath, 'utf8').trim().split('\n').slice(-limitLines);
    return lines.join('\n');
  } catch {
    return '';
  }
}

/**
 * Distill mode reader (Grok-specific toggle surface).
 * Returns "explicit" | "auto" | "disabled". Defaults to explicit (as per plan).
 * File-based so visible/editable in Obsidian + zero magic.
 */
function readDistillMode() {
  try {
    if (fs.existsSync(DISTILL_MODE)) {
      const raw = fs.readFileSync(DISTILL_MODE, 'utf8');
      // Robust: skip leading blank lines and # comment lines; take first content line; strip inline # comments and trim.
      // Per mode file header: LINE 1 ONLY contains the value (or first non-comment line).
      const lines = raw.split('\n');
      for (const line of lines) {
        const trimmed = line.trim();
        if (!trimmed || trimmed.startsWith('#')) continue;
        const val = trimmed.split('#')[0].trim().toLowerCase();
        if (['explicit', 'auto', 'disabled'].includes(val)) return val;
        // If first content line is invalid, fall through to default (explicit)
        break;
      }
    }
  } catch (e) {
    // fail-open: treat as explicit so ritual available; errors logged only to stderr
    console.error(`[grok-sovereign-hook] readDistillMode failed: ${e.message}`);
  }
  return 'explicit';
}

/**
 * Read gauges for safe injection (light, stdlib, bounded).
 * Returns short prefix + note to read full file. Never leaks secrets (our controlled artifact).
 * Used for SessionStart (Layer 1 proximity) and distill keyword fallback.
 */
function readGaugesForInjection(maxLines = 18) {
  try {
    if (!fs.existsSync(DISTILL_GAUGES)) {
      return '(gauges.md not present — seed via ritual or SOVEREIGN-DISTILL-RITUAL-GUIDE.md; default to explicit mode)';
    }
    const full = fs.readFileSync(DISTILL_GAUGES, 'utf8');
    const lines = full.trim().split('\n').slice(0, maxLines);
    const more = full.split('\n').length > maxLines;
    return lines.join('\n') + (more ? '\n... (read full distill/gauges.md for complete live context meter + Decision Aids)' : '');
  } catch (e) {
    console.error(`[grok-sovereign-hook] readGaugesForInjection failed: ${e.message}`);
    return '(gauges read error — fall back to direct vault file distill/gauges.md per SKILL)';
  }
}

function buildStatusLine() {
  const pending = countInbox();
  const snippet = recentLogSnippet(5);
  return [
    `Sovereign Memory (grok-build) active.`,
    pending > 0 ? `${pending} pending item(s) in your inbox — review with sovereign_audit_tail or the console.` : 'Inbox clean.',
    snippet ? `Recent log tail:\n${snippet}` : ''
  ].filter(Boolean).join('\n');
}

function writeDraftToInbox(kind, content) {
  try {
    ensureDir(INBOX);
    const ts = new Date().toISOString().replace(/[:.]/g, '-');
    const file = path.join(INBOX, `grok-hook-${kind}-${ts}.md`);
    const front = [
      '---',
      `title: Grok hook draft (${kind})`,
      'type: draft',
      'status: candidate',
      'privacy: local-only',
      `agent: ${AGENT}`,
      `created: ${new Date().toISOString()}`,
      'sovereign_learning: true',
      '---',
      ''
    ].join('\n');
    fs.writeFileSync(file, front + content + '\n');
    return file;
  } catch (e) {
    // Fail-open for hook: log to stderr only, do not throw.
    console.error(`[grok-sovereign-hook] writeDraftToInbox failed for ${kind}: ${e.message}`);
    return null;
  }
}

const eventArg = process.argv[2] || 'SessionStart';
const payload = readStdinSync();
const event = (eventArg || payload.hookEventName || payload.hook_event_name || 'SessionStart').toString();
let output = { continue: true };

try {
  if (event === 'SessionStart') {
    ensureDir(VAULT);
    const status = buildStatusLine();
    let msg = [
      '<sovereign:context agent="grok-build" event="SessionStart">',
      status,
      'Before ambitious work (plan mode, refactors, subagents, production): call sovereign_prepare_task.',
      'Before learning or /flush: call sovereign_prepare_outcome for dry-run candidates.',
      '</sovereign:context>'
    ].join('\n');
    const dmode = readDistillMode();
    if (dmode !== 'disabled') {
      const g = readGaugesForInjection(12);
      msg += `\n<sovereign:distill-gauges mode="${dmode}" agent="grok-build">Sovereign Distill ritual active (real Layer 1 = identity:grok-build + layer1/ installed; this V1 injection on top). Read gauges first at wind-down. Gauges:\n${g}\nFollow SKILL "Sovereign Distill Ritual" (explicit yes/no or auto per mode file). Keyword fallback also supported on UserPromptSubmit.</sovereign:distill-gauges>`;
    }
    output.systemMessage = msg;
  } else if (event === 'UserPromptSubmit') {
    // Automatic trigger for native /flush, /compact, and /dream (including when written as ". /compact" etc.).
    // Smallest addition per DESIGN: detect keywords in prompt, draft prepare_outcome-style candidate to inbox,
    // inject contract context so model (with SKILL) does the sovereign reflex.
    const prompt = (payload.prompt || payload.userPrompt || payload.input || payload.text || '').toString();
    const match = prompt.match(/(?:^|[\s.])\/(flush|compact|dream)\b/i);
    if (match) {
      const matched = '/' + match[1]; // normalize to clean /command even if prompt had ". /xxx"
      const kind = 'flush-trigger';
      // GUARD (preservation contract): This entire block is the ONLY path that may draft flush-trigger artifacts or emit flush-specific systemMessages.
      // Distill logic lives strictly in the !match block below. Never merge or reorder without updating SKILL "Relationship" + tests.
      // If prompt contains both native flush keyword and a distill trigger, flush path takes precedence (documented in SKILL).
      // SECURITY: Do not embed raw user prompt text (secrets/paths) into persistent indexed vault drafts.
      // Generic description + reference to native session transcript suffices for candidate context.
      // DoS guard: skip if inbox is already large (pre-existing PreCompact/Stop have similar surface).
      const current = countInbox();
      if (current > 300) {
        output.systemMessage = `Sovereign Memory: inbox has ${current} items; skipping new ${matched} draft to avoid resource exhaustion.`;
      } else {
        const summary = [
          `Event: ${event}`,
          `User invoked native Grok ${matched} (or compaction/memory) on productive work.`,
          `Full prompt context lives in the native Grok session transcript (~/.grok/sessions/...).`,
          `Time: ${new Date().toISOString()}`,
          '',
          'Hook drafted prepare_outcome-style candidate to inbox (per stdlib hook pattern).',
          'Per SKILL contract (grok-sovereign-memory): sovereign_prepare_outcome is the default durable reflex.',
          'Review this + any model follow-up candidates via sovereign tools / Obsidian / console.',
          'Native flush still runs (hybrid: operational + proposal-grade).'
        ].join('\n');
        const file = writeDraftToInbox(kind, summary);
        if (file) {
          output.systemMessage = `Sovereign Memory: ${matched} keyword in prompt; drafted ${path.basename(file)} (inbox). Model: call sovereign_prepare_outcome now for high-signal candidates.`;
        } else {
          output.systemMessage = `Sovereign Memory: ${matched} detected but draft write failed (degraded; native flush unaffected).`;
        }
      }
    }
    // Distill ritual fallback (keyword trigger, secondary to Layer 1 injection).
    // Only on non-flush prompts to keep flush path 100% unchanged in behavior/output.
    // Injects gauges + mode + SKILL pointer. Fail-open, bounded read.
    // Frontmatter in gauges snapshot is intentional (carries machine-friendly mode/last_updated for Layer 1 visibility).
    if (!match) {
      // Longest-first for capture fidelity with SKILL-listed phrases.
      const distillMatch = prompt.match(/\b(sovereign distill|close the sprint|distill ritual|distill)\b/i);
      if (distillMatch) {
        const dmode = readDistillMode();
        if (dmode !== 'disabled') {
          const g = readGaugesForInjection(10);
          output.systemMessage = `Sovereign Memory: distill keyword detected ("${distillMatch[0]}"). Mode: ${dmode}. Gauges (read full distill/gauges.md first):\n${g}\n\nFollow the Sovereign Distill Ritual workflow in SKILL (explicit gate or auto per mode file).`;
        }
      }
    }
  } else if (event === 'PreCompact' || event === 'Stop') {
    // Draft scar tissue / key outcomes to inbox (never auto-learn).
    // SECURITY: Do not embed raw payload text (trigger/summary/last_user_message) because it can contain
    // secrets, file paths, or other sensitive material. This mirrors the policy already applied in the
    // UserPromptSubmit /flush/compact/dream path. We only record that context existed and point to the
    // native Grok session transcript for the real details.
    const kind = event === 'PreCompact' ? 'scar-tissue' : 'stop-summary';
    const summary = [
      `Event: ${event}`,
      `Time: ${new Date().toISOString()}`,
      'Payload context was present but redacted for privacy (secrets/paths).',
      'Review the full native Grok session transcript for details (~/.grok/sessions/...).',
      '',
      'Recent log tail (scar tissue / decisions to review):',
      recentLogSnippet(12) || '(no recent log entries captured)',
      '',
      'Action for human: review this draft, promote strong items via sovereign_resolve_candidate or manual vault write, redact as needed.',
      'See DESIGN-flush-integration.md: this participates in /flush+compaction flow.'
    ].join('\n');

    const file = writeDraftToInbox(kind, summary);
    if (file) {
      output.systemMessage = `Sovereign Memory: drafted ${kind} to ${path.basename(file)} (inbox). Review before end of session.`;
    } else {
      output.systemMessage = `Sovereign Memory: failed to draft ${kind} to inbox (write error logged to stderr). Review the native session transcript instead.`;
    }
  }
} catch (e) {
  // Top-level fail-open guarantee for all new + existing paths (bug fix for write robustness).
  // Hooks must always emit valid JSON + continue:true per 10-hooks.md and DESIGN.
  console.error(`[grok-sovereign-hook] uncaught error in ${event}: ${e.message}`);
  output = { continue: true, systemMessage: `Sovereign Memory degraded (hook error on ${event}; native /flush unaffected).` };
}

console.log(JSON.stringify(output));
