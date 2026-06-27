# Migration 013 audit runbook

Migration `013_candidate_and_main_identity_unification.sql` is **one-time** and already applied on installs that ran migrations through 2026-06-11.

## What it did

1. `candidate_packets.principal`: `claudecode` and `main` → `claude-code`
2. `learnings.agent_id`: `main` → `claude-code`
3. Deleted test fixture rows (`principal='test'`, `agent_id='test'`)

Pre-migration backup (if present): `~/.minni/backups/minni-pre-013.db`

## When to inspect

Run this audit when:

- `main` was a **genuine operator identity** on the install (not a Claude Code alias)
- You see unexpected `claude-code` attribution on old learnings/candidates
- You migrated from a pre-013 DB without the backup

## Inspect SQL (read-only)

```sql
-- Rows still tagged main (should be rare post-013)
SELECT COUNT(*) FROM learnings WHERE agent_id = 'main';
SELECT COUNT(*) FROM candidate_packets WHERE principal = 'main';

-- claude-code volume (historical main folded here)
SELECT agent_id, COUNT(*) FROM learnings GROUP BY agent_id ORDER BY COUNT(*) DESC;
SELECT principal, COUNT(*) FROM candidate_packets GROUP BY principal ORDER BY COUNT(*) DESC;
```

## Rollback

Do **not** re-run migration 013. To roll back data:

1. Stop minnid
2. Restore `~/.minni/backups/minni-pre-013.db` over `~/.minni/sovereign_memory.db` (and `-wal`/`shm` if present)
3. Restart minnid

## Repair (operator-only)

If `main` memories were wrongly reassigned to `claude-code`, restore from backup or manually UPDATE rows after human review — there is no automatic repair script in-tree.
