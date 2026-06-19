# Content Digest Format

Aurora content digests are SHA-256 over the normalized edit body, hex-encoded.
The Teal Ledger stores the first sixteen hex characters as a short id and the
full digest as the canonical reference.

Two edits with the same short id but different full digests are treated as a
collision and escalated to the Lindgren team lead.
