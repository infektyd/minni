"""
Sovereign Memory V3.1 — Markdown-Aware Chunker (Phase 2).

Phase 2 updates:
- Chunk size: 512 tokens (word approximation), 128 overlap (25%)
- Sentence-level snap: chunks snap to sentence boundaries, not raw token counts
- Min chunk: 64 tokens (drop smaller fragments)
- Max chunk: 1024 tokens (hard cap, code blocks truncated with flag)
- Code treatment: preserve code blocks as single chunks, flag if truncated
"""

import re
import logging
from typing import List, Tuple, Optional
from dataclasses import dataclass, field

import numpy as np

from config import SovereignConfig, DEFAULT_CONFIG
from tokens import count_tokens


# Sentence boundary regex: match end-of-sentence punctuation followed by space
_SENTENCE_END = re.compile(r'(?<=[.!?])\s+')

logger = logging.getLogger("sovereign.chunker")


def get_embedder():
    """Lazy embedder import for optional semantic chunk merging."""
    from models import get_embedder as _get_embedder
    return _get_embedder()


@dataclass
class Chunk:
    """A chunk of text with its heading and metadata context."""
    text: str
    heading: str           # The nearest heading above this chunk
    heading_path: str      # Full breadcrumb: "# Title > ## Section > ### Subsection"
    chunk_index: int       # Position within the document
    # Phase 2 metadata
    is_code: bool = False  # True if this chunk contains a code block
    truncated: bool = False  # True if chunk was truncated at max_tokens


class MarkdownChunker:
    """
    Markdown-aware document chunker with Phase 2 refinements.

    Strategy:
    1. Parse document into sections by headings
    2. Each section becomes one or more chunks
    3. Code blocks are kept atomic (single_chunk treatment)
    4. Sliding window snaps to sentence boundaries
    5. Chunks below min_tokens are dropped
    6. Heading breadcrumbs are attached to every chunk
    """

    def __init__(self, config: SovereignConfig = DEFAULT_CONFIG):
        self.chunk_size = config.chunk_size          # 512
        self.chunk_overlap = config.chunk_overlap    # 128
        self.strategy = config.chunk_strategy
        self.min_tokens = config.min_tokens          # 64
        self.max_tokens = config.max_tokens          # 1024
        self.sentence_snap = config.sentence_snap    # True
        self.code_treatment = config.code_treatment  # "single_chunk"
        self.semantic_merge = getattr(config, "chunking_semantic_merge", False)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def chunk_document(self, text: str) -> List[Chunk]:
        """
        Split a markdown document into chunks.

        Returns list of Chunk objects with heading context and metadata flags.
        """
        if self.strategy == "sliding":
            return self._finalize(self._sliding_window(text))

        # Markdown-aware chunking
        sections = self._split_sections(text)
        chunks = []
        idx = 0

        for heading_path, heading, body in sections:
            if not body.strip():
                continue

            # Try to keep each section as one chunk
            words = body.split()
            if len(words) <= self.chunk_size:
                is_code = self._is_code_block(body)
                chunks.append(Chunk(
                    text=body.strip(),
                    heading=heading,
                    heading_path=heading_path,
                    chunk_index=idx,
                    is_code=is_code,
                ))
                idx += 1
            else:
                # Section too large — split by atomic blocks, then sliding window
                blocks = self._split_atomic_blocks(body)
                for block in blocks:
                    block_words = block.split()
                    block_is_code = self._is_code_block(block)

                    # Code blocks: treat as single chunk, truncate at max
                    if block_is_code and self.code_treatment == "single_chunk":
                        if len(block_words) > self.max_tokens:
                            # Truncate with sentinel
                            truncated_words = block_words[:self.max_tokens]
                            truncated_text = " ".join(truncated_words)
                            truncated_text += "\n\n<!-- TRUNCATED: exceeded max_tokens -->"
                            chunks.append(Chunk(
                                text=truncated_text.strip(),
                                heading=heading,
                                heading_path=heading_path,
                                chunk_index=idx,
                                is_code=True,
                                truncated=True,
                            ))
                        else:
                            chunks.append(Chunk(
                                text=block.strip(),
                                heading=heading,
                                heading_path=heading_path,
                                chunk_index=idx,
                                is_code=True,
                            ))
                        idx += 1
                        continue

                    if len(block_words) <= self.chunk_size:
                        chunks.append(Chunk(
                            text=block.strip(),
                            heading=heading,
                            heading_path=heading_path,
                            chunk_index=idx,
                            is_code=block_is_code,
                        ))
                        idx += 1
                    else:
                        # Atomic block still too big — sliding window with sentence snap
                        sub_chunks = self._sliding_window_raw(
                            block_words, heading, heading_path, idx,
                            is_code=block_is_code,
                        )
                        chunks.extend(sub_chunks)
                        idx += len(sub_chunks)

        # Fallback: if no sections found (no headings), use sliding window
        if not chunks:
            return self._finalize(self._sliding_window(text))

        return self._finalize(chunks)

    # ------------------------------------------------------------------
    # Section parsing
    # ------------------------------------------------------------------

    def _split_sections(self, text: str) -> List[Tuple[str, str, str]]:
        """
        Split markdown into sections by headings.

        Returns: [(heading_path, heading_text, body_text), ...]
        """
        heading_pattern = re.compile(r'^(#{1,6})\s+(.+)$', re.MULTILINE)

        sections = []
        heading_stack: List[Tuple[int, str]] = []
        last_end = 0
        last_heading = ""
        last_path = ""

        for match in heading_pattern.finditer(text):
            body = text[last_end:match.start()]
            if last_end > 0 or body.strip():
                sections.append((last_path, last_heading, body))

            level = len(match.group(1))
            heading_text = match.group(2).strip()

            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            heading_stack.append((level, heading_text))

            last_path = " > ".join(h[1] for h in heading_stack)
            last_heading = heading_text
            last_end = match.end()

        body = text[last_end:]
        if body.strip():
            sections.append((last_path, last_heading, body))

        if not sections:
            sections = [("", "", text)]

        return sections

    # ------------------------------------------------------------------
    # Atomic blocks (code, lists, paragraphs)
    # ------------------------------------------------------------------

    def _split_atomic_blocks(self, text: str) -> List[str]:
        """
        Split text into atomic blocks that should not be broken:
        - Code blocks (``` ... ```)
        - Ordered/unordered list blocks (contiguous list items)
        - Regular paragraphs (split on double newline)

        Returns list of block strings.
        """
        blocks = []
        lines = text.split('\n')
        current_block = []
        in_code_block = False

        for line in lines:
            if line.strip().startswith('```'):
                if in_code_block:
                    current_block.append(line)
                    blocks.append('\n'.join(current_block))
                    current_block = []
                    in_code_block = False
                else:
                    if current_block:
                        blocks.append('\n'.join(current_block))
                        current_block = []
                    current_block.append(line)
                    in_code_block = True
                continue

            if in_code_block:
                current_block.append(line)
                continue

            if not line.strip():
                is_list = any(
                    l.strip().startswith(('-', '*', '+'))
                    or re.match(r'^\s*\d+\.', l)
                    for l in current_block
                )
                if current_block and not is_list:
                    blocks.append('\n'.join(current_block))
                    current_block = []
                elif current_block:
                    current_block.append(line)
                continue

            current_block.append(line)

        if current_block:
            blocks.append('\n'.join(current_block))

        return [b for b in blocks if b.strip()]

    def _is_code_block(self, text: str) -> bool:
        """Check if a block of text is primarily code (contains triple backticks)."""
        return '```' in text

    # ------------------------------------------------------------------
    # Sliding window with sentence snapping
    # ------------------------------------------------------------------

    def _sliding_window_raw(
        self,
        words: List[str],
        heading: str,
        heading_path: str,
        start_idx: int,
        is_code: bool = False,
    ) -> List[Chunk]:
        """Sliding window over a word list with sentence boundary snapping."""
        step = max(self.chunk_size - self.chunk_overlap, 1)
        chunks = []
        idx = start_idx

        for start in range(0, len(words), step):
            window = words[start:start + self.chunk_size]

            # Hard cap at max_tokens
            if len(window) > self.max_tokens:
                window = window[:self.max_tokens]

            # Sentence snapping: extend to nearest sentence boundary
            if self.sentence_snap and not is_code:
                window = self._snap_to_sentence(words, start, len(window))

            text = " ".join(window)

            if text.strip() and len(window) >= self.min_tokens:
                truncated = len(window) >= self.max_tokens
                chunks.append(Chunk(
                    text=text,
                    heading=heading,
                    heading_path=heading_path,
                    chunk_index=idx,
                    is_code=is_code,
                    truncated=truncated,
                ))
                idx += 1

            if start + self.chunk_size >= len(words):
                break

        return chunks

    def _snap_to_sentence(
        self,
        words: List[str],
        start: int,
        window_size: int,
    ) -> List[str]:
        """
        Adjust window end to snap to nearest sentence boundary.

        Looks ahead up to overlap_tokens to find a sentence end mark (.!?).
        If found, extends the window to include the full sentence.
        If not, keeps the original window (no snap needed).
        """
        end = start + window_size
        if end >= len(words):
            return words[start:end]

        # Look ahead up to half the overlap to find sentence boundary
        max_lookahead = max(self.chunk_overlap // 2, 5)
        lookahead_end = min(end + max_lookahead, len(words))
        lookahead = " ".join(words[start:lookahead_end])

        # Find the last sentence-ending punctuation within the lookahead
        matches = list(_SENTENCE_END.finditer(lookahead))
        if matches:
            # Find the sentence end closest to but not before position `window_size`
            best = None
            for m in matches:
                word_at_boundary = self._word_index_at(lookahead, m.start(), words[start:start + max_lookahead])
                if word_at_boundary is not None and word_at_boundary <= max_lookahead // 2 + window_size - len(words[start:end]):
                    # We found a reasonable sentence boundary
                    actual_end = start + word_at_boundary
                    if actual_end > end and actual_end <= lookahead_end:
                        best = words[start:actual_end]

            if best is not None:
                return best

        return words[start:end]

    def _word_index_at(self, text: str, char_pos: int, prefix_words: List[str]) -> Optional[int]:
        """
        Estimate which word index a character position falls at.
        Uses the prefix_words list to approximate.
        """
        # Approximate: count words up to char_pos in text
        subtext = text[:char_pos]
        return len(subtext.split())

    def _filter_and_finalize(self, chunks: List[Chunk]) -> List[Chunk]:
        """Apply min_tokens filter and reindex using tiktoken-accurate token counts."""
        result = []
        for chunk in chunks:
            token_count = count_tokens(chunk.text)
            if token_count < self.min_tokens:
                continue  # Drop undersized chunks
            result.append(chunk)

        # Reindex
        for i, chunk in enumerate(result):
            chunk.chunk_index = i

        return result

    def _finalize(self, chunks: List[Chunk]) -> List[Chunk]:
        """Apply normal finalization and the optional semantic merge post-pass."""
        chunks = self._filter_and_finalize(chunks)
        if self.semantic_merge:
            chunks = self._semantic_merge_chunks(chunks)
            chunks = self._filter_and_finalize(chunks)
        return chunks

    def _semantic_merge_chunks(self, chunks: List[Chunk]) -> List[Chunk]:
        """Merge adjacent same-heading chunks when their embeddings are very close."""
        if len(chunks) < 2:
            return chunks

        try:
            embedder = get_embedder()
        except Exception as exc:
            logger.warning("Semantic chunk merge disabled: embedder unavailable (%s)", exc)
            return chunks

        if embedder is None:
            logger.warning("Semantic chunk merge disabled: embedder unavailable")
            return chunks

        merged: List[Chunk] = []
        current = chunks[0]
        current_vec = None

        for nxt in chunks[1:]:
            if current.heading_path != nxt.heading_path:
                merged.append(current)
                current = nxt
                current_vec = None
                continue

            candidate_text = f"{current.text}\n\n{nxt.text}".strip()
            if count_tokens(candidate_text) > self.max_tokens:
                merged.append(current)
                current = nxt
                current_vec = None
                continue

            try:
                if current_vec is None:
                    current_vec = self._encode_for_merge(embedder, current.text)
                next_vec = self._encode_for_merge(embedder, nxt.text)
                similarity = self._cosine(current_vec, next_vec)
            except Exception as exc:
                logger.warning("Semantic chunk merge skipped for a pair: %s", exc)
                merged.append(current)
                current = nxt
                current_vec = None
                continue

            if similarity > 0.9:
                current = Chunk(
                    text=candidate_text,
                    heading=current.heading,
                    heading_path=current.heading_path,
                    chunk_index=current.chunk_index,
                    is_code=current.is_code or nxt.is_code,
                    truncated=current.truncated or nxt.truncated,
                )
                current_vec = self._average_vectors(current_vec, next_vec)
            else:
                merged.append(current)
                current = nxt
                current_vec = None

        merged.append(current)
        return merged

    @staticmethod
    def _encode_for_merge(embedder, text: str) -> np.ndarray:
        vec = embedder.encode(text)
        return np.asarray(vec, dtype=np.float32)

    @staticmethod
    def _cosine(a: np.ndarray, b: np.ndarray) -> float:
        denom = float(np.linalg.norm(a) * np.linalg.norm(b))
        if denom < 1e-8:
            return 0.0
        return float(np.dot(a, b) / denom)

    @staticmethod
    def _average_vectors(a: np.ndarray, b: np.ndarray) -> np.ndarray:
        return ((a + b) / 2.0).astype(np.float32)

    def _sliding_window(self, text: str) -> List[Chunk]:
        """Fallback: V3-style sliding window with sentence snapping."""
        words = text.split()
        return self._sliding_window_raw(words, "", "", 0)
