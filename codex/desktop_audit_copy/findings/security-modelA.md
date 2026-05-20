# Dimension 3: Security & Privilege Boundaries (Model A - Gemini 3.5 Flash)

## Security Vulnerabilities & Scan Results

### 1. Insecure Cryptographic Hash Algorithm (SHA1)
* **Rule ID:** `python.lang.security.insecure-hash-algorithms.insecure-hash-algorithm-sha1`
* **Severity:** Medium / Blocking
* **Files:**
  - [engine/afm_passes/procedure_extraction.py:30](file:///Users/hansaxelsson/Projects/sovereignMemory/engine/afm_passes/procedure_extraction.py#L30)
  - [engine/afm_passes/pruning.py:30](file:///Users/hansaxelsson/Projects/sovereignMemory/engine/afm_passes/pruning.py#L30)
  - [engine/afm_passes/reorganization.py:28](file:///Users/hansaxelsson/Projects/sovereignMemory/engine/afm_passes/reorganization.py#L28)
  - [engine/afm_passes/session_distillation.py:68](file:///Users/hansaxelsson/Projects/sovereignMemory/engine/afm_passes/session_distillation.py#L68)
  - [engine/afm_passes/synthesis.py:34](file:///Users/hansaxelsson/Projects/sovereignMemory/engine/afm_passes/synthesis.py#L34)
* **Summary:** The system uses SHA1 to generate a 10-character digest/ID from text content (title, trace_id, sources). SHA1 is not collision-resistant.
* **Recommendation:** Replace with `hashlib.sha256(...).hexdigest()[:10]`.

### 2. Dynamic urllib Usage (SSRF/Local File Read Risk)
* **Rule ID:** `python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected`
* **Severity:** Medium / Blocking
* **File:** [engine/afm_provider.py:211](file:///Users/hansaxelsson/Projects/sovereignMemory/engine/afm_provider.py#L211)
* **Summary:** `urllib.request.urlopen` is used with a dynamic endpoint config. Because `urllib` natively supports `file://` schemes, a compromised endpoint URL could lead to arbitrary local file reads.
* **Recommendation:** Restrict URL schemes to `http://` and `https://` before calling `urlopen`, or migrate to the `requests` library.

### 3. Dynamic SQL Construction (String Concatenation Code-Smell)
* **Rule ID:** `python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query`
* **Severity:** Low / Blocking
* **Files:**
  - [engine/agent_api.py:347](file:///Users/hansaxelsson/Projects/sovereignMemory/engine/agent_api.py#L347)
  - [engine/indexer.py:268](file:///Users/hansaxelsson/Projects/sovereignMemory/engine/indexer.py#L268)
  - [engine/retrieval.py:287](file:///Users/hansaxelsson/Projects/sovereignMemory/engine/retrieval.py#L287)
  - [engine/writeback.py:221](file:///Users/hansaxelsson/Projects/sovereignMemory/engine/writeback.py#L221)
* **Summary:** The engine constructs SQL query templates dynamically using f-strings or `.format()` to generate placeholder lists (e.g. `IN (?, ?, ?)`). Although the actual values are parameterized, using string formatting in SQL statements is a security risk if the list of values or the formatting logic is ever modified to bypass serialization.
* **Recommendation:** Use SQLAlchemy's expression builder or helper functions that securely parameterize lists without raw string formatting.
