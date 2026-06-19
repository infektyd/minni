"""membench — scrutiny-proof memory benchmark harness for Minni.

This package is ISOLATED from the Minni ``engine/`` and ``plugins/`` trees
(fairness control §7.5 of the design spec). Nothing under ``bench/`` imports a
private ``engine.*`` / ``plugins.*`` module. The ``minni`` adapter talks to the
Minni daemon ONLY through its public Unix-socket JSON-RPC interface — the same
interface any external client uses.

Slice s1 ships: the adapter contract + data types (§3), the frozen-corpus
loader with content-hash refusal + path-traversal guard (§5), the canonical
tokenizer (§7.8), pinned config (§7.7), a minimal Layer-1 runner with
harness-owned token-budget enforcement (§3.1/§9.4), a synthetic fixture corpus,
the ``minni`` adapter (isolated throwaway daemon), and a deterministic in-memory
stub adapter that proves the contract end-to-end.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"  # slice s1
