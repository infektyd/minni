# Minni Rename — P3b: Daemon De-identification (neutral `minni` core)

> Daemon + Hermes gateway + console are DOWN. Offline work. Operator chose **full de-identify now**.

## Why this phase exists (the keystone)
The standalone problem has a root cause: the **Nous Research Hermes agent hallucinated the operator's intent and built the memory system INTO ITSELF** instead of as a separate core. That's why the daemon runs *as* `hermes`, under the `com.openclaw.*` namespace, from a generic `Python` process — Minni was never visible as Minni. **P3b extracts Minni out of Hermes** so the core stands alone and is visibly `minni`.

## Decisions
- **Full de-identify** to a neutral `minni` core (operator, 2026-05-30).
- **Hermes is dormant** → its gateway is brought down; no Hermes *client* config is built now; Hermes's old vault `~/wiki` is **parked untouched** (not folded into the core).
- AFM bridge (`com.openclaw.foundation-models-bridge`) stays up (model backend, not vault-coupled).

## Target end state
| Now | Target |
|---|---|
| launchd `com.openclaw.sovrd` | **`com.minni.minnid`** (relabel + rename plist file) |
| `MINNI_AGENT_ID=hermes` | **`minni`** (neutral core identity, not an agent) |
| `engine/sovrd.py` | **`engine/minnid.py`** |
| `engine/sovrd_client.py` | **`engine/minnid_client.py`** |
| socket `~/.minni/run/sovrd.sock` | **`~/.minni/run/minnid.sock`** |
| core vault `MINNI_VAULT_PATH=~/wiki` (hermes's) | core hosts per-agent vaults under `~/.minni/`; drop the hermes agent-vault from the core plist (`~/wiki` parked) |

## Tasks (execute when ready; daemon stays down through bring-up)
1. **Rename the daemon binary + client:** `git mv engine/sovrd.py engine/minnid.py`, `git mv engine/sovrd_client.py engine/minnid_client.py` (if present). Fix all in-repo references (imports, the `--socket`/help strings, `sovrd.py` mentions in code; leave docs to a targeted pass). Gate: `python3 -c "import ast; ast.parse(open('engine/minnid.py').read())"`.
2. **Rename the socket** `sovrd.sock` → `minnid.sock` everywhere it's referenced: TS `config.ts` default, Python `config.py`/`minnid_client.py`, the launchd plist, and **all client configs** (`gemini-extension.json`, `grok-minni/.mcp.json`, per-platform skill docs, `sm-propagation`). This is the ripple — grep `sovrd.sock` repo-wide (excl. docs-as-history) and update. Gate: `rg "sovrd\.sock" plugins engine -g '!*.md'` → EMPTY.
3. **Neutralize the core identity:** in the launchd plist, `MINNI_AGENT_ID` `hermes`→`minni`; remove/neutralize `MINNI_VAULT_PATH=~/wiki` (core doesn't own an agent vault). If the daemon code requires an agent id/vault to boot, set a neutral core default (`minni`, `~/.minni`).
4. **Rename + relabel the launchd job:** new file `~/Library/LaunchAgents/com.minni.minnid.plist` (label `com.minni.minnid`, ProgramArguments → `minnid.py`, StandardOut/Err → `~/Library/Logs/minni/minnid.{out,err}.log`); back up + remove the old `com.openclaw.sovrd.plist`. Do NOT load yet (bring-up loads it).
5. **Verify (offline):** TS build exit 0; Python syntax OK; `rg "sovrd|SOVEREIGN_|hermes|com.openclaw" engine plugins -g '!*.md'` shows only intentional leftovers (AFM bridge label, legacy ~/.openclaw scripts); plist lint OK.

## Bring-up (final, operator present — NOT in P3b)
`launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.minni.minnid.plist` → confirm Activity Monitor shows `minnid` / `com.minni`, socket `~/.minni/run/minnid.sock` created, recall/learn round-trip across all platforms.

## Sequencing
P5 (skills) and P3b are both offline and independent — either order. Bring-up is last, after both.
