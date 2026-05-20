# Dimension 6: Operability & Lifecycle (Model B - Claude 4.6 Sonnet)

## Service Supervisor & Maintenance Review

### 1. launchd Daemon Deployment
The system provides a service configuration file `engine/launchd/com.openclaw.sovrd.plist.example` for running `sovrd` as a persistent background daemon under macOS.
- **Security Guardrails:** Uses `Umask=63` (octal 077), ensuring that log files and standard outputs are generated with mode `0600` (user-readable-only), preventing sensitive logs or memory excerpts from being read by other local processes.
- **Process Supervision:** Configured as a background launchd agent with `KeepAlive` enabled, ensuring automatic restart on crashes.

---

## Findings

### [Medium] [Operational] launchd plist contains unexpanded tildes (~) in path keys
* **File:** [engine/launchd/com.openclaw.sovrd.plist.example](file:///Users/hansaxelsson/Projects/sovereignMemory/engine/launchd/com.openclaw.sovrd.plist.example#L61)
* **Summary:** The example launchd plist uses the tilde path shorthand (e.g., `~/Library/Logs/...`) for standard output and standard error log paths. Because macOS `launchd` does not expand tilde (`~`) symbols inside string values, standard launchd will fail to write logs (either trying to write to a literal path named `~` relative to the working directory or failing to start because it cannot create the output files).
* **Recommendation:** Change `~/Library/Logs/` to `/Users/YOUR_USERNAME/Library/Logs/` in the program examples, or explicitly document that the paths *must* be absolute and cannot contain the tilde shorthand.

### [Low] [Operational] Lack of automatic log rotation
* **File:** [engine/launchd/com.openclaw.sovrd.plist.example](file:///Users/hansaxelsson/Projects/sovereignMemory/engine/launchd/com.openclaw.sovrd.plist.example)
* **Summary:** Because the daemon runs persistently and appends standard output and standard error directly to files, the log files will grow indefinitely over time. Without newsyslog/logrotate configurations, this can lead to disk exhaustion on long-running developer systems.
* **Recommendation:** Include an example `newsyslog` configuration file in `engine/launchd` or document how to configure log rotation on macOS.
