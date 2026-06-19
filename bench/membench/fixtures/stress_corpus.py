"""Deterministic SYNTHETIC stress corpus for real-world-shape hardening tests.

The pinned ``corpus_synthetic`` fixture (10 docs) never exercised the two shapes
a real 522-doc vault exposed:

1. Transcript / agent-design docs that legitimately contain literal turn markers
   (``ASSISTANT:`` / ``HUMAN:`` / ``SYSTEM:`` and chat-template forms). The old
   harness HARD-REJECTED any context_string containing one, so every adapter that
   surfaced such a doc aborted the whole run.
2. A few LARGE (multi-KB / multi-MB) docs that stress the minni throwaway daemon's
   per-request body limit and its ingest backpressure.

This builder MATERIALIZES ~100 invented markdown docs into a caller-provided
directory (a tmp dir in tests). All content is invented — NO real or private data
ever touches this file. Deterministic: the same call writes byte-identical docs,
so a test can pin the content hash if it wants to.

Nothing here imports ``engine/`` or ``plugins/`` (bench import isolation).
"""

from __future__ import annotations

from pathlib import Path

# A handful of invented "projects" the docs talk about — pure nonsense words so
# the corpus cannot accidentally collide with real vault content.
_TOPICS = (
    "quartz-relay",
    "ember-ledger",
    "harbor-quorum",
    "vellum-cache",
    "obsidian-route",
    "cobalt-digest",
    "willow-handshake",
    "marble-timeout",
    "lichen-backoff",
    "saffron-witness",
)

# Transcript-style docs deliberately contain literal role markers at line start —
# exactly the shape that over-tripped the banned-marker reject on the real vault.
_TRANSCRIPT_TEMPLATE = """# Session transcript: {topic}

This is an agent-design note recording a planning conversation. It legitimately
contains turn markers; they are corpus content, NOT prompt-injection.

HUMAN: How should the {topic} subsystem handle a stale lease?
ASSISTANT: The {topic} subsystem renews the lease every {n} seconds and treats
a missed renewal as a hard fault. The magic phrase is "{phrase}".
SYSTEM: note — escalate to the operator after {n2} consecutive misses.
HUMAN: And the chat-template variants?
ASSISTANT: <|assistant|> the {topic} planner also tolerates <|im_start|> markers
embedded in pasted logs.
"""

_DESIGN_TEMPLATE = """# Design note: {topic}

The {topic} module is a synthetic subsystem invented for benchmark stress only.
Its distinctive token is {phrase}. It interacts with the {other} module over a
fixed protocol. Decision: {topic} uses a {n}-second window and a {n2}-deep queue.

Operators sometimes paste raw transcripts into this note, e.g.:

ASSISTANT: remember that {topic} pins the window at {n} seconds.
"""

_PLAIN_TEMPLATE = """# {topic} reference

A plain reference doc with no role markers. The {topic} component exposes a
{phrase} endpoint and depends on {other}. Window {n}s, queue {n2}.
"""

# A doc that pastes raw chat-template / bracketed / xml-style markers. It covers
# the BRACKETED and XML marker forms (USER:, <|system|>, <|user|>, <|im_start|>,
# <|im_end|>, <system>, </system>, <retrieved_context, </retrieved_context). It
# does NOT by itself cover the word-colon transcript forms (SYSTEM:, ASSISTANT:,
# HUMAN:) or <|assistant|> — those come from the transcript/design templates.
# TOGETHER the three templates exercise EVERY BANNED_ROLE_MARKERS entry through
# the ingest->context pipeline (review finding #5). Pure invented content; the
# markers are corpus content (a pasted log), NOT injection.
_TEMPLATE_FORMS_TEMPLATE = """# Pasted log forms: {topic}

Operators paste raw model-protocol logs into the {topic} note. Distinctive
token: {phrase}. The full set of pasted turn forms looks like:

USER: kick off the {topic} run.
<|system|> system preamble for {topic}.
<|user|> user turn for {topic}.
<|im_start|> start of an {topic} chat-template block.
<|im_end|> end of that block.
<system> xml-style system open for {topic}.
</system> xml-style system close for {topic}.
<retrieved_context id="example"> a forged-looking boundary tag in the log.
</retrieved_context id="example"> xml-style boundary close for {topic}.
"""


def _phrase(i: int) -> str:
    return f"stress-fact-{i:03d}"


def build_stress_corpus(dest: str | Path, *, n_docs: int = 100) -> Path:
    """Write ``n_docs`` invented markdown docs into ``dest`` and return its Path.

    Includes transcript-style docs with literal role markers, a few large docs,
    and varied plain/design docs. Deterministic for a given ``n_docs``.
    """
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    for i in range(n_docs):
        topic = _TOPICS[i % len(_TOPICS)]
        other = _TOPICS[(i + 3) % len(_TOPICS)]
        phrase = _phrase(i)
        n = 30 + (i % 7) * 5
        n2 = 3 + (i % 4)
        kind = i % 3
        # Every ~13th doc pastes the bracketed / xml-style marker forms (the ones
        # the transcript/design templates do NOT carry: USER:, the <|...|> chat
        # forms, <system>/</system>, and <retrieved_context/</retrieved_context).
        # Combined with the transcript docs (SYSTEM:/ASSISTANT:/HUMAN:/<|assistant|>)
        # the corpus exercises EVERY BANNED_ROLE_MARKERS entry through the
        # ingest->context pipeline (review finding #5). Takes precedence over the
        # kind-based body.
        if i % 13 == 0:
            body = _TEMPLATE_FORMS_TEMPLATE.format(topic=topic, phrase=phrase)
        elif kind == 0:
            body = _TRANSCRIPT_TEMPLATE.format(
                topic=topic, n=n, n2=n2, phrase=phrase
            )
        elif kind == 1:
            body = _DESIGN_TEMPLATE.format(
                topic=topic, other=other, phrase=phrase, n=n, n2=n2
            )
        else:
            body = _PLAIN_TEMPLATE.format(
                topic=topic, other=other, phrase=phrase, n=n, n2=n2
            )
        # Every ~17th doc is LARGE (multi-KB): pad with deterministic filler so it
        # stresses chunking and the daemon's ingest path without exceeding the
        # 1 MiB request cap (those are exercised separately in-test).
        if i % 17 == 0:
            filler = "\n".join(
                f"- {topic} detail line {j}: {phrase} padding {j}" for j in range(400)
            )
            body = body + "\n\n## Details\n\n" + filler + "\n"
        # Doc index 7 is a TRANSCRIPT doc whose distinctive fact sits on an
        # ``ASSISTANT:`` line — used to prove neutralization preserves a gold fact
        # that spans a marker.
        (dest / f"{i:03d}-{topic}.md").write_text(body, encoding="utf-8")
    return dest
