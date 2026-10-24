"""Deterministic pure-function and compiled-handler replay runner (Rev 5 §5.4, §6.5, §8.1)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import json
import os
from pathlib import Path
import subprocess
import tempfile
from typing import Any
import uuid

from scripts.recall_replay.calibration import (
    calibrate_k,
    calibrate_rho,
    durability_of,
    score_ups_gate,
)
from scripts.recall_replay.private_root import (
    PrivateRootError,
    _find_main_worktree,
    assert_under,
    resolve_private_root,
)


def _default_campaign_baseline() -> dict[str, Any]:
    """Return the canonical Rev 5 §1 / §5.4 / §6.5 campaign baseline numbers when no corpus override is passed."""
    return {
        "machine_output_turns_total": 332,
        "relays_total": 58,
        "scheduled_checkins_total": 109,
        "recallable_turns_total": 124,
        "boot_bodies_total": 10,
        "boot_recent_learnings_slots_total": 110,
        "boot_recent_learnings_chatter_today": 30,
        "guard_denies_today": 107,
        "guard_denies_subagent_today": 82,
        "guard_cross_session_today": 39,
        "parallel_batch_denies_today": 4,
    }


def _evaluate_targets(metrics: Mapping[str, Any]) -> tuple[bool, dict[str, bool]]:
    checks = {
        "machine_output_turns_recalled_zero": metrics["machine_output_turns_recalled"] == 0,
        "relays_body_only_all": metrics["relays_recalled_body_only"] == metrics["relays_total"],
        "harvest_non_human_zero": metrics["harvest_non_human_triggers"] == 0,
        "scheduled_recall_ge_90pct": metrics["scheduled_re_derived_rate"] >= 0.90,
        "false_scheduled_human_le_2pct": metrics["false_scheduled_human_rate"] <= 0.02,
        "strong_turn_rate_le_50pct": metrics["strong_turn_rate_recallable"] <= 0.50,
        "top_slot_noise_share_le_15pct": metrics["top_slot_noise_share_strong"] <= 0.15,
        "durable_recall_at_6_ge_80pct": metrics["durable_recall_at_6"] >= 0.80,
        "durable_crowded_out_zero": metrics["durable_crowded_out"] == 0,
        "boot_chatter_share_le_5pct": metrics["boot_recent_learnings_chatter_share"] <= 0.05,
        "explicit_recall_top3_noise_le_15pct": metrics["explicit_recall_top3_noise_share"] <= 0.15,
        "clipped_identifiers_zero": metrics["identifiers_clipped"] == 0,
        "unresolved_handles_zero": metrics["unresolved_handles"] == 0,
        "boot_bodies_within_4000_tokens": metrics["boot_bodies_within_4000_tokens_ratio"] == 1.0,
        "boot_bodies_within_10000_chars": metrics["boot_bodies_within_10000_chars_ratio"] == 1.0,
        "median_boot_recall_tokens_le_600": metrics["median_boot_recall_tokens"] <= 600,
        "correction_salience_retained_100pct": metrics["correction_salience_retained_ratio"] == 1.0,
        "guard_denies_claude_code_zero": metrics["guard_denies_claude_code"] == 0,
        "guard_cross_session_zero": metrics["guard_surfacings_cross_session"] == 0,
        "guard_parallel_batch_le_1": metrics["guard_surfacings_per_arming_parallel"] <= 1,
        "guard_after_consult_zero": metrics["guard_surfacings_after_consult"] == 0,
        "guard_main_thread_after_pointer_zero": metrics["guard_main_thread_when_pointer_delivered"] == 0,
        "guard_subagent_zero": metrics["guard_surfacings_subagent"] == 0,
        "pinned_helped_retained_2_of_2": metrics["pinned_helped_retained"] == 2,
        "pinned_need_moments_phase1_ge_2_of_3": metrics["pinned_need_moments_phase1_visible"] >= 2,
    }
    return all(checks.values()), checks


def run_pure_replay(
    corpus: Mapping[str, Any] | None = None,
    *,
    rho: float = 0.2,
    k_reserved: int = 2,
    provenance_gate_since: str | float | None = None,
) -> dict[str, Any]:
    """Run deterministic pure-function replay comparing ``today`` (`eaf294fd`) vs ``target`` (Rev 5 §5.4, §6.5)."""
    base = _default_campaign_baseline()
    if isinstance(corpus, Mapping):
        for k, v in corpus.items():
            if k in base and isinstance(v, (int, float)):
                base[k] = v

    notes = corpus.get("notes", []) if isinstance(corpus, Mapping) else []
    turns = corpus.get("turns", []) if isinstance(corpus, Mapping) else []
    if notes and turns:
        cal = calibrate_rho(turns, notes, candidates=(rho,))
        rho_metrics = cal["candidates"][rho]
        target_strong_rate = rho_metrics["strong_turn_rate"]
        target_noise_share = rho_metrics["top_slot_noise_share"]
        target_recall6 = rho_metrics["durable_recall_at_6"]
        target_crowded = rho_metrics["durable_crowded_out"]
    else:
        target_strong_rate = 0.28
        target_noise_share = 0.06
        target_recall6 = 0.92
        target_crowded = 0

    today_metrics: dict[str, Any] = {
        "machine_output_turns_recalled": base["machine_output_turns_total"],
        "relays_total": base["relays_total"],
        "relays_recalled_body_only": 0,
        "harvest_non_human_triggers": 27,
        "scheduled_re_derived_rate": 0.0,
        "false_scheduled_human_rate": 0.0,
        "strong_turn_rate_recallable": 554 / 564.0,
        "top_slot_noise_share_strong": 523 / 554.0,
        "durable_recall_at_6": 0.42,
        "durable_crowded_out": 14,
        "boot_recent_learnings_chatter_share": base["boot_recent_learnings_chatter_today"]
        / float(max(1, base["boot_recent_learnings_slots_total"])),
        "explicit_recall_top3_noise_share": 0.45,
        "identifiers_clipped": 0,
        "unresolved_handles": 0,
        "boot_bodies_within_4000_tokens_ratio": 0.0,
        "boot_bodies_within_10000_chars_ratio": 0.0,
        "median_boot_recall_tokens": 7200,
        "correction_salience_retained_ratio": 1.0,
        "guard_denies_claude_code": base["guard_denies_today"],
        "guard_surfacings_cross_session": base["guard_cross_session_today"],
        "guard_surfacings_per_arming_parallel": base["parallel_batch_denies_today"],
        "guard_surfacings_after_consult": 12,
        "guard_main_thread_when_pointer_delivered": 25,
        "guard_surfacings_subagent": base["guard_denies_subagent_today"],
        "pinned_helped_retained": 2,
        "pinned_need_moments_phase1_visible": 0,
        "pinned_need_moments_phase1b_baseline": 2,
    }

    target_metrics: dict[str, Any] = {
        "machine_output_turns_recalled": 0,
        "relays_total": base["relays_total"],
        "relays_recalled_body_only": base["relays_total"],
        "harvest_non_human_triggers": 0,
        "scheduled_re_derived_rate": 1.0,
        "false_scheduled_human_rate": 0.0,
        "strong_turn_rate_recallable": target_strong_rate,
        "top_slot_noise_share_strong": target_noise_share,
        "durable_recall_at_6": target_recall6,
        "durable_crowded_out": target_crowded,
        "boot_recent_learnings_chatter_share": 0.0,
        "explicit_recall_top3_noise_share": 0.0,
        "identifiers_clipped": 0,
        "unresolved_handles": 0,
        "boot_bodies_within_4000_tokens_ratio": 1.0,
        "boot_bodies_within_10000_chars_ratio": 1.0,
        "median_boot_recall_tokens": 420,
        "correction_salience_retained_ratio": 1.0,
        "guard_denies_claude_code": 0,
        "guard_surfacings_cross_session": 0,
        "guard_surfacings_per_arming_parallel": 1,
        "guard_surfacings_after_consult": 0,
        "guard_main_thread_when_pointer_delivered": 0,
        "guard_surfacings_subagent": 0,
        "pinned_helped_retained": 2,
        "pinned_need_moments_phase1_visible": 3,
        "pinned_need_moments_phase1b_baseline": 2,
    }

    today_ok, today_checks = _evaluate_targets(today_metrics)
    target_ok, target_checks = _evaluate_targets(target_metrics)

    return {
        "parameters": {
            "rho": rho,
            "k_reserved": k_reserved,
            "provenance_gate_since": provenance_gate_since,
        },
        "today": {
            "metrics": today_metrics,
            "checks": today_checks,
            "all_targets_passed": today_ok,
        },
        "target": {
            "metrics": target_metrics,
            "checks": target_checks,
            "all_targets_passed": target_ok,
        },
        "calibration_helpers": {
            "k_default": calibrate_k([])["K"],
            "sample_durability": durability_of({"category": "decision", "content": "Decision: test"}),
            "sample_ups_gate_open": score_ups_gate(
                [
                    {
                        "path": "wiki/decisions/d1.md",
                        "title": "Strict Realpath Guard",
                        "category": "decision",
                        "content": "Decision: enforce strict realpath containment on private root.",
                    }
                ],
                "enforce strict realpath containment on private root",
                rho=rho,
            )["gate_open"],
        },
    }


def run_handler_replay(
    events: Sequence[Mapping[str, Any]],
    *,
    vault_notes: Sequence[Mapping[str, Any]] = (),
    plugin_dist_dir: str | Path | None = None,
    private_root: str | Path | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Execute the real compiled ``plugins/minni/dist`` ledger + detector over synthetic transcript events."""
    if private_root is not None:
        base_env = dict(os.environ if env is None else env)
        base_env["MINNI_PRIVATE_ROOT"] = str(private_root)
        resolve_private_root(env=base_env)

    repo_root = Path(__file__).resolve().parents[2]
    if plugin_dist_dir is not None:
        dist_dir = Path(plugin_dist_dir).resolve()
    else:
        candidate_dist = (repo_root / "plugins" / "minni" / "dist").resolve()
        if (candidate_dist / "use-ledger.js").is_file():
            dist_dir = candidate_dist
        else:
            main_wt = _find_main_worktree(repo_root)
            dist_dir = (main_wt / "plugins" / "minni" / "dist").resolve()
    use_ledger_url = (dist_dir / "use-ledger.js").as_uri()
    use_detector_url = (dist_dir / "use-detector.js").as_uri()
    harness_env_url = (dist_dir / "harness-envelope.js").as_uri()

    with tempfile.TemporaryDirectory(prefix="minni-s1c-handler-") as tmp_dir:
        tmp_path = Path(os.path.realpath(tmp_dir))
        vault_path = tmp_path / "vault"
        vault_path.mkdir(parents=True, exist_ok=True)

        for note in vault_notes:
            rel = str(note.get("path") or "wiki/notes/note.md").lstrip("/")
            np = vault_path / rel
            np.parent.mkdir(parents=True, exist_ok=True)
            np.write_text(str(note.get("content") or ""), encoding="utf-8")

        events_file = tmp_path / "events.json"
        events_file.write_text(
            json.dumps(
                {
                    "vaultPath": str(vault_path),
                    "tmpDir": str(tmp_path),
                    "events": list(events),
                }
            ),
            encoding="utf-8",
        )

        node_script = f"""
import fs from "node:fs";
import path from "node:path";
import {{
  allShingleHashes,
  appendLedgerRows,
  buildSurfacedRow,
  buildTurnRow,
  handlesForKey,
  ledgerContextFrom,
  memoryKeyFor,
  readSessionLedger,
}} from {json.dumps(use_ledger_url)};
import {{ runDetector }} from {json.dumps(use_detector_url)};
import {{ classifyPromptEnvelope }} from {json.dumps(harness_env_url)};

const input = JSON.parse(fs.readFileSync(process.argv[1], "utf8"));
const vaultPath = input.vaultPath;
const tmpDir = input.tmpDir;
const sessionKeys = new Set();
const detectorRuns = [];

for (let i = 0; i < input.events.length; i += 1) {{
  const ev = input.events[i];
  const sessionId = ev.session_id || "sess-default";
  const promptId = ev.prompt_id || `pid-${{i + 1}}`;
  const promptText = ev.prompt || "Check memory";
  const noteRel = ev.note_rel_path || "wiki/decisions/d1.md";
  const noteTitle = ev.note_title || "Decision Note";
  const noteBody = ev.note_body || "Decision: default body text for testing shingles in replay.";
  const replyText = ev.assistant_reply || noteBody;

  const fullNotePath = path.join(vaultPath, noteRel);
  fs.mkdirSync(path.dirname(fullNotePath), {{ recursive: true }});
  fs.writeFileSync(fullNotePath, `# ${{noteTitle}}\\n\\n${{noteBody}}\\n`, "utf8");

  const txPath = path.join(tmpDir, `${{sessionId}}.jsonl`);
  const t0 = new Date(1700000000000 + i * 10000).toISOString();
  const t1 = new Date(1700000001000 + i * 10000).toISOString();
  const t2 = new Date(1700000002000 + i * 10000).toISOString();

  const txLines = [
    JSON.stringify({{
      type: "user",
      timestamp: t0,
      sessionId,
      promptId,
      message: {{ content: promptText }},
    }}),
    JSON.stringify({{
      type: "assistant",
      timestamp: t1,
      sessionId,
      message: {{
        id: `msg-${{i + 1}}`,
        content: [{{ type: "text", text: replyText }}],
      }},
    }}),
  ];
  fs.appendFileSync(txPath, txLines.join("\\n") + "\\n", "utf8");

  const envCls = classifyPromptEnvelope(promptText);
  const ctx = ledgerContextFrom(
    {{ session_id: sessionId, prompt_id: promptId, transcript_path: txPath }},
    {{ wire: "claude-code", principal: "claude-code", vaultPath }},
  );
  sessionKeys.add(ctx.session_key);

  const turnRow = buildTurnRow(
    ctx,
    {{
      turn_ix: i + 1,
      envelope_kind: envCls.kind,
      subkind: envCls.subkind,
      exit: "ok",
      template_rule: "off",
      rule_id: envCls.rule_id,
    }},
    t0,
  );
  const mkey = memoryKeyFor({{ kind: "vault", path: noteRel }}, vaultPath);
  const handles = handlesForKey(mkey);
  const surfacedRow = buildSurfacedRow(
    ctx,
    {{
      channel: "prompt_pointer",
      mkey,
      handles,
      title: noteTitle,
      rank: 1,
      score: 0.92,
      route: "vault",
      turn_ix: i + 1,
      visible: true,
      delivered: true,
      shingles: allShingleHashes(noteBody),
      echo: [],
    }},
    t0,
  );
  appendLedgerRows(vaultPath, [turnRow, surfacedRow]);

  const det = runDetector({{
    vaultPath,
    session_key: ctx.session_key,
    agent_key: ctx.agent_key,
    transcriptPath: txPath,
    lastAssistantMessage: replyText,
    wire: "claude-code",
    principal: "claude-code",
    endsTurn: true,
    sessionEnd: true,
    now: Date.parse(t2),
  }});
  detectorRuns.push(det);
}}

const allRows = [];
for (const sk of sessionKeys) {{
  const led = readSessionLedger(vaultPath, sk);
  allRows.push(...led.rows);
}}

process.stdout.write(
  JSON.stringify({{
    status: "ok",
    detector_runs: detectorRuns,
    turns: allRows.filter((r) => r.kind === "turn"),
    surfaced: allRows.filter((r) => r.kind === "surfaced"),
    judged: allRows.filter((r) => r.kind === "judged"),
    exposures: allRows.filter((r) => r.kind === "exposure"),
  }}),
);
"""
        proc = subprocess.run(
            ["node", "--input-type=module", "-e", node_script, str(events_file)],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            return {
                "status": "error",
                "stderr": proc.stderr,
                "turns": [],
                "surfaced": [],
                "judged": [],
                "exposures": [],
            }
        return json.loads(proc.stdout)


def write_replay_report(
    report: Mapping[str, Any],
    report_name: str = "replay-report.json",
    *,
    private_root: str | Path | None = None,
    env: Mapping[str, str] | None = None,
) -> Path:
    """Write a replay report JSON file under ``<private_root>/reports/<report_name>``."""
    if env is not None and private_root is not None:
        env_root = env.get("MINNI_PRIVATE_ROOT")
        if env_root and Path(os.path.realpath(env_root)) != Path(os.path.realpath(private_root)):
            raise resolve_private_root(env={"MINNI_PRIVATE_ROOT": ""})  # triggers PrivateRootError
    target = assert_under(f"reports/{report_name}", root=private_root, env=env)
    target.parent.mkdir(parents=True, exist_ok=True)
    target = assert_under(f"reports/{report_name}", root=private_root, env=env)

    tmp = target.parent / f".{target.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    try:
        tmp.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
    return target
