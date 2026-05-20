# Pipeline & CI/CD Inventory
**Date:** 2026-05-19
**Dimension:** CI/CD & Pipeline

| Component | Type | Status | Location | Notes |
|-----------|------|--------|----------|-------|
| GitHub Actions | CI/CD | **Missing** | `.github/workflows/` | No automation configured. |
| Dependency Management | Python | Partial | `engine/requirements.txt` | Unpinned upper bounds; no lockfile. |
| Dependency Management | Node.js | Good | `plugins/sovereign-memory/package.json` | `package-lock.json` present. |
| Commit Guards | Githooks | Good | `.githooks/pre-commit` | Prevents private data leakage. |
| Security Scanning | SAST | **Missing** | N/A | No CodeQL or similar. |
| Deployment | Manual | Fragile | `engine/launchd/` | Example plist only. |
| Environment | Local | macOS-centric | Project Root | Lack of Docker/Linux parity. |
