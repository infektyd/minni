# Dimension 4: Multi-Model Evaluation & Quality (Model A - Gemini 3.5 Flash)

## Evaluation Capabilities & Metrics

### 1. Recall Evaluation Harness
* **Files:**
  - [engine/eval/harness.py](file:///Users/hansaxelsson/Projects/sovereignMemory/engine/eval/harness.py)
  - [engine/eval/dataset.py](file:///Users/hansaxelsson/Projects/sovereignMemory/engine/eval/dataset.py)
* **Metrics Tracked:**
  - **Recall@k (k=1, 3, 5, 10):** Measures proportion of expected document IDs retrieved within the top k results.
  - **nDCG@10 (Normalized Discounted Cumulative Gain):** Measures ranking quality, supporting graded relevance judgments.
  - **TB-R@5 (Token-Budget-Recall@5):** Evaluates recall efficiency under a strict token budget.
  - **MRR (Mean Reciprocal Rank):** Evaluates rank of the first relevant result.
  - **Mean Calibration Error:** Evaluates model self-confidence scores.
  - **Latency:** Measures average search response time in seconds.

### 2. Live Scenarios & Regression Suite
* **File:** [scripts/afm-sovereign-scenarios.mjs](file:///Users/hansaxelsson/Projects/sovereignMemory/scripts/afm-sovereign-scenarios.mjs)
* **Summary:** The scenario suite tests the integration of Apple Foundation Models (AFM) under 8 distinct prompts/scenarios:
  1. *Harvesting:* Synthesizing text to a single durable learning.
  2. *Evidence Summary:* Merging a multi-agent team report.
  3. *Privacy Gate:* Deciding if a learning contains secrets or absolute paths.
  4. *Recall Expansion (HyDE):* Generating query alternatives.
  5. *Auto-Tagging:* Extracting topics.
  6. *Promotion Classifier:* Evaluating temporary agents.
  7. *Deduplication:* Classifying duplicate/merge/distinct states.
  8. *Scar Tissue:* Extracting warning lessons from failures.

---

## Findings

### [Low] [Usability] Illustrative-only expected doc IDs in evaluation dataset
* **File:** [eval/queries.jsonl](file:///Users/hansaxelsson/Projects/sovereignMemory/eval/queries.jsonl)
* **Summary:** The default evaluation query file `eval/queries.jsonl` contains hardcoded `expected_doc_ids` (e.g. `8412`, `8413`). These IDs are illustrative placeholders and do not correspond to actual documents in a newly initialized workspace database. Running the live search evaluation out-of-the-box (without the `--mock` flag) fails to evaluate actual retrieval quality unless an operator manually modifies the database content and edits the JSONL queries file to match the generated SQLite document IDs.
