-- Migration 017: repair documents mislabeled layer='knowledge' by vault_ingest.
--
-- Audit GA6-1. afm_passes/vault_ingest.py hardcoded layer='knowledge' on all
-- three of its writes (the documents UPDATE, the documents INSERT and the
-- chunk_embeddings INSERT) while indexer.VaultIndexer._extract_metadata infers
-- the layer from page_type. Two writers, two answers, and the one that ran on
-- every vault sweep was the wrong one: every artifact-typed page ingested
-- through that pass was stamped 'knowledge' and became invisible to
-- layer-scoped recall — 88 artifact-typed documents across 5 vault DBs.
--
-- The writer is fixed (it now calls _infer_layer), but a writer fix only helps
-- pages that get re-ingested, and vault_ingest skips unchanged files by mtime.
-- Without this backfill those 88 documents stay mislabeled until someone edits
-- each file. So the existing rows are repaired here.
--
-- The repair rule is exactly the writer's own inference, not a guess:
-- _infer_layer maps page_type='artifact' to layer='artifact'. Applying the same
-- rule to stored rows cannot disagree with what a re-ingest would produce.
--
-- Deliberately conservative in three ways:
--
--   1. Only rows whose layer is currently 'knowledge' are touched. A row that
--      already says 'artifact' is correct, and a row saying anything else was
--      set by a path this migration knows nothing about.
--
--   2. The identity repair below keys on whole_document + the 'identity:' agent
--      prefix as STORED, never on page frontmatter. That distinction is the
--      whole point: inferring identity from frontmatter is the self-assignment
--      indexer.py:95-104 strips as untrusted, but a stored whole_document row
--      with an 'identity:' agent can only have come from the trusted seed path.
--
--   3. chunk_embeddings rows follow their parent document rather than being
--      re-derived, so a chunk can never end up on a different layer than the
--      document it belongs to.
--
-- grok-review round 1 (finding 4): the first cut repaired artifact rows only and
-- explicitly declined to touch identity. That was over-cautious and left a real
-- hole. seed_identity skips agents that are already seeded, so its writer fix
-- never revisits an existing envelope — and any envelope seeded AFTER migration
-- 004's one-shot backfill kept layer=NULL, which COALESCE(layer,'knowledge')
-- reads back as knowledge. A layers=['identity'] recall still missed them. The
-- rule below is not new: it is verbatim the trusted mapping migration 004
-- already applied (`WHEN whole_document = 1 AND agent LIKE 'identity:%' THEN
-- 'identity'`), re-run over the rows written since. The PR's own argument —
-- knowledge is the one layer an identity envelope must never be — applies to
-- stored rows exactly as it applies to new seeds.

UPDATE documents
   SET layer = 'artifact'
 WHERE LOWER(COALESCE(page_type, '')) = 'artifact'
   AND COALESCE(layer, 'knowledge') = 'knowledge';

UPDATE chunk_embeddings
   SET layer = 'artifact'
 WHERE COALESCE(layer, 'knowledge') = 'knowledge'
   AND doc_id IN (SELECT doc_id FROM documents WHERE layer = 'artifact');

-- Identity repair (grok-review finding 4). Ordered AFTER the artifact pass so
-- an identity envelope that also carries page_type='artifact' ends on the
-- identity layer: identity is the stronger claim, and it is the one
-- _infer_layer itself checks first.
UPDATE documents
   SET layer = 'identity'
 WHERE whole_document = 1
   AND agent LIKE 'identity:%'
   AND COALESCE(layer, 'knowledge') IN ('knowledge', 'artifact');

UPDATE chunk_embeddings
   SET layer = 'identity'
 WHERE COALESCE(layer, 'knowledge') != 'identity'
   AND doc_id IN (SELECT doc_id FROM documents WHERE layer = 'identity');
