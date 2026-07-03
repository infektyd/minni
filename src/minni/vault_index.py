"""Per-vault index store helpers.

Agent vault markdown is indexed into a local store under ``<vault>/.index`` so
personal recall can stay small and self-contained. The shared Minni database is
not part of this factory.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Optional, Tuple

from minni.config import DEFAULT_CONFIG, SovereignConfig
from minni.db import SovereignDB
from minni.indexer import VaultIndexer

INDEX_DIRNAME = ".index"
VAULT_DB_NAME = "vault.db"
VAULT_FAISS_NAME = "vault.faiss"
VAULT_FAISS_MANIFEST_NAME = "vault.manifest.json"


@dataclass(frozen=True)
class VaultIndexPaths:
    index_dir: Path
    db_path: Path
    faiss_index_path: Path
    faiss_manifest_path: Path


def vault_index_paths(vault_path: str | Path) -> VaultIndexPaths:
    vault = Path(vault_path).expanduser()
    index_dir = vault / INDEX_DIRNAME
    return VaultIndexPaths(
        index_dir=index_dir,
        db_path=index_dir / VAULT_DB_NAME,
        faiss_index_path=index_dir / VAULT_FAISS_NAME,
        faiss_manifest_path=index_dir / VAULT_FAISS_MANIFEST_NAME,
    )


def build_vault_index_config(
    vault_path: str | Path,
    *,
    base_config: Optional[SovereignConfig] = None,
) -> SovereignConfig:
    """Return a SovereignConfig pointed at ``<vault>/.index`` only."""
    base = base_config or DEFAULT_CONFIG
    paths = vault_index_paths(vault_path)
    return replace(
        base,
        vault_path=str(Path(vault_path).expanduser()),
        db_path=str(paths.db_path),
        graph_export_dir=str(paths.index_dir / "graphs"),
        faiss_index_path=str(paths.faiss_index_path),
        faiss_manifest_path=str(paths.faiss_manifest_path),
        writeback_enabled=False,
        writeback_path=str(paths.index_dir / "learnings"),
    )


def open_vault_index(
    vault_path: str | Path,
    *,
    base_config: Optional[SovereignConfig] = None,
) -> Tuple[SovereignDB, VaultIndexer]:
    """Open the vault-local DB and indexer for ``vault_path``."""
    cfg = build_vault_index_config(vault_path, base_config=base_config)
    db = SovereignDB(cfg)
    return db, VaultIndexer(db, cfg)
