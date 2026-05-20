"""
Sovereign Memory V3.1 — Graph Exporter.

V3.1 changes:
- No compression — semantic edges computed in raw float32 space
- Uses FAISS index for similarity if available, falls back to numpy
"""

import os
import json
import time
import logging
from typing import Dict, List, Optional

import numpy as np

from config import SovereignConfig, DEFAULT_CONFIG
from db import SovereignDB

logger = logging.getLogger("sovereign.graph")


class GraphExporter:
    """Export Sovereign Memory graph for visualization."""

    def __init__(
        self,
        db: SovereignDB,
        config: SovereignConfig = DEFAULT_CONFIG,
    ):
        self.db = db
        self.config = config

    def export(
        self,
        agent_filter: Optional[str] = None,
        semantic_edge_threshold: float = 0.45,
        include_episodic: bool = True,
        include_threads: bool = True,
    ) -> Dict:
        """Export full graph as Cytoscape.js JSON."""
        nodes = []
        edges = []
        colors = self.config.agent_colors

        with self.db.cursor() as c:
            # === Document nodes ===
            query = "SELECT doc_id, path, agent, sigil, access_count, decay_score FROM documents"
            params = []
            if agent_filter:
                query += " WHERE agent = ?"
                params.append(agent_filter)

            c.execute(query, params)
            doc_ids = set()
            for row in c.fetchall():
                did = row["doc_id"]
                doc_ids.add(did)
                nodes.append({
                    "data": {
                        "id": f"doc_{did}",
                        "label": os.path.basename(row["path"]),
                        "type": "doc",
                        "agent": row["agent"],
                        "sigil": row["sigil"],
                        "access_count": row["access_count"],
                        "decay_score": round(row["decay_score"] or 1.0, 2),
                        "title": row["path"],
                    },
                    "style": {
                        "background-color": colors.get(row["agent"], colors["unknown"]),
                        "width": min((row["access_count"] or 0) * 5 + 20, 60),
                        "height": min((row["access_count"] or 0) * 5 + 20, 60),
                        "opacity": max(0.3, row["decay_score"] or 1.0),
                    },
                })

            # === Explicit memory links ===
            c.execute("SELECT source_doc_id, target_doc_id, link_type, weight FROM memory_links")
            explicit_edges = 0
            for row in c.fetchall():
                if row["source_doc_id"] in doc_ids and row["target_doc_id"] in doc_ids:
                    edges.append({
                        "data": {
                            "source": f"doc_{row['source_doc_id']}",
                            "target": f"doc_{row['target_doc_id']}",
                            "link_type": row["link_type"],
                            "weight": row["weight"] or 1.0,
                        },
                        "style": {
                            "width": 2,
                            "line-color": "#0088FF",
                        },
                    })
                    explicit_edges += 1

            # === Semantic edges (from chunk embeddings) ===
            if explicit_edges == 0:
                sem_edges = self._compute_semantic_edges(
                    c, doc_ids, threshold=semantic_edge_threshold
                )
                edges.extend(sem_edges)

            # === Thread nodes + edges ===
            if include_threads:
                try:
                    c.execute("SELECT thread_id, title, agent_count, message_count FROM threads")
                    for row in c.fetchall():
                        tid = row["thread_id"]
                        size = min(30 + (row["message_count"] or 0) * 5, 80)
                        nodes.append({
                            "data": {
                                "id": f"thread_{tid}",
                                "label": row["title"] or f"Thread {tid}",
                                "type": "thread",
                                "message_count": row["message_count"],
                                "agent_count": row["agent_count"],
                                "shape": "hexagon",
                            },
                            "style": {
                                "background-color": "#FFD700",
                                "width": size,
                                "height": size,
                                "shape": "hexagon",
                            },
                        })

                    c.execute("""
                        SELECT tdl.thread_id, tdl.doc_id, tdl.similarity
                        FROM thread_doc_links tdl
                    """)
                    for row in c.fetchall():
                        if row["doc_id"] in doc_ids:
                            sim = row["similarity"]
                            edges.append({
                                "data": {
                                    "source": f"thread_{row['thread_id']}",
                                    "target": f"doc_{row['doc_id']}",
                                    "link_type": "propagation",
                                    "weight": sim,
                                },
                                "style": {
                                    "width": max(2, int(sim * 4)),
                                    "line-color": "#FFD700",
                                    "line-style": "dashed",
                                    "opacity": float(min(0.9, sim)),
                                },
                            })
                except Exception:
                    pass

            # === Episodic event nodes ===
            if include_episodic:
                try:
                    c.execute("""
                        SELECT event_id, agent_id, event_type, content
                        FROM episodic_events
                        WHERE thread_id IS NOT NULL
                        ORDER BY created_at DESC
                        LIMIT 50
                    """)
                    for row in c.fetchall():
                        eid = row["event_id"]
                        nodes.append({
                            "data": {
                                "id": f"event_{eid}",
                                "label": row["event_type"],
                                "type": "event",
                                "agent": row["agent_id"],
                                "content": (row["content"] or "")[:50],
                            },
                            "style": {
                                "background-color": colors.get(row["agent_id"], colors["unknown"]),
                                "width": 12,
                                "height": 12,
                                "shape": "diamond",
                            },
                        })
                except Exception:
                    pass

        return {
            "nodes": nodes,
            "edges": edges,
            "metadata": {
                "node_count": len(nodes),
                "edge_count": len(edges),
                "agent_filter": agent_filter,
                "exported_at": time.time(),
            },
        }

    def _compute_semantic_edges(
        self,
        cursor,
        doc_ids: set,
        threshold: float = 0.45,
    ) -> List[Dict]:
        """
        Compute semantic edges between documents using chunk embeddings.
        All comparisons in raw float32 space (no compression).
        """
        doc_vecs: Dict[int, np.ndarray] = {}

        cursor.execute("""
            SELECT doc_id, embedding
            FROM chunk_embeddings
            WHERE chunk_index = 0
        """)

        for row in cursor.fetchall():
            did = row["doc_id"]
            if did not in doc_ids:
                continue
            vec = np.frombuffer(row["embedding"], dtype=np.float32)
            doc_vecs[did] = vec

        edges = []
        ids = list(doc_vecs.keys())
        for i, a in enumerate(ids):
            va = doc_vecs[a]
            na = np.linalg.norm(va)
            if na < 1e-8:
                continue
            va_n = va / na

            for b in ids[i + 1:]:
                vb = doc_vecs[b]
                nb = np.linalg.norm(vb)
                if nb < 1e-8:
                    continue

                sim = float(np.dot(va_n, vb / nb))
                if sim >= threshold:
                    edges.append({
                        "data": {
                            "source": f"doc_{a}",
                            "target": f"doc_{b}",
                            "link_type": "semantic",
                            "weight": round(sim, 3),
                        },
                        "style": {
                            "width": max(1, int(sim * 3)),
                            "line-color": "#0088FF",
                            "opacity": float(min(0.8, sim)),
                        },
                    })

        return edges

    def export_to_file(
        self,
        output_path: Optional[str] = None,
        agent_filter: Optional[str] = None,
    ) -> str:
        """Export graph to JSON file."""
        if output_path is None:
            self.config.ensure_dirs()
            suffix = f"_{agent_filter}" if agent_filter else "_all"
            output_path = os.path.join(
                self.config.graph_export_dir, f"graph{suffix}.json"
            )

        data = self.export(agent_filter=agent_filter)
        with open(output_path, "w") as f:
            json.dump(data, f, indent=2)

        logger.info(
            "Exported %s: %d nodes, %d edges",
            output_path, data["metadata"]["node_count"], data["metadata"]["edge_count"],
        )
        return output_path


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    db = SovereignDB()
    exporter = GraphExporter(db)
    path = exporter.export_to_file()
    print(f"Exported: {path}")
    db.close()
