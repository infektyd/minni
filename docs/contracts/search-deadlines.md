# Cooperative search request deadlines

`search.timeout_ms` supplies a cooperative work budget. The daemon starts the
budget at request acceptance, including time waiting for its worker thread, and
reserves transport headroom. It does not cancel arbitrary native inference or
promise a strict wall-clock completion time.

The request budget applies to `SovereignDB` operations on the search worker,
including all database instances it uses. Schema and connection locks have
bounded acquisition; each SQL execute, fetch and commit recalculates the
remaining SQLite busy timeout. A VM progress handler interrupts long-running
queries. Ordinary calls outside a request retain their configured SQLite waits.
Connection busy timeouts and existing progress handlers are restored after each
operation, including failure. Expired transactions roll back; cleanup is allowed
after expiration. No background cancellation timer can interrupt a later request
that reuses the connection.

Generic SQL is fail-closed at remaining 0: a new statement raises, and the
VM progress handler stays installed so an expensive query cannot run
unbounded. ``allow_expired_sql`` is a narrow *entry* permit for (1)
completed-hybrid qty/calibration bookkeeping and (2) ranking-deadline
lexical FTS/chrono fills. It does not silence the progress handler.

After document retrieval, expiration skips optional packing, learning search,
episodic reads, and recall-trace writes/cleanup. A completed hybrid ranking
still receives document-access qty and score calibration. Deadline-poisoned
FTS-only rankings skip that accounting. A retrieve that expires becomes a
degraded 200 with whatever ranking already completed, not ``-32000``.
Caller-visible results always drop ``confidence_raw`` and private carriers.
Dirty transactions still roll back. The response reports skipped or interrupted
stages in `degradation` with `src=request`.
An empty omitted layer therefore does not claim that its corpus was searched.
If learning tracking expires after its read completed, that tracking transaction
rolls back and the completed learning rows remain available. If score calibration
expires partway through a response, confidence and its dependent action/rationale
fields retain their original consistent values. Earlier transactions that
finished before expiration remain committed.

This is cooperative containment. SQLite progress callbacks cannot interrupt
arbitrary Python callbacks, native model calls, or filesystem operations. Commit
and rollback may still wait for operating-system I/O. A strict execution deadline
would require a separate process lifecycle design, not a longer socket timeout
or cancellation of an `asyncio.to_thread` awaitable.

The scope uses a context variable. Code introducing a new executor inside search
must snapshot ``copy_context()`` on the submitting thread for **each** spawned
task (``bind_copied_deadline``) and enter that private copy on the worker.
The same ``Context`` object must not be ``run`` concurrently. Threads do not
inherit this request scope automatically; a reused worker must not observe a
previous request's budget after ``Context.run`` returns.
