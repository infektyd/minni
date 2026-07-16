#!/usr/bin/env python3
"""Three-way cross-check: workload ledgers (ground truth) vs watch.jsonl
(artifact under test) vs the raw stores. Exit 0 only if every check passes.
Hardened per adversarial review: proposed_at filter, non-vacuous checks,
positional ordering, adversarial-entry assertions, second-vault attribution,
burst-drain proof, raw-escape scan."""
import json
import sqlite3
import sys
from collections import Counter
from datetime import datetime

ART = sys.argv[1]
START = float(sys.argv[2])  # workload-start epoch; earlier rows are excluded
DB = "/tmp/mlive/minni.db"
SESSION = "sess-live-A"

failures = []


def check(name, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}"
          + (f": {detail}" if detail else ""))
    if not ok:
        failures.append(name)


def load_jsonl(path):
    rows = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def ts_epoch(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


ledger = load_jsonl(f"{ART}/ledger.jsonl")
audit_ledger = load_jsonl(f"{ART}/audit_ledger.jsonl")
watch_raw = open(f"{ART}/watch.jsonl").read()
watch = [w for w in load_jsonl(f"{ART}/watch.jsonl") if ts_epoch(w["ts"]) >= START]
receipt = json.load(open(f"{ART}/receipt.json"))
text_once = open(f"{ART}/watch_text.out", errors="surrogateescape").read()
agent_filtered = load_jsonl(f"{ART}/watch_agent_filter.jsonl")

led_searches = [r for r in ledger if r["op"] == "search"]
led_events = [r for r in ledger if r["op"] == "log_event"]
led_learns = [r for r in ledger if r["op"] == "learn"]
aud_loadgen = [r for r in audit_ledger if r["vault"].endswith("loadgen-vault")]
aud_teammate = [r for r in audit_ledger if r["vault"].endswith("teammate-vault")]

print("== 0. workload health ==")
errored = [r for r in ledger if r["response"].get("error")]
check("no ledger op errored", not errored,
      f"{len(errored)} errors, first: {errored[:1]}")
check("workload volume sane", len(led_searches) >= 250 + 40 and led_learns,
      f"searches={len(led_searches)} learns={len(led_learns)}")

w_daemon_recall = [w for w in watch if w["source"] == "daemon" and w["tool"] == "recall"]
w_daemon_events = [w for w in watch if w["source"] == "daemon" and w["tool"] == "ticket_triaged"]
w_plugin = [w for w in watch if w["source"] == "plugin"]
w_plugin_loadgen = [w for w in w_plugin if w["agent"] == "loadgen"]
w_plugin_teammate = [w for w in w_plugin if w["agent"] == "mapped-bot"]

print("== 1. recall bijection (ledger <-> watch <-> sqlite) ==")
check("count ledger==watch", len(led_searches) == len(w_daemon_recall),
      f"ledger={len(led_searches)} watch={len(w_daemon_recall)}")
check("burst exceeded one poll batch (drain proof)", len(led_searches) >= 250)
unmatched = []
for r in led_searches:
    expect = f'recall "{r["args"]["query"][:120]}" — {r["response"]["hits"]} hits'
    matches = [w for w in w_daemon_recall if w["summary"].startswith(expect)]
    if len(matches) != 1:
        unmatched.append((r["seq"], expect, len(matches)))
check("each search matched exactly once in watch", not unmatched,
      str(unmatched[:3]))
conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
conn.row_factory = sqlite3.Row
db_recalls = conn.execute(
    "SELECT content, thread_id FROM episodic_events"
    " WHERE event_type='recall' AND created_at >= ?", (START,)).fetchall()
check("count sqlite==ledger", len(db_recalls) == len(led_searches),
      f"db={len(db_recalls)}")
check("all traces stamped with session", bool(db_recalls) and all(
    row["thread_id"] == SESSION for row in db_recalls))

print("== 2. log_event bijection ==")
check("count ledger==watch", len(led_events) == len(w_daemon_events),
      f"ledger={len(led_events)} watch={len(w_daemon_events)}")
bad = [r["seq"] for r in led_events
       if len([w for w in w_daemon_events
               if w["summary"] == r["args"]["content"]]) != 1]
check("each log_event matched exactly once", not bad, str(bad[:3]))

print("== 3. learns staged as candidates ==")
db_cands = conn.execute(
    "SELECT status FROM candidate_packets WHERE proposed_at >= ?",
    (START,)).fetchall()
check("ledger learns == staged candidates (non-empty)",
      len(led_learns) > 0 and len(db_cands) == len(led_learns),
      f"db={len(db_cands)} ledger={len(led_learns)}")
check("all candidates proposed",
      bool(db_cands) and all(r["status"] == "proposed" for r in db_cands),
      str({r["status"] for r in db_cands}))
check("ledger learn responses proposed with candidate ids",
      all(r["response"]["status"] == "proposed"
          and r["response"]["candidate_id"] for r in led_learns))

print("== 4. plugin-lane bijection + mapped attribution ==")
check("loadgen count ledger==watch",
      len(aud_loadgen) == len(w_plugin_loadgen),
      f"ledger={len(aud_loadgen)} watch={len(w_plugin_loadgen)}")
check("teammate vault attributed to mapped-bot (MINNI_AGENT_VAULTS live)",
      len(aud_teammate) > 0 and len(aud_teammate) == len(w_plugin_teammate),
      f"ledger={len(aud_teammate)} watch={len(w_plugin_teammate)}")
def _plugin_key(tool, summary):
    # Mirror what the plugin+watch pipeline does to a raw summary:
    # recordAudit's SEC-014 inline escaping first (backslash doubling,
    # newline/CR to literal \n \r — keeps the audit header single-line),
    # then watch's terminal control-char stripping.
    escaped = (summary.replace("\\", "\\\\")
               .replace("\r", "\\r").replace("\n", "\\n"))
    clean = "".join(ch for ch in escaped
                    if not (ord(ch) < 32 or 0x7f <= ord(ch) <= 0x9f))
    return (tool, clean)


led_multiset = Counter(_plugin_key(r["tool"], r["summary"]) for r in aud_loadgen)
watch_multiset = Counter((w["tool"], w["summary"]) for w in w_plugin_loadgen)
check("loadgen audit multiset equality (per-line, duplicates safe)",
      led_multiset == watch_multiset,
      f"only-in-ledger={list((led_multiset - watch_multiset).keys())[:3]} "
      f"only-in-watch={list((watch_multiset - led_multiset).keys())[:3]}")

print("== 5. adversarial entry ==")
adv = [w for w in w_plugin_loadgen if "adversarial" in w["summary"]]
check("forged-header entry bijected as exactly one event", len(adv) == 1,
      f"found {len(adv)}")
check("no phantom fake_tool event",
      not any(w["tool"] == "fake_tool" for w in watch))
check("no raw escapes in watch.jsonl", "\x1b" not in watch_raw
      and "\x9b" not in watch_raw and "\x07" not in watch_raw)
check("no raw escapes in text-mode output", "\x1b" not in text_once
      and "\x9b" not in text_once and "\x07" not in text_once)
check("adversarial summary visible sanitized", "adversarial" in text_once)

print("== 6. no phantoms (per-line mapping, not just totals) ==")
expected_summaries = Counter()
for r in led_searches:
    expected_summaries[
        f'recall "{r["args"]["query"][:120]}" — {r["response"]["hits"]} hits, '
        f"top 0.00"] += 1
for r in led_events:
    expected_summaries[r["args"]["content"]] += 1
for r in audit_ledger:
    expected_summaries[_plugin_key(r["tool"], r["summary"])[1]] += 1
actual_summaries = Counter(w["summary"] for w in watch)
phantoms = actual_summaries - expected_summaries
missing = expected_summaries - actual_summaries
check("every watch line maps to a ledger line and vice versa",
      not phantoms and not missing,
      f"phantoms={list(phantoms.items())[:3]} missing={list(missing.items())[:3]}")

print("== 7. ordering ==")
pairs_ok = all(
    w["summary"].startswith(f'recall "{r["args"]["query"][:120]}"')
    for r, w in zip(led_searches, w_daemon_recall))
check("watch daemon-lane order pairs positionally with ledger seq", pairs_ok)
inversions = [
    (a["ts"], b["ts"]) for a, b in zip(watch, watch[1:])
    if ts_epoch(a["ts"]) - ts_epoch(b["ts"]) > 2.0]
check("cross-lane inversions bounded by poll interval (<2s)", not inversions,
      str(inversions[:3]))

print("== 8. --agent filter live ==")
check("--agent mapped-bot returns only mapped-bot",
      bool(agent_filtered) and all(w["agent"] == "mapped-bot"
                                   for w in agent_filtered),
      f"{len(agent_filtered)} rows")

print("== 9. session receipt vs audit ledger ==")
tally = {
    "recalls_strong": sum(1 for r in aud_loadgen
                          if r["tool"].endswith("_user_prompt_submit")
                          and r["details"].get("recall_strong") is True),
    "recalls_weak": sum(1 for r in aud_loadgen
                        if r["tool"].endswith("_user_prompt_submit")
                        and r["details"].get("recall_strong") is False),
    "guard_denied": sum(1 for r in aud_loadgen
                        if r["tool"].endswith("_pretooluse_guard")
                        and r["summary"].startswith("recall guard denied")),
    "learns": sum(1 for r in aud_loadgen if r["tool"] == "minni_learn"),
    "candidates_drafted": sum(r["details"].get("candidates", 0)
                              for r in aud_loadgen
                              if r["tool"].endswith("_stop")),
}
for key, want in tally.items():
    check(f"receipt.{key} == {want}", receipt.get(key) == want,
          f"receipt={receipt.get(key)}")

print()
if failures:
    print(f"VERDICT: FAIL — {len(failures)} check(s): {failures}")
    sys.exit(1)
print("VERDICT: PASS — watch recorded exactly the issued operations")
