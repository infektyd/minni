-- Migration 026: Living Repo Map subsystem cards metadata (Wave 4 / BUG-15)
-- Named repo_map_cards to avoid colliding with legacy document_metadata(doc_id, path, agent, sigil).

CREATE TABLE IF NOT EXISTS repo_map_cards (
    doc_id INTEGER PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    repo_root TEXT NOT NULL,
    subsystem_slug TEXT NOT NULL,
    title TEXT NOT NULL,
    summary_line TEXT NOT NULL DEFAULT '',
    owned_files_json TEXT NOT NULL DEFAULT '[]',
    git_sha TEXT NOT NULL DEFAULT '',
    updated_at REAL NOT NULL,
    FOREIGN KEY(doc_id) REFERENCES documents(doc_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_repo_map_cards_ws ON repo_map_cards(workspace_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_repo_map_cards_ws_slug ON repo_map_cards(workspace_id, subsystem_slug);
