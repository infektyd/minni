#!/usr/bin/env python3
"""
sovrd.py — Sovereign Memory daemon (Phase 2)
HTTP server over Unix socket for OpenClaw plugin bridge.

SEC-001 hardened endpoints (the only routes accepted):
  GET  /health
  GET  /status
  POST /read
  POST /learn

All other paths/methods return 404.

Authentication (SEC-001 temporary local-capability scheme):
  /read and /learn require an `Authorization: Bearer <token>` header where
  <token> matches the contents of ~/.sovereign-memory/run/openclaw.token.
  This is a TEMPORARY local-capability token. Phase B (SEC-002) will replace
  it with a runtime-stamped EffectivePrincipal resolved from process/session
  config. Do not build new policy on this token; treat it strictly as a
  stop-gap that proves the caller can read a 0600 file in the user's home.
"""

import argparse
import hashlib
import json
import os
import secrets
import sqlite3
import socket
import sys
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

# Add sovereign-memory engine to path
ENGINE_PATH = os.path.expanduser("~/.openclaw/sovereign-memory-v3.1")
if ENGINE_PATH not in sys.path:
    sys.path.insert(0, ENGINE_PATH)

# SovereignAgent is optional at import time so the hardening tests can exercise
# the HTTP perimeter without the full engine being installed. Real deployments
# always have it on sys.path.
try:
    from agent_api import SovereignAgent  # type: ignore
except Exception:  # pragma: no cover - exercised only when engine missing
    SovereignAgent = None  # type: ignore

# ---------------------------------------------------------------------------
# Paths and capability token
# ---------------------------------------------------------------------------

RUN_DIR = Path.home() / ".sovereign-memory" / "run"
SOCKET_PATH = str(RUN_DIR / "openclaw.sock")
TOKEN_PATH = RUN_DIR / "openclaw.token"
DB_PATH = os.path.expanduser("~/.openclaw/sovereign_memory.db")

# Hard cap on inbound POST bodies (SEC-001 #4). 64 KiB matches the engine
# MAX_LEARN_CHARS soft cap and is plenty for a learn payload or a read path.
MAX_BODY_BYTES = 64 * 1024

# Default read-root allowlist. The daemon CLI may extend this with one or
# more --allowed-read-root flags.
DEFAULT_ALLOWED_READ_ROOTS = [Path.home() / "sovereignMemory"]

# Mutable runtime state populated by `run_server`. Tests inject directly.
ALLOWED_READ_ROOTS: list[Path] = []
AUTH_TOKEN: str = ""

# LRU cache of SovereignAgent instances keyed by agent_id
_agent_instances: dict[str, "SovereignAgent"] = {}
_agent_instances_lock = threading.Lock()


def ensure_run_dir(run_dir: Path = RUN_DIR) -> Path:
    """Create the run directory at mode 0700 (SEC-001 #1)."""
    run_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(run_dir, 0o700)
    return run_dir


def ensure_token(token_path: Path = TOKEN_PATH) -> str:
    """Ensure the local-capability token file exists at mode 0600 and return it.

    SEC-001 #2 — temporary local-capability scheme. Phase B replaces this with
    a runtime-stamped EffectivePrincipal.
    """
    ensure_run_dir(token_path.parent)
    if not token_path.exists():
        # 32 bytes of randomness via secrets.token_urlsafe; the resulting
        # string is ~43 chars, URL-safe base64. Write atomically through a
        # 0600 fd so a brief 0644 window can't open between create and chmod.
        token = secrets.token_urlsafe(32)
        fd = os.open(str(token_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w") as f:
                f.write(token)
        except Exception:
            try:
                os.unlink(token_path)
            except FileNotFoundError:
                pass
            raise
    # Re-chmod even if pre-existing — the file may have been created loosely.
    os.chmod(token_path, 0o600)
    return token_path.read_text().strip()


def _parse_allowed_roots(extra_roots: list[str] | None) -> list[Path]:
    """Resolve the read-root allowlist. Missing roots are kept in the list so
    the daemon can warn but they will never match a resolved request path.
    """
    roots: list[Path] = []
    for r in DEFAULT_ALLOWED_READ_ROOTS:
        roots.append(Path(r))
    for r in extra_roots or []:
        roots.append(Path(r).expanduser())
    # Resolve each (non-strict — root may not exist yet) so containment checks
    # compare realpath-against-realpath.
    resolved: list[Path] = []
    for r in roots:
        try:
            resolved.append(r.resolve(strict=False))
        except Exception:
            resolved.append(r)
    return resolved


def _path_is_contained(candidate: Path, roots: list[Path]) -> bool:
    """True iff `candidate` (already resolved) is equal to or a child of any
    root in `roots` (also resolved)."""
    cand = str(candidate)
    for root in roots:
        root_s = str(root)
        # Match exact root or child via os.sep boundary.
        if cand == root_s or cand.startswith(root_s + os.sep):
            return True
    return False


def _safe_resolve_read_path(raw: str, roots: list[Path]) -> Path:
    """Validate and resolve a /read path. Raises ValueError with a stable
    message on rejection so the handler can pick the right HTTP status.

    Error tags used by the handler:
      - "bad-path"    -> 400 (syntactically rejected fast-path)
      - "not-found"   -> 404 (path does not exist)
      - "forbidden"   -> 403 (resolved outside allowlist)
    """
    if not isinstance(raw, str) or not raw:
        raise ValueError("bad-path")
    # Fast-path rejection: any traversal segment or non-printable char.
    if ".." in raw.split("/") or ".." in raw.split(os.sep):
        raise ValueError("bad-path")
    if any((ord(c) < 0x20 or ord(c) == 0x7F) for c in raw):
        raise ValueError("bad-path")
    p = Path(raw).expanduser()
    try:
        # strict=True: must exist. Symlinks are resolved.
        resolved = p.resolve(strict=True)
    except FileNotFoundError:
        raise ValueError("not-found")
    except (OSError, RuntimeError):
        raise ValueError("bad-path")
    if not _path_is_contained(resolved, roots):
        raise ValueError("forbidden")
    if not resolved.is_file():
        raise ValueError("not-found")
    return resolved


# ---------------------------------------------------------------------------
# Engine adapters (unchanged from prior sovrd.py)
# ---------------------------------------------------------------------------


def get_agent(agent_id: str = "hermes") -> "SovereignAgent":
    """Lazily initialize a SovereignAgent per agent_id (shared across calls)."""
    if SovereignAgent is None:
        raise RuntimeError("SovereignAgent engine not available on sys.path")
    if agent_id not in _agent_instances:
        with _agent_instances_lock:
            if agent_id not in _agent_instances:
                _agent_instances[agent_id] = SovereignAgent(agent_id=agent_id)
    return _agent_instances[agent_id]


def _content_hash(text: str) -> str:
    """SHA-256 hash of normalized text for dedup."""
    normalized = " ".join(text.lower().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _infer_layer(agent_id: str, category: str = "general", path: str = "") -> str:
    """Infer the memory layer from context."""
    if category in ("identity", "self") or "SOUL" in path or "IDENTITY" in path:
        return "identity"
    if category == "episodic":
        return "episodic"
    return "knowledge"


def _read_file_text(file_path: str) -> str:
    """Read full file content from disk. Returns empty string on failure."""
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


def _learn(content: str, agent_id: str = "hermes", category: str = "general",
           content_hash: str = None, workspace_id: str = "") -> dict:
    """Learn with content-hash dedup and metadata tagging."""
    if not content_hash:
        content_hash = _content_hash(content)

    if _is_duplicate(agent_id, content_hash, content):
        return {"status": "duplicate", "hash": content_hash}

    a = get_agent(agent_id)
    inferred_layer = _infer_layer(agent_id, category)
    result = a.learn(content, category=category)
    _write_chunk_metadata(
        agent_id=agent_id,
        workspace_id=workspace_id,
        content_hash=content_hash,
        layer=inferred_layer,
        content=content,
    )
    return {"status": "learned", "result": result, "hash": content_hash, "layer": inferred_layer}


def _is_duplicate(agent_id: str, content_hash: str, content: str = "") -> bool:
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        try:
            c.execute(
                "SELECT COUNT(*) as cnt FROM chunk_embeddings WHERE content_hash = ? AND doc_id IN "
                "(SELECT doc_id FROM documents WHERE agent LIKE ? OR agent = ?)",
                (content_hash, f"%{agent_id}%", agent_id)
            )
            row = c.fetchone()
            if row and row["cnt"] > 0:
                conn.close()
                return True
        except sqlite3.OperationalError:
            pass
        if content:
            c.execute(
                "SELECT content FROM learnings WHERE agent_id = ? AND superseded_by IS NULL",
                (agent_id,)
            )
            for row in c.fetchall():
                if _content_hash(row["content"]) == content_hash:
                    conn.close()
                    return True
        conn.close()
        return False
    except Exception:
        return False


def _write_chunk_metadata(agent_id: str, workspace_id: str, content_hash: str,
                          layer: str, content: str):
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute(
            "SELECT doc_id FROM documents WHERE agent = ? ORDER BY indexed_at DESC LIMIT 1",
            (agent_id,)
        )
        row = c.fetchone()
        if not row:
            conn.close()
            return
        doc_id = row[0]
        now = time.time()
        try:
            c.execute(
                "UPDATE documents SET workspace_id = ?, agent = ?, layer = ? WHERE doc_id = ?",
                (workspace_id, agent_id, layer, doc_id)
            )
        except sqlite3.OperationalError:
            pass
        try:
            c.execute(
                "UPDATE chunk_embeddings SET content_hash = ?, is_code = ?, truncated = 0, "
                "learned_at = ? WHERE doc_id = ?",
                (content_hash, 1 if "```" in content else 0, now, doc_id)
            )
        except sqlite3.OperationalError:
            pass
        conn.commit()
        conn.close()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------


class SovereignHandler(BaseHTTPRequestHandler):
    """HTTP request handler. Strict method+path allowlist (SEC-001 #5)."""

    # The four endpoints accepted by the daemon. (method, path) -> handler-attr
    _ALLOWED = {
        ("GET", "/health"),
        ("GET", "/status"),
        ("POST", "/read"),
        ("POST", "/learn"),
    }

    # Endpoints that require the local-capability bearer token.
    _AUTH_REQUIRED = {("POST", "/read"), ("POST", "/learn")}

    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        """Suppress default logging."""
        pass

    # -- response helpers --------------------------------------------------

    def _send_json(self, data, status=200):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _reject(self, status: int, message: str):
        self._send_json({"error": message}, status)

    # -- request validation -----------------------------------------------

    def _check_auth(self) -> bool:
        """Validate the Authorization: Bearer <token> header. Sends 401 and
        returns False on failure."""
        header = self.headers.get("Authorization", "")
        prefix = "Bearer "
        if not header.startswith(prefix):
            self._reject(401, "Missing bearer token")
            return False
        provided = header[len(prefix):].strip()
        # constant-time compare to avoid token length leaks
        if not AUTH_TOKEN or not secrets.compare_digest(provided, AUTH_TOKEN):
            self._reject(401, "Invalid bearer token")
            return False
        return True

    def _read_body(self) -> bytes | None:
        """Read the request body honoring the 64 KiB cap and refusing chunked
        transfer encoding. Returns None and sends a response on rejection."""
        if "chunked" in self.headers.get("Transfer-Encoding", "").lower():
            self._reject(411, "Chunked transfer encoding not supported")
            return None
        cl_raw = self.headers.get("Content-Length")
        if cl_raw is None:
            self._reject(411, "Content-Length required")
            return None
        try:
            content_length = int(cl_raw)
        except ValueError:
            self._reject(400, "Invalid Content-Length")
            return None
        if content_length < 0:
            self._reject(400, "Invalid Content-Length")
            return None
        if content_length > MAX_BODY_BYTES:
            self._reject(413, "Body too large")
            return None
        if content_length == 0:
            return b""
        return self.rfile.read(content_length)

    # -- dispatch ----------------------------------------------------------

    def _dispatch(self, method: str):
        parsed = urlparse(self.path)
        path = parsed.path
        if (method, path) not in self._ALLOWED:
            # Drain any small body so the client's write half doesn't break.
            self._drain_body_if_small()
            self._reject(404, f"Unknown endpoint: {method} {path}")
            return
        # For POST endpoints we read the body up-front so we can both enforce
        # the size cap and avoid leaving an unread body on the socket when we
        # early-reject (which can RST the client mid-write on Unix sockets).
        body: bytes | None = None
        if method == "POST":
            body = self._read_body()
            if body is None:
                return  # _read_body already responded
        if (method, path) in self._AUTH_REQUIRED and not self._check_auth():
            return
        handler_name = f"_handle_{method.lower()}_{path.strip('/').replace('/', '_')}"
        handler = getattr(self, handler_name, None)
        if handler is None:  # pragma: no cover — _ALLOWED guards this
            self._reject(404, f"Unknown endpoint: {method} {path}")
            return
        try:
            if method == "POST":
                handler(parsed, body)
            else:
                handler(parsed)
        except Exception as e:  # pragma: no cover - defensive
            self._reject(500, str(e))

    def _drain_body_if_small(self):
        """Best-effort drain so the kernel doesn't RST the client mid-write
        when we early-reject. Skipped for oversize bodies (those get 413)."""
        cl_raw = self.headers.get("Content-Length")
        if cl_raw is None:
            return
        try:
            cl = int(cl_raw)
        except ValueError:
            return
        if cl <= 0 or cl > MAX_BODY_BYTES:
            return
        try:
            self.rfile.read(cl)
        except Exception:
            pass

    def do_GET(self):  # noqa: N802 - stdlib name
        self._dispatch("GET")

    def do_POST(self):  # noqa: N802 - stdlib name
        self._dispatch("POST")

    # Reject everything else explicitly so curl -X DELETE / etc see 404.
    def do_PUT(self):  # noqa: N802
        self._reject(404, "Unknown endpoint")

    def do_DELETE(self):  # noqa: N802
        self._reject(404, "Unknown endpoint")

    def do_PATCH(self):  # noqa: N802
        self._reject(404, "Unknown endpoint")

    def do_OPTIONS(self):  # noqa: N802
        self._reject(404, "Unknown endpoint")

    def do_HEAD(self):  # noqa: N802
        self._reject(404, "Unknown endpoint")

    # -- handlers ----------------------------------------------------------

    def _handle_get_health(self, parsed):
        self._send_json({"status": "ok", "agent": "shared-daemon"})

    def _handle_get_status(self, parsed):
        self._send_json({
            "status": "ok",
            "socket": SOCKET_PATH,
            "allowed_read_roots": [str(r) for r in ALLOWED_READ_ROOTS],
        })

    def _handle_post_read(self, parsed, body: bytes):
        try:
            data = json.loads(body or b"{}")
        except json.JSONDecodeError:
            self._reject(400, "Invalid JSON")
            return
        raw = data.get("path") or data.get("key")
        if not raw:
            self._reject(400, "Missing 'path'")
            return
        try:
            resolved = _safe_resolve_read_path(raw, ALLOWED_READ_ROOTS)
        except ValueError as e:
            tag = str(e)
            if tag == "bad-path":
                self._reject(400, "Invalid path")
            elif tag == "forbidden":
                self._reject(403, "Path outside allowlist")
            else:  # not-found
                self._reject(404, "File not found")
            return
        text = _read_file_text(str(resolved))
        if not text:
            self._reject(404, "File not found")
            return
        self._send_json({"text": text, "path": str(resolved)})

    def _handle_post_learn(self, parsed, body: bytes):
        try:
            data = json.loads(body or b"{}")
        except json.JSONDecodeError:
            self._reject(400, "Invalid JSON")
            return
        content = data.get("content")
        if not content:
            self._reject(400, "Missing 'content' field")
            return
        category = data.get("category", "general")
        agent_id = data.get("agent_id") or "hermes"
        workspace_id = data.get("workspace_id", "")
        content_hash = data.get("content_hash")
        try:
            result = _learn(
                content,
                agent_id=agent_id,
                category=category,
                content_hash=content_hash,
                workspace_id=workspace_id,
            )
            self._send_json(result)
        except Exception as e:
            self._reject(500, str(e))


# ---------------------------------------------------------------------------
# Server / startup
# ---------------------------------------------------------------------------


class UnixHTTPServer(HTTPServer):
    """HTTPServer that listens on a Unix domain socket with 0600 perms."""
    address_family = socket.AF_UNIX

    def server_bind(self):
        if os.path.exists(self.server_address):
            os.unlink(self.server_address)
        self.socket.bind(self.server_address)
        # SEC-001 #1: lock the socket down before server_activate() so no
        # connection can race in at the default umask-derived mode.
        os.chmod(self.server_address, 0o600)
        self.server_name = "localhost"
        self.server_port = 0


def build_server(socket_path: str = SOCKET_PATH,
                 allowed_roots: list[Path] | None = None,
                 token: str | None = None) -> UnixHTTPServer:
    """Construct (but do not yet serve) the daemon. Used by tests too."""
    global ALLOWED_READ_ROOTS, AUTH_TOKEN
    ensure_run_dir(Path(socket_path).parent)
    AUTH_TOKEN = token if token is not None else ensure_token()
    ALLOWED_READ_ROOTS = (
        [Path(r).resolve(strict=False) for r in allowed_roots]
        if allowed_roots is not None
        else _parse_allowed_roots(None)
    )
    return UnixHTTPServer(socket_path, SovereignHandler)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Sovereign Memory OpenClaw bridge")
    p.add_argument(
        "--allowed-read-root",
        action="append",
        default=[],
        help="Additional directory allowed for /read (may be repeated).",
    )
    p.add_argument("--socket-path", default=SOCKET_PATH)
    return p.parse_args(argv)


def run_server(argv: list[str] | None = None):
    args = _parse_args(argv)
    global ALLOWED_READ_ROOTS, AUTH_TOKEN
    ALLOWED_READ_ROOTS = _parse_allowed_roots(args.allowed_read_root)
    AUTH_TOKEN = ensure_token()
    server = UnixHTTPServer(args.socket_path, SovereignHandler)
    print(f"sovrd listening on {args.socket_path}", flush=True)
    print(f"sovrd token at {TOKEN_PATH}", flush=True)
    print(f"sovrd allowed read roots: {[str(r) for r in ALLOWED_READ_ROOTS]}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    run_server()
