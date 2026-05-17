# PR-10: Phase 4.6 — Agent Inbox/Outbox + Handoff Spec

> **Scope:** Cross-agent inbox/outbox contract, daemon handoff mediator, handoff page type, context priming on SessionStart.
>
> **Depends on:** PR-1b (vault contract defines inbox), PR-2 (envelope, vault schema).
>
> **Behavior change:** Agents can now send/receive structured packets via inbox. Handoffs become durable wiki pages.

---

## 4.6 — Inbox/Outbox + Handoff

### Inbox Schema

```json
{
  "from_agent": "codex",
  "to_agent": "claude-code",
  "kind": "handoff | candidate_learning | request | answer",
  "task": "...",
  "envelope": "<sovereign:context ...>",
  "wikilink_refs": [...],
  "expires_at": "...",
  "trace_id": "...",
  "created_at": "..."
}
```

### Files

| Action | Path | Details |
|--------|------|---------|
| **MODIFY** | `engine/sovrd.py` | New JSON-RPC: `daemon.handoff(from_agent, to_agent, packet)`. Validates, redacts per `POLICY.md`, writes to recipient inbox, audits both sides. |
| **MODIFY** | `engine/seed_identity.py` | Ensure `<vault>/inbox/` and `<vault>/outbox/` directories exist on init. |
| **MODIFY** | `plugins/sovereign-memory/src/server.ts` | Existing `sovereign_negotiate_handoff` builds the packet; now delivers via `daemon.handoff()`. |

### Outbox

Symmetric to inbox: `<vault>/outbox/` holds packets the agent has sent, for audit and retry. Drained by daemon's outbox-watcher into recipient's inbox.

### Handoff Page Type

Significant handoffs compiled into `wiki/handoffs/` page (status `accepted`) so they become recallable durable memory, not just transient inbox files.

### Handoff Context Priming

When `SessionStart` parses a pending handoff packet, the daemon eagerly resolves `wikilink_refs` and injects their snippets into the boot envelope. Receiving agent wakes up pre-warmed with the exact context needed.

### Constraints

- Inbox/outbox directories already exist for Claude Code. Promote to all agents.
- Handoff mediator validates packet schema and redacts per POLICY.md.
- Agents cannot write directly into another agent's vault — only via inbox.

### Verification

```bash
# Cross-agent round-trip
# 1. Codex sends handoff to Claude Code
python -c "
from engine.sovrd_client import handoff
handoff(
    from_agent='codex',
    to_agent='claude-code',
    packet={'task': 'Review auth migration', 'kind': 'handoff', 'wikilink_refs': ['wiki/decisions/auth-migration']}
)
print('Handoff sent')
"

# 2. Verify inbox file exists
ls ~/.sovereign-memory/claudecode-vault/inbox/

# 3. Verify handoff page created in wiki/handoffs/
ls ~/.sovereign-memory/codex-vault/wiki/handoffs/

# 4. Claude Code's next SessionStart reads inbox and surfaces context
```

---

## PR-10 Completion Checklist

- [ ] `daemon.handoff()` JSON-RPC validates, redacts, writes to recipient inbox
- [ ] Outbox stores sent packets for audit
- [ ] `inbox/` and `outbox/` dirs created by `ensureVault()`
- [ ] Handoff compiled into `wiki/handoffs/` page
- [ ] SessionStart context priming resolves `wikilink_refs`
- [ ] Packet schema validated before delivery
- [ ] Redaction per POLICY.md applied
- [ ] Cross-agent round-trip verified
- [ ] All existing tests pass

---

## Next Steps

→ [12_PR11_Phase4_5_Phase5_Observability.md](./12_PR11_Phase4_5_Phase5_Observability.md)
