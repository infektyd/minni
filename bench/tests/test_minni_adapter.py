"""Minni adapter live round-trip (§3, §4, §7.5) — skips if standup infeasible.

CRITICAL SAFETY: the adapter spins an ISOLATED throwaway daemon (own temp
MINNI_HOME + temp socket). This test additionally asserts the adapter never
targets the operator's LIVE socket/DB. If standing up a headless isolated
daemon is infeasible in this environment, the live round-trip SKIPs (not fails)
with a clear reason — the contract is still proven by the stub-adapter suite.
"""

import os
from pathlib import Path

import pytest

from membench.adapters.minni_adapter import (
    MinniAdapter,
    MinniStandupError,
    _LIVE_DB,
    _LIVE_SOCKET,
)
from membench.contract import QueryResult, assert_well_formed


def test_adapter_never_references_live_paths_by_default():
    """A fresh adapter holds no live-path handles (data-safety, static)."""
    a = MinniAdapter()
    assert a._socket_path is None
    assert a._tmp_home is None
    # The module-level live-path constants point at ~/.minni, which the adapter
    # only ever uses as a *guard* (refuse), never as a target.
    assert str(_LIVE_SOCKET).endswith(".minni/run/minnid.sock")
    assert str(_LIVE_DB).endswith(".minni/minni.db")


def test_spawn_daemon_refuses_path_inside_live_home(monkeypatch):
    """The data-safety guard MUST fire if mkdtemp lands inside ~/.minni.

    Mocks tempfile.mkdtemp to return a path under the live home and asserts
    _spawn_daemon raises MinniStandupError BEFORE any subprocess is launched.
    """
    injected = str(Path.home() / ".minni" / "injected-membench")

    import membench.adapters.minni_adapter as mod

    monkeypatch.setattr(mod.tempfile, "mkdtemp", lambda *a, **k: injected)

    # Guard against accidentally spawning a real process if the guard fails.
    def _boom(*a, **k):  # pragma: no cover - only hit on guard regression
        raise AssertionError("subprocess launched despite live-home path")

    monkeypatch.setattr(mod.subprocess, "Popen", _boom)

    adapter = MinniAdapter()
    try:
        with pytest.raises(MinniStandupError):
            adapter._spawn_daemon()
        # The guard MUST fire BEFORE any filesystem mutation under tmp_home:
        # no directory may be created at (or under) the injected live-home path.
        # mkdtemp was mocked to NOT create the dir, so the run_dir mkdir is the
        # only fs mutation that could have happened — assert it did not (finding
        # #1: the guard precedes run_dir.mkdir()).
        assert not Path(injected).exists(), (
            "guard must abort before creating any directory under the live home"
        )
        assert not (Path(injected) / "run").exists()
        # The injected path is under the live home; teardown must NOT delete it.
    finally:
        # _tmp_home was set to the injected (live-home) path by the guard's
        # early assignment; clear it so teardown cannot touch the live home.
        adapter._tmp_home = None
        adapter.teardown()


def test_spawn_daemon_refuses_path_inside_openclaw(monkeypatch):
    """The data-safety guard MUST fire if mkdtemp lands inside ~/.openclaw.

    Finding #4 added ~/.openclaw as a second live root (the engine's hardcoded
    dual-write flat-file dir). This mirrors the ~/.minni guard test for that root
    so a typo that skips the openclaw root cannot pass the suite while leaving the
    operator's ~/.openclaw unprotected (review finding #5).
    """
    injected = str(Path.home() / ".openclaw" / "injected-membench")

    import membench.adapters.minni_adapter as mod

    monkeypatch.setattr(mod.tempfile, "mkdtemp", lambda *a, **k: injected)

    def _boom(*a, **k):  # pragma: no cover - only hit on guard regression
        raise AssertionError("subprocess launched despite live-openclaw path")

    monkeypatch.setattr(mod.subprocess, "Popen", _boom)

    adapter = MinniAdapter()
    try:
        with pytest.raises(MinniStandupError):
            adapter._spawn_daemon()
        # The guard must abort BEFORE any filesystem mutation under tmp_home.
        assert not Path(injected).exists(), (
            "guard must abort before creating any directory under ~/.openclaw"
        )
        assert not (Path(injected) / "run").exists()
    finally:
        adapter._tmp_home = None
        adapter.teardown()


def test_rpc_rejects_non_dict_json_response(monkeypatch, tmp_path):
    """A valid-JSON but non-dict JSON-RPC reply (e.g. a list) must raise a
    redacted MinniStandupError, NOT a raw AttributeError/TypeError (finding #4)."""
    import membench.adapters.minni_adapter as mod

    # Fake socket whose recv() yields a JSON list terminated by a newline.
    class _FakeSock:
        def __init__(self, *a, **k):
            self._sent = False

        def settimeout(self, *_a):
            pass

        def connect(self, *_a):
            pass

        def sendall(self, *_a):
            pass

        def recv(self, _n):
            if self._sent:
                return b""
            self._sent = True
            return b'[1, 2, 3]\n'

        def close(self):
            pass

    monkeypatch.setattr(mod.socket, "socket", lambda *a, **k: _FakeSock())

    with pytest.raises(MinniStandupError) as exc:
        mod._rpc(tmp_path / "fake.sock", "ping", {})
    assert "non-dict" in str(exc.value)


def test_rpc_parses_first_frame_with_trailing_bytes(monkeypatch, tmp_path):
    """A single recv() delivering JSON + newline + trailing bytes must parse the
    FIRST frame only (newline-delimited JSON), not choke on 'Extra data' (#7)."""
    import membench.adapters.minni_adapter as mod

    class _FakeSock:
        def __init__(self, *a, **k):
            self._sent = False

        def settimeout(self, *_a):
            pass

        def connect(self, *_a):
            pass

        def sendall(self, *_a):
            pass

        def recv(self, _n):
            if self._sent:
                return b""
            self._sent = True
            # First frame + newline + leading bytes of a following frame.
            return b'{"result": {"ok": 1}}\n{"result": {"next":'

        def close(self):
            pass

    monkeypatch.setattr(mod.socket, "socket", lambda *a, **k: _FakeSock())
    out = mod._rpc(tmp_path / "fake.sock", "ping", {})
    assert out == {"ok": 1}


def test_redact_covers_linux_ci_paths():
    """/home/, /opt/, /root/ absolute paths must be redacted (finding #6)."""
    from membench.adapters.minni_adapter import _redact

    assert "[REDACTED_PATH]" in _redact(
        "/home/runner/work/Minni/engine/minnid.py"
    )
    assert "runner" not in _redact("/home/runner/work/Minni/engine/minnid.py")
    assert "[REDACTED_PATH]" in _redact("/opt/homebrew/bin/python")
    assert "[REDACTED_PATH]" in _redact("/root/.minni/minni.db")


@pytest.mark.parametrize(
    "doc_id",
    [
        # safe id (baseline)
        "03-teal-ledger.md",
        # marker-breaking chars the percent-encoding path exists for: ']' and
        # newline are the marker's own delimiters, '%' is the encode escape.
        # Un-escaped, the recovery regex would truncate and the id would fail the
        # valid_ids membership check (silent zero-retrieval) — these exercise
        # _encode_doc_id/_decode_doc_id (finding #3).
        "docs/foo]bar.md",
        "a%b.md",
        "a\nb.md",
    ],
)
def test_mark_content_roundtrip(doc_id):
    """_doc_id_from_content must recover exactly what _mark_content stamped in
    (the sole ingest↔retrieval mapping; a bracket/regex mismatch would silently
    regress the whole fix to zero retrieval, finding #7). Parametrized over ids
    containing ']', '%', and newline to exercise the percent-encoding path."""
    from membench.adapters.minni_adapter import (
        _doc_id_from_content,
        _mark_content,
    )

    body = "some body text\nwith newlines"
    marked = _mark_content(doc_id, body)
    assert _doc_id_from_content(marked, {doc_id}) == doc_id
    # The original body must still be present after the marker prefix.
    assert "some body text" in marked


@pytest.mark.parametrize(
    "content, valid_ids, expected",
    [
        # (1) non-string content -> None
        (None, {"a"}, None),
        (42, {"a"}, None),
        # (2) marker id NOT in valid_ids (corrupt/adversarial daemon) -> None
        ("[membench_doc_id::evil.md]\n\nbody", {"a.md"}, None),
        # (3) no marker at all -> None
        ("just plain content, no marker here", {"a.md"}, None),
        # (4) correct marker in valid_ids -> the id
        ("[membench_doc_id::a.md]\n\nbody", {"a.md"}, "a.md"),
    ],
)
def test_doc_id_from_content_branches(content, valid_ids, expected):
    """All four branches of _doc_id_from_content (finding #7)."""
    from membench.adapters.minni_adapter import _doc_id_from_content

    assert _doc_id_from_content(content, valid_ids) == expected


def test_spawn_daemon_env_pythonpath_is_engine_only(monkeypatch):
    """The throwaway daemon's PYTHONPATH must be EXACTLY str(_ENGINE_DIR) — the
    parent PYTHONPATH is never inherited (import-hijack guard, finding #5)."""
    import membench.adapters.minni_adapter as mod

    # Poison the parent PYTHONPATH; it must NOT leak into the subprocess env.
    monkeypatch.setenv("PYTHONPATH", "/evil/inject/path")

    captured: dict = {}

    class _DeadProc:
        returncode = 7

        def __init__(self, *a, **k):
            captured["env"] = k.get("env")

        def poll(self):
            return self.returncode  # appears exited -> spawn raises cleanly

        @property
        def stdout(self):
            import io

            return io.BytesIO(b"")

    monkeypatch.setattr(mod.subprocess, "Popen", _DeadProc)

    adapter = MinniAdapter()
    try:
        with pytest.raises(MinniStandupError):
            adapter._spawn_daemon()
    finally:
        adapter.teardown()

    env = captured["env"]
    assert env is not None
    assert env["PYTHONPATH"] == str(mod._ENGINE_DIR)
    assert "/evil/inject/path" not in env["PYTHONPATH"]


def _prime_adapter_for_query(adapter, corpus, tmp_path):
    """Put a fresh MinniAdapter into the post-ingest state WITHOUT a live daemon.

    Sets the socket path + corpus so query() runs its result-parsing path; the
    actual socket round-trip is monkeypatched out per-test via _rpc.
    """
    adapter._socket_path = tmp_path / "fake.sock"
    adapter._corpus = corpus


@pytest.mark.parametrize("bad_results", [None, {"not": "a list"}, 42, "str"])
def test_query_handles_null_or_nonlist_results(monkeypatch, corpus, budget, tmp_path, bad_results):
    """`results: null` must be coerced to [] (no TypeError); a non-list results
    must raise a redacted MinniStandupError, never a raw TypeError (finding #3)."""
    import membench.adapters.minni_adapter as mod

    adapter = MinniAdapter()
    _prime_adapter_for_query(adapter, corpus, tmp_path)
    monkeypatch.setattr(mod, "_rpc", lambda *a, **k: {"results": bad_results})
    try:
        if bad_results is None:
            # null -> [] -> empty, well-formed result (graceful skip, no crash).
            result = adapter.query("anything", budget)
            assert result.ranked_results == []
            assert result.context_string == ""
        else:
            # A non-list results value raises MinniStandupError (not TypeError).
            with pytest.raises(MinniStandupError):
                adapter.query("anything", budget)
    finally:
        adapter._corpus = None  # avoid teardown touching anything real
        adapter.teardown()


@pytest.mark.parametrize("bad_learnings", [None, {"not": "a list"}, 42, "str"])
def test_query_handles_null_or_nonlist_learnings(
    monkeypatch, corpus, budget, tmp_path, bad_learnings
):
    """The learnings stream is the ACTUAL retrieval path after the fix; the same
    _as_list() contract must hold for it: `learnings: null` -> [] (no TypeError),
    a non-list -> redacted MinniStandupError (finding #5). results is absent."""
    import membench.adapters.minni_adapter as mod

    adapter = MinniAdapter()
    _prime_adapter_for_query(adapter, corpus, tmp_path)
    monkeypatch.setattr(mod, "_rpc", lambda *a, **k: {"learnings": bad_learnings})
    try:
        if bad_learnings is None:
            result = adapter.query("anything", budget)
            assert result.ranked_results == []
            assert result.context_string == ""
        else:
            with pytest.raises(MinniStandupError):
                adapter.query("anything", budget)
    finally:
        adapter._corpus = None
        adapter.teardown()


def test_query_context_respects_budget(monkeypatch, corpus, tmp_path):
    """The minni adapter must trim context_string to the TokenBudget like the
    other adapters, so an over-budget result can't trip the runner abort (NIT-a)."""
    import membench.adapters.minni_adapter as mod
    from membench import tokenizer
    from membench.contract import TokenBudget

    adapter = MinniAdapter()
    _prime_adapter_for_query(adapter, corpus, tmp_path)
    # Return EVERY corpus doc as a hit so the untrimmed context would be large.
    doc_ids = list(corpus.doc_ids())
    hits = [{"metadata": {"membench_doc_id": d}, "score": 1.0} for d in doc_ids]
    monkeypatch.setattr(mod, "_rpc", lambda *a, **k: {"results": hits})

    from membench import config

    tight = TokenBudget(max_tokens=40, max_docs=config.K)
    try:
        result = adapter.query("anything", tight)
        assert tokenizer.count_tokens(result.context_string) <= 40, (
            "minni context must be trimmed to the token budget"
        )
    finally:
        adapter._corpus = None
        adapter.teardown()


def test_query_learnings_respects_budget(monkeypatch, corpus, tmp_path):
    """The learnings loop has its OWN budget cap (adapter: `if len(ranked) >=
    budget.max_docs: break`). Exercise the path where results=[] and learnings
    carries MORE valid hits than budget.max_docs; ranked must stay capped (#6)."""
    import membench.adapters.minni_adapter as mod
    from membench.adapters.minni_adapter import _mark_content
    from membench.contract import TokenBudget

    adapter = MinniAdapter()
    _prime_adapter_for_query(adapter, corpus, tmp_path)
    doc_ids = list(corpus.doc_ids())
    # One learning per corpus doc, each carrying the doc-id marker in content so
    # the learnings path maps and ranks it.
    learnings = [
        {"content": _mark_content(d, f"body for {d}")} for d in doc_ids
    ]
    monkeypatch.setattr(
        mod, "_rpc", lambda *a, **k: {"results": [], "learnings": learnings}
    )

    cap = max(1, len(doc_ids) - 2)
    assert cap < len(doc_ids), "test needs more learnings than the budget cap"
    tight = TokenBudget(max_tokens=100_000, max_docs=cap)
    try:
        result = adapter.query("anything", tight)
        # All learnings are valid and there are MORE than cap, so the budget
        # `break` must yield EXACTLY cap docs — not merely <= cap. Asserting ==
        # tightly verifies the cap is both enforced and fully consumed (the loop
        # does not terminate early for some other reason) (review finding #7).
        assert len(result.ranked_results) == cap, (
            "learnings path must consume exactly budget.max_docs when enough valid"
        )
        # The first appended learning scores exactly 1.0 (rank_idx starts at 0).
        assert result.ranked_results[0].score == 1.0
    finally:
        adapter._corpus = None
        adapter.teardown()


def test_query_learnings_first_hit_scores_one_after_skips(
    monkeypatch, corpus, tmp_path
):
    """If the first learnings item is skipped (non-dict / no marker), the first
    VALID learning must still score 1.0 and scores stay gap-free (finding #3)."""
    import membench.adapters.minni_adapter as mod
    from membench.adapters.minni_adapter import _mark_content
    from membench.contract import TokenBudget

    adapter = MinniAdapter()
    _prime_adapter_for_query(adapter, corpus, tmp_path)
    doc_ids = list(corpus.doc_ids())
    assert len(doc_ids) >= 2
    # Item 0 is a non-dict (skipped); item 1 has no marker (skipped); items 2..
    # are valid. The first VALID item must score 1.0, the next 0.99.
    learnings = [
        "i am not a dict",
        {"content": "no marker in here at all"},
        {"content": _mark_content(doc_ids[0], "body0")},
        {"content": _mark_content(doc_ids[1], "body1")},
    ]
    monkeypatch.setattr(
        mod, "_rpc", lambda *a, **k: {"results": [], "learnings": learnings}
    )
    tight = TokenBudget(max_tokens=100_000, max_docs=10)
    try:
        result = adapter.query("anything", tight)
        scores = [rd.score for rd in result.ranked_results]
        assert scores[0] == 1.0, f"first valid learning must score 1.0; got {scores}"
        assert scores[1] == pytest.approx(0.99), f"strictly-descending; got {scores}"
    finally:
        adapter._corpus = None
        adapter.teardown()


def test_query_mixed_results_and_learnings_ordering(monkeypatch, corpus, tmp_path):
    """Mixed semantic `results` + lexical `learnings` stream (review finding #8).

    The adapter appends semantic hits FIRST (in their daemon rank order), then
    learnings. POSITION order is what membench metrics consume and must be
    results-then-learnings. The synthesized learning scores are INDEPENDENT of the
    preceding semantic scores (rank_idx starts at 0), so the first learning scores
    1.0 even though a preceding semantic hit scored 0.5 — i.e. a lexical hit CAN
    carry a score above a preceding semantic hit. This test pins that documented
    behavior so the comment and code stay honest.
    """
    import membench.adapters.minni_adapter as mod
    from membench.adapters.minni_adapter import _mark_content
    from membench.contract import TokenBudget

    adapter = MinniAdapter()
    _prime_adapter_for_query(adapter, corpus, tmp_path)
    doc_ids = list(corpus.doc_ids())
    assert len(doc_ids) >= 3
    sem_id, learn_id0, learn_id1 = doc_ids[0], doc_ids[1], doc_ids[2]
    # One low-scored semantic hit, then two learnings.
    results = [{"metadata": {"membench_doc_id": sem_id}, "score": 0.5}]
    learnings = [
        {"content": _mark_content(learn_id0, "body0")},
        {"content": _mark_content(learn_id1, "body1")},
    ]
    monkeypatch.setattr(
        mod, "_rpc", lambda *a, **k: {"results": results, "learnings": learnings}
    )
    wide = TokenBudget(max_tokens=100_000, max_docs=10)
    try:
        result = adapter.query("anything", wide)
        ids = [rd.doc_id for rd in result.ranked_results]
        scores = [rd.score for rd in result.ranked_results]
        # POSITION order: semantic hit first, then the two learnings in order.
        assert ids == [sem_id, learn_id0, learn_id1], f"position order wrong: {ids}"
        # Semantic hit keeps its daemon score; first learning scores 1.0
        # (rank_idx starts at 0, independent of the preceding semantic count).
        assert scores[0] == pytest.approx(0.5)
        assert scores[1] == 1.0
        assert scores[2] == pytest.approx(0.99)
        # Documented consequence: the lexical learning (1.0) outscores the
        # preceding semantic hit (0.5) — accepted; metrics use position order.
        assert scores[1] > scores[0]
    finally:
        adapter._corpus = None
        adapter.teardown()


def test_query_dedups_same_doc_across_streams(monkeypatch, corpus, tmp_path):
    """A doc returned in BOTH the semantic `results` stream and the lexical
    `learnings` stream shares the single `seen` set and must be emitted ONCE.

    The results loop adds the doc to `seen`; the learnings loop must skip it via
    `doc_id in seen`. Without the shared dedup the same canonical doc-id would be
    double-counted in ranked_results (inflating recall)."""
    import membench.adapters.minni_adapter as mod
    from membench.adapters.minni_adapter import _mark_content
    from membench.contract import TokenBudget

    adapter = MinniAdapter()
    _prime_adapter_for_query(adapter, corpus, tmp_path)
    doc_ids = list(corpus.doc_ids())
    dup_id = doc_ids[0]
    # Same doc in BOTH streams: semantic hit + a learning carrying its marker.
    results = [{"metadata": {"membench_doc_id": dup_id}, "score": 0.8}]
    learnings = [{"content": _mark_content(dup_id, "body for dup")}]
    monkeypatch.setattr(
        mod, "_rpc", lambda *a, **k: {"results": results, "learnings": learnings}
    )
    wide = TokenBudget(max_tokens=100_000, max_docs=10)
    try:
        result = adapter.query("anything", wide)
        ids = [rd.doc_id for rd in result.ranked_results]
        assert ids == [dup_id], f"shared doc must be emitted once; got {ids}"
        assert ids.count(dup_id) == 1
    finally:
        adapter._corpus = None
        adapter.teardown()


def test_minni_live_roundtrip(corpus, budget, tmp_path):
    """Full contract round-trip through an isolated throwaway daemon.

    SKIPs (does not fail) if the daemon cannot be stood up headlessly.
    """
    adapter = MinniAdapter()
    try:
        try:
            report = adapter.ingest(corpus)
        except (MinniStandupError, OSError, ConnectionError) as exc:
            # Only daemon-standup / socket-not-ready failures SKIP. A genuine
            # adapter bug (AttributeError, ImportError, TypeError, …) must
            # propagate and FAIL the test, not be silently hidden.
            pytest.skip(f"isolated minnid standup infeasible here: {exc}")

        # Data-safety: the live socket/DB must NOT exist as the adapter's target.
        assert adapter._socket_path is not None
        assert Path(os.path.realpath(adapter._socket_path)) != Path(
            os.path.realpath(_LIVE_SOCKET)
        )
        assert adapter._tmp_home is not None
        # The load-bearing safety property: the throwaway home is NOT the live
        # ~/.minni home (nor under it). Containment-under-system-tempdir is a
        # secondary sanity check, not the safety guarantee.
        live_home_real = os.path.realpath(Path.home() / ".minni")
        home_real = os.path.realpath(adapter._tmp_home)
        assert home_real != live_home_real
        assert not home_real.startswith(live_home_real + os.sep)
        # Sanity: it should live under the OS temp root.
        tmp_root = os.path.realpath(__import__("tempfile").gettempdir())
        assert home_real.startswith(tmp_root + os.sep)

        assert report.doc_count == len(corpus.doc_ids())

        result = adapter.query("Aurora Protocol", budget)
        # The round-trip must complete and return a well-formed QueryResult
        # through the contract. The throwaway daemon ingests through the real
        # governance path (learn -> resolve_candidate accept) and serves retrieval
        # via the daemon's LEXICAL FTS5 learnings index (see the adapter docstring
        # for the retrieval-mode disclosure). Post-fix, that path RETRIEVES: the
        # over-count and recall tests enforce non-empty retrieval, and this
        # roundtrip — which has already paid the daemon-standup cost — asserts it
        # too with a distinctive verbatim probe ("Aurora Protocol").
        assert isinstance(result, QueryResult)
        assert_well_formed(result, corpus, budget)
        assert isinstance(result.refused, bool)
        assert isinstance(result.wall_clock_ms, float)
        assert len(result.ranked_results) > 0, (
            "live throwaway daemon must RETRIEVE through the lexical learnings "
            f"path, not return empty; ranked_results={result.ranked_results!r}"
        )
        # NOTE: we do NOT assert refused == (ranked_results == []). Per §3.1
        # `refused` means an EXPLICIT governance decline, which is distinct from
        # an ordinary zero-hit retrieval. The current search RPC carries no
        # gate-fired flag, so an empty result is reported as refused=False (a
        # plain miss), not a refusal (see adapter + review finding #2).
        if result.refused:
            # An explicit refusal must come with no ranked results.
            assert result.ranked_results == []
    finally:
        adapter.teardown()
        # teardown() then query() must raise (teardown contract).
        from membench.contract import TeardownError

        with pytest.raises(TeardownError):
            adapter.query("anything", budget)


def test_ingest_raises_when_learn_returns_no_candidate_id(
    monkeypatch, corpus, tmp_path
):
    """If learn returns no candidate_id, the doc was NOT staged for promotion;
    ingest must RAISE MinniStandupError rather than silently dropping the doc and
    over-counting doc_count (finding #9 / finding #2). No live daemon needed: we
    stub the daemon standup and the learn RPC."""
    import membench.adapters.minni_adapter as mod

    adapter = MinniAdapter()

    def _fake_spawn(self):
        self._socket_path = tmp_path / "fake.sock"
        self._proc = None  # nothing to terminate

    monkeypatch.setattr(mod.MinniAdapter, "_spawn_daemon", _fake_spawn)
    # learn returns {} (no candidate_id); resolve_candidate would never be reached.
    monkeypatch.setattr(mod, "_rpc", lambda *a, **k: {})

    try:
        with pytest.raises(MinniStandupError) as exc:
            adapter.ingest(corpus)
        assert "candidate_id" in str(exc.value)
    finally:
        adapter._corpus = None
        adapter.teardown()


def test_ingest_tears_down_prior_daemon_before_respawn(
    monkeypatch, corpus, tmp_path
):
    """ingest() called TWICE on one adapter must tear down the prior throwaway
    daemon and spawn a FRESH one over a clean temp home (review finding #6, the
    cross-trial-contamination fix). Asserts: (a) the second spawn uses a different
    temp home, (b) the first daemon process was terminated, (c) the first temp
    home was deleted. No live daemon: _spawn_daemon and _rpc are stubbed."""
    import tempfile

    import membench.adapters.minni_adapter as mod

    adapter = MinniAdapter()
    spawned_homes: list[Path] = []

    class _FakeProc:
        def __init__(self, tag):
            self.tag = tag
            self.terminated = False

        def terminate(self):
            self.terminated = True

        def wait(self, timeout=None):
            return 0

        def kill(self):  # pragma: no cover - terminate succeeds in this stub
            self.terminated = True

    fake_procs: list[_FakeProc] = []

    def _fake_spawn(self):
        # Mirror the real _spawn_daemon's bookkeeping: allocate a fresh temp home,
        # record it, and attach a fake process + socket so teardown has something
        # to terminate/clean.
        home = Path(tempfile.mkdtemp(prefix="membench-fake-home-"))
        (home / "run").mkdir(parents=True, exist_ok=True)
        self._tmp_home = home
        self._socket_path = home / "run" / "minnid.sock"
        proc = _FakeProc(tag=len(fake_procs))
        fake_procs.append(proc)
        self._proc = proc
        spawned_homes.append(home)

    monkeypatch.setattr(mod.MinniAdapter, "_spawn_daemon", _fake_spawn)
    # learn -> candidate, resolve_candidate -> {} so the ingest loop promotes.
    # candidate_id MUST be an int: the adapter now rejects a non-integer
    # candidate_id before forwarding into resolve_candidate (review finding #2).
    monkeypatch.setattr(
        mod, "_rpc", lambda sock, method, params, **k: (
            {"candidate_id": 1, "status": "proposed"}
            if method == "learn"
            else {}
        ),
    )

    expected_doc_count = len(list(corpus.doc_ids()))
    try:
        report1 = adapter.ingest(corpus)
        # IngestReport.doc_count must equal the number of promoted docs (every
        # corpus doc is promoted by the stub above) — guards against over/under
        # counting on the fresh-daemon-per-trial path (review finding #5).
        assert report1.doc_count == expected_doc_count, (
            f"doc_count {report1.doc_count} != promoted {expected_doc_count}"
        )
        first_home = spawned_homes[0]
        first_proc = fake_procs[0]
        assert first_home.exists()

        report2 = adapter.ingest(corpus)
        # The respawn over a clean temp home must report the same promoted count.
        assert report2.doc_count == expected_doc_count, (
            f"doc_count {report2.doc_count} != promoted {expected_doc_count}"
        )
        second_home = spawned_homes[1]

        # (a) different temp home each ingest (clean DB per trial)
        assert first_home != second_home
        # (b) the first daemon process was terminated before respawn
        assert first_proc.terminated, "prior daemon must be terminated on re-ingest"
        # (c) the first temp home was deleted
        assert not first_home.exists(), "prior temp home must be cleaned up"
    finally:
        adapter._corpus = None
        adapter.teardown()


def test_minni_overcount_crosscheck_unique_uuid(corpus, budget):
    """§9.5 over-count cross-check against the REAL retrieval system.

    A query for the unique UUID must surface the one doc that contains it,
    proving the adapter actually RETRIEVED the indexed content through the real
    daemon+gate path rather than self-reporting doc_count. Run against
    MinniAdapter (NOT the stub: against a lexical stub UUID-in-query/UUID-in-doc
    is a tautology).

    RETRIEVAL MODE: this rides Minni's PUBLIC governance path (learn ->
    resolve_candidate(accept) -> search) and is therefore served by the daemon's
    LEXICAL FTS5 learnings index (see minni_adapter docstring) — a genuine
    retrieval through the daemon, not a corpus reach-around. Only daemon-standup
    failures SKIP; a stood-up daemon that does NOT return the UUID doc is a real
    FAILURE (the over-count cross-check no longer self-defeats by skipping on a
    zero-retrieval result — that was the bug this test was un-skipped to catch).
    """
    from membench import config

    adapter = MinniAdapter()
    try:
        try:
            adapter.ingest(corpus)
        except (MinniStandupError, OSError, ConnectionError) as exc:
            pytest.skip(f"isolated minnid standup infeasible here: {exc}")

        result = adapter.query(config.FIXTURE_UNIQUE_UUID, budget)
        ids = [rd.doc_id for rd in result.ranked_results]
        # The unique UUID lives in EXACTLY one fixture doc; retrieving it proves
        # the daemon indexed the ingested content. No skip-on-empty escape hatch.
        assert "03-teal-ledger.md" in ids, (
            "minni did not retrieve the unique-UUID doc through the real daemon "
            f"recall path; ranked_results doc_ids={ids!r}"
        )
        # The retrieving doc must NOT also be over-counted: it is one ranked hit.
        assert ids.count("03-teal-ledger.md") == 1
    finally:
        adapter.teardown()


def test_minni_recall_over_gold_is_nonempty(corpus, budget):
    """Minni recall@k over the fixture gold set is > 0 — it actually retrieves
    relevant docs through the real daemon, not an empty/None result.

    RETRIEVAL MODE (honest): Minni's public governance ingest path serves
    retrieval via the daemon's LEXICAL FTS5 learnings index (see the adapter
    docstring). Lexical FTS5 implicit-ANDs query terms, so a full natural-language
    question often misses; we therefore probe with DISTINCTIVE gold-derived terms
    that appear verbatim in the gold doc — the SAME query string is handed to the
    daemon's own retriever (no rewrite that rigs a semantic score). Each probe's
    gold doc is asserted to land in the top-k ranked results, and aggregate
    recall@k is asserted > 0. SKIPs only if the isolated daemon cannot stand up.
    """
    from membench import config as _cfg
    from membench.metrics import recall_at_k

    # (query, gold_doc_id) probes grounded in the lexical retrieval the daemon
    # genuinely serves (verified against the live throwaway daemon). Distinctive
    # multi-word terms that occur verbatim in exactly the target doc's body.
    probes = [
        (_cfg.FIXTURE_UNIQUE_UUID, "03-teal-ledger.md"),
        ("witness quorum", "04-witness-quorum.md"),
        ("Borealis handshake", "02-borealis-handshake.md"),
        ("Lindgren team", "06-lindgren-team.md"),
        ("seal timeout", "05-seal-timeout.md"),
        ("backoff policy", "09-backoff-policy.md"),
        ("Aurora Protocol", "01-aurora-protocol.md"),
        ("digest format", "10-digest-format.md"),
    ]

    adapter = MinniAdapter()
    try:
        try:
            adapter.ingest(corpus)
        except (MinniStandupError, OSError, ConnectionError) as exc:
            pytest.skip(f"isolated minnid standup infeasible here: {exc}")

        recalls = []
        for query, gold_id in probes:
            result = adapter.query(query, budget)
            assert_well_formed(result, corpus, budget)
            ids = [rd.doc_id for rd in result.ranked_results]
            recalls.append(recall_at_k(ids, {gold_id}, k=_cfg.K))
            assert gold_id in ids, (
                f"minni recall miss in lexical mode: query={query!r} "
                f"gold={gold_id!r} ranked={ids!r}"
            )

        mean_recall = sum(recalls) / len(recalls)
        assert mean_recall > 0.0, (
            "minni retrieved NOTHING relevant across the gold probes "
            f"(mean recall@{_cfg.K}={mean_recall}); retrieval is empty"
        )
        # Stronger than the >0 floor the task requires: every distinctive probe
        # should retrieve its gold doc in lexical mode.
        assert mean_recall == 1.0, (
            f"expected full lexical recall on distinctive probes; got {mean_recall}"
        )
    finally:
        adapter.teardown()
