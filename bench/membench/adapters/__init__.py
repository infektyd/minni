"""membench adapters — each wraps one system under test in the §3.1 contract.

Slice s1 ships:
- ``stub`` — a deterministic in-memory adapter that proves the contract,
  corpus loader, and token-budget enforcement end-to-end (always available, no
  external deps), plus negative variants used by the conformance suite.
- ``minni`` — the real Minni adapter over the public Unix-socket JSON-RPC
  protocol, spun against an ISOLATED throwaway daemon (own temp socket + temp
  MINNI_HOME). Never touches the operator's live daemon/DB/socket.

Baseline adapters (naive_rag, native_platform, markdown_grep, llm_wiki,
sanity_random) are later slices.
"""
