# Minni Rename — P1 (Repo-Internal) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan one task at a time. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rename the safe, non-runtime-coupled surface of the repo from `sovereign`/`sovereign-memory` to `minni` — brand strings, docs, and the plugin directory/manifests — without touching the live daemon, vault, or any identifier that would break an installed plugin.

**Architecture:** P1 is **in-git only and reversible**. It deliberately does NOT rename runtime-coupled identifiers (MCP server key `mcp__sovereign-memory__`, tool verbs `sovereign_recall`, `SOVEREIGN_*` env, vault path). Those derive from / wire into the live system and are renamed in **P2 with a simultaneous compat-alias layer** so nothing is ever broken. P1 produces a branch that is brand-correct and structurally renamed but still functionally identical at runtime.

**Tech Stack:** TypeScript MCP plugin (`plugins/sovereign-memory` → `plugins/minni`), Node `--test`, Vite, ripgrep for audit gates.

> **EXECUTION STATUS (2026-05-30):** Tasks 1–3 COMPLETE (commits `91fe2f7`, `75b85d3`). The plugin dir is renamed and the build passes. NOTE: commit `91fe2f7` also ran an over-broad markdown sweep that flattened some old→new descriptions in this plan (and the spec/playbook, since repaired). The original `plugins/sovereign-memory` "before" strings below were corrupted to `plugins/minni`; treat the Task 1–3 prose as historical. Tasks 4–6 (branding + verify) are driven inline by the controller.

**Spec:** `docs/superpowers/specs/2026-05-29-minni-deep-rename-design.md`

**Verification model:** A rename has no unit "feature" to TDD. The verification gates are: (a) **grep audits** — the right strings changed, the runtime-coupled strings did NOT; (b) **plugin build** still succeeds; (c) **existing test suite** still passes; (d) **public-boundary check** still passes. Every task ends with the relevant gate + a commit.

---

## Scope boundary (READ FIRST)

**P1 RENAMES (safe):**
- Plugin directory `plugins/sovereign-memory/` → `plugins/minni/` (git mv — path only; install source updated in manifests).
- Human-facing brand/description strings in manifests and docs ("Sovereign Memory" → "Minni" where it's branding, not an identifier).
- npm `package.json` `name` fields.
- Skill *directory names* and SKILL.md frontmatter titles where they are branding.

**P1 does NOT touch (deferred to P2, with aliases):**
- `mcpServers` key `sovereign-memory` in `plugin.json` / `.mcp.json` (this IS the `mcp__sovereign-memory__` namespace).
- Tool verbs `sovereign_recall` / `sovereign_learn` / … in `src/`.
- `SOVEREIGN_*` env var names.
- `~/.sovereign-memory/` vault path references.
- Slash-command file names in `commands/` (renaming the plugin already re-namespaces `/sovereign-memory:x` → these stay as `recall.md` etc.; the namespace flips with the plugin name in P2 once aliases exist).

**Why this split:** renaming the plugin `name` and `mcpServers` key flips the live namespace the moment the plugin is reinstalled. Doing that without the P2 alias layer would leave any agent still configured for `sovereign-memory` broken. So P1 stops short of the coupled identifiers.

---

## Task 1: Pre-flight — branch + rename inventory

**Files:**
- Create: `docs/migration/minni-p1-inventory.txt` (working artifact, git-ignored or committed as evidence)

- [ ] **Step 1: Confirm branch**

Run: `cd ~/Projects/Minni && git branch --show-current`
Expected: `rebrand/minni-deep-rename`

- [ ] **Step 2: Capture the full sovereign reference inventory as a baseline**

Run:
```bash
cd ~/Projects/Minni
rg -n "sovereign" --glob '!node_modules' --glob '!venv' --glob '!.venv' --glob '!dist' --glob '!.git' --glob '!_archive' --glob '!_cleanup-quarantine' --glob '!package-lock.json' > docs/migration/minni-p1-inventory.txt
wc -l docs/migration/minni-p1-inventory.txt
```
Expected: a few hundred lines. This is the audit baseline.

- [ ] **Step 3: Classify the runtime-coupled strings that P1 must NOT change**

Run:
```bash
cd ~/Projects/Minni
rg -n "mcp__sovereign-memory__|sovereign_recall|sovereign_learn|sovereign_vault_write|SOVEREIGN_[A-Z_]+|\.sovereign-memory/|\"sovereign-memory\":" --glob '!node_modules' --glob '!dist' | tee docs/migration/minni-p1-protected.txt | wc -l
```
Expected: a list of protected references. These stay untouched in P1.

- [ ] **Step 4: Commit the inventory as evidence**

```bash
cd ~/Projects/Minni
git add docs/migration/minni-p1-inventory.txt docs/migration/minni-p1-protected.txt
git commit -m "chore(minni-p1): capture rename inventory + protected-string baseline"
```

---

## Task 2: Rename the plugin directory

**Files:**
- Move: `plugins/sovereign-memory/` → `plugins/minni/`

- [ ] **Step 1: git mv the directory**

Run:
```bash
cd ~/Projects/Minni
git mv plugins/sovereign-memory plugins/minni
```

- [ ] **Step 2: Verify nothing else referenced the literal path `plugins/minni`**

Run:
```bash
cd ~/Projects/Minni
rg -n "plugins/minni" --glob '!node_modules' --glob '!dist' --glob '!.git'
```
Expected: a list of manifest/doc references (marketplace.json source paths, README, etc.) — fix these in the next steps. Note them.

- [ ] **Step 3: Update install-source paths in both marketplace manifests**

In `.claude-plugin/marketplace.json` change `"source": "./plugins/minni"` → `"./plugins/minni"`.
In `.agents/plugins/marketplace.json` change `"path": "./plugins/minni"` → `"./plugins/minni"`.

- [ ] **Step 4: Gate — confirm no stale `plugins/minni` path remains (outside dist/node_modules)**

Run:
```bash
cd ~/Projects/Minni
rg -n "plugins/minni" --glob '!node_modules' --glob '!dist' --glob '!.git' --glob '!docs/migration/minni-p1-inventory.txt'
```
Expected: no output (empty). If lines remain, fix them.

- [ ] **Step 5: Commit**

```bash
cd ~/Projects/Minni
git add -A
git commit -m "refactor(minni-p1): rename plugins/sovereign-memory -> plugins/minni + fix install-source paths"
```

---

## Task 3: Rename npm package names (non-runtime brand identifiers)

**Files:**
- Modify: `plugins/minni/package.json` (`name`)

- [ ] **Step 1: Update package name**

In `plugins/minni/package.json` change `"name": "sovereign-memory-multi-plugin"` → `"name": "minni-multi-plugin"`.

- [ ] **Step 2: Gate — package builds with the new name**

Run:
```bash
cd ~/Projects/Minni/plugins/sovereign-memory
npm run build:server 2>&1 | tail -20
```
Expected: TypeScript compiles, exit 0. (This proves the dir rename + package edit didn't break the build. We build only the server, not the frontend, to keep it fast.)

- [ ] **Step 3: Commit**

```bash
cd ~/Projects/Minni
git add plugins/minni/package.json
git commit -m "refactor(minni-p1): rename npm package sovereign-memory-multi-plugin -> minni-multi-plugin"
```

---

## Task 4: Rebrand human-facing strings in plugin manifests

Only `description`, `displayName`, `owner.name`, `author.name`, `keywords` brand entries, and `homepage`/`repository` URLs (the GitHub repo `infektyd/minni` already redirects). Do NOT touch the `name` fields that equal `sovereign-memory` (the plugin identifier — that flips in P2 with aliases) or the `mcpServers` key.

**Files:**
- Modify: `.claude-plugin/marketplace.json`, `.claude-plugin/plugin.json`, `.agents/plugins/marketplace.json`, `plugins/minni/.claude-plugin/plugin.json`

- [ ] **Step 1: Rebrand `.claude-plugin/marketplace.json`**

Change `"owner": { "name": "Sovereign Memory" }` → `"Minni"`, the top-level `description` "Local Sovereign Memory agent plugins" → "Local Minni agent plugins", the plugin `description` and `author.name` "Sovereign Memory" → "Minni", and `homepage` `…/sovereign-memory` → `…/minni`. **Leave both `"name": "sovereign-memory"` identifier fields unchanged.**

- [ ] **Step 2: Rebrand `plugins/minni/.claude-plugin/plugin.json`**

Change `description` "...Sovereign Memory spine..." → "...Minni spine...", `homepage`/`repository` `…/sovereign-memory` → `…/minni`. **Leave `"name": "sovereign-memory"`, the `keywords` entry `"sovereign-memory"`, and the entire `mcpServers` block unchanged** (P2 owns those).

- [ ] **Step 3: Rebrand `.agents/plugins/marketplace.json`**

Change `"interface": { "displayName": "Sovereign Memory" }` → `"Minni"`. **Leave the two `"name": "sovereign-memory"` fields unchanged.**

- [ ] **Step 4: Rebrand root `.claude-plugin/plugin.json`**

Change `description`, `owner.name`/`author.name` "Sovereign Memory" → "Minni", `homepage` → `…/minni`. **Leave `name` + `keywords` identifier `sovereign-memory` unchanged.**

- [ ] **Step 5: Gate — protected identifiers untouched**

Run:
```bash
cd ~/Projects/Minni
rg -n '"name": *"sovereign-memory"|"sovereign-memory":|mcp__sovereign-memory__' .claude-plugin .agents plugins/minni/.claude-plugin plugins/minni/.mcp.json
```
Expected: the `name`/`mcpServers`-key/namespace lines STILL present (we intentionally kept them). If any disappeared, restore — they belong to P2.

- [ ] **Step 6: Commit**

```bash
cd ~/Projects/Minni
git add .claude-plugin .agents/plugins/marketplace.json plugins/minni/.claude-plugin
git commit -m "refactor(minni-p1): rebrand human-facing strings in plugin manifests (identifiers untouched)"
```

---

## Task 5: Rebrand top-level project docs

**Files:**
- Modify: `README.md`, `AGENTS.md`, `DESIGN.md`, `SECURITY_PLAN.md`, `docs/CANONICAL-PATHS.md`, and any other top-level `.md` where "Sovereign Memory" is branding.

- [ ] **Step 1: Find brand occurrences in top-level docs (excluding identifier/path references)**

Run:
```bash
cd ~/Projects/Minni
rg -ln "Sovereign Memory" README.md AGENTS.md DESIGN.md SECURITY_PLAN.md docs/*.md
```
Expected: a file list. Review each.

- [ ] **Step 2: Replace the brand phrase, preserving identifiers/paths**

For each file, replace the human-readable brand phrase "Sovereign Memory" → "Minni", but leave intact: literal paths (`~/.sovereign-memory/`), MCP namespace (`mcp__sovereign-memory__`), tool/skill IDs, env vars, the `agent_origin` value. Where a doc states the equivalence ("Minni is the brand; sovereign-memory is the runtime namespace"), keep that explanatory sentence — update it to note the deep-rename-in-progress instead of "do not rename."

Apply per file:
```bash
cd ~/Projects/Minni
# Review-then-apply: do NOT blind-sed paths/identifiers. Edit each file deliberately.
```

- [ ] **Step 3: Gate — protected strings survived the doc edits**

Run:
```bash
cd ~/Projects/Minni
rg -c "mcp__sovereign-memory__|~/.sovereign-memory|sovereign_recall|SOVEREIGN_" README.md AGENTS.md DESIGN.md SECURITY_PLAN.md docs/CANONICAL-PATHS.md 2>/dev/null
```
Expected: counts > 0 wherever those identifiers were legitimately documented (they should still be present, now framed as "current runtime name, rename in progress").

- [ ] **Step 4: Public-boundary check still passes**

Run:
```bash
cd ~/Projects/Minni
bash scripts/check-public-boundary.sh 2>&1 | tail -20
```
Expected: pass (exit 0). If it flags the operator's real name in the `plugin.json` author block, replace that name field with the project handle (`infektyd` / `relayBit`) to satisfy the guard.

- [ ] **Step 5: Commit**

```bash
cd ~/Projects/Minni
git add -A
git commit -m "docs(minni-p1): rebrand Sovereign Memory -> Minni in top-level docs (paths/identifiers preserved)"
```

---

## Task 6: Final P1 verification gate

- [ ] **Step 1: Plugin build is green**

Run:
```bash
cd ~/Projects/Minni/plugins/sovereign-memory
npm run build:server 2>&1 | tail -10
```
Expected: exit 0.

- [ ] **Step 2: Existing plugin test suite passes**

Run:
```bash
cd ~/Projects/Minni/plugins/sovereign-memory
npm test 2>&1 | tail -30
```
Expected: tests pass (or the same pre-existing failures as on `main` — diff against a baseline run if unsure). Record any failures; do not claim green if red.

- [ ] **Step 3: Protected-identifier audit — P1 left the runtime surface intact**

Run:
```bash
cd ~/Projects/Minni
echo "MCP namespace refs:"; rg -c "mcp__sovereign-memory__" --glob '!dist' --glob '!node_modules' | wc -l
echo "tool verb refs:";    rg -c "sovereign_recall|sovereign_learn" --glob '!dist' --glob '!node_modules' | wc -l
echo "vault path refs:";   rg -c "\.sovereign-memory/" --glob '!dist' --glob '!node_modules' | wc -l
```
Expected: all still non-zero — confirming P1 did NOT prematurely rename coupled identifiers (those are P2's job).

- [ ] **Step 4: Summary diff review**

Run:
```bash
cd ~/Projects/Minni
git log --oneline rebrand/minni-deep-rename...main
git diff --stat main...rebrand/minni-deep-rename
```
Expected: commits from Tasks 1–5; changes confined to manifests, docs, and the dir rename.

- [ ] **Step 5: Hand off to P2**

P1 is complete and the branch is functionally identical at runtime. **Do NOT install/deploy this branch yet** — the plugin `name`/`mcpServers` key still say `sovereign-memory`, which is correct until P2 renames them WITH the alias layer. Next: write the P2 plan (`docs/superpowers/plans/<date>-minni-rename-p2-runtime-aliases.md`).

---

## Downstream plans (not in this file)

- **P2** — runtime identifier rename + compat aliases (MCP server key, tool verbs, env vars), daemon dual-naming. Needs the live daemon and operator awareness.
- **P3** — vault migration, daemon DOWN (operator authorized).
- **P4** — per-platform reconfig/repair (Gemini first, then Claude Code / Codex / Kilocode / Grok-build; fix grok stale paths).
- **P5** — skills keep/merge/retire audit.
- **P6** — deprecate `sovereign` aliases (decision deferred to P6).
