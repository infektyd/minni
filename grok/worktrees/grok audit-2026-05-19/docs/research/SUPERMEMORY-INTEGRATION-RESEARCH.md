# Sovereign Memory Integration Research

## Problem Statement
The current OpenClaw memory system suffers from "context pollution." By injecting too much historical or semi-relevant data into the prompt, the agent's attention is diluted. This leads to higher latency, increased costs, and "context distraction," where the model over-relies on past patterns rather than reasoning fresh for the current query. The goal is to move from passive "everything-injection" to active, "just-in-time" memory retrieval.

## Current State
OpenClaw maintains decentralized memory across several SQLite databases in `~/.openclaw/memory/`:
- **Agent-Specific Stores**: `syntra.sqlite`, `forge.sqlite`, `recon.sqlite`, `pulse.sqlite`.
- **Shared/Main Store**: `main.sqlite`.
- **Schema Features**:
    - `chunks` table with raw text and high-dimensional embeddings.
    - `chunks_fts` virtual table for FTS5 full-text search.
    - `embedding_cache` for optimized performance.
    - Metadata-driven isolation by agent name and file path.

---

## 5 Standard Integration Patterns

### Pattern 1: On-Demand Tool Retrieval (Lazy Retrieval)
- **What**: Remove all automatic memory injection. Instead, expose memory strictly as a tool (e.g., `recall_memory(query)`).
- **How**: The agent's system prompt includes a directive: "Use the recall_memory tool if you need context from past interactions or specific project history."
- **Pros**: Zero context bloat by default; agent explicitly decides what it needs.
- **Cons**: Adds one round-trip latency for a tool call; depends on agent capability to recognize the need.
- **Rationale**: Solves the problem of "dirtying context" by ensuring no memory is present unless it is specifically requested for the task at hand.

### Pattern 2: Hierarchical Consolidation (Episodic vs. Semantic)
- **What**: Maintain three distinct memory tiers: Working (current thread), Episodic (recent sessions), and Semantic (stable facts/preferences).
- **How**:
    - **Working**: Last 10 turns (raw).
    - **Episodic**: Previous 3-5 sessions (summarized).
    - **Semantic**: Long-term facts (retrieved via vector search).
- **Pros**: Preserves high-level continuity without raw transcript noise.
- **Cons**: Requires a "cleanup" process to summarize sessions after they end.
- **Rationale**: Reduces the token weight of past interactions by compressing them into "lessons learned" or "session highlights" before injection.

### Pattern 3: Dynamic Importance Thresholding & Reranking
- **What**: A two-stage retrieval process that strictly limits the injection "budget."
- **How**:
    - Stage 1: Retrieve top 20 candidates from SQLite via hybrid search (Vector + FTS5).
    - Stage 2: Rerank based on an importance score (Recency * Similarity * User-Defined Priority).
    - Injection: Only inject the top 2-3 items, and only if their score exceeds a "high confidence" threshold (e.g., 0.85).
- **Pros**: High signal-to-noise ratio.
- **Cons**: Requires tuning the threshold to avoid "memory amnesia."
- **Rationale**: Directly addresses over-injection by filtering out "vaguely relevant" noise that typically pollutes the context window.

### Pattern 4: Agent-Scoped Namespace Isolation
- **What**: Strictly isolate memory retrieval to the current agent's domain unless a cross-domain search is explicitly requested.
- **How**: Modify `memory_search` to default to `agent_id` matching the caller. If "Pulse" is running, it only sees `pulse.sqlite` chunks unless it calls a specialized `cross_agent_recall`.
- **Pros**: Minimizes irrelevant context from other specialized agents.
- **Cons**: Can limit collaborative intelligence if agents need shared knowledge.
- **Rationale**: Since OpenClaw uses per-agent SQLite files, this leverages the existing architecture to prune context based on the agent's specialized role.

### Pattern 5: Topic-Based Context Sliding (Slot Management)
- **What**: Allocate specific "memory slots" in the prompt (e.g., 2000 tokens) and swap content based on the current topic.
- **How**: Use a keyword extractor on the last 3 user messages. If the topic shifts (e.g., from "coding" to "billing"), flush the "coding" memories from the context and inject "billing" facts.
- **Pros**: Context stays highly relevant to the active sub-task.
- **Cons**: Topic detection adds overhead; can be jarring if the user switches topics rapidly.
- **Rationale**: Optimizes the context window as a "scarce resource," ensuring that every token spent on memory is directly related to the current conversation focus.

---

## 2 Wildcard Approaches

### Wildcard 1: The "Memory Consultant" (Shadow Agent)
- **What**: Instead of injecting text, the system spawns a invisible sub-agent that has full access to memory.
- **How**: The main agent sends a message to the "Memory Consultant": "Do we have any previous notes on the user's preferred Ruby version?" The consultant replies with a concise 1-sentence answer.
- **Why it might work**: It moves the "reasoning over memory" out of the main context. The main agent only gets the *answer* from the memory, not the raw *content*.
- **Risks**: Increased latency and cost due to the sub-agent call.

### Wildcard 2: Sentiment-Aware Emotional Memory
- **What**: Prioritize memories based on the emotional weight or user frustration levels during the original recording.
- **How**: When storing chunks, include a `sentiment_score`. During retrieval, prioritize moments where the user was frustrated or gave explicit "correction" feedback.
- **Why it might work**: Agents often repeat mistakes that previously annoyed the user. Prioritizing "corrective memories" helps the agent avoid repeating specific friction points.
- **Risks**: Sentiment analysis can be inaccurate; might miss neutral but technically vital facts.

---

## Recommendation
I recommend a **Hybrid of Pattern 1 (Lazy Retrieval) and Pattern 3 (Dynamic Reranking).**
1. **Tool-First**: By default, keep context clean. Teach agents to use `recall` tools.
2. **Quality Gate**: When the agent *does* call for memory, use a strict reranker to ensure only the absolute best matches are returned.
3. **Budget**: Never inject more than 3 memory chunks into a single turn unless the agent explicitly asks for a "full history dump."

## Implementation Hints
- **SQLite Optimization**: Enable WAL mode in all `.sqlite` files to allow the `memory_search` tool to run concurrently with background memory-writing processes.
- **Reranking**: Use a lightweight model (like `cross-encoder/ms-marco-MiniLM-L-6-v2`) for the second-pass reranking to keep latency under 100ms.
- **Metadata**: Add a `importance_score` column to the `chunks` table, allowing the system to decay "low-value" memories over time while keeping "user-pinned" facts forever.
