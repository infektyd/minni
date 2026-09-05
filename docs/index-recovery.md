# Recover accepted memory indexing

An accepted learning is committed even if its response says `indexed: false`.
Do not submit it again or repeat acceptance to repair an index: that can create
another proposal, and terminal candidate decisions are not a reindex interface.
The learning may already be available through learning recall while missing from
document search.

The daemon normally runs bounded background repair every hour. The
`MINNI_BACKFILL_INTERVAL` setting changes that interval, with a five-minute
minimum; `MINNI_BACKFILL=off` disables it. Once the underlying database or model
failure is resolved, healthy passes repair missing learning embeddings, rebuild
absent document projections of active committed learnings, and fill missing
vectors in existing documents. The running shared index is refreshed; affected
per-vault retrieval caches are rebuilt on the daemon path.

Reconstruction rechecks the current owner, content and lifecycle in the same
transaction that publishes the document. Rejected, expired or superseded
learnings do not regain visibility through repair. Private content retains its
privacy metadata and fixed learning type; repair does not grant cross-agent
access. Repeated passes update derived indexes without repeating a governance
decision or creating another learning.

Check daemon logs for `projection batch` results. `examined`, `missing`,
`repaired`, `skipped`, and `failed` describe one bounded batch, not the complete
backlog. The default batch examines up to 200 active learning rows per index;
a cursor advances so a failing row cannot permanently block later rows. Large
stores can need multiple scheduled passes. A repaired document can still await
vector repair if its encoder remains unavailable. Existing embedding-coverage
ratios describe existing rows and cannot alone prove that every learning has a
document projection.

`python -m minni.index_all` indexes disk sources and rebuilds their vector index;
it does not reconstruct virtual learning documents from committed rows. Use the
running daemon's repair path for that case. There is no new operator retry RPC
in this change. A configured repair loop, available dependencies and clean logs
are prerequisites; no fixed recovery latency is guaranteed.
