# Teal Ledger

The Teal Ledger is an append-only log of sealed Aurora edits. Each entry
records the editor, the seal timestamp, and a content digest. The ledger is
sharded by calendar month.

Unique trace marker for over-count cross-checks: bb145163-d5e5-44a1-8869-214fd05a6b85

This UUID appears in exactly one fixture doc and is used by the doc-count
over-count cross-check (§9.5): an adapter that retrieves it proves it actually
indexed contents rather than reporting a hardcoded doc_count.
