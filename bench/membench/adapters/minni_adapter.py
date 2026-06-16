"""The ``minni`` adapter — talks to Minni ONLY over its public socket (§4, §7.5).

CRITICAL SAFETY (S1 scope item 7): this adapter MUST NEVER touch the operator's
LIVE daemon, live socket (``~/.minni/run/minnid.sock``), or live DB
(``~/.minni/minni.db``). It stands up a DEDICATED, THROWAWAY ``minnid`` with its
OWN temp socket + temp ``MINNI_HOME`` (temp DB + temp vault), ingests the frozen
corpus, answers queries, then tears it down.

ISOLATION (fairness §7.5): the adapter reaches Minni ONLY through the public
Unix-socket JSON-RPC protocol — the same interface any external client uses. It
imports NOTHING from ``engine/`` or ``plugins/``. The JSON-RPC envelope is
re-implemented here (it is a stable public wire format) rather than imported, to
keep ``bench/`` structurally isolated.

If standing up an isolated daemon headlessly is infeasible in this environment,
the live round-trip is SKIPPED (not failed) with a clear reason — the contract,
corpus loader, and token enforcement are still proven by the deterministic stub
adapter so the suite stays green (spec-sanctioned fallback).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .. import config
from ..contract import (
    FrozenCorpus,
    IngestReport,
    PreIngestError,
    QueryResult,
    RankedDoc,
    TeardownError,
    TokenBudget,
)

# Cap on bytes accepted from the daemon over the socket. A valid JSON-RPC
# response in this protocol is kilobytes; 4 MB is far beyond that. Bounds memory
# against a crash-looping or rogue process on the socket path.
MAX_RESPONSE_BYTES = 4 * 1024 * 1024

# Local-path redaction for anything we surface in an exception (pytest output,
# logs). Mirrors minnid.py's _LOCAL_PATH_PATTERN but is defined here to keep
# bench/ structurally isolated from engine/ (§7.5). Applied to raw daemon
# stdout/stderr before embedding — the daemon does NOT redact what it writes to
# the subprocess pipe.
_LOCAL_PATH_PATTERN = re.compile(
    r"(?:/Users/[^ \n\r\t\"'<>]+"
    r"|/Volumes/[^ \n\r\t\"'<>]+"
    r"|/private/[^ \n\r\t\"'<>]+"
    r"|/var/folders/[^ \n\r\t\"'<>]+"  # macOS per-user temp (TMPDIR / mkdtemp)
    r"|/var/[^ \n\r\t\"'<>]+"
    r"|/home/[^ \n\r\t\"'<>]+"  # Linux CI runners (e.g. /home/runner/)
    r"|/opt/[^ \n\r\t\"'<>]+"  # Homebrew / opt installs (e.g. /opt/homebrew/)
    r"|/root/[^ \n\r\t\"'<>]+"  # Linux root home
    r"|/tmp/[^ \n\r\t\"'<>]+)"
)


def _redact(text: str) -> str:
    """Redact local filesystem paths from text bound for an exception/log."""
    return _LOCAL_PATH_PATTERN.sub("[REDACTED_PATH]", text)

# Live paths the adapter must NEVER use. Asserted against at spawn time as a
# belt-and-suspenders data-safety guard.
_LIVE_HOME = Path.home() / ".minni"
_LIVE_SOCKET = _LIVE_HOME / "run" / "minnid.sock"
_LIVE_DB = _LIVE_HOME / "minni.db"

# Path to the engine's minnid entrypoint and its venv python. The adapter
# *launches* these as a subprocess (a public process boundary), it does not
# import them — so isolation (§7.5) holds.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_ENGINE_DIR = _REPO_ROOT / "engine"
_MINNID_PY = _ENGINE_DIR / "minnid.py"
_VENV_PYTHON = _ENGINE_DIR / ".venv" / "bin" / "python"


class MinniStandupError(RuntimeError):
    """Raised when the isolated throwaway daemon cannot be stood up.

    The live round-trip test catches this and SKIPs with the message as reason.
    """


def _rpc(socket_path: Path, method: str, params: dict, timeout: float = 30.0) -> dict:
    """Send one JSON-RPC request over the public Unix-socket protocol."""
    request = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    payload = (json.dumps(request) + "\n").encode("utf-8")
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(str(socket_path))
        s.sendall(payload)
        chunks: list[bytes] = []
        total_bytes = 0
        while True:
            chunk = s.recv(8192)
            if not chunk:
                break
            chunks.append(chunk)
            total_bytes += len(chunk)
            # Bound memory against a rogue/crash-looping sender (§ data-safety).
            if total_bytes > MAX_RESPONSE_BYTES:
                raise MinniStandupError(
                    f"daemon response exceeded {MAX_RESPONSE_BYTES} bytes for "
                    f"{method!r} — aborting (possible rogue process on socket)."
                )
            if b"\n" in chunk:
                break
    finally:
        s.close()
    data = b"".join(chunks)
    if not data:
        raise MinniStandupError(f"empty response from daemon for {method!r}")
    try:
        resp = json.loads(data.decode("utf-8"))
    except json.JSONDecodeError as exc:
        # The recv loop stops at the first newline; a daemon error whose message
        # embeds a newline (e.g. a multi-line traceback) can yield a truncated,
        # unparseable buffer. Redact + truncate before surfacing so raw socket
        # bytes (internal paths, stack traces) never leak to pytest/CI output.
        detail = _redact(str(exc))[:500]
        raise MinniStandupError(
            f"unparseable response from daemon for {method!r}: {detail}"
        ) from None
    # A non-dict (list, null, number) is valid JSON but NOT a valid JSON-RPC
    # envelope. Membership (`'error' in resp`) and `resp.get(...)` would raise a
    # raw TypeError/AttributeError, leaking a traceback with bench/ paths. Catch
    # it here and surface a redacted, length-capped message instead.
    if not isinstance(resp, dict):
        detail = _redact(repr(resp))[:200]
        raise MinniStandupError(
            f"non-dict JSON-RPC response from daemon for {method!r}: {detail}"
        )
    if "error" in resp and resp["error"]:
        # Surface ONLY the human-readable summary, truncated — the raw error
        # object can carry stack traces / internal paths the daemon does not
        # redact over the wire.
        err = resp["error"]
        msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
        msg = _redact(str(msg))[:500]
        raise MinniStandupError(f"daemon error on {method!r}: {msg}")
    return resp.get("result", {})


class MinniAdapter:
    """MemoryAdapter backed by an ISOLATED throwaway Minni daemon (§3.1)."""

    name = "minni"

    def __init__(self) -> None:
        self.config_hash = "minni-s1"
        self._proc: subprocess.Popen | None = None
        self._tmp_home: Path | None = None
        self._socket_path: Path | None = None
        self._corpus: FrozenCorpus | None = None
        self._torn_down = False

    # -- isolated daemon lifecycle --------------------------------------
    def _spawn_daemon(self) -> None:
        if not _MINNID_PY.exists():
            raise MinniStandupError(
                f"minnid.py not found at {_redact(str(_MINNID_PY))}"
            )
        python = _VENV_PYTHON if _VENV_PYTHON.exists() else Path(sys.executable)

        tmp_home = Path(tempfile.mkdtemp(prefix="membench-minni-home-"))
        # Record the temp home IMMEDIATELY so teardown() can always clean it up,
        # even if the data-safety guard below raises before subprocess launch.
        self._tmp_home = tmp_home
        run_dir = tmp_home / "run"
        sock = run_dir / "minnid.sock"

        # DATA-SAFETY GUARD: refuse to proceed if ANY path resolves to or inside
        # the operator's live home. This MUST run BEFORE any filesystem mutation
        # under the candidate tmp_home (no mkdir, no write) so a tmp_home that
        # (maliciously/accidentally) resolves inside ~/.minni can NEVER cause a
        # directory to be created in the live home before we abort. The temp home
        # is freshly mkdtemp'd so this is belt-and-suspenders, but the rule is
        # uniform for every path: anything equal to _LIVE_HOME or under it aborts
        # — not just the one exact live socket file. (A temp socket like
        # ~/.minni/run/bench.sock must abort too.)
        live_real = Path(os.path.realpath(_LIVE_HOME))
        live_prefix = str(live_real) + os.sep
        for p, label in ((tmp_home, "home"), (sock, "socket")):
            real = Path(os.path.realpath(p))
            if real == live_real or str(real).startswith(live_prefix):
                raise MinniStandupError(
                    f"refusing: {label} path resolves inside the LIVE home"
                )

        # Guard passed — NOW it is safe to create directories under tmp_home.
        run_dir.mkdir(parents=True, exist_ok=True)

        # Build a MINIMAL environment for the throwaway daemon. The full parent
        # os.environ may carry credential env vars (e.g. MEMBENCH_AGENT_API_KEY,
        # ANTHROPIC_API_KEY) the throwaway has no legitimate need for — passing
        # them risks leaking secrets into the subprocess context (§7.14). We pass
        # only the vars the daemon needs and never anything matching a known
        # credential name.
        _PASSTHROUGH = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR")
        _credential_names = set(config.CREDENTIAL_ENV_VARS.values())
        env = {
            k: os.environ[k]
            for k in _PASSTHROUGH
            if k in os.environ and k not in _credential_names
        }
        env["MINNI_HOME"] = str(tmp_home)
        env["MINNI_VAULT_PATH"] = str(tmp_home / "vault")
        env["MINNI_DB_PATH"] = str(tmp_home / "minni.db")
        env["MINNI_FAISS_PATH"] = str(tmp_home / "minni_faiss.index")
        env["MINNI_AFM_LOOP"] = "off"  # no background loop in the throwaway
        # PYTHONPATH is set to the engine dir ONLY — we do NOT append the parent
        # os.environ['PYTHONPATH']. Inheriting it into the throwaway daemon is an
        # import-hijack vector (a poisoned parent path could shadow engine
        # modules). If other paths are ever needed they must be enumerated here
        # explicitly, never inherited wholesale.
        env["PYTHONPATH"] = str(_ENGINE_DIR)

        proc = subprocess.Popen(
            [str(python), str(_MINNID_PY), "--socket", str(sock)],
            cwd=str(_ENGINE_DIR),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        self._proc = proc
        self._socket_path = sock

        # Wait for the socket to appear and answer ping (bounded).
        deadline = time.time() + 30.0
        while time.time() < deadline:
            if proc.poll() is not None:
                out = proc.stdout.read().decode("utf-8", "replace") if proc.stdout else ""
                # Redact local paths (username under /Users/<name>/, vault/DB
                # paths) before surfacing — the daemon does not redact its raw
                # stdout captured by the subprocess pipe.
                safe_out = _redact(out[-2000:])
                raise MinniStandupError(
                    f"daemon exited early (rc={proc.returncode}):\n{safe_out}"
                )
            if sock.exists():
                try:
                    _rpc(sock, "ping", {}, timeout=5.0)
                    return
                except Exception:
                    pass
            time.sleep(0.25)
        raise MinniStandupError("daemon did not become ready within 30s")

    # -- contract --------------------------------------------------------
    def ingest(self, corpus: FrozenCorpus) -> IngestReport:
        self._require_live()
        start = time.perf_counter()
        if self._proc is None:
            self._spawn_daemon()
        self._corpus = corpus
        assert self._socket_path is not None

        # Ingest each frozen-corpus doc through Minni's PUBLIC governance path:
        # learn (stages a proposed candidate) -> resolve_candidate(accept)
        # (promotes it to a durable learning). We tag the doc-id so query
        # results can be mapped back to canonical doc-ids. This goes through the
        # real gate — no engine internals, no index reach-around (§7.5).
        for doc_id in corpus.doc_ids():
            text = corpus.read(doc_id).decode("utf-8", "replace")
            learned = _rpc(
                self._socket_path,
                "learn",
                {
                    "content": text,
                    "category": "membench_fixture",
                    "metadata": {"membench_doc_id": doc_id},
                },
                timeout=60.0,
            )
            cid = learned.get("candidate_id")
            if cid is not None:
                # Promote through the public operator-gated resolution RPC.
                _rpc(
                    self._socket_path,
                    "resolve_candidate",
                    {
                        "candidate_id": cid,
                        "decision": "accept",
                        "reason": "membench fixture ingest",
                    },
                    timeout=60.0,
                )
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        return IngestReport(
            build_wall_clock_ms=elapsed_ms,
            doc_count=len(corpus.doc_ids()),
            index_size_bytes=0,
            ingest_tokens_used=0,
        )

    def query(self, q: str, budget: TokenBudget) -> QueryResult:
        self._require_live()
        if self._socket_path is None or self._corpus is None:
            raise PreIngestError("query() before ingest()")
        start = time.perf_counter()
        result = _rpc(
            self._socket_path,
            "search",
            {"query": q, "limit": budget.max_docs, "depth": "snippet"},
            timeout=60.0,
        )
        elapsed_ms = (time.perf_counter() - start) * 1000.0

        valid_ids = set(self._corpus.doc_ids())
        ranked: list[RankedDoc] = []
        seen: set[str] = set()
        parts: list[str] = []
        for item in result.get("results", []):
            doc_id = self._map_doc_id(item, valid_ids)
            if doc_id is None or doc_id in seen:
                continue
            seen.add(doc_id)
            score = float(item.get("score", item.get("relevance", 0.0)) or 0.0)
            ranked.append(RankedDoc(doc_id=doc_id, score=score))
            parts.append(self._corpus.read(doc_id).decode("utf-8", "replace").strip())
            if len(ranked) >= budget.max_docs:
                break

        context = "\n\n".join(parts)
        # §3.1: `refused` is True ONLY when the system EXPLICITLY declines (e.g.
        # Minni's gate refuses on insufficient provenance). Prefer an explicit
        # flag from the daemon if the search RPC exposes one; only then is a
        # governance refusal distinguishable from a genuine zero-hit query.
        # NOTE (s1 limitation): the current `search` RPC does NOT yet carry a
        # refusal/gate-fired flag, so when none is present we CANNOT tell an
        # explicit governance refusal apart from an ordinary no-match. We do NOT
        # equate empty-results with refusal here (that mis-codes retrieval
        # failures as governance refusals and inflates false_refusal_rate). We
        # report refused only on an explicit daemon signal; absent that, an empty
        # result is left as refused=False (a plain retrieval miss). Wiring a real
        # gate-fired field into the search response is a follow-up (see review
        # finding #2).
        refused = bool(
            result.get("refused")
            or result.get("declined")
            or result.get("gate_fired")
        )
        return QueryResult(
            ranked_results=ranked,
            context_string=context,
            wall_clock_ms=elapsed_ms,
            refused=refused,
        )

    def teardown(self) -> None:
        self._torn_down = True
        if self._proc is not None:
            try:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=10.0)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
                    self._proc.wait(timeout=5.0)
            except Exception:
                pass
            self._proc = None
        if self._tmp_home is not None and self._tmp_home.exists():
            shutil.rmtree(self._tmp_home, ignore_errors=True)
            self._tmp_home = None
        self._socket_path = None
        self._corpus = None

    # -- helpers ---------------------------------------------------------
    def _require_live(self) -> None:
        if self._torn_down:
            raise TeardownError("adapter used after teardown() (§9.4)")

    def _map_doc_id(self, item: dict, valid_ids: set[str]) -> str | None:
        """Map a search hit back to a canonical corpus doc-id.

        Prefers the membench metadata tag stamped at ingest; falls back to any
        field that already equals a known canonical doc-id. Hits that cannot be
        mapped are dropped (they are not corpus docs).
        """
        meta = item.get("metadata") or {}
        tagged = meta.get("membench_doc_id")
        if tagged in valid_ids:
            return tagged
        for key in ("doc_id", "source", "path", "id"):
            val = item.get(key)
            if isinstance(val, str) and val in valid_ids:
                return val
        return None
