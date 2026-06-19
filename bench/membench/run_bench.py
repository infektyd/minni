"""membench orchestration entrypoint — ONE command, end-to-end, reproducible.

``python -m membench.run_bench`` (or ``make bench``) runs the WHOLE benchmark on
the public synthetic FIXTURE by default:

  (a) load the pinned FrozenCorpus (hash-gated — ``corpus.load_corpus``);
  (b) run Layer 1 over all adapters × the gold set -> scorecards;
  (c) run Layer 2 over the episodes (offline StubAgent/StubJudge by default; the
      real LLM is gated behind ``--real-llm`` + env keys + ``MAX_API_CALLS``);
  (d) compute significance (stats.py Wilcoxon + BH-FDR + 95% CI);
  (e) compute the §6.7 efficiency composite + §6.8 ingest cost;
  (f) write the report (Markdown) + a machine-readable results JSON + a pinned
      run-manifest + the byte-reproducible Layer-1 scorecard JSON.

PER-ADAPTER ERROR ISOLATION (load-bearing). Each adapter's run is wrapped in its
OWN try/except: a single adapter raising in ingest/query/teardown is recorded as
a FAILED adapter (with a REDACTED error) and the run CONTINUES scoring the other
adapters — one crash never aborts the whole benchmark. ``teardown()`` is ALWAYS
called, even for a failed adapter.

REPRODUCIBILITY (§T-e). Layer-1 on the fixture is BYTE-reproducible: the
scorecard JSON is byte-identical across two runs (after the determinism strip).
Layer-2 is CI-only (non-deterministic LLM). The run-manifest pins everything
needed to reproduce.

Output artifacts go to a gitignored ``results/`` dir by default (real runs may
carry private-derived numbers). Fully offline by default — no network in tests.
"""

from __future__ import annotations

import argparse
import json
import platform
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import config
from .agent import StubAgent
from .contract import MemoryAdapter, TokenBudget
from .corpus import compute_content_hash, load_corpus
from .efficiency import efficiency_block
from .goldset import load_jsonl
from .judge import StubJudge
from .report import render_report
from .runner_layer1 import (
    GoldScoredRecord,
    assert_ingest_accounting,
    build_scorecards,
    canonical_json as layer1_canonical_json,
    score_gold_query,
)
from .runner_layer2 import (
    AdapterLayer2Result,
    results_to_dict as layer2_results_to_dict,
    run_episode_trial,
)
from .episodes import load_episodes
from .paths import REPO_ROOT
from . import metrics

_PKG_DIR = Path(__file__).resolve().parent
_DEFAULT_CORPUS = _PKG_DIR / "fixtures" / "corpus_synthetic"
_DEFAULT_GOLD = _PKG_DIR / "fixtures" / "gold_synthetic.jsonl"
_DEFAULT_EPISODES = _PKG_DIR / "fixtures" / "episodes" / "synthetic_episodes.jsonl"
# Default output lands under the gitignored bench/results/ (root + bench
# .gitignore both ignore results/). Real runs may carry private-derived numbers.
_DEFAULT_OUT = _PKG_DIR.parent / "results"

# A deterministic, fixed Layer-2 nonce keeps the COMPOSED prompts byte-stable so
# the stub Layer-2 path is reproducible across the two fixture runs.
_FIXTURE_NONCE = "0" * 32


# ---------------------------------------------------------------------------
# Per-adapter error isolation
# ---------------------------------------------------------------------------
@dataclass
class AdapterRun:
    """One adapter's outcome through the orchestration (success OR failure)."""

    name: str
    failed: bool = False
    phase: str = ""  # ingest / query / layer2 / teardown when failed
    error: str = ""  # REDACTED error string
    teardown_error: str = ""  # REDACTED teardown error (when ALSO failed earlier)
    layer2_failed: bool = False  # PARTIAL failure: Layer-2 crashed after Layer-1 OK
    layer2_error: str = ""  # REDACTED Layer-2 error (when layer2_failed)
    l1_records: list[GoldScoredRecord] = field(default_factory=list)
    l2_result: AdapterLayer2Result | None = None
    ingest_tokens_used: int = 0
    doc_count: int = 0
    # DISCLOSED PARTIAL INGEST (§9.5): docs the adapter honestly declined to
    # ingest. ``doc_count + skipped_doc_count`` must account for the whole corpus
    # (else it is a silent undercount and the adapter aborts). Surfaced into the
    # results so a partial run can never look complete.
    skipped_doc_count: int = 0
    partial_ingest: dict | None = None  # set when 0 < doc_count < corpus size


# Local-path redaction for anything we surface in a redacted error. Mirrors
# minni_adapter._LOCAL_PATH_PATTERN — a bare ``str(Path.home())`` substitution
# only strips the CURRENT operator's home and lets ANY other /Users/*,
# /home/runner/*, /var/folders/*, etc. path leak into results.json / report.md.
#
# SPACE-IN-PATH: a POSIX path may legally contain spaces (e.g. ``/Users/jane
# doe/.minni``). A stop class that halts at a bare space (``[^ \n\r\t"'<>]+``)
# would redact only ``/Users/jane`` and LEAK the ``doe/.minni`` continuation.
# Mirroring minni_adapter._PATH_TAIL, the tail consumes a space ONLY when it is
# INTERNAL to the path — immediately followed by another path char. A trailing
# space, or a space before a quote/newline/tab/angle-bracket, still terminates
# the token so we never greedily swallow following prose.
_PATH_CHAR = r"[^\s\"'<>]"
_PATH_TAIL = rf"{_PATH_CHAR}(?:{_PATH_CHAR}| {_PATH_CHAR})*"
_LOCAL_PATH_PATTERN = re.compile(
    r"(?:/Users/" + _PATH_TAIL
    + r"|/Volumes/" + _PATH_TAIL
    + r"|/private/" + _PATH_TAIL
    + r"|/var/folders/" + _PATH_TAIL  # macOS per-user temp (TMPDIR / mkdtemp)
    + r"|/var/" + _PATH_TAIL
    + r"|/home/" + _PATH_TAIL  # Linux CI runners (e.g. /home/runner/)
    + r"|/opt/" + _PATH_TAIL  # Homebrew / opt installs (e.g. /opt/homebrew/)
    + r"|/root/" + _PATH_TAIL  # Linux root home
    + r"|/tmp/" + _PATH_TAIL
    + r"|/proc/" + _PATH_TAIL  # Linux process table (e.g. /proc/1/environ)
    + r"|/dev/" + _PATH_TAIL  # device nodes (e.g. /dev/sda1)
    + r"|/etc/" + _PATH_TAIL  # system config (e.g. /etc/shadow)
    + r"|/sys/" + _PATH_TAIL  # Linux sysfs
    + r"|/run/" + _PATH_TAIL  # runtime state (e.g. /run/secrets)
    + r"|/mnt/" + _PATH_TAIL + r")"  # mount points
)


def _redact(exc: BaseException) -> str:
    """Redact an adapter error to its type + a bounded, path-stripped message.

    A raw exception string could embed a filesystem path or other run-specific
    detail; we keep the type and a short, length-bounded message so the report
    stays diagnostic without leaking ANY local path (not just the current
    operator's home) into the results JSON / report.
    """
    msg = str(exc).splitlines()[0] if str(exc) else ""
    msg = _LOCAL_PATH_PATTERN.sub("[REDACTED_PATH]", msg)
    if len(msg) > 160:
        msg = msg[:160] + "…"
    return f"{type(exc).__name__}: {msg}" if msg else type(exc).__name__


# Cap on a single skipped doc-id stored in the manifest (relative corpus paths
# are short; a pathological adapter must not bloat results.json with one giant id).
_MAX_SKIP_ID_LEN = 256


def _redact_str(text: str) -> str:
    """Path-strip + length-bound an ADAPTER-SUPPLIED free string (e.g. skip_reason).

    ``skip_reason`` is an open string any adapter populates; without this it could
    leak a local temp path or run-specific detail into results.json / report.md
    (the error-isolation path already redacts via ``_redact``; this gives the same
    treatment to the skip-reason that does NOT arrive as an exception). First line
    only, every known local path stripped, length-capped.
    """
    msg = str(text).splitlines()[0] if str(text) else ""
    msg = _LOCAL_PATH_PATTERN.sub("[REDACTED_PATH]", msg)
    if len(msg) > 160:
        msg = msg[:160] + "…"
    return msg


def _normalize_skip_id(doc_id: str) -> str:
    """Path-strip + length-bound a skipped corpus doc-id for the manifest.

    Corpus doc-ids are relative paths inside the corpus dir; on a real private
    vault they can encode operator-specific structure. Strip any absolute local
    path that leaked into the id (defence in depth — ids should already be
    relative) and bound the length, mirroring ``_display_path`` for manifest
    paths so nothing operator-specific reaches results.json.
    """
    out = _LOCAL_PATH_PATTERN.sub("[REDACTED_PATH]", str(doc_id))
    if len(out) > _MAX_SKIP_ID_LEN:
        out = out[:_MAX_SKIP_ID_LEN] + "…"
    return out


def _run_one_adapter(
    adapter: MemoryAdapter,
    corpus,
    gold_items,
    episodes,
    agent,
    judge,
    budget: TokenBudget,
    *,
    n_trials: int,
    do_layer2: bool,
) -> AdapterRun:
    """Run ONE adapter through Layer 1 (+ optionally Layer 2), error-isolated.

    Any exception is caught HERE, recorded as a FAILED adapter with a redacted
    error and the phase it failed in, and ``teardown()`` is ALWAYS called. The
    caller continues with the next adapter — one crash never aborts the run.
    """
    run = AdapterRun(name=adapter.name)
    layer1_ok = False
    try:
        # ── Stage 1 — Layer 1: ingest once (capture ingest cost), then score the
        # gold set. A failure HERE marks the whole adapter failed and skips
        # Layer 2 (no Layer-1 records to preserve).
        try:
            ingest_report = adapter.ingest(corpus)
            run.ingest_tokens_used = ingest_report.ingest_tokens_used
            run.doc_count = ingest_report.doc_count
            run.skipped_doc_count = ingest_report.skipped_doc_count
            corpus_size = len(corpus.doc_ids())
            # §9.5 ingest accounting — the SINGLE shared gate (runner_layer1) so
            # every caller of ingest() applies identical rules (over-count abort;
            # fully-accounted partial proceeds; silent undercount or fabricated
            # non-corpus skip-id aborts). ``doc_count`` counts ONLY promoted docs,
            # so inflating ``skipped_doc_count`` cannot game the gate into scoring
            # an adapter that didn't actually index: a skipped doc is never
            # retrievable, and IngestReport.__post_init__ forces the count to equal
            # the id-list length.
            assert_ingest_accounting(ingest_report, corpus)
            if ingest_report.doc_count < corpus_size:
                # Disclosed partial ingest — record it so the report/manifest
                # surfaces that this adapter did NOT ingest the whole corpus and
                # is scored only on what it did. skip_reason is adapter-supplied
                # free text and skipped_doc_ids are corpus doc-ids (relative
                # paths): both are path-redacted + length-bounded before they land
                # in results.json (mirroring the error-isolation path) so no
                # operator-specific path leaks. The id list is truncated to a
                # bound and ``skipped_doc_ids_truncated`` flags when that happened
                # so a consumer can tell the manifest does not list every id.
                _MAX_SKIP_IDS = 50
                truncated = len(ingest_report.skipped_doc_ids) > _MAX_SKIP_IDS
                run.partial_ingest = {
                    "doc_count": ingest_report.doc_count,
                    "skipped_doc_count": ingest_report.skipped_doc_count,
                    "corpus_size": corpus_size,
                    "skip_reason": _redact_str(ingest_report.skip_reason),
                    "skipped_doc_ids": [
                        _normalize_skip_id(d)
                        for d in ingest_report.skipped_doc_ids[:_MAX_SKIP_IDS]
                    ],
                    "skipped_doc_ids_truncated": truncated,
                }
            run.phase = "query"
            run.l1_records = [
                score_gold_query(adapter, corpus, item, budget)
                for item in gold_items
            ]
            layer1_ok = True
        except Exception as exc:  # noqa: BLE001 — isolation is the whole point
            run.failed = True
            if not run.phase:
                run.phase = "ingest"
            run.error = _redact(exc)
            run.l1_records = []
            run.l2_result = None

        # ── Stage 2 — Layer 2: play each episode for N trials (re-ingests per
        # episode). Runs ONLY if Layer 1 succeeded. A failure HERE is a PARTIAL
        # failure (Layer-2 only): the valid Layer-1 records are PRESERVED so the
        # adapter still appears in the scorecards — it is NOT marked fully failed.
        # Set the phase BEFORE the loop so a crash in run_episode_trial is
        # recorded as a Layer-2 failure, not mislabeled as the prior 'query'.
        if layer1_ok and do_layer2 and episodes:
            run.phase = "layer2"
            try:
                l2 = AdapterLayer2Result(
                    adapter=adapter.name,
                    n_trials=n_trials,
                    n_episodes=len(episodes),
                )
                for episode in episodes:
                    for trial in range(n_trials):
                        l2.trials.append(
                            run_episode_trial(
                                adapter, episode, agent, judge, trial,
                                nonce=_FIXTURE_NONCE,
                            )
                        )
                run.l2_result = l2
            except Exception as exc:  # noqa: BLE001 — Layer-2 isolation
                # Partial failure: keep Layer-1 records, drop only Layer-2.
                run.layer2_failed = True
                run.layer2_error = _redact(exc)
                run.l2_result = None
    finally:
        # teardown() is ALWAYS called — even for a failed adapter — so a real
        # adapter holding fs locks / handles / connections never leaks them.
        try:
            adapter.teardown()
        except Exception as exc:  # noqa: BLE001 — a teardown crash must not abort the run
            teardown_err = _redact(exc)
            if not run.failed and not run.layer2_failed:
                run.failed = True
                run.phase = "teardown"
                run.error = teardown_err
            else:
                # The adapter ALSO failed earlier (ingest/query/layer2) OR is a
                # PARTIAL failure (Layer-1 OK, Layer-2 crashed). A teardown crash
                # must NOT promote a partial-failure adapter to a full failure —
                # that would discard its valid Layer-1 records. Record the
                # teardown crash separately so the compound failure (e.g. a real
                # adapter that holds fs locks) is never silently swallowed, while
                # the adapter stays a survivor when it was only a partial failure.
                run.teardown_error = teardown_err
    return run


# ---------------------------------------------------------------------------
# Run manifest (§6.9.7 — pins EVERYTHING needed to reproduce)
# ---------------------------------------------------------------------------
def _tiktoken_version() -> str:
    try:
        import tiktoken

        return getattr(tiktoken, "__version__", "")
    except Exception:
        return ""


def _scipy_numpy_versions() -> dict:
    out = {}
    for mod in ("numpy", "scipy"):
        try:
            out[mod] = __import__(mod).__version__
        except Exception:
            out[mod] = ""
    return out


def _display_path(path: Path) -> str:
    """Path for the manifest/report: relative to repo root when contained.

    A run-specific ABSOLUTE path (e.g. ``/Users/<operator>/...``) embedded in the
    manifest would (a) leak the operator's home dir into a committed example
    report and (b) break the cross-machine repro claim (the path differs per
    machine). Paths under the repo root are recorded RELATIVE to it; anything
    outside is home-stripped to ``~`` and then run through the SAME
    ``_LOCAL_PATH_PATTERN`` used for error redaction, so no platform path
    (``/tmp/*``, ``/var/folders/*``, ``/Volumes/*``, ``/opt/*``, a CI runner's
    ``/home/runner/*`` that differs from the local home, etc.) ever leaks into
    the manifest / results.json / example report.
    """
    path = Path(path)
    try:
        return str(path.resolve().relative_to(REPO_ROOT.resolve()))
    except ValueError:
        result = str(path).replace(str(Path.home()), "~")
        return _LOCAL_PATH_PATTERN.sub("[REDACTED_PATH]", result)


def build_manifest(
    *,
    corpus_dir: Path,
    corpus_hash: str,
    scrubbed: bool,
    gold_path: Path,
    episodes_path: Path,
    episode_set_hash: str,
    adapters: list[MemoryAdapter],
    degraded_adapters: list[str],
    seed: int,
    real_llm: bool,
    is_fixture_run: bool,
    n_trials: int,
) -> dict:
    """Build the pinned run-manifest (§6.9.7).

    Embeds the corpus content-hash, scrub status, per-adapter configs, embedder
    id, tokenizer id+version, k, budget, N, seeds, model ids + families, episode
    hashes, runtime versions, and which adapters are degraded / stub vs real.
    The runtime versions make the byte-identity determinism gate hold across
    machines.
    """
    return {
        "corpus": {
            "dir": _display_path(corpus_dir),
            "content_hash": corpus_hash,
            "scrubbed": scrubbed,
        },
        "gold": {
            "path": _display_path(gold_path),
        },
        "episodes": {
            "path": _display_path(episodes_path),
            # The FIXTURE episode hash (public synthetic episodes, CI repro) and
            # the RUN episode hash (episodes actually consumed) are the same on a
            # fixture run; on a private run they differ (§6.9.7).
            "fixture_episode_hash": episode_set_hash if is_fixture_run else "",
            "run_episode_hash": episode_set_hash,
        },
        "adapters": {
            a.name: {
                "config_hash": getattr(a, "config_hash", ""),
                "degraded": a.name in degraded_adapters,
            }
            for a in adapters
        },
        "embedder": {
            "model_id": config.EMBEDDER_MODEL_ID,
            "dim": config.EMBEDDING_DIM,
        },
        "tokenizer": {
            "id": config.CANONICAL_TOKENIZER_ID,
            "tiktoken_version": _tiktoken_version(),
        },
        "models": {
            "agent": {
                "model_id": config.AGENT_MODEL.model_id,
                "model_family": config.AGENT_MODEL.model_family,
            },
            "judge": {
                "model_id": config.JUDGE_MODEL.model_id,
                "model_family": config.JUDGE_MODEL.model_family,
            },
            # Real run uses the live LLM; fixture/stub run uses the offline stubs.
            "layer2_mode": "real-llm" if real_llm else "stub (offline)",
        },
        "retrieval": {
            "k": config.K,
            "budget_max_tokens": config.DEFAULT_MAX_TOKENS,
            "n_trials": n_trials,
        },
        "seeds": {
            "ingest_seed": config.INGEST_SEED,
            "run_seed": seed,
        },
        "runtime": {
            "python": platform.python_version(),
            "lockfile_hash": config.RUNTIME_LOCKFILE_HASH,
            **_scipy_numpy_versions(),
        },
        "run": {
            "is_fixture_run": is_fixture_run,
            "real_llm": real_llm,
            "degraded_adapters": sorted(degraded_adapters),
        },
    }


def compute_episode_set_hash(episodes_path: Path) -> str:
    """SHA-256 over the sorted canonical manifest of the consumed episode file(s).

    Computed at RUN TIME (not hardcoded). For a single episode JSONL this hashes
    that file's bytes under its doc-id; the same loader-style canonical manifest
    the corpus uses (§6.9.7).
    """
    import hashlib

    episodes_path = Path(episodes_path)
    hasher = hashlib.sha256()
    name = episodes_path.name
    file_hash = hashlib.sha256(episodes_path.read_bytes()).hexdigest()
    hasher.update(name.encode("utf-8"))
    hasher.update(b"\n")
    hasher.update(file_hash.encode("utf-8"))
    hasher.update(b"\n")
    return hasher.hexdigest()


# ---------------------------------------------------------------------------
# Adapter roster (mirrors run_scorer; minni-as-stub by default)
# ---------------------------------------------------------------------------
def _build_adapters(live_minni: bool, extra: list[MemoryAdapter] | None = None):
    """Build the default adapter roster (minni-as-stub fallback by default)."""
    from .adapters.llm_wiki import LlmWikiAdapter
    from .adapters.markdown_grep import MarkdownGrepAdapter
    from .adapters.naive_rag import NaiveRagAdapter
    from .adapters.native_platform import NativePlatformAdapter
    from .adapters.sanity_random import SanityRandomAdapter
    from .adapters.stub import StubAdapter
    from .run_scorer import _MinniStubAdapter

    adapters: list[MemoryAdapter] = [
        StubAdapter(),
        NaiveRagAdapter(),
        MarkdownGrepAdapter(),
        LlmWikiAdapter(),
        NativePlatformAdapter(),
        SanityRandomAdapter(),
    ]
    if live_minni:
        from .adapters.minni_adapter import MinniAdapter

        adapters.append(MinniAdapter())
    else:
        adapters.append(_MinniStubAdapter())
    if extra:
        adapters.extend(extra)
    return adapters


# Adapters whose numbers are inherently fidelity-limited / degraded (§-threats).
_DEGRADED_ADAPTERS = {"native_platform"}


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def orchestrate(
    *,
    corpus_dir: Path,
    gold_path: Path,
    episodes_path: Path,
    seed: int = config.INGEST_SEED,
    real_llm: bool = False,
    live_minni: bool = False,
    n_trials: int = config.N,
    adapters: list[MemoryAdapter] | None = None,
    is_fixture_run: bool = True,
    corpus_scrubbed: bool = False,
) -> dict:
    """Run the whole benchmark and return the machine-readable results dict.

    Pure orchestration: loads the hash-gated corpus, runs every adapter through
    Layer 1 (+ Layer 2) with PER-ADAPTER error isolation, computes significance /
    efficiency / ingest cost, and assembles the results dict (manifest +
    scorecards + layer2 + efficiency + ingest_cost + failures). Offline by
    default (StubAgent/StubJudge); ``real_llm=True`` is the gated live path.
    """
    corpus_dir = Path(corpus_dir)
    pinned_hash = (
        config.FIXTURE_CORPUS_HASH
        if is_fixture_run
        else compute_content_hash(corpus_dir)
    )
    corpus = load_corpus(
        corpus_dir, pinned_hash=pinned_hash, scrubbed=corpus_scrubbed
    )
    corpus_hash = corpus.content_hash
    gold_items = load_jsonl(gold_path)
    episodes = load_episodes(episodes_path)
    budget = TokenBudget(max_tokens=config.DEFAULT_MAX_TOKENS, max_docs=config.K)

    if real_llm:
        # GATED live path — never reached by tests. The real agent/judge resolve
        # keys at call time and enforce MAX_API_CALLS.
        from .agent import LLMAgent
        from .judge import LLMJudge

        agent = LLMAgent()
        judge = LLMJudge()
    else:
        agent = StubAgent()
        judge = StubJudge()

    roster = adapters if adapters is not None else _build_adapters(live_minni)
    degraded = [a.name for a in roster if a.name in _DEGRADED_ADAPTERS]

    runs: list[AdapterRun] = []
    for adapter in roster:
        runs.append(
            _run_one_adapter(
                adapter, corpus, gold_items, episodes, agent, judge, budget,
                n_trials=n_trials, do_layer2=True,
            )
        )

    # ── Assemble survivors vs failures.
    survivors = [r for r in runs if not r.failed]
    failures = {
        r.name: {
            "phase": r.phase,
            "error": r.error,
            **({"teardown_error": r.teardown_error} if r.teardown_error else {}),
        }
        for r in runs
        if r.failed
    }
    # Partial failures (Layer-2 crashed after Layer-1 succeeded): the adapter
    # stays a SURVIVOR (its valid Layer-1 records are scored) but its Layer-2
    # crash is surfaced so it is never silently swallowed.
    partial_failures = {
        r.name: {
            "phase": "layer2",
            "error": r.layer2_error,
            **({"teardown_error": r.teardown_error} if r.teardown_error else {}),
        }
        for r in survivors
        if r.layer2_failed
    }

    # Layer-1 scorecards (survivors only).
    l1_by_adapter = {r.name: r.l1_records for r in survivors}
    scorecards = build_scorecards(l1_by_adapter, config.K)

    # Layer-2 aggregate (survivors with a Layer-2 result).
    l2_by_adapter = {
        r.name: r.l2_result for r in survivors if r.l2_result is not None
    }
    layer2 = layer2_results_to_dict(l2_by_adapter) if l2_by_adapter else {}
    efficiency = efficiency_block(l2_by_adapter) if l2_by_adapter else {}

    # §6.8 ingest cost (every survivor, 0 for non-LLM adapters). ``skipped`` is
    # surfaced per-adapter so the table is machine-readable + reproducible: a
    # survivor that ingested the whole corpus reports skipped 0.
    ingest_cost = {
        r.name: {
            "ingest_tokens_used": r.ingest_tokens_used,
            "doc_count": r.doc_count,
            "skipped_doc_count": r.skipped_doc_count,
        }
        for r in survivors
    }

    # DISCLOSED PARTIAL INGEST (§9.5): adapters that honestly ingested FEWER docs
    # than the corpus (fully accounted: doc_count + skipped == corpus). Surfaced
    # as a machine-readable block AND in the report so a partial run can never
    # look complete. Empty when every survivor ingested the whole corpus.
    partial_ingest = {
        r.name: r.partial_ingest
        for r in survivors
        if r.partial_ingest is not None
    }

    episode_set_hash = compute_episode_set_hash(episodes_path)
    manifest = build_manifest(
        corpus_dir=corpus_dir,
        corpus_hash=corpus_hash,
        scrubbed=corpus_scrubbed,
        gold_path=gold_path,
        episodes_path=episodes_path,
        episode_set_hash=episode_set_hash,
        adapters=roster,
        degraded_adapters=degraded,
        seed=seed,
        real_llm=real_llm,
        is_fixture_run=is_fixture_run,
        n_trials=n_trials,
    )

    return {
        "manifest": manifest,
        "scorecards": scorecards,
        "layer2": layer2,
        "efficiency": efficiency,
        "ingest_cost": ingest_cost,
        "partial_ingest": partial_ingest,
        "failures": failures,
        "partial_failures": partial_failures,
    }


# ---------------------------------------------------------------------------
# Artifact writing
# ---------------------------------------------------------------------------
def layer1_scorecard_json(results: dict) -> str:
    """The BYTE-REPRODUCIBLE Layer-1 scorecard JSON (determinism strip applied).

    This is the artifact the §T-e repro gate diffs: the timing fields are
    stripped so two fixture runs produce byte-identical output.
    """
    stripped = metrics.strip_excluded_fields(results["scorecards"])
    return layer1_canonical_json(stripped)


def write_artifacts(results: dict, out_dir: Path) -> dict:
    """Write the report + results JSON + manifest + Layer-1 scorecard JSON.

    Returns a dict of ``{artifact_name: path}``. ``out_dir`` defaults to the
    gitignored ``results/`` tree; real runs may carry private-derived numbers so
    they MUST stay out of public git.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}

    # Machine-readable full results (Layer-2 + timing are NOT byte-stable here).
    paths["results_json"] = out_dir / "results.json"
    paths["results_json"].write_text(
        json.dumps(results, sort_keys=True, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # The pinned run-manifest, on its own for easy provenance inspection.
    paths["manifest_json"] = out_dir / "manifest.json"
    paths["manifest_json"].write_text(
        json.dumps(results["manifest"], sort_keys=True, ensure_ascii=False,
                   indent=2),
        encoding="utf-8",
    )

    # The BYTE-REPRODUCIBLE Layer-1 scorecard JSON (the §T-e repro gate target).
    paths["layer1_scorecard_json"] = out_dir / "layer1_scorecard.json"
    paths["layer1_scorecard_json"].write_text(
        layer1_scorecard_json(results), encoding="utf-8"
    )

    # The rendered Markdown report (tables-only when matplotlib is absent).
    paths["report_md"] = out_dir / "report.md"
    paths["report_md"].write_text(
        render_report(results, plots_dir=out_dir / "plots"), encoding="utf-8"
    )
    return paths


def run_fixture(out_dir: Path | None = None) -> tuple[dict, dict]:
    """Run the default FIXTURE end-to-end and write artifacts.

    Returns ``(results, artifact_paths)``. This is what ``make bench`` / the
    bare module invocation runs: the public synthetic corpus + gold + episodes,
    offline stubs, written to the gitignored ``results/`` dir.
    """
    out_dir = Path(out_dir) if out_dir is not None else _DEFAULT_OUT
    results = orchestrate(
        corpus_dir=_DEFAULT_CORPUS,
        gold_path=_DEFAULT_GOLD,
        episodes_path=_DEFAULT_EPISODES,
        real_llm=False,
        is_fixture_run=True,
    )
    paths = write_artifacts(results, out_dir)
    return results, paths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="membench orchestration entrypoint (s7) — one command, "
        "end-to-end, reproducible. Default: the public synthetic fixture."
    )
    parser.add_argument("--corpus", default=str(_DEFAULT_CORPUS),
                        help="corpus directory (default: fixture)")
    parser.add_argument("--gold", default=str(_DEFAULT_GOLD),
                        help="gold-set JSONL (default: fixture)")
    parser.add_argument("--episodes", default=str(_DEFAULT_EPISODES),
                        help="episodes JSONL (default: fixture)")
    parser.add_argument("--out", default=str(_DEFAULT_OUT),
                        help="output dir (default: gitignored results/)")
    parser.add_argument("--seed", type=int, default=config.INGEST_SEED,
                        help="run seed (pinned into the manifest)")
    parser.add_argument("--real-llm", action="store_true",
                        help="use the gated real LLM agent/judge (default: off, "
                        "offline stubs). Requires env keys + MAX_API_CALLS.")
    parser.add_argument("--live-minni", action="store_true",
                        help="attempt the real isolated Minni daemon")
    parser.add_argument(
        "--scrubbed",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="enforce the scrub gate check on the corpus",
    )
    args = parser.parse_args(argv)

    is_fixture = (
        Path(args.corpus).resolve() == _DEFAULT_CORPUS.resolve()
        and Path(args.gold).resolve() == _DEFAULT_GOLD.resolve()
        and Path(args.episodes).resolve() == _DEFAULT_EPISODES.resolve()
    )
    results = orchestrate(
        corpus_dir=Path(args.corpus),
        gold_path=Path(args.gold),
        episodes_path=Path(args.episodes),
        seed=args.seed,
        real_llm=args.real_llm,
        live_minni=args.live_minni,
        is_fixture_run=is_fixture,
        corpus_scrubbed=args.scrubbed,
    )
    paths = write_artifacts(results, Path(args.out))

    print("membench run complete. Artifacts:")
    for name, path in sorted(paths.items()):
        print(f"  {name}: {path}")
    failures = results["failures"]
    if failures:
        print(f"\nFAILED adapters (run continued for survivors): "
              f"{sorted(failures)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
