# Dimension 4: Multi-Model Evaluation & Quality (Model B - Claude 4.6 Sonnet)

## Multi-Model Robustness & Fallback Integrity

### 1. AFM Bridge Fallback Architecture
The system supports two execution paths for Apple Foundation Models (AFM):
- **Native mode:** Runs a compiled Swift helper binary `native_afm_helper` which communicates directly with the macOS Apple Foundation Models framework.
- **Bridge mode:** Executes HTTP POST requests to an OpenAI-compatible endpoint (default `http://127.0.0.1:11437/v1/chat/completions`).

When configured in `auto` mode, the provider attempts native execution first, and catches any error (such as a missing binary, invalid JSON, or framework unavailability) to transparently fall back to bridge mode. This protects downstream tasks (e.g. contradiction detection, HyDE expansion, and privacy gating) from failing if the local macOS foundation model daemon is busy or unresponsive.

### 2. Contradiction Resolution Lifecycle
During a learn operation (PR-6), if the cosine similarity of the new learning's embedding to any existing learning exceeds `contradiction_threshold` (0.85), the operation is blocked (returning status `"contradiction"` with candidates).
To resolve, an agent must invoke `resolve_contradiction` which writes the new consolidated learning and marks the superseded learning IDs as status `"superseded"` in a single SQLite transaction.

---

## Findings

### [Low] [Robustness] String-parsing fragility in AFM JSON responses
* **File:** [engine/afm_provider.py](file:///Users/hansaxelsson/Projects/sovereignMemory/engine/afm_provider.py#L162-L180)
* **Summary:** The `invoke_native_afm` function expects the native helper's stdout to contain exactly a JSON object. If the binary outputs auxiliary system logs or errors to stdout (e.g. sandbox warnings or standard macOS library info logs), JSON parsing fails, causing the engine to flag the helper as unavailable and fallback to the bridge.
* **Recommendation:** Ensure the Swift binary outputs *only* JSON to `stdout`, and redirects all warnings/logs to `stderr`.

### [Low] [Semantic] Lack of dynamic validation on HyDE output format
* **File:** [engine/hyde.py](file:///Users/hansaxelsson/Projects/sovereignMemory/engine/hyde.py)
* **Summary:** When generating query expansions using AFM, the system instructs the model to return a JSON array of strings. If the model appends markdown fences (e.g. \`\`\`json ... \`\`\`) or introductory text (e.g. "Here are the alternate phrasings:"), the parser may fail and drop back to baseline retrieval.
* **Recommendation:** Wrap JSON parsing of model outputs with a regex cleaner that strips code fences and extracts raw JSON arrays.
