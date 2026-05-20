# Pipeline & CI/CD Findings: Sovereign Memory (Agent A Findings)
**Dimension:** CI/CD & Pipeline
**Model:** Gemini 3.5 Flash (High)
**Date:** 2026-05-19

## 1. CI/CD Infrastructure
- **P0: Total Absence of CI Automation**: The `.github/workflows` directory is empty/non-existent. There are no automated tests running on push or pull requests.
- **P1: Manual "Passing Tests" Claim**: README claims "454 passing tests," but this is a manual badge update and cannot be verified by a third party or CI system.

## 2. Dependency Management
- **P1: Fragile Python Requirements**: `engine/requirements.txt` lacks exact version pinning and upper bounds for core ML libraries (`sentence-transformers`, `faiss-cpu`). This will lead to "it works on my machine" bugs as upstream libraries release breaking changes.
- **P2: Missing Lockfiles**: No `poetry.lock` or `requirements.txt` with hashes. Build reproducibility is not guaranteed.

## 3. Security & Quality Scanning
- **P1: No Automated SAST**: Missing CodeQL, Bandit, or any static analysis in the pipeline.
- **P1: No Secret Scanning**: No `dependabot.yml` or secret-scanning workflows detected.

## 4. Deployment & Parity
- **P2: Manual Deployment Fragility**: Deployment is driven by manual `launchd` plist configuration (`com.openclaw.sovrd.plist.example`). No automated deployment or rollback scripts.
- **P2: OS-Centric Lock-in**: The project is heavily optimized for macOS (launchd, AF_UNIX). No CI testing for Linux or Windows environments despite generic Python/Node claims.
