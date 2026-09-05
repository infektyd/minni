# Disposable MergeStorm integration fixture

This directory has no production imports or runtime integration. It tests an inclusive capacity contract: a reservation may exactly fill capacity, negative counts are rejected, and remaining capacity clamps at zero. These synthetic branches must not be merged or landed.

Run `python3 -m unittest discover -s tools/vortex-integration-fixture -v` from the repository root. The separate stacked layers exercise CLI topology and policy propagation, not a production release.
