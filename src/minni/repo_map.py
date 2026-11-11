"""Living Repo Maps (Wave 4) — deterministic subsystem map synthesis & git-diff staleness.

Builds concise, cold-agent-oriented Subsystem Map Cards under
``<minni_home>/vault/maps/<workspace_slug>/`` plus ``_index.json``, indexes
cards into ``documents`` / ``vault_fts`` / ``chunk_embeddings`` / ``repo_map_cards``
(and live ``RetrievalEngine`` FAISS/Trie when running inside ``minnid``), and
computes fast git-diff staleness banners with non-blocking background refresh.
"""

from __future__ import annotations

import ast
from collections import defaultdict
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading
import time
from typing import Any, Optional

import yaml

from minni.config import (
    CANONICAL_SOVEREIGN_HOME,
    MinniConfig,
    normalize_workspace_scope,
)
from minni.db import MinniDB

logger = logging.getLogger("sovereign.repo_map")

MAX_AST_FILE_BYTES = 256 * 1024
MAX_TEXT_READ_BYTES = 256 * 1024
MAX_SUBSYSTEMS = 18
FLAT_MODULE_GROUP_THRESHOLD = 12
AUTO_REFRESH_COOLDOWN_SEC = 60.0
AUTO_REFRESH_DEBOUNCE_SEC = 1.5

EXCLUDED_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "dist",
        "build",
        ".build",
        "DerivedData",
        "__pycache__",
        "_private",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".next",
        "coverage",
        ".antigravitycli",
        ".cursor",
    }
)

EXCLUDED_SUFFIXES: tuple[str, ...] = (
    ".pyc",
    ".pyo",
    ".so",
    ".dylib",
    ".dll",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".ico",
    ".pdf",
    ".zip",
    ".tar",
    ".gz",
    ".whl",
    ".lock",
    ".sqlite",
    ".db",
    ".index",
    ".pem",
    ".key",
    ".p12",
    ".pfx",
    ".keystore",
    ".jks",
    ".kdbx",
)

_ALLOWED_DOT_DIRS: frozenset[str] = frozenset({".github", ".githooks"})
_SECRET_CONFIG_SUFFIXES: frozenset[str] = frozenset(
    {
        "",
        ".json",
        ".yaml",
        ".yml",
        ".toml",
        ".ini",
        ".cfg",
        ".conf",
        ".env",
        ".properties",
        ".xml",
        ".txt",
        ".plist",
        ".csv",
    }
)
_RESTRICTED_MARKDOWN_PRIVACIES: frozenset[str] = frozenset({"private", "blocked", "local-only"})
_FRONTMATTER_BLOCK_RE = re.compile(r"\A---\s*\r?\n(.*?)\r?\n---\s*(?:\r?\n|\Z)", re.DOTALL)
_GIT_HEX_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")

# Domain buckets used when a flat package directory (e.g. src/<pkg>/) has >12 sibling modules.
_FLAT_DOMAIN_RULES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "cli",
        "CLI & Client Interfaces",
        ("cli", "client", "mcp", "arg", "command", "console", "watch"),
    ),
    (
        "daemon-runtime",
        "Daemon & RPC Runtime",
        ("minnid", "daemon", "server", "rpc", "ipc", "runtime", "dispatch", "socket", "http"),
    ),
    (
        "retrieval-search",
        "Retrieval, FTS & Vector Search",
        (
            "retriev",
            "search",
            "faiss",
            "index",
            "chunk",
            "embed",
            "rerank",
            "model",
            "trie",
            "fts",
            "query",
            "confidence",
            "repo_map",
        ),
    ),
    (
        "memory-graph",
        "Memory Graph, Threads & Consolidation",
        (
            "graph",
            "episodic",
            "thread",
            "contradict",
            "handoff",
            "consolidat",
            "salience",
            "decay",
            "durable",
            "learning",
            "wiki",
            "link",
        ),
    ),
    (
        "governance-afm",
        "Identity, Governance & Safety",
        (
            "principal",
            "afm",
            "govern",
            "privacy",
            "candidate",
            "trust",
            "audit",
            "policy",
            "security",
            "guard",
            "redact",
            "envelope",
            "instruct",
        ),
    ),
    (
        "storage-schema",
        "SQLite Schema, Migrations & Config",
        ("db", "migrat", "schema", "config", "sqlite", "timestamp", "trace", "budget"),
    ),
    (
        "wire-deploy",
        "Host Wire, Hooks & Diagnostics",
        ("wire", "deploy", "hook", "install", "propagat", "doctor", "health", "hygiene"),
    ),
)

_SWIFT_TS_RS_SYMBOL_RE = re.compile(
    r"^\s*(?:public\s+(?:final\s+)?|export\s+(?:default\s+)?(?:async\s+)?|pub(?:\([^)]*\))?\s+(?:async\s+)?)"
    r"(?:class|struct|actor|enum|protocol|interface|type|function|fn|const)\s+([A-Za-z_][A-Za-z0-9_]*)",
    re.MULTILINE,
)

_EVIDENCE_OPEN_TAG_RE = re.compile(r"^(\s*<EVIDENCE\b[^>]*>)")

_refresh_lock = threading.Lock()
_last_refresh_scheduled: dict[str, float] = {}
_pending_refresh_timers: dict[str, threading.Timer] = {}


def workspace_slug_from_id(workspace_id: str) -> str:
    """Convert ``workspace-foo-bar`` -> ``foo-bar`` safely for filesystem paths."""
    raw = str(workspace_id or "").strip().lower()
    if raw.startswith("workspace-"):
        raw = raw[len("workspace-") :]
    slug = re.sub(r"[^a-z0-9._-]+", "-", raw).strip("-.")
    return slug or "repo"


def _sanitize_subsystem_slug(raw: str) -> str:
    slug = re.sub(r"[^a-z0-9_-]+", "-", raw.lower()).strip("-_")
    if not slug:
        slug = "core"
    # VaultIndexer excludes literal 'index.md'; 'overview.md' is reserved for repo summary.
    if slug == "index":
        return "index-module"
    if slug == "overview":
        return "overview-module"
    return slug


def _human_title(slug: str) -> str:
    parts = [p for p in re.split(r"[-_]+", slug) if p]
    acronyms = {"cli": "CLI", "rpc": "RPC", "afm": "AFM", "fts": "FTS", "db": "DB", "ui": "UI", "api": "API"}
    return " ".join(acronyms.get(p.lower(), p.capitalize()) for p in parts) or "Core"


def _run_git(repo_root: Path, args: list[str], *, timeout: float = 1.5) -> tuple[int, str]:
    repo_root_str = str(repo_root)
    if not repo_root_str or repo_root_str.startswith("-") or not os.path.isabs(repo_root_str):
        return 1, ""
    try:
        proc = subprocess.run(
            ["git", "-C", repo_root_str, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
        )
        return proc.returncode, proc.stdout
    except (OSError, subprocess.SubprocessError):
        return 1, ""


def resolve_repo_root(repo_path: Optional[str | Path] = None) -> Path:
    """Validate and resolve *repo_path* (or git root of ``Path.cwd()`` when omitted).

    Raises ``ValueError`` when *repo_path* does not exist, is not a directory,
    or is a symlink directly escaping its parent.
    """
    if repo_path is not None and str(repo_path).strip():
        raw = Path(repo_path).expanduser()
        if raw.is_symlink():
            raise ValueError(f"Refusing symlinked repository target: {repo_path}")
        if not raw.exists():
            raise ValueError(f"Repository path does not exist: {repo_path}")
        if not raw.is_dir():
            raise ValueError(f"Repository path is not a directory: {repo_path}")
        return raw.resolve()

    cwd = Path.cwd().resolve()
    rc, out = _run_git(cwd, ["rev-parse", "--show-toplevel"], timeout=1.5)
    if rc == 0 and out.strip():
        candidate = Path(out.strip())
        if candidate.exists() and candidate.is_dir() and not candidate.is_symlink():
            return candidate.resolve()
    return cwd


def _is_excluded_rel_path(rel_posix: str) -> bool:
    if not rel_posix or rel_posix.startswith("/"):
        return True
    parts = rel_posix.split("/")
    for part in parts[:-1]:
        if part in EXCLUDED_DIRS or (part.startswith(".") and part not in _ALLOWED_DOT_DIRS):
            return True
    leaf = parts[-1]
    if leaf in EXCLUDED_DIRS or leaf == ".DS_Store":
        return True
    lower_leaf = leaf.lower()
    if (
        lower_leaf.startswith(".env")
        or lower_leaf in {".netrc", ".npmrc", ".pypirc", ".pgpass", ".htpasswd"}
    ):
        return True
    if lower_leaf.startswith(("id_rsa", "id_ed25519", "id_ecdsa", "id_dsa")):
        return True
    if any(lower_leaf.endswith(suf) for suf in EXCLUDED_SUFFIXES):
        return True
    leaf_suffix = Path(lower_leaf).suffix
    if ("credential" in lower_leaf or "secret" in lower_leaf) and leaf_suffix in _SECRET_CONFIG_SUFFIXES:
        return True
    return False


def list_repo_files(repo_root: Path, *, max_files: int = 5000) -> tuple[list[str], str]:
    """Return ``(tracked_rel_posix_files, git_head_sha)`` for *repo_root*."""
    git_sha = ""
    if (repo_root / ".git").exists():
        rc_sha, out_sha = _run_git(repo_root, ["rev-parse", "HEAD"], timeout=1.5)
        if rc_sha == 0:
            candidate_sha = out_sha.strip()
            if _GIT_HEX_SHA_RE.fullmatch(candidate_sha):
                git_sha = candidate_sha
        rc_ls, out_ls = _run_git(repo_root, ["ls-files", "-z"], timeout=3.0)
        if rc_ls == 0 and out_ls:
            files: list[str] = []
            for raw_entry in out_ls.split("\x00"):
                rel = raw_entry.strip().replace("\\", "/")
                if not rel or _is_excluded_rel_path(rel):
                    continue
                full = repo_root / rel
                try:
                    if full.is_symlink() or not full.is_file():
                        continue
                    if not full.resolve().is_relative_to(repo_root):
                        continue
                except OSError:
                    continue
                files.append(rel)
                if len(files) >= max_files:
                    break
            files.sort()
            return files, git_sha

    # Bounded fallback directory walk for non-git or zero-commit directories.
    walked: list[str] = []
    for root, dirs, filenames in os.walk(repo_root, followlinks=False):
        root_path = Path(root)
        dirs[:] = sorted(
            d
            for d in dirs
            if d not in EXCLUDED_DIRS
            and not (d.startswith(".") and d not in _ALLOWED_DOT_DIRS)
            and not (root_path / d).is_symlink()
        )
        for fname in sorted(filenames):
            full = root_path / fname
            try:
                if full.is_symlink() or not full.is_file():
                    continue
                if not full.resolve().is_relative_to(repo_root):
                    continue
                rel = full.relative_to(repo_root).as_posix()
            except (OSError, ValueError):
                continue
            if _is_excluded_rel_path(rel):
                continue
            walked.append(rel)
            if len(walked) >= max_files:
                break
        if len(walked) >= max_files:
            break
    walked.sort()
    return walked, git_sha


def _first_sentence(text: str, max_len: int = 160) -> str:
    cleaned = re.sub(r"\s+", " ", (text or "").strip())
    if not cleaned:
        return ""
    m = re.match(r"^(.+?[.!?])(?:\s|$)", cleaned)
    sent = m.group(1) if m else cleaned
    if len(sent) > max_len:
        return sent[: max_len - 3].rstrip() + "..."
    return sent


def _extract_python_metadata(full_path: Path) -> dict[str, Any]:
    """Deterministically extract module docstring summary + top-level classes/functions."""
    try:
        size = full_path.stat().st_size
        if size <= 0 or size > MAX_AST_FILE_BYTES:
            return {"summary": "", "symbols": []}
        src = full_path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(src, filename=full_path.name)
    except (OSError, SyntaxError, ValueError):
        return {"summary": "", "symbols": []}

    doc = ast.get_docstring(tree) or ""
    summary = _first_sentence(doc)
    symbols: list[str] = []
    subcommands: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
            symbols.append(f"class {node.name}")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_"):
            symbols.append(f"{node.name}()")
    # Detect argparse add_parser("...") subcommands cleanly.
    for sub in ast.walk(tree):
        if (
            isinstance(sub, ast.Call)
            and isinstance(sub.func, ast.Attribute)
            and sub.func.attr == "add_parser"
            and sub.args
            and isinstance(sub.args[0], ast.Constant)
            and isinstance(sub.args[0].value, str)
        ):
            cmd = sub.args[0].value.strip()
            if cmd and cmd not in subcommands:
                subcommands.append(cmd)
    if subcommands:
        symbols.insert(0, f"subcommands: {', '.join(subcommands[:10])}")
    return {"summary": summary, "symbols": symbols[:10]}


def _extract_code_symbols(full_path: Path) -> dict[str, Any]:
    suffix = full_path.suffix.lower()
    if suffix == ".py":
        return _extract_python_metadata(full_path)
    if suffix in {".ts", ".tsx", ".js", ".mjs", ".swift", ".rs"}:
        try:
            size = full_path.stat().st_size
            if size <= 0 or size > MAX_TEXT_READ_BYTES:
                return {"summary": "", "symbols": []}
            text = full_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return {"summary": "", "symbols": []}
        names: list[str] = []
        for match in _SWIFT_TS_RS_SYMBOL_RE.finditer(text):
            sym = match.group(1)
            if sym not in names:
                names.append(sym)
            if len(names) >= 10:
                break
        summary = ""
        doc_match = re.search(r"/\*\*(.*?)\*/", text, re.DOTALL)
        if doc_match:
            cleaned = re.sub(r"^\s*\*\s?", "", doc_match.group(1), flags=re.MULTILINE)
            summary = _first_sentence(cleaned)
        return {"summary": summary, "symbols": names}
    return {"summary": "", "symbols": []}


def _extract_markdown_summary(full_path: Path) -> dict[str, Any]:
    try:
        size = full_path.stat().st_size
        if size <= 0 or size > MAX_TEXT_READ_BYTES:
            return {"headings": [], "summary": ""}
        raw = full_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {"headings": [], "summary": ""}
    # Parse YAML frontmatter first and skip restricted privacy documents (BUG-37).
    fm_match = _FRONTMATTER_BLOCK_RE.match(raw)
    if fm_match:
        yaml_block = fm_match.group(1)
        declared_privacies: set[str] = set()
        try:
            fm_data = yaml.safe_load(yaml_block)
            if isinstance(fm_data, dict):
                for key in ("privacy", "privacy_level"):
                    val = fm_data.get(key)
                    if val is not None:
                        declared_privacies.add(str(val).strip().strip("\"'").lower())
        except Exception:
            pass
        for raw_val in re.findall(r"^(?:privacy|privacy_level):\s*(.+)$", yaml_block, re.MULTILINE):
            declared_privacies.add(str(raw_val).strip().strip("\"'").lower())
        if declared_privacies & _RESTRICTED_MARKDOWN_PRIVACIES:
            return {"headings": [], "summary": "", "restricted": True}
        text = raw[fm_match.end() :]
    else:
        text = raw
    headings: list[str] = []
    paragraphs: list[str] = []
    cur_para: list[str] = []
    in_fence = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if stripped.startswith("#"):
            if cur_para:
                paragraphs.append(" ".join(cur_para))
                cur_para = []
            h_match = re.match(r"^(#{1,2})\s+(.+)$", stripped)
            if h_match:
                headings.append(h_match.group(2).strip())
        elif not stripped:
            if cur_para:
                paragraphs.append(" ".join(cur_para))
                cur_para = []
        elif not stripped.startswith(("|", ">", "-", "*")) and not re.match(r"^\d+\.", stripped):
            cur_para.append(stripped)
    if cur_para:
        paragraphs.append(" ".join(cur_para))
    summary = _first_sentence(paragraphs[0]) if paragraphs else ""
    return {"headings": headings[:8], "summary": summary}


def _extract_gate_commands(repo_root: Path, files: list[str]) -> list[str]:
    gates: list[str] = []
    file_set = set(files)
    if "Makefile" in file_set:
        try:
            mk = (repo_root / "Makefile").read_text(encoding="utf-8", errors="replace")[:MAX_TEXT_READ_BYTES]
            targets: list[str] = []
            for m in re.finditer(r"^([a-zA-Z0-9_-]+)\s*:", mk, re.MULTILINE):
                tgt = m.group(1)
                if tgt not in {"PHONY", "FORCE"} and not tgt.startswith(".") and tgt not in targets:
                    targets.append(tgt)
            preferred = [t for t in ("setup", "test", "lint", "check", "build", "attest", "verify") if t in targets]
            chosen = preferred or targets[:6]
            for t in chosen[:6]:
                gates.append(f"make {t}")
        except OSError:
            pass
    if "package.json" in file_set:
        try:
            pkg = json.loads((repo_root / "package.json").read_text(encoding="utf-8", errors="replace"))
            scripts = pkg.get("scripts") if isinstance(pkg, dict) else None
            if isinstance(scripts, dict):
                for name in ("build", "test", "lint", "typecheck", "check"):
                    if name in scripts:
                        gates.append(f"npm run {name}")
        except (OSError, ValueError):
            pass
    for sub_pkg in [f for f in files if f.endswith("/package.json") and f.count("/") <= 2][:3]:
        prefix = os.path.dirname(sub_pkg)
        try:
            pkg = json.loads((repo_root / sub_pkg).read_text(encoding="utf-8", errors="replace"))
            scripts = pkg.get("scripts") if isinstance(pkg, dict) else None
            if isinstance(scripts, dict):
                present = [n for n in ("build", "test", "lint", "typecheck") if n in scripts]
                if present:
                    gates.append(f"npm --prefix {prefix} run {' / '.join(present)}")
        except (OSError, ValueError):
            pass
    if "pyproject.toml" in file_set:
        try:
            pyproj = (repo_root / "pyproject.toml").read_text(encoding="utf-8", errors="replace")[:MAX_TEXT_READ_BYTES]
            if "pytest" in pyproj and not any("pytest" in g for g in gates):
                gates.append("pytest")
            if "ruff" in pyproj and not any("ruff" in g for g in gates):
                gates.append("ruff check")
        except OSError:
            pass
    return gates[:10]


def _classify_flat_module(stem: str, pkg_name: str) -> tuple[str, str]:
    lower = stem.lower()
    for slug, title, keywords in _FLAT_DOMAIN_RULES:
        if any(kw in lower for kw in keywords):
            return slug, title
    fallback = _sanitize_subsystem_slug(f"{pkg_name}-core")
    return fallback, f"{_human_title(pkg_name)} Core"


def _cluster_subsystems(files: list[str]) -> list[dict[str, Any]]:
    """Partition tracked files into ~4-18 logical subsystems."""
    # Pre-count flat files inside src/<pkg>/ or top-level <pkg>/ so we know whether
    # to emit per-module cards (<=12 flat files) or domain-grouped cards (>12 flat files).
    src_pkg_flat_counts: dict[str, int] = defaultdict(int)
    for rel in files:
        parts = rel.split("/")
        if len(parts) == 3 and parts[0] == "src" and parts[2] != "__init__.py":
            src_pkg_flat_counts[parts[1]] += 1

    buckets: dict[str, dict[str, Any]] = {}

    def _add(slug: str, title: str, rel: str) -> None:
        safe_slug = _sanitize_subsystem_slug(slug)
        entry = buckets.get(safe_slug)
        if entry is None:
            entry = {"slug": safe_slug, "title": title, "files": []}
            buckets[safe_slug] = entry
        entry["files"].append(rel)

    for rel in files:
        parts = rel.split("/")
        top = parts[0]
        if len(parts) == 1:
            # Root config/docs belong to overview.md; standalone root code files go to root-tools.
            if Path(rel).suffix.lower() in {".py", ".ts", ".js", ".mjs", ".rs", ".swift", ".sh"}:
                _add("root-entrypoints", "Root Entrypoints", rel)
            continue

        if top == "src" and len(parts) >= 3:
            pkg = parts[1]
            if len(parts) == 3:
                fname = parts[2]
                stem = Path(fname).stem
                if stem == "__init__":
                    if src_pkg_flat_counts[pkg] > FLAT_MODULE_GROUP_THRESHOLD:
                        _add(f"{pkg}-core", f"{_human_title(pkg)} Core", rel)
                    continue
                if src_pkg_flat_counts[pkg] <= FLAT_MODULE_GROUP_THRESHOLD:
                    _add(stem, f"{_human_title(stem)} Subsystem", rel)
                else:
                    d_slug, d_title = _classify_flat_module(stem, pkg)
                    _add(d_slug, d_title, rel)
            else:
                subpkg = parts[2]
                if subpkg == "minnid_runtime":
                    _add("daemon-runtime", "Daemon & RPC Runtime", rel)
                elif subpkg == "migrations":
                    _add("storage-schema", "SQLite Schema, Migrations & Config", rel)
                else:
                    _add(subpkg, f"{_human_title(subpkg)} Subsystem", rel)
            continue

        if top in {"plugins", "packages", "Packages", "Apps", "apps", "Sources", "crates", "services"}:
            if len(parts) >= 2:
                sub = parts[1]
                slug = f"{top.lower()}-{sub}" if top.lower() not in {"sources"} else sub
                _add(slug, f"{_human_title(top)} / {sub}", rel)
            else:
                _add(top, _human_title(top), rel)
            continue

        if top in {"docs", "doc"}:
            _add("docs", "Architecture & Documentation", rel)
            continue

        if top in {"tests", "test", "__tests__", "spec"}:
            _add("tests", "Verification & Test Suites", rel)
            continue

        if top in {"scripts", "bin", "tools", ".githooks", ".github"}:
            _add("scripts-ci", "Scripts, Hooks & CI Gates", rel)
            continue

        # Generic top-level package or folder
        _add(top, f"{_human_title(top)} Subsystem", rel)

    ordered = sorted(buckets.values(), key=lambda b: (b["slug"] in {"docs", "tests", "scripts-ci"}, b["slug"]))

    # Cap total subsystems to MAX_SUBSYSTEMS by merging smallest overflow buckets into misc-modules.
    if len(ordered) > MAX_SUBSYSTEMS:
        by_size = sorted(ordered, key=lambda b: len(b["files"]), reverse=True)
        kept = by_size[: MAX_SUBSYSTEMS - 1]
        overflow = by_size[MAX_SUBSYSTEMS - 1 :]
        merged_files: list[str] = []
        for item in overflow:
            merged_files.extend(item["files"])
        merged_files.sort()
        kept.append(
            {
                "slug": "other-subsystems",
                "title": "Additional Modules",
                "files": merged_files,
            }
        )
        ordered = sorted(kept, key=lambda b: b["slug"])

    return ordered


def _compute_unique_dir_prefixes(subsystems: list[dict[str, Any]]) -> dict[str, list[str]]:
    """Compute directory prefixes uniquely owned by a single subsystem."""
    dir_owners: dict[str, set[str]] = defaultdict(set)
    for sub in subsystems:
        slug = sub["slug"]
        for rel in sub["files"]:
            parent = os.path.dirname(rel)
            if parent:
                dir_owners[parent + "/"].add(slug)
                top_two = "/".join(parent.split("/")[:2]) + "/"
                dir_owners[top_two].add(slug)

    unique_by_slug: dict[str, list[str]] = defaultdict(list)
    for prefix, owners in sorted(dir_owners.items()):
        if len(owners) == 1:
            owner = next(iter(owners))
            unique_by_slug[owner].append(prefix)
    return unique_by_slug


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _render_card_markdown(
    *,
    title: str,
    workspace_id: str,
    subsystem_slug: str,
    repo_root: str,
    git_sha: str,
    summary_line: str,
    owned_files: list[str],
    overview_lines: list[str],
    file_symbol_lines: list[str],
    docs_and_gates_lines: list[str],
) -> str:
    frontmatter = {
        "title": title,
        "workspace_id": workspace_id,
        "page_type": "repo_map",
        "status": "accepted",
        "privacy": "safe",
        "agent": "repo-map",
        "sigil": "🗺️",
        "subsystem_slug": subsystem_slug,
        "repo_root": repo_root,
        "summary_line": summary_line,
        "git_sha": git_sha,
        "owned_files": owned_files[:200],
    }
    fm_yaml = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True).strip()
    sections = [
        "---",
        fm_yaml,
        "---",
        "",
        f"# {title}",
        "",
        "## Overview",
        *(overview_lines or [summary_line or f"Subsystem `{subsystem_slug}` in `{workspace_id}`."]),
        "",
        "## Key Files & Symbols",
        *(file_symbol_lines or ["- No tracked source symbols extracted."]),
        "",
        "## Docs & Gates",
        *(docs_and_gates_lines or ["- Inspect repository root docs for verification commands."]),
        "",
    ]
    return "\n".join(sections)


def _upsert_repo_map_card_row(
    db: MinniDB,
    *,
    doc_id: int,
    workspace_id: str,
    repo_root: str,
    subsystem_slug: str,
    title: str,
    summary_line: str,
    owned_files: list[str],
    git_sha: str,
    updated_at: float,
) -> None:
    owned_json = json.dumps(owned_files, separators=(",", ":"))
    with db.transaction() as c:
        c.execute(
            """DELETE FROM repo_map_cards
               WHERE workspace_id = ? AND subsystem_slug = ? AND doc_id != ?""",
            (workspace_id, subsystem_slug, doc_id),
        )
        c.execute(
            """INSERT INTO repo_map_cards
               (doc_id, workspace_id, repo_root, subsystem_slug, title,
                summary_line, owned_files_json, git_sha, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(doc_id) DO UPDATE SET
                   workspace_id = excluded.workspace_id,
                   repo_root = excluded.repo_root,
                   subsystem_slug = excluded.subsystem_slug,
                   title = excluded.title,
                   summary_line = excluded.summary_line,
                   owned_files_json = excluded.owned_files_json,
                   git_sha = excluded.git_sha,
                   updated_at = excluded.updated_at""",
            (
                doc_id,
                workspace_id,
                repo_root,
                subsystem_slug,
                title,
                summary_line,
                owned_json,
                git_sha,
                updated_at,
            ),
        )


def _prune_stale_workspace_cards(
    *,
    db: MinniDB,
    engine: Optional[Any],
    maps_ws_dir: Path,
    workspace_id: str,
    active_slugs: set[str],
    active_resolved_paths: set[str],
) -> int:
    pruned = 0
    if maps_ws_dir.is_dir():
        for md_file in sorted(maps_ws_dir.glob("*.md")):
            if md_file.stem not in active_slugs and not md_file.is_symlink():
                try:
                    md_file.unlink()
                    pruned += 1
                except OSError:
                    pass

    stale_doc_ids: list[int] = []
    with db.transaction() as c:
        rows = c.execute(
            "SELECT doc_id, subsystem_slug FROM repo_map_cards WHERE workspace_id = ?",
            (workspace_id,),
        ).fetchall()
        for r in rows:
            if r["subsystem_slug"] not in active_slugs:
                stale_doc_ids.append(int(r["doc_id"]))

        doc_rows = c.execute(
            "SELECT doc_id, path FROM documents WHERE page_type = 'repo_map' AND workspace_id = ?",
            (workspace_id,),
        ).fetchall()
        for r in doc_rows:
            if str(r["path"]) not in active_resolved_paths and int(r["doc_id"]) not in stale_doc_ids:
                stale_doc_ids.append(int(r["doc_id"]))

        if stale_doc_ids:
            placeholders = ",".join("?" for _ in stale_doc_ids)
            chunk_rows = c.execute(
                f"SELECT chunk_id FROM chunk_embeddings WHERE doc_id IN ({placeholders})",
                stale_doc_ids,
            ).fetchall()
            stale_chunk_ids = [int(r["chunk_id"]) for r in chunk_rows]
            if engine is not None and stale_chunk_ids:
                try:
                    engine._invalidate_durable_rerank(stale_chunk_ids)
                    engine._remove_live_faiss(stale_chunk_ids)
                except Exception:
                    pass
            c.execute(f"DELETE FROM repo_map_cards WHERE doc_id IN ({placeholders})", stale_doc_ids)
            c.execute(f"DELETE FROM vault_fts WHERE doc_id IN ({placeholders})", stale_doc_ids)
            c.execute(f"DELETE FROM chunk_embeddings WHERE doc_id IN ({placeholders})", stale_doc_ids)
            c.execute(f"DELETE FROM documents WHERE doc_id IN ({placeholders})", stale_doc_ids)
            pruned += len(stale_doc_ids)
    return pruned


def _effective_minni_home(minni_home: Optional[str | Path] = None, engine: Optional[Any] = None) -> Path:
    if minni_home is not None and str(minni_home).strip():
        return Path(minni_home).expanduser().resolve()
    env_home = (os.environ.get("MINNI_HOME") or "").strip()
    if env_home:
        return Path(env_home).expanduser().resolve()
    if engine is not None and getattr(engine, "config", None) is not None:
        db_path = getattr(engine.config, "db_path", "")
        if db_path:
            return Path(db_path).expanduser().resolve().parent
    return Path(CANONICAL_SOVEREIGN_HOME).expanduser().resolve()


def build_and_index_repo_map(
    repo_path: Optional[str | Path] = None,
    *,
    workspace_id: Optional[str] = None,
    refresh: bool = False,
    minni_home: Optional[str | Path] = None,
    db: Optional[MinniDB] = None,
    engine: Optional[Any] = None,
) -> dict[str, Any]:
    """Synthesize Subsystem Map Cards + ``_index.json`` and index into SQLite + FAISS."""
    repo_root = resolve_repo_root(repo_path)
    ws_id = (
        normalize_workspace_scope(workspace_id, default="")
        if workspace_id and str(workspace_id).strip()
        else ""
    )
    if not ws_id or ws_id in ("default", "global", "*"):
        ws_id = normalize_workspace_scope(repo_root.name, default="workspace-repo")
    ws_slug = workspace_slug_from_id(ws_id)

    home_dir = _effective_minni_home(minni_home, engine=engine)
    vault_dir = home_dir / "vault"
    maps_root = vault_dir / "maps"
    if vault_dir.is_symlink() or maps_root.is_symlink():
        raise ValueError(f"Refusing to write repo maps through symlinked vault path under {home_dir}")
    maps_ws_dir = maps_root / ws_slug
    if maps_ws_dir.exists() and maps_ws_dir.is_symlink():
        raise ValueError(f"Refusing to write repo maps through symlinked workspace map dir: {maps_ws_dir}")
    maps_ws_dir.mkdir(parents=True, exist_ok=True)

    files, git_sha = list_repo_files(repo_root)
    index_json_path = maps_ws_dir / "_index.json"

    own_db = db is None
    if engine is None:
        cfg = MinniConfig(
            vault_path=str(vault_dir),
            db_path=str(db.config.db_path) if db is not None else str(home_dir / "minni.db"),
            faiss_index_path=str(home_dir / "minni_faiss.index"),
            graph_export_dir=str(home_dir / "graphs"),
        )
        from minni.retrieval import RetrievalEngine

        engine = RetrievalEngine(db=db, config=cfg)
    if db is None:
        db = engine.db

    # Fast idempotent check when refresh=False and git_sha is unchanged.
    if not refresh and git_sha and index_json_path.is_file():
        try:
            cached_idx = json.loads(index_json_path.read_text(encoding="utf-8"))
            cached_subs = cached_idx.get("subsystems") or []
            if cached_idx.get("git_sha") == git_sha and isinstance(cached_subs, list) and cached_subs:
                with db.cursor() as c:
                    cnt_row = c.execute(
                        "SELECT COUNT(*) AS cnt FROM repo_map_cards WHERE workspace_id = ? AND git_sha = ?",
                        (ws_id, git_sha),
                    ).fetchone()
                expected_cards = len(cached_subs) + 1  # subsystems + overview
                if cnt_row and int(cnt_row["cnt"]) == expected_cards:
                    return {
                        "status": "up_to_date",
                        "workspace_id": ws_id,
                        "workspace_slug": ws_slug,
                        "repo_root": str(repo_root),
                        "git_sha": git_sha,
                        "subsystems_mapped": len(cached_subs),
                        "cards_indexed": expected_cards,
                        "index_path": str(index_json_path),
                        "subsystems": [
                            {
                                "slug": s.get("slug"),
                                "title": s.get("title"),
                                "summary_line": s.get("summary_line"),
                                "file_count": s.get("file_count", 0),
                                "card_rel_path": s.get("card_rel_path"),
                            }
                            for s in cached_subs
                        ],
                    }
        except Exception:
            pass

    gate_commands = _extract_gate_commands(repo_root, files)
    doc_files = [
        f
        for f in files
        if f in {"AGENTS.md", "CLAUDE.md", "README.md", "ARCHITECTURE.md", "CONTRIBUTING.md"}
        or (f.startswith("docs/") and f.lower().endswith(".md"))
    ][:25]
    root_owned_files = [
        f
        for f in files
        if "/" not in f and f in {"AGENTS.md", "CLAUDE.md", "README.md", "ARCHITECTURE.md", "Makefile", "pyproject.toml", "package.json"}
    ]

    doc_summaries: dict[str, dict[str, Any]] = {}
    for df in doc_files:
        doc_summaries[df] = _extract_markdown_summary(repo_root / df)

    subsystems = _cluster_subsystems(files)
    unique_prefixes = _compute_unique_dir_prefixes(subsystems)
    now = time.time()

    subsystem_index_entries: list[dict[str, Any]] = []
    cards_to_index: list[dict[str, Any]] = []

    for sub in subsystems:
        slug = sub["slug"]
        title = sub["title"]
        sub_files: list[str] = sorted(sub["files"])

        file_symbol_lines: list[str] = []
        extracted_summaries: list[str] = []
        for rel in sub_files[:16]:
            full = repo_root / rel
            if rel.lower().endswith(".md"):
                md_info = doc_summaries.get(rel) or _extract_markdown_summary(full)
                headings = md_info.get("headings") or []
                summ = md_info.get("summary") or ""
                if summ:
                    extracted_summaries.append(summ)
                detail = summ or (", ".join(headings[:4]) if headings else "documentation")
                file_symbol_lines.append(f"- `{rel}` — {detail}")
            else:
                code_info = _extract_code_symbols(full)
                syms = code_info.get("symbols") or []
                summ = code_info.get("summary") or ""
                if summ:
                    extracted_summaries.append(summ)
                parts_desc: list[str] = []
                if summ:
                    parts_desc.append(summ)
                if syms:
                    parts_desc.append(f"symbols: {', '.join(syms[:6])}")
                desc = " | ".join(parts_desc) if parts_desc else "module"
                file_symbol_lines.append(f"- `{rel}` — {desc}")
        if len(sub_files) > 16:
            file_symbol_lines.append(f"- *(+{len(sub_files) - 16} additional files in subsystem)*")

        summary_line = (
            extracted_summaries[0]
            if extracted_summaries
            else f"{title} ({len(sub_files)} tracked file{'s' if len(sub_files) != 1 else ''}: {', '.join(sub_files[:3])})."
        )
        overview_lines = [
            f"- **Subsystem**: `{slug}` ({len(sub_files)} file{'s' if len(sub_files) != 1 else ''})",
            f"- **Summary**: {summary_line}",
        ]
        docs_and_gates_lines: list[str] = []
        if gate_commands:
            docs_and_gates_lines.append(f"- **Verification Gates**: `{'`, `'.join(gate_commands[:5])}`")
        for df in doc_files[:5]:
            d_sum = (doc_summaries.get(df) or {}).get("summary") or ""
            docs_and_gates_lines.append(f"- `{df}`" + (f" — {d_sum}" if d_sum else ""))

        card_path = maps_ws_dir / f"{slug}.md"
        card_md = _render_card_markdown(
            title=f"{repo_root.name} — {title}",
            workspace_id=ws_id,
            subsystem_slug=slug,
            repo_root=str(repo_root),
            git_sha=git_sha,
            summary_line=summary_line,
            owned_files=sub_files,
            overview_lines=overview_lines,
            file_symbol_lines=file_symbol_lines,
            docs_and_gates_lines=docs_and_gates_lines,
        )
        _atomic_write_text(card_path, card_md)

        card_rel_path = f"maps/{ws_slug}/{slug}.md"
        subsystem_index_entries.append(
            {
                "slug": slug,
                "title": title,
                "summary_line": summary_line,
                "card_rel_path": card_rel_path,
                "file_count": len(sub_files),
                "sample_files": sub_files[:5],
                "owned_files": sub_files,
                "dir_prefixes": unique_prefixes.get(slug, []),
            }
        )
        cards_to_index.append(
            {
                "slug": slug,
                "title": f"{repo_root.name} — {title}",
                "summary_line": summary_line,
                "owned_files": sub_files,
                "card_path": card_path,
                "content": card_md,
            }
        )

    # Synthesize repository-level overview.md card.
    root_summary = ""
    for primary_doc in ("AGENTS.md", "CLAUDE.md", "README.md"):
        if primary_doc in doc_summaries and doc_summaries[primary_doc].get("summary"):
            root_summary = doc_summaries[primary_doc]["summary"]
            break
    if not root_summary:
        root_summary = f"Repository map for {repo_root.name} ({len(subsystem_index_entries)} subsystems, {len(files)} tracked files)."

    overview_lines = [
        f"- **Workspace**: `{ws_id}` (`{repo_root.name}`)",
        f"- **Summary**: {root_summary}",
        f"- **Tracked Files**: {len(files)} across {len(subsystem_index_entries)} subsystems",
    ]
    if git_sha:
        overview_lines.append(f"- **Git HEAD**: `{git_sha[:12]}`")

    overview_sub_lines = [
        f"- `{entry['slug']}` (`{entry['card_rel_path']}`, {entry['file_count']} files) — {entry['summary_line']}"
        for entry in subsystem_index_entries
    ]
    overview_doc_gate_lines: list[str] = []
    if gate_commands:
        overview_doc_gate_lines.append(f"- **Verification Gates**: `{'`, `'.join(gate_commands)}`")
    for df in doc_files[:10]:
        d_info = doc_summaries.get(df) or {}
        d_sum = d_info.get("summary") or ", ".join((d_info.get("headings") or [])[:3])
        overview_doc_gate_lines.append(f"- `{df}`" + (f" — {d_sum}" if d_sum else ""))

    overview_path = maps_ws_dir / "overview.md"
    overview_md = _render_card_markdown(
        title=f"{repo_root.name} — Repository Overview",
        workspace_id=ws_id,
        subsystem_slug="overview",
        repo_root=str(repo_root),
        git_sha=git_sha,
        summary_line=root_summary,
        owned_files=root_owned_files,
        overview_lines=overview_lines,
        file_symbol_lines=overview_sub_lines,
        docs_and_gates_lines=overview_doc_gate_lines,
    )
    _atomic_write_text(overview_path, overview_md)
    cards_to_index.append(
        {
            "slug": "overview",
            "title": f"{repo_root.name} — Repository Overview",
            "summary_line": root_summary,
            "owned_files": root_owned_files,
            "card_path": overview_path,
            "content": overview_md,
        }
    )

    index_payload = {
        "workspace_id": ws_id,
        "workspace_slug": ws_slug,
        "repo_root": str(repo_root),
        "git_sha": git_sha,
        "updated_at": now,
        "gate_commands": gate_commands,
        "doc_files": doc_files,
        "subsystems": subsystem_index_entries,
    }
    _atomic_write_text(index_json_path, json.dumps(index_payload, indent=2) + "\n")

    active_slugs: set[str] = set()
    active_resolved_paths: set[str] = set()
    indexed_count = 0
    for card in cards_to_index:
        resolved_card_path = str(card["card_path"].resolve())
        active_slugs.add(card["slug"])
        active_resolved_paths.add(resolved_card_path)
        idx_res = engine.index_durable_document(
            content=card["content"],
            path=resolved_card_path,
            agent="repo-map",
            sigil="🗺️",
            privacy_level="safe",
            page_status="accepted",
            page_type="repo_map",
            layer="knowledge",
            workspace_id=ws_id,
        )
        doc_id = idx_res.get("doc_id")
        if doc_id is not None:
            _upsert_repo_map_card_row(
                db,
                doc_id=int(doc_id),
                workspace_id=ws_id,
                repo_root=str(repo_root),
                subsystem_slug=card["slug"],
                title=card["title"],
                summary_line=card["summary_line"],
                owned_files=card["owned_files"],
                git_sha=git_sha,
                updated_at=now,
            )
            indexed_count += 1

    pruned_count = _prune_stale_workspace_cards(
        db=db,
        engine=engine,
        maps_ws_dir=maps_ws_dir,
        workspace_id=ws_id,
        active_slugs=active_slugs,
        active_resolved_paths=active_resolved_paths,
    )

    if own_db and db is not None:
        try:
            db.close()
        except Exception:
            pass

    return {
        "status": "ok",
        "workspace_id": ws_id,
        "workspace_slug": ws_slug,
        "repo_root": str(repo_root),
        "git_sha": git_sha,
        "subsystems_mapped": len(subsystem_index_entries),
        "cards_indexed": indexed_count,
        "cards_pruned": pruned_count,
        "index_path": str(index_json_path),
        "gate_commands": gate_commands,
        "subsystems": [
            {
                "slug": s["slug"],
                "title": s["title"],
                "summary_line": s["summary_line"],
                "file_count": s["file_count"],
                "card_rel_path": s["card_rel_path"],
            }
            for s in subsystem_index_entries
        ],
    }


def compute_repo_map_staleness(
    db: MinniDB,
    workspace_id: str,
    *,
    timeout_sec: float = 0.5,
) -> dict[str, Any]:
    """Check git HEAD vs stored ``repo_map_cards.git_sha`` for *workspace_id*.

    Fast path: single ``git rev-parse HEAD`` (<15ms) when ``HEAD == git_sha``.
    When ``HEAD != git_sha``, computes ``git diff --name-only <git_sha>..HEAD``
    and maps changed files to affected ``subsystem_slug`` and ``doc_id`` entries.
    """
    ws_id = normalize_workspace_scope(workspace_id, default="")
    if not ws_id or ws_id in ("default", "global", "*"):
        return {"stale": False, "workspace_id": ws_id}

    try:
        with db.cursor() as c:
            has_tbl = c.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='repo_map_cards'"
            ).fetchone()
            if has_tbl is None:
                return {"stale": False, "workspace_id": ws_id}
            rows = c.execute(
                """SELECT doc_id, repo_root, subsystem_slug, owned_files_json, git_sha
                   FROM repo_map_cards WHERE workspace_id = ?""",
                (ws_id,),
            ).fetchall()
    except Exception:
        return {"stale": False, "workspace_id": ws_id}

    if not rows:
        return {"stale": False, "workspace_id": ws_id}

    repo_root_str = str(rows[0]["repo_root"] or "").strip()
    built_sha = str(rows[0]["git_sha"] or "").strip()
    if (
        not repo_root_str
        or repo_root_str.startswith("-")
        or not os.path.isabs(repo_root_str)
        or not built_sha
        or not _GIT_HEX_SHA_RE.fullmatch(built_sha)
    ):
        return {"stale": False, "workspace_id": ws_id, "built_sha": ""}

    repo_root = Path(repo_root_str)
    if not repo_root.is_dir() or not (repo_root / ".git").exists():
        return {"stale": False, "workspace_id": ws_id, "built_sha": built_sha}

    rc_head, out_head = _run_git(repo_root, ["rev-parse", "HEAD"], timeout=timeout_sec)
    if rc_head != 0 or not out_head.strip():
        return {"stale": False, "workspace_id": ws_id, "built_sha": built_sha}
    head_sha = out_head.strip()
    if not _GIT_HEX_SHA_RE.fullmatch(head_sha):
        return {"stale": False, "workspace_id": ws_id, "built_sha": built_sha}
    if head_sha == built_sha:
        return {
            "stale": False,
            "workspace_id": ws_id,
            "repo_root": repo_root_str,
            "built_sha": built_sha,
            "head_sha": head_sha,
            "changed_files": [],
            "affected_subsystems": {},
            "affected_doc_ids": {},
        }

    rc_diff, out_diff = _run_git(
        repo_root,
        ["diff", "--name-only", f"{built_sha}..{head_sha}", "--"],
        timeout=timeout_sec,
    )
    if rc_diff == 0:
        changed_files = [
            ln.strip().replace("\\", "/")
            for ln in out_diff.splitlines()
            if ln.strip() and not _is_excluded_rel_path(ln.strip().replace("\\", "/"))
        ]
    else:
        changed_files = ["<commits diverged>"]

    if not changed_files:
        return {
            "stale": False,
            "workspace_id": ws_id,
            "repo_root": repo_root_str,
            "built_sha": built_sha,
            "head_sha": head_sha,
            "changed_files": [],
            "affected_subsystems": {},
            "affected_doc_ids": {},
        }

    cards_meta: list[dict[str, Any]] = []
    sub_for_prefixes: list[dict[str, Any]] = []
    for r in rows:
        try:
            owned = json.loads(r["owned_files_json"] or "[]")
            if not isinstance(owned, list):
                owned = []
        except ValueError:
            owned = []
        slug = str(r["subsystem_slug"])
        doc_id = int(r["doc_id"])
        cards_meta.append({"doc_id": doc_id, "slug": slug, "owned": [str(x) for x in owned]})
        if slug != "overview":
            sub_for_prefixes.append({"slug": slug, "files": [str(x) for x in owned]})

    unique_prefixes = _compute_unique_dir_prefixes(sub_for_prefixes)
    affected_subsystems: dict[str, list[str]] = {}
    affected_doc_ids: dict[int, dict[str, Any]] = {}
    sha7 = built_sha[:7]

    for card in cards_meta:
        slug = card["slug"]
        doc_id = card["doc_id"]
        owned_set = set(card["owned"])
        prefixes = unique_prefixes.get(slug, [])
        if slug == "overview":
            matched = list(changed_files)
        else:
            matched = []
            for cf in changed_files:
                if cf in owned_set or any(cf.startswith(p) for p in prefixes):
                    matched.append(cf)
                elif Path(cf).stem.lower() == slug:
                    matched.append(cf)
        if matched:
            shown = ", ".join(matched[:8])
            if len(matched) > 8:
                shown += f" (+{len(matched) - 8} more)"
            note = f"[Note: Map built at {sha7}; changed since then: {shown}]"
            affected_subsystems[slug] = matched
            affected_doc_ids[doc_id] = {
                "staleness_note": note,
                "changed_files": matched,
                "built_sha": built_sha,
                "head_sha": head_sha,
                "subsystem_slug": slug,
            }

    return {
        "stale": True,
        "workspace_id": ws_id,
        "repo_root": repo_root_str,
        "built_sha": built_sha,
        "head_sha": head_sha,
        "changed_files": changed_files,
        "affected_subsystems": affected_subsystems,
        "affected_doc_ids": affected_doc_ids,
    }


def _prepend_staleness_banner(text: str, note: str) -> str:
    if not text or note in text:
        return text
    m = _EVIDENCE_OPEN_TAG_RE.match(text)
    if m:
        open_tag = m.group(1)
        rest = text[len(open_tag) :]
        return f"{open_tag}{note}\n{rest}"
    return f"{note}\n{text}"


def schedule_repo_map_refresh(
    *,
    repo_root: str,
    workspace_id: str,
    minni_home: Optional[str | Path] = None,
    engine: Optional[Any] = None,
    cooldown_sec: float = AUTO_REFRESH_COOLDOWN_SEC,
    debounce_sec: float = AUTO_REFRESH_DEBOUNCE_SEC,
) -> bool:
    """Queue a debounced, non-blocking background refresh for *workspace_id*.

    Guarded by a per-workspace cooldown and filesystem existence checks so
    short-lived test fixtures tearing down temporary directories never crash.
    """
    if (os.environ.get("MINNI_DISABLE_AUTO_MAP_REFRESH") or "").strip() == "1":
        return False
    now_m = time.monotonic()
    with _refresh_lock:
        last = _last_refresh_scheduled.get(workspace_id)
        if last is not None and (now_m - last) < cooldown_sec:
            return False
        _last_refresh_scheduled[workspace_id] = now_m
        prev_timer = _pending_refresh_timers.pop(workspace_id, None)
        if prev_timer is not None:
            prev_timer.cancel()

        resolved_home = _effective_minni_home(minni_home, engine=engine)
        db_path = (
            Path(engine.config.db_path)
            if engine is not None and getattr(engine, "config", None) is not None
            else resolved_home / "minni.db"
        )

        def _worker() -> None:
            with _refresh_lock:
                _pending_refresh_timers.pop(workspace_id, None)
            try:
                rp = Path(repo_root)
                if not rp.is_dir() or not resolved_home.is_dir() or not db_path.exists():
                    return
                build_and_index_repo_map(
                    rp,
                    workspace_id=workspace_id,
                    refresh=True,
                    minni_home=resolved_home,
                    db=engine.db if engine is not None else None,
                    engine=engine,
                )
            except Exception as exc:
                logger.debug("repo_map background refresh skipped/failed for %s: %s", workspace_id, exc)
            finally:
                if engine is not None and getattr(engine, "db", None) is not None:
                    try:
                        engine.db.close()
                    except Exception:
                        pass

        timer = threading.Timer(debounce_sec, _worker)
        timer.daemon = True
        _pending_refresh_timers[workspace_id] = timer
        timer.start()
        return True


def annotate_repo_map_results(
    results: list[Any],
    *,
    db: Optional[MinniDB],
    engine: Optional[Any] = None,
    trigger_refresh: bool = True,
) -> None:
    """In-place attach git-diff staleness banners onto ``page_type == 'repo_map'`` results."""
    if not results or db is None:
        return

    candidate_items: list[dict[str, Any]] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        pt = str(item.get("page_type") or "").lower()
        src = str(item.get("source") or item.get("path") or "")
        if pt == "repo_map" or "/vault/maps/" in src:
            if item.get("doc_id") is not None:
                candidate_items.append(item)

    if not candidate_items:
        return

    # Resolve workspace_id per doc_id (from item or repo_map_cards lookup).
    missing_ws_doc_ids = [
        int(it["doc_id"]) for it in candidate_items if not it.get("workspace_id")
    ]
    ws_by_doc_id: dict[int, str] = {}
    if missing_ws_doc_ids:
        try:
            placeholders = ",".join("?" for _ in missing_ws_doc_ids)
            with db.cursor() as c:
                has_tbl = c.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='repo_map_cards'"
                ).fetchone()
                if has_tbl is not None:
                    for r in c.execute(
                        f"SELECT doc_id, workspace_id FROM repo_map_cards WHERE doc_id IN ({placeholders})",
                        missing_ws_doc_ids,
                    ).fetchall():
                        ws_by_doc_id[int(r["doc_id"])] = str(r["workspace_id"] or "")
        except Exception:
            pass

    staleness_by_ws: dict[str, dict[str, Any]] = {}
    for item in candidate_items:
        doc_id = int(item["doc_id"])
        ws_id = str(item.get("workspace_id") or ws_by_doc_id.get(doc_id) or "").strip()
        if not ws_id:
            continue
        st = staleness_by_ws.get(ws_id)
        if st is None:
            st = compute_repo_map_staleness(db, ws_id)
            staleness_by_ws[ws_id] = st
            if st.get("stale") and trigger_refresh and st.get("repo_root"):
                schedule_repo_map_refresh(
                    repo_root=str(st["repo_root"]),
                    workspace_id=ws_id,
                    engine=engine,
                )
        if not st.get("stale"):
            continue
        doc_info = (st.get("affected_doc_ids") or {}).get(doc_id)
        if not doc_info:
            continue
        note = doc_info["staleness_note"]
        item["stale"] = True
        item["staleness_note"] = note
        item["built_git_sha"] = doc_info["built_sha"]
        item["head_git_sha"] = doc_info["head_sha"]
        item["changed_files"] = doc_info["changed_files"]
        for field_name in ("text", "snippet", "content", "chunk_text", "evidence_envelope"):
            val = item.get(field_name)
            if isinstance(val, str) and val:
                item[field_name] = _prepend_staleness_banner(val, note)
