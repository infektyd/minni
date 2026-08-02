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
--   2. The identity layer is never assigned here. _infer_layer grants identity
--      only for an 'identity:'-prefixed agent, which is assignable solely
--      through the trusted seed path — inferring it from stored frontmatter is
--      exactly the self-assignment indexer.py:95-104 strips as untrusted.
--
--   3. chunk_embeddings rows follow their parent document rather than being
--      re-derived, so a chunk can never end up on a different layer than the
--      document it belongs to.

UPDATE documents
   SET layer = 'artifact'
 WHERE LOWER(COALESCE(page_type, '')) = 'artifact'
   AND COALESCE(layer, 'knowledge') = 'knowledge';

UPDATE chunk_embeddings
   SET layer = 'artifact'
 WHERE COALESCE(layer, 'knowledge') = 'knowledge'
   AND doc_id IN (SELECT doc_id FROM documents WHERE layer = 'artifact');
