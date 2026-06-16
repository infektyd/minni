# Witness Quorum

The witness phase of the Aurora Protocol needs a quorum of two reachable peers.
If only one peer is reachable, the edit stays in the proposed state and is
retried with exponential backoff up to five attempts.

A proposed edit that never reaches quorum is garbage-collected after seven days.
