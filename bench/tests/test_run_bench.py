"""Orchestration + report + reproducibility tests (slice s7).

Covers:
- orchestration runs the fixture end-to-end and emits report + results JSON +
  manifest (every required metric section + manifest fields present);
- PER-ADAPTER error isolation: a crashing adapter -> the run COMPLETES, the
  crasher is marked FAILED (redacted error), teardown() is still called, and the
  report renders for the survivors;
- matplotlib-absent path renders tables, never crashes;
- Layer-1 repro: byte-identical scorecard JSON across two full runs;
- no network anywhere (offline stubs + HF offline; nothing reaches a socket).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from membench import config, report
from membench.adapters.stub import StubAdapter
from membench.contract import IngestReport, QueryResult
from membench import run_bench

_PKG = Path(run_bench.__file__).resolve().parent
_CORPUS = _PKG / "fixtures" / "corpus_synthetic"
_GOLD = _PKG / "fixtures" / "gold_synthetic.jsonl"
_EPISODES = _PKG / "fixtures" / "episodes" / "synthetic_episodes.jsonl"


def _orchestrate(adapters=None, n_trials=2):
    """Run orchestration on the fixture with a (small) adapter list for speed."""
    return run_bench.orchestrate(
        corpus_dir=_CORPUS,
        gold_path=_GOLD,
        episodes_path=_EPISODES,
        n_trials=n_trials,
        adapters=adapters,
        is_fixture_run=True,
    )


# ---------------------------------------------------------------------------
# Crashing adapter (per-adapter error isolation fixture)
# ---------------------------------------------------------------------------
class _CrashingAdapter:
    """A contract-shaped adapter that raises on query() and records teardown.

    ``ingest`` succeeds (so the failure is isolated at the QUERY phase, the s5
    abort the spec calls out); ``query`` raises; ``teardown`` flips a flag the
    test asserts WAS called even though the adapter failed.
    """

    name = "crasher"

    def __init__(self) -> None:
        self.config_hash = "crasher-test"
        self.torn_down = False
        self._n = 0

    def ingest(self, corpus) -> IngestReport:
        self._n = len(corpus.doc_ids())
        return IngestReport(build_wall_clock_ms=0.0, doc_count=self._n)

    def query(self, q: str, budget) -> QueryResult:
        raise RuntimeError("induced crash at /Users/secret/path/leak in query()")

    def teardown(self) -> None:
        self.torn_down = True


class _IngestCrashingAdapter:
    """Raises in ingest() — the failure must be recorded with phase='ingest'."""

    name = "ingest_crasher"

    def __init__(self) -> None:
        self.config_hash = "ingest-crasher-test"
        self.torn_down = False

    def ingest(self, corpus) -> IngestReport:
        raise RuntimeError("induced crash at /home/runner/work/leak in ingest()")

    def query(self, q: str, budget) -> QueryResult:  # pragma: no cover
        raise AssertionError("query() must not be reached after ingest() crash")

    def teardown(self) -> None:
        self.torn_down = True


class _Layer2CrashingAdapter:
    """Succeeds ingest+query (Layer 1 fully scored) but raises during Layer 2.

    The Layer-2 crash must be isolated as a PARTIAL failure: the valid Layer-1
    records are PRESERVED (adapter stays a survivor, scored in the scorecards)
    and the crash is surfaced under phase='layer2' — NOT marked fully failed.
    """

    name = "l2_crasher"

    def __init__(self) -> None:
        self.config_hash = "l2-crasher-test"
        self.torn_down = False

    def ingest(self, corpus) -> IngestReport:
        self._n = len(corpus.doc_ids())
        return IngestReport(build_wall_clock_ms=0.0, doc_count=self._n)

    def query(self, q: str, budget) -> QueryResult:
        return QueryResult(
            ranked_results=[], context_string="", wall_clock_ms=0.0, refused=True
        )

    def teardown(self) -> None:
        self.torn_down = True


class _TeardownCrashingAdapter:
    """Succeeds ingest+query, raises in teardown() — phase must be 'teardown'."""

    name = "teardown_crasher"

    def __init__(self) -> None:
        self.config_hash = "teardown-crasher-test"
        self.torn_down = False

    def ingest(self, corpus) -> IngestReport:
        self._n = len(corpus.doc_ids())
        return IngestReport(build_wall_clock_ms=0.0, doc_count=self._n)

    def query(self, q: str, budget) -> QueryResult:
        return QueryResult(
            ranked_results=[], context_string="", wall_clock_ms=0.0, refused=True
        )

    def teardown(self) -> None:
        self.torn_down = True
        raise RuntimeError("induced teardown crash at /var/folders/xy/leak")


# ---------------------------------------------------------------------------
# 1. End-to-end orchestration emits the artifacts + every required section
# ---------------------------------------------------------------------------
def test_orchestration_runs_fixture_end_to_end(tmp_path):
    results = _orchestrate(adapters=[StubAdapter()])
    paths = run_bench.write_artifacts(results, tmp_path)

    for key in ("results_json", "manifest_json", "layer1_scorecard_json",
                "report_md"):
        assert paths[key].exists(), f"{key} not written"

    # results JSON parses and carries every top-level block.
    parsed = json.loads(paths["results_json"].read_text())
    for block in ("manifest", "scorecards", "layer2", "efficiency",
                  "ingest_cost", "failures", "partial_failures"):
        assert block in parsed, f"results JSON missing {block!r}"


def test_report_contains_every_required_section_and_manifest_fields(tmp_path):
    results = _orchestrate(adapters=[StubAdapter()])
    md = report.render_report(results)

    # Every required metric section header is present.
    for header in (
        "## Run manifest",
        "## Layer 1 — retrieval scorecards",
        "### Per-band breakdown",
        "## Layer 2 — agent-in-the-loop",
        "## Significance — pairwise task success",
        "## Token-efficiency composite (§6.7)",
        "## Ingest cost (§6.8",
        "## Failed adapters",
        "## Partial failures",
        "## Threats to validity & honesty caveats",
    ):
        assert header in md, f"report missing section {header!r}"

    # Every required Layer-1 metric label appears in the table.
    for label in ("recall@k", "prec@k", "ndcg@k", "mrr", "corr_ref",
                  "false_ref", "tok_cost", "p50ms", "p95ms"):
        assert label in md, f"report missing Layer-1 metric {label!r}"

    # Manifest pins everything needed to reproduce.
    for field in (
        "content_hash", "corpus.scrubbed", "embedder.model_id",
        "tokenizer.id", "tokenizer.tiktoken_version", "retrieval.k",
        "retrieval.budget_max_tokens", "retrieval.n_trials",
        "models.agent.model_id", "models.agent.model_family",
        "models.judge.model_id", "models.judge.model_family",
        "seeds.ingest_seed", "seeds.run_seed", "run_episode_hash",
        "runtime.python",
    ):
        assert field in md, f"manifest missing pinned field {field!r}"

    # The Layer-2 table is non-empty: the 'stub' adapter row + the task_success
    # column header actually render (a bare section header would pass the header
    # check above even with an empty table).
    assert "task_success" in md, "Layer-2 table header row missing"
    assert "stub" in md, "Layer-2 adapter row for 'stub' missing"

    # Honest labeling: fixture/stub run marked NOT the headline result, and the
    # Layer-1-byte / Layer-2-CI repro caveat is carried into the report.
    assert "not the headline result" in md.lower()
    assert "byte-reproducible" in md.lower()
    assert "ci-only" in md.lower()


# ---------------------------------------------------------------------------
# 2. Per-adapter error isolation (load-bearing)
# ---------------------------------------------------------------------------
def test_crashing_adapter_isolated_run_continues_for_survivors(tmp_path):
    crasher = _CrashingAdapter()
    survivor = StubAdapter()
    results = _orchestrate(adapters=[crasher, survivor])

    # The crasher is marked FAILED with a phase + redacted error...
    assert "crasher" in results["failures"], "crasher not marked failed"
    info = results["failures"]["crasher"]
    assert info["phase"] == "query"
    assert "RuntimeError" in info["error"]
    # ...teardown() was STILL called for the failed adapter...
    assert crasher.torn_down, "teardown() not called for the failed adapter"
    # ...AND teardown() was called for the SURVIVOR too (spec: ALWAYS called)...
    assert survivor._torn_down, "teardown() not called for the surviving adapter"
    # ...and the survivor was still scored (run did NOT abort).
    assert "stub" in results["scorecards"]["adapters"]
    assert "crasher" not in results["scorecards"]["adapters"]

    # The report still renders, marks the crasher failed, and keeps the survivor.
    md = report.render_report(results)
    assert "crasher" in md and "RuntimeError" in md
    assert "stub" in md


def test_failed_adapter_error_is_redacted(tmp_path):
    results = _orchestrate(adapters=[_CrashingAdapter(), StubAdapter()])
    err = results["failures"]["crasher"]["error"]
    # The raw exception embedded a private-looking path; the report must not leak
    # ANY local path verbatim (not just the current operator's home). The crash
    # message embeds /Users/secret/path/leak — a DIFFERENT user than Path.home()
    # — which must still be redacted to [REDACTED_PATH].
    assert str(Path.home()) not in err
    assert "/Users/secret/path/leak" not in err, "non-home /Users path leaked"
    assert "/Users/secret" not in err
    assert "[REDACTED_PATH]" in err
    # Bound: _redact() truncates msg at 160 chars + a short type prefix
    # ('RuntimeError: ' = 14). Keep the bound tight so a regression that widens
    # the cap is caught (160 + 14 = 174).
    assert len(err) <= 174


def test_ingest_phase_crash_isolated_and_labeled(tmp_path):
    """An adapter raising in ingest() -> phase='ingest', run continues."""
    crasher = _IngestCrashingAdapter()
    survivor = StubAdapter()
    results = _orchestrate(adapters=[crasher, survivor])

    assert "ingest_crasher" in results["failures"]
    info = results["failures"]["ingest_crasher"]
    assert info["phase"] == "ingest", f"expected ingest phase, got {info['phase']}"
    assert "RuntimeError" in info["error"]
    # The /home/runner path in the message must be redacted, not leaked.
    assert "/home/runner/work/leak" not in info["error"]
    assert "[REDACTED_PATH]" in info["error"]
    # teardown() still called for the failed adapter; survivor still scored.
    assert crasher.torn_down
    assert "stub" in results["scorecards"]["adapters"]
    assert "ingest_crasher" not in results["scorecards"]["adapters"]


def test_teardown_phase_crash_isolated_and_labeled(tmp_path):
    """An adapter raising ONLY in teardown() -> phase='teardown', run continues.

    ingest+query succeed, so the adapter is NOT a survivor (teardown failure
    marks it failed) but the run still completes for the other adapters.
    """
    crasher = _TeardownCrashingAdapter()
    survivor = StubAdapter()
    results = _orchestrate(adapters=[crasher, survivor])

    assert "teardown_crasher" in results["failures"]
    info = results["failures"]["teardown_crasher"]
    assert info["phase"] == "teardown", f"got {info['phase']}"
    assert "RuntimeError" in info["error"]
    assert "/var/folders/xy/leak" not in info["error"]
    assert "[REDACTED_PATH]" in info["error"]
    assert crasher.torn_down
    # Run continued for the survivor; the teardown-crasher is absent from scores.
    assert "stub" in results["scorecards"]["adapters"]
    assert "teardown_crasher" not in results["scorecards"]["adapters"]


def test_layer2_crash_after_layer1_preserves_layer1_and_labels_phase(
    monkeypatch, tmp_path
):
    """A Layer-2 crash AFTER Layer-1 succeeds is a PARTIAL failure.

    Regression guard for the bug where the single try/except wrapping both layers
    cleared valid Layer-1 records on a Layer-2 crash, dropping the adapter from
    the scorecards. The adapter must STAY a survivor (Layer-1 scored) and the
    Layer-2 crash must be surfaced under phase='layer2'.
    """
    crasher = _Layer2CrashingAdapter()
    survivor = StubAdapter()

    real_trial = run_bench.run_episode_trial

    def _maybe_crash(adapter, *a, **k):
        if adapter.name == "l2_crasher":
            raise RuntimeError("induced layer2 crash at /var/folders/zz/leak")
        return real_trial(adapter, *a, **k)

    monkeypatch.setattr(run_bench, "run_episode_trial", _maybe_crash)
    results = _orchestrate(adapters=[crasher, survivor])

    # The Layer-1 records survived: the crasher IS in the scorecards, NOT marked
    # a full failure.
    assert "l2_crasher" in results["scorecards"]["adapters"]
    assert "l2_crasher" not in results["failures"]
    # The Layer-2 crash is surfaced as a partial failure with phase='layer2'.
    assert "l2_crasher" in results["partial_failures"]
    info = results["partial_failures"]["l2_crasher"]
    assert info["phase"] == "layer2", f"expected layer2 phase, got {info['phase']}"
    assert "RuntimeError" in info["error"]
    assert "/var/folders/zz/leak" not in info["error"]
    assert "[REDACTED_PATH]" in info["error"]
    # teardown() still called; the survivor still scored.
    assert crasher.torn_down
    assert "stub" in results["scorecards"]["adapters"]
    # The crasher has no Layer-2 result (dropped), but the survivor does.
    assert "l2_crasher" not in results["layer2"].get("adapters", {})

    # The report renders the partial-failures section with the adapter name +
    # its redacted error (exercises render_partial_failures with a NON-EMPTY
    # dict — a bare header check would pass even with an empty table).
    md = report.render_report(results)
    assert "## Partial failures" in md
    assert "l2_crasher" in md
    assert "RuntimeError" in md
    assert "/var/folders/zz/leak" not in md, "partial-failure path leaked into report"


def test_layer2_partial_failure_survives_teardown_crash(monkeypatch, tmp_path):
    """A partial-failure adapter whose teardown ALSO crashes STAYS a survivor.

    Regression guard for the bug where a teardown crash on a partial-failure
    adapter (Layer-1 OK, Layer-2 crashed) hit the ``if not run.failed`` guard,
    got promoted to a FULL failure, and was dropped from the scorecards —
    discarding its valid Layer-1 records. The adapter must remain a survivor
    with its Layer-1 records intact, and BOTH the Layer-2 error and the teardown
    error must be recorded (redacted).
    """

    class _L2AndTeardownCrasher(_Layer2CrashingAdapter):
        name = "l2_td_crasher"

        def teardown(self) -> None:
            self.torn_down = True
            raise RuntimeError("teardown crash at /tmp/partial/lock after layer2")

    crasher = _L2AndTeardownCrasher()
    survivor = StubAdapter()

    real_trial = run_bench.run_episode_trial

    def _maybe_crash(adapter, *a, **k):
        if adapter.name == "l2_td_crasher":
            raise RuntimeError("induced layer2 crash at /var/folders/qq/leak")
        return real_trial(adapter, *a, **k)

    monkeypatch.setattr(run_bench, "run_episode_trial", _maybe_crash)
    results = _orchestrate(adapters=[crasher, survivor])

    # The adapter is STILL a survivor: its valid Layer-1 records are preserved
    # and it is NOT promoted to a full failure by the teardown crash.
    assert "l2_td_crasher" in results["scorecards"]["adapters"]
    assert "l2_td_crasher" not in results["failures"], \
        "teardown crash wrongly promoted a partial failure to a full failure"
    # Both the Layer-2 error AND the teardown error are recorded, redacted.
    info = results["partial_failures"]["l2_td_crasher"]
    assert info["phase"] == "layer2"
    assert "RuntimeError" in info["error"]
    assert "/var/folders/qq/leak" not in info["error"]
    assert "[REDACTED_PATH]" in info["error"]
    assert "teardown_error" in info, "teardown crash silently swallowed"
    assert "RuntimeError" in info["teardown_error"]
    assert "/tmp/partial/lock" not in info["teardown_error"]
    assert "[REDACTED_PATH]" in info["teardown_error"]
    assert crasher.torn_down
    assert "stub" in results["scorecards"]["adapters"]

    # The report surfaces both errors and leaks neither raw path.
    md = report.render_report(results)
    assert "l2_td_crasher" in md
    assert "+teardown" in md
    assert "/tmp/partial/lock" not in md
    assert "/var/folders/qq/leak" not in md


def test_redact_strips_system_paths():
    """_redact must scrub /etc, /proc, /dev (and friends) from error messages.

    An OSError/PermissionError message can embed a sensitive system path
    (/etc/shadow, /proc/1/environ, /dev/sda1); none may leak into results.json /
    the report verbatim.
    """
    for raw in ("/etc/shadow", "/proc/1/environ", "/dev/sda1",
                "/sys/kernel/x", "/run/secrets/token", "/mnt/data/secret"):
        red = run_bench._redact(PermissionError(f"denied opening {raw}"))
        assert raw not in red, f"{raw} leaked through _redact: {red!r}"
        assert "[REDACTED_PATH]" in red


def test_display_path_redacts_out_of_repo_path():
    """_display_path's out-of-repo fallback home-strips/redacts, never leaks.

    An out-of-repo corpus path (e.g. /tmp, /var/folders, an absolute home path)
    must come back home-stripped to ~ and run through the path-redaction pattern
    — never returned verbatim with a real platform path embedded.
    """
    from pathlib import Path

    # A /tmp corpus path (out of repo) must be redacted, not leaked verbatim.
    out = run_bench._display_path(Path("/tmp/membench_corpus/x"))
    assert "/tmp/membench_corpus/x" not in out
    assert "[REDACTED_PATH]" in out

    # A path under the operator home (out of repo) is home-stripped to ~.
    home_path = Path.home() / "out_of_repo_corpus" / "docs"
    out2 = run_bench._display_path(home_path)
    assert str(Path.home()) not in out2
    assert out2.startswith("~")


def test_md_table_escapes_pipe_in_cell_content():
    """A literal '|' in a cell must be escaped so it can't corrupt the table.

    An unescaped '|' inside cell content (e.g. a redacted error string) would be
    parsed as a column separator and shift every following cell.
    """
    table = report._md_table(
        ["adapter", "error"],
        [["x", "boom a|b|c happened"]],
    )
    # The raw unescaped pipe must not appear in cell content; it is escaped.
    assert "a\\|b\\|c" in table
    assert "a|b|c" not in table
    # Structure intact: the data row still has exactly the header's column count.
    data_row = table.splitlines()[-1]
    # 2 columns -> 3 unescaped delimiters (leading, middle, trailing).
    assert data_row.count("|") - data_row.count("\\|") == 3


def test_compound_failure_records_teardown_error_separately(tmp_path):
    """An adapter that fails in query() AND in teardown() records BOTH.

    The query failure sets phase='query'/error; the teardown crash must NOT be
    silently swallowed — it lands in a supplementary 'teardown_error' field.
    """

    class _DoubleCrasher(_CrashingAdapter):
        name = "double_crasher"

        def teardown(self) -> None:
            self.torn_down = True
            raise RuntimeError("teardown also failed at /tmp/lock/leak")

    crasher = _DoubleCrasher()
    results = _orchestrate(adapters=[crasher, StubAdapter()])

    info = results["failures"]["double_crasher"]
    # Primary failure is still the query phase (not overwritten by teardown).
    assert info["phase"] == "query"
    assert "RuntimeError" in info["error"]
    # The teardown crash is captured separately, redacted, not swallowed.
    assert "teardown_error" in info, "teardown crash silently swallowed"
    assert "RuntimeError" in info["teardown_error"]
    assert "/tmp/lock/leak" not in info["teardown_error"]
    assert "[REDACTED_PATH]" in info["teardown_error"]
    assert crasher.torn_down
    # And it surfaces in the rendered report.
    md = report.render_report(results)
    assert "+teardown" in md
    # Report-level redaction check: the raw /tmp path must NOT leak verbatim.
    assert "/tmp/lock/leak" not in md


# ---------------------------------------------------------------------------
# 3. matplotlib-absent path renders tables, never crashes
# ---------------------------------------------------------------------------
def test_report_renders_tables_when_matplotlib_absent(monkeypatch, tmp_path):
    # Force the matplotlib-absent branch regardless of the host venv.
    monkeypatch.setattr(report, "matplotlib_available", lambda: False)
    results = _orchestrate(adapters=[StubAdapter()])
    md = report.render_report(results, plots_dir=tmp_path / "plots")
    # Degrades to Markdown tables — no PNG embed, no crash, tables present.
    assert "ASCII/Markdown tables" in md
    assert "| adapter |" in md
    # The Layer-1 metric columns must still render as a TABLE (not a degraded
    # paragraph fallback with no metric labels) in the no-matplotlib path.
    assert "recall@k" in md
    assert "ndcg@k" in md
    assert "![" not in md  # no image embeds


# ---------------------------------------------------------------------------
# 4. Layer-1 reproducibility — byte-identical across two full runs
# ---------------------------------------------------------------------------
def test_layer1_scorecard_json_is_byte_identical_across_two_runs():
    a = _orchestrate(adapters=[StubAdapter()])
    b = _orchestrate(adapters=[StubAdapter()])
    assert run_bench.layer1_scorecard_json(a) == run_bench.layer1_scorecard_json(b)


def test_layer1_repro_holds_for_the_full_default_roster(tmp_path):
    """The DEFAULT roster (minni-as-stub fallback) is also byte-reproducible.

    Uses the real default adapter set (not a hand-picked pair) so the repro
    claim is tested on what `make bench` actually runs. Marked slow-ish but
    fully offline + deterministic.
    """
    a = run_bench.orchestrate(
        corpus_dir=_CORPUS, gold_path=_GOLD, episodes_path=_EPISODES,
        n_trials=1, is_fixture_run=True,
    )
    b = run_bench.orchestrate(
        corpus_dir=_CORPUS, gold_path=_GOLD, episodes_path=_EPISODES,
        n_trials=1, is_fixture_run=True,
    )
    assert run_bench.layer1_scorecard_json(a) == run_bench.layer1_scorecard_json(b)


# ---------------------------------------------------------------------------
# 5. No network: the orchestration import surface touches no socket
# ---------------------------------------------------------------------------
def test_no_network_during_orchestration(monkeypatch):
    """A hard guard: socket.socket.connect raises if the run touches the network.

    The offline stubs + cached embeddings must never open a socket. If any code
    path tries, this test fails loudly rather than silently hitting the network.
    """
    import socket

    def _no_connect(self, *a, **k):
        raise AssertionError("network access attempted during orchestration")

    monkeypatch.setattr(socket.socket, "connect", _no_connect)
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    results = _orchestrate(adapters=[StubAdapter()])
    assert "stub" in results["scorecards"]["adapters"]


# ---------------------------------------------------------------------------
# 6. Efficiency composite — zero-denominator flag (§6.7)
# ---------------------------------------------------------------------------
def test_efficiency_no_context_flag_for_refuse_everything_adapter():
    """An adapter returning ~0 context per turn is FLAGGED, not given a huge score."""
    from membench.efficiency import adapter_efficiency
    from membench.runner_layer2 import AdapterLayer2Result, TrialResult

    res = AdapterLayer2Result(adapter="empty", n_trials=1, n_episodes=2)
    for ep in ("e1", "e2"):
        res.trials.append(TrialResult(
            adapter="empty", episode_id=ep, trial=0, correct=0, success=0,
            tokens_to_model=10, ctx_tokens=0, wall_clock_ms=0.0, answer="",
        ))
    comp = adapter_efficiency(res)
    assert comp.no_context is True
    assert comp.efficiency == 0.0  # 0 success, not a huge number off a tiny denom


def test_efficiency_zero_trials_early_exit():
    """The n==0 early-return guard (efficiency.py) is exercised directly.

    A result with ZERO appended trials must take the explicit early-exit branch:
    n_turns==0, no_context True, efficiency 0.0 — never a div-by-zero or a huge
    score off an empty denominator.
    """
    from membench.efficiency import adapter_efficiency
    from membench.runner_layer2 import AdapterLayer2Result

    res = AdapterLayer2Result(adapter="empty", n_trials=0, n_episodes=0)
    comp = adapter_efficiency(res)
    assert comp.n_turns == 0
    assert comp.no_context is True
    assert comp.efficiency == 0.0


def test_efficiency_composite_formula_on_nonzero_case():
    """Pin the §6.7 formula value on a normal (non-degenerate) adapter.

    Formula: mean_success / (max(mean_ctx, 1) / 1000). With 1.0 success and
    500 ctx tokens -> 1.0 / (500/1000) = 2.0. A typo (e.g. * vs /, wrong floor)
    would change this value; the zero-denominator test alone can't catch it.
    """
    from membench.efficiency import adapter_efficiency
    from membench.runner_layer2 import AdapterLayer2Result, TrialResult

    res = AdapterLayer2Result(adapter="normal", n_trials=1, n_episodes=2)
    for ep in ("e1", "e2"):
        res.trials.append(TrialResult(
            adapter="normal", episode_id=ep, trial=0, correct=1, success=1,
            tokens_to_model=600, ctx_tokens=500, wall_clock_ms=0.0, answer="ok",
        ))
    comp = adapter_efficiency(res)
    assert comp.mean_task_success == 1.0
    assert comp.mean_ctx_tokens == 500.0
    assert comp.no_context is False
    assert comp.efficiency == pytest.approx(2.0)

    # A second known point: 0.5 success, 250 ctx -> 0.5 / (250/1000) = 2.0 too,
    # but with 1000 ctx -> 0.5 / 1.0 = 0.5 (guards the /1000 scaling direction).
    res2 = AdapterLayer2Result(adapter="half", n_trials=1, n_episodes=2)
    res2.trials.append(TrialResult(
        adapter="half", episode_id="e1", trial=0, correct=1, success=1,
        tokens_to_model=1100, ctx_tokens=1000, wall_clock_ms=0.0, answer="ok",
    ))
    res2.trials.append(TrialResult(
        adapter="half", episode_id="e2", trial=0, correct=0, success=0,
        tokens_to_model=1100, ctx_tokens=1000, wall_clock_ms=0.0, answer="",
    ))
    comp2 = adapter_efficiency(res2)
    assert comp2.mean_task_success == 0.5
    assert comp2.mean_ctx_tokens == 1000.0
    assert comp2.efficiency == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# 7. No operator/home path leaks into the manifest (committed-example safety)
# ---------------------------------------------------------------------------
def test_manifest_carries_no_absolute_home_path():
    """The manifest must embed REPO-RELATIVE paths, never an operator home path.

    An absolute ``/Users/<operator>/...`` path would leak into a committed
    example report AND break the cross-machine repro claim. The fixture paths
    under the repo root must be recorded relative to it.
    """
    results = _orchestrate(adapters=[StubAdapter()])
    blob = json.dumps(results["manifest"])
    assert str(Path.home()) not in blob, "home path leaked into the manifest"
    assert results["manifest"]["corpus"]["dir"].startswith("bench/")


def test_committed_example_report_is_clean_and_marked():
    """The committed fixture example carries the non-headline banner + no leak."""
    example = _PKG / "fixtures" / "example_report.md"
    if not example.exists():
        pytest.skip("example report not generated in this checkout")
    text = example.read_text()
    assert "NOT A HEADLINE" in text
    assert str(Path.home()) not in text
    assert "![" not in text  # tables-only; no PNG embed paths committed
