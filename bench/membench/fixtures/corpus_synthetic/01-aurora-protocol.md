# Aurora Protocol

The Aurora Protocol is a fictional internal sync scheme used by the Lindgren
team for reconciling offline edits. It uses a three-phase commit: propose,
witness, seal. The witness phase requires at least two reachable peers.

Aurora was introduced in the 2024 winter planning cycle and replaced the older
Borealis handshake, which lacked a seal phase and so could leave half-applied
edits on flaky links.
