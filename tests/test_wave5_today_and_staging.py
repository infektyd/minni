"""Wave 5 verification suite:
1. Stdlib-only import invariant on `minni.today_digest` + `minni.minni_cli`.
2. Live `minnid` subprocess E2E: `minni map`, `minni learn --accept` (rule + scar +
   contradicting rule), `legacy_unclassified` filtering, `<minni_home>/TODAY.md`
   (`0600`) generation, `minni status` human overview, and post-commit
   `minni activity` staleness refresh (`--json` stdout purity + `--no-refresh`).
3. Symlink refusal, git flag-injection (`--output=...` / `-dash` repo root) hardening,
   `300ms` git timeout handling, wrapper/blocked/escape sanitization, and leaked
   `<32-hex>-vault` test-dir filtering on `today_digest.py`.
4. `BUG-06` non-clobbering proof: `bundle_server.mjs --outfile`, `stage_payload.py`,
   and `build_from_repo` produce bundled staged `dist/server.js` while leaving
   `plugins/minni/dist/server.js` modular (`tsc` output) so
   `plugins/minni/tests/vault-search-process-path.test.mjs` passes immediately
   without re-running `tsc`.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time

import pytest

import minni.minni_cli as minni_cli
import minni.today_digest as today_digest

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO_ROOT / "plugins" / "minni"
PAYLOAD_ROOT = REPO_ROOT / "src" / "minni" / "plugin_payload"


def _git(repo: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Wave5 Builder",
        "GIT_AUTHOR_EMAIL": "wave5@example.invalid",
        "GIT_COMMITTER_NAME": "Wave5 Builder",
        "GIT_COMMITTER_EMAIL": "wave5@example.invalid",
        "GIT_TERMINAL_PROMPT": "0",
    }
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return proc.stdout.strip()


def _load_stage_payload_module():
    spec = importlib.util.spec_from_file_location(
        "_wave5_stage_payload", REPO_ROOT / "scripts" / "stage_payload.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_today_digest_and_minni_cli_preserve_stdlib_only_import_invariant(tmp_path: Path) -> None:
    """`today_digest.py` and `minni_cli.py` must never import heavy ML/YAML or engine modules."""
    isolated_home = tmp_path / "minni-home"
    isolated_home.mkdir(parents=True)
    probe = (
        "import json, sys\n"
        "import minni.today_digest as td\n"
        "import minni.minni_cli as cli\n"
        "md, data = td.render_today_digest()\n"
        "assert \"# Minni — Today's Digest\" in md\n"
        "forbidden = {\n"
        "    'numpy', 'torch', 'faiss', 'sentence_transformers', 'yaml',\n"
        "    'minni.retrieval', 'minni.db', 'minni.repo_map', 'minni.minnid',\n"
        "}\n"
        "loaded = sorted(m for m in sys.modules if m in forbidden or m.split('.')[0] in {'numpy', 'torch', 'faiss', 'yaml', 'sentence_transformers'})\n"
        "print(json.dumps({'loaded': loaded, 'plans': data['active_plans_count']}))\n"
    )
    env = {
        **os.environ,
        "PYTHONPATH": str(REPO_ROOT / "src"),
        "HOME": str(tmp_path),
        "MINNI_HOME": str(isolated_home),
    }
    proc = subprocess.run(
        [sys.executable, "-c", probe],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    )
    payload = json.loads(proc.stdout.strip())
    assert payload["loaded"] == [], f"Forbidden non-stdlib modules imported: {payload['loaded']}"


def test_e2e_live_minnid_today_digest_status_and_activity_refresh(tmp_path: Path) -> None:
    """Live `minnid` subprocess E2E proving auto-generated TODAY.md (0600), conflict filtering,
    human-grouped learnings/scars, `minni status` summary, and post-commit `minni activity` refresh."""
    short_dir = Path(tempfile.mkdtemp(prefix="smw5-", dir="/tmp"))
    sock_path = short_dir / "d.sock"
    minni_home = tmp_path / "minni-home"
    fake_user_home = tmp_path / "user-home"
    minni_home.mkdir(parents=True)
    fake_user_home.mkdir(parents=True)

    # Seed live session, dead stale session (>3h ago), active plan, pending handoff, and leaked hex vault
    codex_rt = minni_home / "codex-vault" / ".runtime"
    codex_rt.mkdir(parents=True)
    (codex_rt / f"active-session-codex-{os.getpid()}.json").write_text(
        json.dumps(
            {
                "agent_id": "codex",
                "host_pid": os.getpid(),
                "workspace_id": "workspace-alpha",
                "updated_at": time.time(),
            }
        ),
        encoding="utf-8",
    )
    stale_sess = codex_rt / "active-session-codex-999999.json"
    stale_sess.write_text(
        json.dumps(
            {
                "agent_id": "stale-worker",
                "host_pid": 999999,
                "workspace_id": "workspace-alpha",
                "updated_at": time.time() - 14400.0,
            }
        ),
        encoding="utf-8",
    )

    artifacts_dir = minni_home / "codex-vault" / "wiki" / "artifacts"
    artifacts_dir.mkdir(parents=True)
    plan_note = artifacts_dir / "alpha-rollout.md"
    plan_note.write_text(
        "---\ntitle: Harden Alpha Storage Gate\nstatus: in_progress\nworkspace_id: workspace-alpha\n---\n\n# Harden Alpha Storage Gate\n",
        encoding="utf-8",
    )
    (artifacts_dir / "_active_plan.json").write_text(
        json.dumps(
            {
                "plan_id": "thread-alpha-rollout",
                "agent_id": "codex",
                "workspace_id": "workspace-alpha",
                "notePath": "wiki/artifacts/alpha-rollout.md",
            }
        ),
        encoding="utf-8",
    )

    inbox_dir = minni_home / "codex-vault" / "inbox"
    inbox_dir.mkdir(parents=True)
    (inbox_dir / "handoff-next.json").write_text(
        json.dumps(
            {
                "kind": "handoff",
                "lease_id": "lease-alpha-1",
                "from_agent": "claude-code",
                "to_agent": "codex",
                "status": "pending",
                "task": "Verify storage retry timeout after login refactor.",
            }
        ),
        encoding="utf-8",
    )

    # Leaked <32-hex>-vault directory must never surface in TODAY.md
    leaked_rt = minni_home / "a1b2c3d4e5f60718293a4b5c6d7e8f90-vault" / ".runtime"
    leaked_rt.mkdir(parents=True)
    (leaked_rt / f"active-session-leaked-{os.getpid()}.json").write_text(
        json.dumps(
            {
                "agent_id": "leaked-test-agent",
                "host_pid": os.getpid(),
                "workspace_id": "workspace-alpha",
                "updated_at": time.time(),
            }
        ),
        encoding="utf-8",
    )

    # Create a real git repository for `workspace-alpha`
    repo = tmp_path / "alpha"
    (repo / "src" / "alpha" / "auth").mkdir(parents=True)
    (repo / "src" / "alpha" / "storage").mkdir(parents=True)
    (repo / "AGENTS.md").write_text("# Alpha Service\n\nModular auth and storage service.\n", encoding="utf-8")
    (repo / "src" / "alpha" / "auth" / "login.py").write_text(
        '"""Authentication subsystem."""\n\ndef verify_session(token: str) -> bool:\n    return bool(token)\n',
        encoding="utf-8",
    )
    (repo / "src" / "alpha" / "storage" / "store.py").write_text(
        '"""Storage subsystem."""\n\ndef open_store(uri: str) -> str:\n    return uri\n',
        encoding="utf-8",
    )
    _git(repo, "init")
    _git(
        repo,
        "add",
        "AGENTS.md",
        "src/alpha/auth/login.py",
        "src/alpha/storage/store.py",
    )
    _git(repo, "commit", "-m", "initial alpha structure")
    sha1 = _git(repo, "rev-parse", "HEAD")
    sha1_short = sha1[:7]

    env = {
        **os.environ,
        "PYTHONPATH": str(REPO_ROOT / "src"),
        "HOME": str(fake_user_home),
        "MINNI_HOME": str(minni_home),
        "MINNI_VAULT_PATH": str(minni_home / "vault"),
        "MINNI_DB_PATH": str(minni_home / "minni.db"),
        "MINNI_FAISS_PATH": str(minni_home / "minni_faiss.index"),
        "MINNI_GRAPH_DIR": str(minni_home / "graphs"),
        "MINNI_SOCKET_PATH": str(sock_path),
        "MINNI_LOCAL_OPERATOR": "1",
        "MINNI_MODEL_DEVICE": "cpu",
        "MINNI_WARMUP": "off",
        "MINNI_BACKFILL": "off",
        "MINNI_DECAY": "off",
        "MINNI_VAULT_WATCH": "off",
        "MINNI_AFM_LOOP": "off",
        "MINNI_AFM_PROVIDER_MODE": "disabled",
        "MINNI_DISABLE_AUTO_MAP_REFRESH": "1",
    }
    env.pop("MINNI_AGENT_ID", None)
    env.pop("MINNI_WORKSPACE_ID", None)

    daemon_log = tmp_path / "minnid.log"
    log_fh = daemon_log.open("w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-u", "-m", "minni.minnid", "--socket", str(sock_path)],
        env=env,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        cwd=str(REPO_ROOT),
    )
    try:
        deadline = time.monotonic() + 35.0
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                log_fh.flush()
                pytest.fail(
                    f"minnid exited early ({proc.returncode}):\n"
                    f"{daemon_log.read_text(encoding='utf-8', errors='replace')}"
                )
            if minni_cli._daemon_alive(sock_path):
                break
            time.sleep(0.1)
        else:
            log_fh.flush()
            pytest.fail(
                f"minnid did not bind socket within 35s:\n"
                f"{daemon_log.read_text(encoding='utf-8', errors='replace')}"
            )

        def _run_cli(*cli_args: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [sys.executable, "-m", "minni.minni_cli", "--socket", str(sock_path), *cli_args],
                env=env,
                capture_output=True,
                text=True,
                timeout=60,
                check=True,
            )

        # 1. Map the repository via live daemon
        map_res = _run_cli("map", str(repo), "--workspace", "workspace-alpha")
        assert "workspace-alpha" in map_res.stdout

        today_file = minni_home / "TODAY.md"
        assert today_file.is_file(), "TODAY.md should be auto-generated on `minni map`"
        assert stat.S_IMODE(today_file.stat().st_mode) == 0o600

        # Seed a legacy_unclassified contradiction_log row + dummy learnings to prove TODAY.md ignores it
        conn = sqlite3.connect(str(minni_home / "minni.db"))
        try:
            now_epoch = time.time() - 172800.0  # >24h ago so dummy rows don't count as 24h learnings
            cur1 = conn.execute(
                "INSERT INTO learnings (agent_id, category, content, confidence, created_at, workspace_id) "
                "VALUES ('main', 'fact', 'Legacy unclassified noise side A', 0.9, ?, 'workspace-alpha')",
                (now_epoch,),
            )
            cur2 = conn.execute(
                "INSERT INTO learnings (agent_id, category, content, confidence, created_at, workspace_id) "
                "VALUES ('main', 'fact', 'Legacy unclassified noise side B', 0.9, ?, 'workspace-alpha')",
                (now_epoch,),
            )
            conn.execute(
                "INSERT INTO contradiction_log "
                "(memory_a_id, memory_b_id, detected_at, detection_method, confidence, resolution_status) "
                "VALUES (?, ?, ?, 'nli', 0.88, 'legacy_unclassified')",
                (cur1.lastrowid, cur2.lastrowid, now_epoch),
            )
            conn.execute(
                "INSERT INTO contradiction_log "
                "(memory_a_id, memory_b_id, detected_at, detection_method, confidence, resolution_status) "
                "VALUES (?, ?, ?, 'cosine', 0.85, 'unresolved')",
                (cur1.lastrowid, cur2.lastrowid, now_epoch),
            )
            conn.commit()
        finally:
            conn.close()

        # 2. Record durable rule, scar, and contradicting rule via `minni learn ... --accept`
        _run_cli(
            "learn",
            "--workspace",
            "workspace-alpha",
            "--category",
            "fact",
            "--accept",
            "Always enable HTTP keepalive connection pooling on internal gateway clients.",
        )
        _run_cli(
            "learn",
            "--workspace",
            "workspace-alpha",
            "--category",
            "scar",
            "--accept",
            "SQLite reader transactions held across network retries stall WAL checkpoints.",
        )
        _run_cli(
            "learn",
            "--workspace",
            "workspace-alpha",
            "--category",
            "decision",
            "--accept",
            "Never enable HTTP keepalive connection pooling on internal gateway clients.",
        )

        assert today_file.is_file()
        assert stat.S_IMODE(today_file.stat().st_mode) == 0o600
        digest_md = today_file.read_text(encoding="utf-8")

        # Headline & plain structure assertions
        assert "# Minni — Today's Digest (" in digest_md
        assert "System: running · Active plans: 1 · New learnings (24h): 3 · Mapped projects: 1" in digest_md

        # Needs Your Call surfaces the real graph conflict and ignores legacy_unclassified
        assert "## Needs Your Call" in digest_md
        assert "Nothing waiting on you." not in digest_md
        assert "Always enable HTTP keepalive connection pooling" in digest_md
        assert "Never enable HTTP keepalive connection pooling" in digest_md
        assert "Legacy unclassified noise" not in digest_md

        # Live & Recent Agent Work checks
        assert "## Live & Recent Agent Work" in digest_md
        assert "**Session (codex):** live now in **Alpha**" in digest_md
        assert "stale-worker" not in digest_md
        assert "leaked-test-agent" not in digest_md
        assert "**Plan:** Harden Alpha Storage Gate (*codex*, **Alpha**, in progress)" in digest_md
        assert "**Handoff waiting** for *codex* (from *claude-code*): Verify storage retry timeout" in digest_md

        # Today's Learnings & Scars grouped under Alpha with Scars BEFORE Rules
        assert "## Today's Learnings & Scars (Last 24h)" in digest_md
        assert "### Alpha" in digest_md
        scars_pos = digest_md.index("- **Scars & Gotchas**")
        rules_pos = digest_md.index("- **Rules & Decisions**")
        assert scars_pos < rules_pos, "Scars & Gotchas must render before Rules & Decisions"
        assert "SQLite reader transactions held across network retries" in digest_md

        # Project Map Freshness initially up to date
        assert "## Project Map Freshness" in digest_md
        assert f"- **Alpha** (`{sha1_short}`) — up to date (2 subsystems)" in digest_md

        # 3. `minni status` plain-spoken top overview
        status_res = _run_cli("status")
        assert "Active plans: 1" in status_res.stdout
        assert "Mapped projects: 1" in status_res.stdout
        assert "Needs your call: 1 unresolved conflict" in status_res.stdout
        assert f"Daily digest: {today_file}" in status_res.stdout
        assert "database: ok" in status_res.stdout

        # 4. Advance git commit in `alpha` touching `src/alpha/auth/login.py`
        (repo / "src" / "alpha" / "auth" / "login.py").write_text(
            '"""Authentication subsystem."""\n\ndef verify_session(token: str) -> bool:\n    return len(token) >= 8\n',
            encoding="utf-8",
        )
        _git(repo, "add", "src/alpha/auth/login.py")
        _git(repo, "commit", "-m", "enforce minimum session token length")

        # Verify `--no-refresh` leaves TODAY.md untouched
        before_no_refresh = today_file.read_text(encoding="utf-8")
        no_ref_res = _run_cli("activity", "--no-refresh")
        assert "Digest updated:" not in no_ref_res.stdout
        assert today_file.read_text(encoding="utf-8") == before_no_refresh

        # Verify `minni activity --json` keeps stdout strictly valid JSON while refreshing TODAY.md
        json_act_res = _run_cli("activity", "--json")
        parsed_json = json.loads(json_act_res.stdout)
        assert parsed_json["daemon"]["reachable"] is True
        assert "Digest updated:" not in json_act_res.stdout

        # Verify human `minni activity` prints `Digest updated:` and TODAY.md reflects `changed since last map`
        act_res = _run_cli("activity")
        assert f"Digest updated: {today_file}" in act_res.stdout
        refreshed_md = today_file.read_text(encoding="utf-8")
        assert (
            f"- **Alpha** (`{sha1_short}`) — 1 subsystem changed since last map (`auth`); "
            "refreshes automatically on recall or via `minni map`"
        ) in refreshed_md
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3.0)
        log_fh.close()
        shutil.rmtree(short_dir, ignore_errors=True)


def test_today_digest_symlink_flag_injection_wrapper_and_hex_vault_hygiene(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Prove symlink refusal, git flag-injection rejection (`--output=...`, `-dash`),
    300ms git timeout, blocked/wrapper/escape sanitization, and `<32-hex>-vault` filtering."""
    # 1. Refuses symlinked minni_home
    real_dir = tmp_path / "real-home"
    real_dir.mkdir()
    symlink_home = tmp_path / "sym-home"
    symlink_home.symlink_to(real_dir)
    with pytest.raises(ValueError, match="minni_home is a symlink"):
        today_digest.write_today_digest(symlink_home)

    # 2. Refuses symlinked TODAY.md without touching target victim file
    victim = tmp_path / "victim.txt"
    victim.write_text("DO_NOT_OVERWRITE\n", encoding="utf-8")
    (real_dir / "TODAY.md").symlink_to(victim)
    with pytest.raises(ValueError, match="target is a symlink"):
        today_digest.write_today_digest(real_dir)
    assert victim.read_text(encoding="utf-8") == "DO_NOT_OVERWRITE\n"
    assert today_digest.refresh_today_digest_safe(real_dir) is None
    (real_dir / "TODAY.md").unlink()

    # 3. Git flag-injection (`--output=...`), `-dash` repo_root, and 300ms timeout enforcement
    repo = tmp_path / "my-app"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "app.py").write_text("def run() -> int:\n    return 1\n", encoding="utf-8")
    _git(repo, "init")
    _git(repo, "add", "src/app.py")
    _git(repo, "commit", "-m", "c1")
    (repo / "src" / "app.py").write_text("def run() -> int:\n    return 2\n", encoding="utf-8")
    _git(repo, "add", "src/app.py")
    _git(repo, "commit", "-m", "c2")

    canary = tmp_path / "git_flag_canary.txt"
    maps_evil = real_dir / "vault" / "maps" / "evil"
    maps_evil.mkdir(parents=True)
    (maps_evil / "_index.json").write_text(
        json.dumps(
            {
                "workspace_id": "workspace-evil",
                "repo_root": str(repo),
                "git_sha": f"--output={canary}",
                "subsystems": [{"slug": "src", "owned_files": ["src/app.py"]}],
            }
        ),
        encoding="utf-8",
    )
    maps_dash = real_dir / "vault" / "maps" / "dash"
    maps_dash.mkdir(parents=True)
    (maps_dash / "_index.json").write_text(
        json.dumps(
            {
                "workspace_id": "workspace-dash",
                "repo_root": "-dash-repo",
                "git_sha": "abcdef1234567",
                "subsystems": [{"slug": "src", "owned_files": ["src/app.py"]}],
            }
        ),
        encoding="utf-8",
    )

    # Valid mapped project to verify 300ms timeout is passed on legitimate git checks
    sha_old = _git(repo, "rev-parse", "HEAD~1")
    maps_good = real_dir / "vault" / "maps" / "my-app"
    maps_good.mkdir(parents=True)
    (maps_good / "_index.json").write_text(
        json.dumps(
            {
                "workspace_id": "workspace-my-app",
                "repo_root": str(repo),
                "git_sha": sha_old,
                "subsystems": [{"slug": "src", "owned_files": ["src/app.py"]}],
            }
        ),
        encoding="utf-8",
    )

    seen_timeouts: list[float] = []
    orig_sub_run = subprocess.run

    def _spy_run(cmd, *a, **kw):
        if isinstance(cmd, list) and cmd and cmd[0] == "git":
            seen_timeouts.append(float(kw.get("timeout", -1.0)))
            assert not any(str(arg).startswith("--output=") for arg in cmd), f"Flag injection reached git: {cmd}"
            assert "-dash-repo" not in cmd, f"Dash repo_root reached git: {cmd}"
        return orig_sub_run(cmd, *a, **kw)

    monkeypatch.setattr(subprocess, "run", _spy_run)

    # 4. BUG-46: `_active_plan.json` / `active-thread.d` `notePath` outside `<minni_home>`
    # (`../outside_secret.md`, absolute `/tmp/.../outside_secret.md`, and intermediate symlink dir)
    # must be refused cleanly without reading the external file.
    outside_md = tmp_path / "outside_secret.md"
    outside_md.write_text(
        "---\ntitle: OUTSIDE_FILE_HEADING_LEAKED\n---\n# OUTSIDE_FILE_HEADING_LEAKED\n"
        '```json\n{"scars": [{"summary": "OUTSIDE_SCAR_LEAKED"}]}\n```\n',
        encoding="utf-8",
    )
    escape_vault = real_dir / "codex-vault"
    escape_art = escape_vault / "wiki" / "artifacts"
    escape_art.mkdir(parents=True)
    (escape_art / "_active_plan.json").write_text(
        json.dumps(
            {
                "plan_id": "rel-escape-thread",
                "agent_id": "codex",
                "workspace_id": "workspace-my-app",
                "notePath": "../../../outside_secret.md",
            }
        ),
        encoding="utf-8",
    )
    escape_td = escape_vault / ".runtime" / "active-thread.d"
    escape_td.mkdir(parents=True)
    (escape_td / "abs-escape.json").write_text(
        json.dumps(
            {
                "plan_id": "abs-escape-thread",
                "agent_id": "codex",
                "workspace_id": "workspace-my-app",
                "notePath": str(outside_md),
            }
        ),
        encoding="utf-8",
    )
    sym_parent = escape_vault / "wiki" / "sym-dir"
    sym_parent.symlink_to(tmp_path, target_is_directory=True)
    (escape_td / "sym-escape.json").write_text(
        json.dumps(
            {
                "plan_id": "sym-escape-thread",
                "agent_id": "codex",
                "workspace_id": "workspace-my-app",
                "notePath": "wiki/sym-dir/outside_secret.md",
            }
        ),
        encoding="utf-8",
    )

    # 5. BUG-45: Seed a REAL `MinniDB(MinniConfig(...))` database (where `learnings` has NO
    # `privacy_level` column and privacy/lifecycle live on `documents` + `learning_documents` /
    # `memory_uri`) plus `contradicts_id`, `superseded_by`, frontmatter `privacy_level: blocked`
    # duplicate keys, wrapper noise, ANSI escapes, and path redaction.
    from minni.config import MinniConfig
    from minni.db import MinniDB

    db_file = real_dir / "minni.db"
    cfg = MinniConfig(
        db_path=db_file,
        vault_path=real_dir / "vault",
        faiss_index_path=real_dir / "minni_faiss.index",
        writeback_path=real_dir / "learnings",
        graph_export_dir=real_dir / "graphs",
    )
    real_db = MinniDB(cfg)
    try:
        now_ts = time.time()
        with real_db.transaction() as c:
            learn_col_names = {
                str(r[1]) for r in c.execute("PRAGMA table_info(learnings)").fetchall()
            }
            assert "privacy_level" not in learn_col_names, (
                "Real MinniDB.learnings has no privacy_level column; test must exercise real schema"
            )

            # Clean active rule + safe canonical document
            cur_clean = c.execute(
                "INSERT INTO learnings (agent_id, category, content, created_at, status, workspace_id) "
                "VALUES ('codex', 'decision', ?, ?, 'active', 'workspace-my-app')",
                ("Keep connection pool idle timeout at 30 seconds.", now_ts),
            )
            lid_clean = int(cur_clean.lastrowid)
            cur_doc_clean = c.execute(
                "INSERT INTO documents (path, agent, page_status, privacy_level, memory_kind, memory_uri, workspace_id) "
                "VALUES (?, 'codex', 'accepted', 'safe', 'learning', ?, 'workspace-my-app')",
                (str(real_dir / "vault" / "clean.md"), f"learning://{lid_clean}"),
            )
            doc_clean = int(cur_doc_clean.lastrowid)
            c.execute(
                "INSERT INTO learning_documents (learning_id, doc_id, created_at) VALUES (?, ?, ?)",
                (lid_clean, doc_clean, now_ts),
            )

            # Quarantined `documents.privacy_level = 'blocked'` learning + conflict edges in both directions
            cur_blocked = c.execute(
                "INSERT INTO learnings (agent_id, category, content, created_at, status, workspace_id, contradicts_id) "
                "VALUES ('codex', 'decision', ?, ?, 'active', 'workspace-my-app', ?)",
                ("Top secret blocked row 998877 must never render", now_ts, lid_clean),
            )
            lid_blocked = int(cur_blocked.lastrowid)
            cur_doc_blocked = c.execute(
                "INSERT INTO documents (path, agent, page_status, privacy_level, memory_kind, memory_uri, workspace_id) "
                "VALUES (?, 'codex', 'accepted', 'blocked', 'learning', ?, 'workspace-my-app')",
                (str(real_dir / "vault" / "blocked.md"), f"learning://{lid_blocked}"),
            )
            doc_blocked = int(cur_doc_blocked.lastrowid)
            c.execute(
                "INSERT INTO learning_documents (learning_id, doc_id, created_at) VALUES (?, ?, ?)",
                (lid_blocked, doc_blocked, now_ts),
            )
            # Reverse `contradicts_id` edge pointing TO the blocked learning + memory_links + contradiction_log
            c.execute(
                "INSERT INTO learnings (agent_id, category, content, created_at, status, workspace_id, contradicts_id) "
                "VALUES ('codex', 'decision', ?, ?, 'active', 'workspace-my-app', ?)",
                ("Clean rule pointing contradicts_id at blocked learning", now_ts, lid_blocked),
            )
            c.execute(
                "INSERT INTO memory_links (source_doc_id, target_doc_id, link_type, created_at, edge_status) "
                "VALUES (?, ?, 'contradicts', ?, 'active')",
                (doc_clean, doc_blocked, now_ts),
            )
            c.execute(
                "INSERT INTO contradiction_log (memory_a_id, memory_b_id, detected_at, detection_method, resolution_status) "
                "VALUES (?, ?, ?, 'nli', 'unresolved')",
                (lid_clean, lid_blocked, now_ts),
            )

            # Closed `documents.page_status = 'draft'` learning linked via memory_uri only
            cur_draft = c.execute(
                "INSERT INTO learnings (agent_id, category, content, created_at, status, workspace_id) "
                "VALUES ('codex', 'decision', ?, ?, 'active', 'workspace-my-app')",
                ("Closed draft document learning 665544 must never render", now_ts),
            )
            lid_draft = int(cur_draft.lastrowid)
            c.execute(
                "INSERT INTO documents (path, agent, page_status, privacy_level, memory_kind, memory_uri, workspace_id) "
                "VALUES (?, 'codex', 'draft', 'safe', 'learning', ?, 'workspace-my-app')",
                (str(real_dir / "vault" / "draft.md"), f"learning://{lid_draft}"),
            )

            # Superseded learning (`superseded_by = lid_clean`)
            c.execute(
                "INSERT INTO learnings (agent_id, category, content, created_at, status, superseded_by, workspace_id) "
                "VALUES ('codex', 'decision', ?, ?, 'active', ?, 'workspace-my-app')",
                ("Superseded retry rule 112233 must never render", now_ts, lid_clean),
            )

            # Raw YAML frontmatter declaring `privacy_level: blocked` with duplicate-key override attempt
            c.execute(
                "INSERT INTO learnings (agent_id, category, content, created_at, status, workspace_id) "
                "VALUES ('codex', 'decision', ?, ?, 'active', 'workspace-my-app')",
                (
                    "---\nprivacy_level: blocked\nprivacy_level: safe\n---\n"
                    "Frontmatter duplicate-key blocked item 445566 must never render",
                    now_ts,
                ),
            )

            # Wrapper noise learning
            c.execute(
                "INSERT INTO learnings (agent_id, category, content, created_at, status, workspace_id) "
                "VALUES ('codex', 'decision', ?, ?, 'active', 'workspace-my-app')",
                ("<SYSTEM_MESSAGE>\n[Message] timestamp=2026-11-11 wrapper noise", now_ts),
            )

            # Clean scar with <EVIDENCE>, benign frontmatter, home path, and ANSI escape
            c.execute(
                "INSERT INTO learnings (agent_id, category, content, created_at, status, workspace_id) "
                "VALUES ('codex', 'scar', ?, ?, 'active', 'workspace-my-app')",
                (
                    "<EVIDENCE source=\"note.md\">---\ntitle: Hidden\n---\n# Clean Scar\n"
                    "Validate /Users/alice/projects/my-app config before \x1b[31mreload\x1b[0m.</EVIDENCE>",
                    now_ts,
                ),
            )
    finally:
        real_db.close()

    today_digest._MAP_FRESHNESS_CACHE.clear()
    out_path, digest_data = today_digest.write_today_digest(real_dir, daemon_running=False)
    rendered = out_path.read_text(encoding="utf-8")

    assert not canary.exists()
    assert seen_timeouts and all(abs(t - 0.3) < 1e-6 for t in seen_timeouts), f"Expected 0.3s timeouts: {seen_timeouts}"
    assert "OUTSIDE_FILE_HEADING_LEAKED" not in rendered
    assert "OUTSIDE_SCAR_LEAKED" not in rendered
    assert "Top secret blocked row 998877" not in rendered
    assert "Closed draft document learning 665544" not in rendered
    assert "Superseded retry rule 112233" not in rendered
    assert "Frontmatter duplicate-key blocked item 445566" not in rendered
    assert digest_data["conflicts"] == [], f"Blocked conflict leaked: {digest_data['conflicts']}"
    assert "Nothing waiting on you." in rendered
    assert "wrapper noise" not in rendered
    assert "<EVIDENCE" not in rendered
    assert "\x1b[" not in rendered
    assert "/Users/alice" not in rendered
    assert "Keep connection pool idle timeout at 30 seconds." in rendered
    assert "Validate ~/projects/my-app config before reload." in rendered
    assert "System: offline — showing saved state" in rendered

    # Reader parity on minni_cli._discover_active_threads & _read_sqlite_activity
    cli_threads, cli_scars = minni_cli._discover_active_threads(real_dir, cutoff_epoch=now_ts - 3600.0)
    assert "OUTSIDE_FILE_HEADING_LEAKED" not in json.dumps(cli_threads)
    assert "OUTSIDE_SCAR_LEAKED" not in json.dumps(cli_scars)
    cli_snap = minni_cli._read_sqlite_activity(db_file, cutoff_epoch=now_ts - 3600.0)
    cli_blob = json.dumps(cli_snap)
    assert "Top secret blocked row 998877" not in cli_blob
    assert "Closed draft document learning 665544" not in cli_blob
    assert "Superseded retry rule 112233" not in cli_blob
    assert "Frontmatter duplicate-key blocked item 445566" not in cli_blob

    # 6. BUG-48: Rapid `refresh_today_digest_safe` calls within 5s hit `_MAP_FRESHNESS_CACHE`
    # without spawning extra `git` subprocesses.
    git_calls_after_write = len(seen_timeouts)
    today_digest.refresh_today_digest_safe(real_dir, daemon_running=True)
    today_digest.refresh_today_digest_safe(real_dir, daemon_running=True)
    assert len(seen_timeouts) == git_calls_after_write, (
        "refresh_today_digest_safe burst within 5s must reuse _MAP_FRESHNESS_CACHE without spawning git"
    )

    # 7. BUG-46 Part 2: `refresh_today_digest_safe(minni_home=None, db_path=sandbox_dir / "minni.db")`
    # with `MINNI_HOME` unset derives `sandbox_dir` from `db_path` and NEVER touches `HOME / ".minni" / "TODAY.md"`.
    fake_op_home = tmp_path / "operator-home"
    (fake_op_home / ".minni").mkdir(parents=True)
    op_today = fake_op_home / ".minni" / "TODAY.md"
    op_today.write_text("OPERATOR_REAL_DIGEST_UNTOUCHED\n", encoding="utf-8")
    sandbox_dir = tmp_path / "sandbox-db-only"
    sandbox_dir.mkdir(parents=True)
    shutil.copy2(db_file, sandbox_dir / "minni.db")

    monkeypatch.setenv("HOME", str(fake_op_home))
    monkeypatch.delenv("MINNI_HOME", raising=False)
    derived_out = today_digest.refresh_today_digest_safe(
        minni_home=None,
        db_path=sandbox_dir / "minni.db",
        daemon_running=False,
    )
    assert derived_out == sandbox_dir / "TODAY.md"
    assert (sandbox_dir / "TODAY.md").is_file()
    assert op_today.read_text(encoding="utf-8") == "OPERATOR_REAL_DIGEST_UNTOUCHED\n"


def test_bug06_non_clobbering_bundle_server_and_staging(tmp_path: Path) -> None:
    """BUG-06 proof: `bundle_server.mjs --outfile`, `stage_payload.py`, and
    `build_from_repo` produce bundled staged `dist/server.js` while leaving
    `plugins/minni/dist/server.js` modular (`tsc` output) so
    `vault-search-process-path.test.mjs` passes immediately without re-running `tsc`."""
    from minni.wire.from_repo import build_from_repo

    # Ensure starting state is clean modular `tsc` output
    subprocess.run(
        ["npm", "--prefix", str(PLUGIN_DIR), "run", "build"],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    repo_server_js = PLUGIN_DIR / "dist" / "server.js"
    modular_bytes = repo_server_js.read_bytes()
    modular_sha = hashlib.sha256(modular_bytes).hexdigest()
    assert re.search(rb"(?m)^\s*import\b[^;\n]*@modelcontextprotocol/sdk", modular_bytes), (
        "Expected `npm run build` to produce modular ESM dist/server.js"
    )

    # 1. Direct `bundle_server.mjs --outfile <tmp>/server.js`
    direct_out = tmp_path / "direct-bundled-server.js"
    subprocess.run(
        [
            "node",
            str(PLUGIN_DIR / "scripts" / "bundle_server.mjs"),
            "--outfile",
            str(direct_out),
        ],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    direct_bytes = direct_out.read_bytes()
    assert b"// dist/server.js\n" in direct_bytes
    assert not re.search(rb"(?m)^\s*import\b[^;\n]*@modelcontextprotocol/sdk", direct_bytes)
    assert hashlib.sha256(repo_server_js.read_bytes()).hexdigest() == modular_sha, (
        "bundle_server.mjs --outfile clobbered plugins/minni/dist/server.js!"
    )

    # 2. `scripts/stage_payload.py` (`copy_payload_tree` + `check_glob_coverage`)
    stage_mod = _load_stage_payload_module()
    try:
        version = stage_mod.read_pyproject_version()
        file_hashes = stage_mod.copy_payload_tree(version)
        stage_mod.check_glob_coverage(file_hashes)

        staged_server = PAYLOAD_ROOT / "dist" / "server.js"
        staged_bytes = staged_server.read_bytes()
        assert b"// dist/server.js\n" in staged_bytes
        assert not re.search(rb"(?m)^\s*import\b[^;\n]*@modelcontextprotocol/sdk", staged_bytes)
        assert file_hashes["dist/server.js"] == f"sha256:{hashlib.sha256(staged_bytes).hexdigest()}"
        assert hashlib.sha256(repo_server_js.read_bytes()).hexdigest() == modular_sha, (
            "stage_payload.py clobbered plugins/minni/dist/server.js!"
        )
    finally:
        if PAYLOAD_ROOT.exists():
            shutil.rmtree(PAYLOAD_ROOT)

    # 3. `build_from_repo(REPO_ROOT)`
    from_repo_tmp, manifest = build_from_repo(REPO_ROOT)
    try:
        fr_bytes = (from_repo_tmp / "dist" / "server.js").read_bytes()
        assert b"// dist/server.js\n" in fr_bytes
        assert not re.search(rb"(?m)^\s*import\b[^;\n]*@modelcontextprotocol/sdk", fr_bytes)
        assert manifest.files["dist/server.js"] == f"sha256:{hashlib.sha256(fr_bytes).hexdigest()}"
        assert hashlib.sha256(repo_server_js.read_bytes()).hexdigest() == modular_sha, (
            "build_from_repo clobbered plugins/minni/dist/server.js!"
        )
    finally:
        shutil.rmtree(from_repo_tmp, ignore_errors=True)

    # 4. `propagate.py` no-`current` fallback (`BUG-47`): bundles into `<install_root>/dist/server.js`
    # without clobbering `plugins/minni/dist/server.js` in-place.
    import argparse

    prop_spec = importlib.util.spec_from_file_location(
        "_wave5_propagate",
        PLUGIN_DIR / "skills" / "minni-install" / "scripts" / "propagate.py",
    )
    assert prop_spec and prop_spec.loader
    prop_mod = importlib.util.module_from_spec(prop_spec)
    prop_spec.loader.exec_module(prop_mod)
    prop_mod.bootstrap_vault = lambda _args: 0  # type: ignore[attr-defined]
    prop_mod.vault_for = lambda _agent: tmp_path / "prop-vault"  # type: ignore[attr-defined]
    prop_mod.native_afm_env = lambda _repo: {}  # type: ignore[attr-defined]

    prop_install = tmp_path / "prop-installed"
    prop_args = argparse.Namespace(
        repo=str(REPO_ROOT),
        workspace=str(REPO_ROOT),
        no_build=True,
        install_root=str(prop_install),
        agent="audit-test",
        socket=str(tmp_path / "prop.sock"),
    )
    prop_mod.update_one_plugin("generic", prop_args)
    prop_bytes = (prop_install / "dist" / "server.js").read_bytes()
    assert b"// dist/server.js\n" in prop_bytes
    assert not re.search(rb"(?m)^\s*import\b[^;\n]*@modelcontextprotocol/sdk", prop_bytes)
    assert hashlib.sha256(repo_server_js.read_bytes()).hexdigest() == modular_sha, (
        "propagate.py no-current fallback clobbered plugins/minni/dist/server.js!"
    )

    # 5. Immediately run `vault-search-process-path.test.mjs` WITHOUT re-running `tsc`
    node_test = subprocess.run(
        [
            "node",
            "--test",
            str(PLUGIN_DIR / "tests" / "vault-search-process-path.test.mjs"),
        ],
        cwd=str(PLUGIN_DIR),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert node_test.returncode == 0, (
        f"vault-search-process-path.test.mjs failed after staging:\n"
        f"STDOUT:\n{node_test.stdout}\nSTDERR:\n{node_test.stderr}"
    )
