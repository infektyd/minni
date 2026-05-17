#!/usr/bin/env python3
"""
Sovereign Memory V3.1 — Agent API.

Single entry point for agents to interact with the Sovereign Memory system.
Supports hybrid retrieval (FAISS + FTS5), write-back learnings, episodic events,
and two-layer startup hydration (identity + knowledge).

Usage:
    python agent_api.py <agent_id> --identity        # Layer 1: who am I?
    python agent_api.py <agent_id> --context          # Both layers: identity + knowledge
    python agent_api.py <agent_id> --learn <content>  # Write-back a learning
    python agent_api.py <agent_id> <query>             # Runtime recall (Layer 2)
"""

import os
import sys
import time
from typing import Optional

from config import SovereignConfig, DEFAULT_CONFIG
from db import SovereignDB
from retrieval import RetrievalEngine
from episodic import EpisodicMemory
from writeback import WriteBackMemory


class SovereignAgent:
    """
    Single entry point for agents to interact with Sovereign Memory.

    Each agent gets:
    - identity_context(): whole-document load of IDENTITY.md + SOUL.md (Layer 1)
    - recall(query): hybrid retrieval with re-ranking and budget (Layer 2)
    - learn(content, category): store a new learning (write-back)
    - log(event_type, content): episodic event logging
    - startup_context(): formatted markdown for system prompt injection
    - thread operations: create/query conversation threads
    """

    def __init__(
        self,
        agent_id: str,
        config: SovereignConfig = DEFAULT_CONFIG,
        db: Optional[SovereignDB] = None,
    ):
        self.agent_id = agent_id
        self.config = config
        self.db = db or SovereignDB(config)
        self.retrieval = RetrievalEngine(self.db, config)
        self.episodic = EpisodicMemory(self.db, config)
        self.writeback = WriteBackMemory(self.db, config)

    # ── Identity (Layer 1: whole-document load) ────────────────

    def identity_context(self) -> str:
        """
        Load the agent's identity files (IDENTITY.md + SOUL.md) as whole documents.

        Identity is NOT chunked — it's loaded in full so the agent knows WHO it is
        before receiving any retrieved knowledge. This is Layer 1 of the two-layer
        hydration: identity first, sovereign memory second.

        Returns formatted markdown or empty string if no identity exists.
        """
        with self.db.cursor() as c:
            c.execute("""
                SELECT d.doc_id, d.path, d.agent, d.sigil, ce.chunk_text
                FROM documents d
                JOIN chunk_embeddings ce ON ce.doc_id = d.doc_id AND ce.chunk_index = 0
                WHERE d.agent = ? AND d.whole_document = 1
                ORDER BY d.path
            """, (f"identity:{self.agent_id}",))
            rows = c.fetchall()

        if not rows:
            return ""

        parts = []
        for row in rows:
            fname = row["path"].split("/")[-1].replace(".md", "").upper()
            content = row["chunk_text"] or ""
            parts.append(f"### {fname}\n{content}")

        header = (
            f"## Agent Identity: {self.agent_id.title()}\n"
            f"Loaded whole (not chunked). This is WHO you are.\n\n"
        )
        return header + "\n\n".join(parts)

    # ── Recall (Layer 2: chunked RAG) ──────────────────────────

    def recall(
        self,
        query: str,
        limit: int = 5,
        format: str = "markdown",
    ) -> str:
        """
        Hybrid retrieval: FAISS semantic + FTS5 keyword, re-ranked.
        """
        results = self.retrieval.retrieve(
            query=query,
            agent_id=self.agent_id,
            limit=limit,
        )
        if not results:
            return f"No recall results for: {query}"
        if format == "markdown":
            parts = []
            for r in results:
                filename = r.get("filename", "")
                heading = r.get("heading_context", "")
                score = r.get("score", 0)
                text = r.get("chunk_text", "")
                header = f"### {filename}"
                if heading:
                    header += f" — {heading}"
                header += f" (score={score:.3f})"
                parts.append(f"{header}\n{text}")
            return "\n\n".join(parts)
        # raw: return as JSON-like string
        import json
        return json.dumps(results, indent=2, default=str)

    # ── Write-Back ─────────────────────────────────────────────

    def learn(
        self,
        content: str,
        category: str = "general",
        confidence: float = 1.0,
        source_doc_ids: Optional[list] = None,
        source_query: Optional[str] = None,
    ) -> int:
        """Store a new learning. Returns learning_id."""
        return self.writeback.store_learning(
            agent_id=self.agent_id,
            content=content,
            category=category,
            confidence=confidence,
            source_doc_ids=source_doc_ids,
            source_query=source_query,
        )

    # ── Episodic Events ───────────────────────────────────────

    def log(
        self,
        event_type: str,
        content: str,
        task_id: Optional[str] = None,
        thread_id: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> int:
        """Log an episodic event."""
        return self.episodic.log_event(
            agent_id=self.agent_id,
            event_type=event_type,
            content=content,
            task_id=task_id,
            thread_id=thread_id,
            metadata=metadata,
        )

    def start_task(self, description: str, task_id: Optional[str] = None) -> str:
        """Start tracking a task."""
        return self.episodic.start_task(
            agent_id=self.agent_id,
            description=description,
            task_id=task_id,
        )

    def end_task(self, task_id: str, status: str = "completed", result: Optional[str] = None):
        """End a tracked task."""
        self.episodic.end_task(self.agent_id, task_id, status, result)

    # ── Startup Context (Layer 2: knowledge graph) ─────────────

    def startup_context(self, limit: int = 5) -> str:
        """
        Get formatted context block for system prompt injection at agent startup.

        V3.1: Includes learnings and respects token budget.

        Combines:
        1. Agent-tagged vault documents (most accessed)
        2. Recent learnings from this agent
        3. Recent episodic events for this agent

        Identity documents (whole_document=1) are excluded here —
        they are loaded separately via identity_context() as Layer 1.
        """
        lines = []

        # Prior context — use agent_context table when available, fall back to agent tag
        with self.db.cursor() as c:
            # Check if agent_context has entries for this agent
            c.execute(
                "SELECT COUNT(*) FROM agent_context WHERE agent_id = ?",
                (self.agent_id,),
            )
            has_context = c.fetchone()[0] > 0

            if has_context:
                # Use agent_context with relevance scoring (includes wiki + vault)
                c.execute("""
                    SELECT d.doc_id, d.path, d.agent, d.sigil,
                           d.access_count, d.decay_score,
                           ac.relevance_score
                    FROM agent_context ac
                    JOIN documents d ON d.doc_id = ac.doc_id
                    WHERE ac.agent_id = ?
                      AND d.whole_document = 0
                    ORDER BY ac.relevance_score DESC,
                             d.last_accessed DESC NULLS LAST
                    LIMIT ?
                """, (self.agent_id, limit))
            else:
                # Fallback: agent-tagged docs + unknown + wiki docs
                c.execute("""
                    SELECT d.doc_id, d.path, d.agent, d.sigil,
                           d.access_count, d.decay_score,
                           (d.decay_score * d.access_count) as relevance_score
                    FROM documents d
                    WHERE (d.agent = ? OR d.agent = 'unknown'
                           OR d.agent LIKE 'wiki:%')
                      AND d.whole_document = 0
                    ORDER BY relevance_score DESC,
                             d.last_accessed DESC NULLS LAST
                    LIMIT ?
                """, (self.agent_id, limit))

            rows = c.fetchall()
            if rows:
                lines.append(f"## Prior Context ({self.agent_id})\n")
                for row in rows:
                    fname = os.path.basename(row["path"])
                    agent_tag = row['agent']
                    # Show wiki type differently from vault docs
                    if agent_tag.startswith('wiki:'):
                        source = agent_tag.replace('wiki:', '')
                    else:
                        source = agent_tag
                    line = (
                        f"- **{fname}** ({row['sigil']}) "
                        f"[{source}] "
                        f"accessed {row['access_count']}x, "
                        f"decay={row['decay_score']:.2f}"
                    )
                    lines.append(line)

        # Recent learnings
        with self.db.cursor() as c:
            c.execute("""
                SELECT learning_id, category, content, confidence, created_at
                FROM learnings
                WHERE agent_id = ? AND superseded_by IS NULL
                ORDER BY created_at DESC
                LIMIT 5
            """, (self.agent_id,))
            learn_rows = c.fetchall()
            if learn_rows:
                lines.append(f"\n## Learnings ({self.agent_id})\n")
                for row in learn_rows:
                    lines.append(
                        f"- [{row['category']}] {row['content'][:120]} "
                        f"(conf={row['confidence']:.1f})"
                    )

        # Recent episodic events
        with self.db.cursor() as c:
            c.execute("""
                SELECT event_type, content, created_at
                FROM episodic_events
                WHERE agent_id = ?
                ORDER BY created_at DESC
                LIMIT 3
            """, (self.agent_id,))
            ep_rows = c.fetchall()
            if ep_rows:
                lines.append(f"\n## Recent Activity ({self.agent_id})\n")
                for row in ep_rows:
                    ts = time.strftime(
                        "%Y-%m-%d %H:%M",
                        time.localtime(row["created_at"]),
                    )
                    lines.append(f"- [{row['event_type']}] {row['content'][:80]} ({ts})")

        if not lines:
            return f"No prior context for agent '{self.agent_id}'."

        return "\n".join(lines)

    # ── Thread Operations ──────────────────────────────────────

    def create_thread(self, title: str, thread_id: Optional[str] = None) -> str:
        """Create a new conversation thread."""
        return self.episodic.create_thread(title=title, thread_id=thread_id)

    def get_thread(self, thread_id: str) -> Optional[dict]:
        """Get thread details."""
        return self.episodic.get_thread(thread_id)

    def link_thread_doc(self, thread_id: str, doc_id: int, similarity: float):
        """Link a document to a thread."""
        return self.episodic.link_thread_doc(thread_id, doc_id, similarity)

    # ── Graph Export ────────────────────────────────────────────

    def export_graph(self, limit: int = 50) -> dict:
        """Export the agent's knowledge graph."""
        with self.db.cursor() as c:
            c.execute("""
                SELECT d.doc_id, d.path, d.agent, d.sigil, d.access_count
                FROM documents d
                WHERE d.agent = ? OR d.agent = 'unknown'
                ORDER BY d.access_count DESC
                LIMIT ?
            """, (self.agent_id, limit))
            nodes = [
                {
                    "id": row["doc_id"],
                    "path": row["path"],
                    "agent": row["agent"],
                    "sigil": row["sigil"],
                    "access_count": row["access_count"],
                }
                for row in c.fetchall()
            ]

            if not nodes:
                return {"nodes": [], "edges": []}

            node_ids = [n["id"] for n in nodes]
            placeholders = ",".join("?" * len(node_ids))
            c.execute(f"""
                SELECT source_doc_id, target_doc_id, link_type, weight
                FROM memory_links
                WHERE source_doc_id IN ({placeholders})
                   OR target_doc_id IN ({placeholders})
            """, node_ids + node_ids)
            edges = [
                {
                    "source": row["source_doc_id"],
                    "target": row["target_doc_id"],
                    "type": row["link_type"],
                    "weight": row["weight"],
                }
                for row in c.fetchall()
            ]

        return {"nodes": nodes, "edges": edges}

    # ── Close ──────────────────────────────────────────────────

    def close(self):
        """Close database connection."""
        self.db.close()


# ── CLI Entry Point ────────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: agent_api.py <agent_id> <command>")
        print("  Commands: --identity, --context, --learn <content>, <query>")
        sys.exit(1)

    agent_id = sys.argv[1]
    agent = SovereignAgent(agent_id)

    if sys.argv[2] == "--context":
        # Layer 2 only: knowledge (chunked RAG). Excludes identity docs.
        limit = 10
        if "--limit" in sys.argv:
            idx = sys.argv.index("--limit")
            if idx + 1 < len(sys.argv):
                limit = int(sys.argv[idx + 1])
        print(agent.startup_context(limit=limit))
    elif sys.argv[2] == "--identity":
        # Layer 1 only: identity (whole document)
        print(agent.identity_context())
    elif sys.argv[2] == "--full":
        # Both layers: identity first, then knowledge
        limit = 10
        if "--limit" in sys.argv:
            idx = sys.argv.index("--limit")
            if idx + 1 < len(sys.argv):
                limit = int(sys.argv[idx + 1])
        identity = agent.identity_context()
        knowledge = agent.startup_context(limit=limit)
        if identity:
            print(identity)
        if knowledge:
            print(knowledge)
    elif sys.argv[2] == "--learn":
        content = " ".join(sys.argv[3:])
        lid = agent.learn(content)
        print(f"Stored learning #{lid}")
    else:
        query = " ".join(sys.argv[2:])
        print(agent.recall(query, limit=5))

    agent.close()
