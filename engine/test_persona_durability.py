"""Tests for persona-durability: seed_hosted must PRESERVE an agent-authored
``## Persona`` block across a template re-render.

Root cause (pre-fix): seed_hosted rendered the hosted envelope from a pure
template on every run and overwrote both the source file and the DB doc, with
no read-back of the existing persona section. Any persona an agent grew across
sessions was silently wiped on the next propagation. These tests pin the
preserve_persona splice that fixes it.
"""

import sys
from pathlib import Path

SCRIPTS_DIR = str(
    Path(__file__).resolve().parent.parent
    / "plugins" / "minni" / "skills" / "minni-install" / "scripts"
)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import propagate  # noqa: E402


def _rendered(agent: str = "claude-code") -> str:
    return propagate.render_hosted_envelope(
        agent, "workspace-test", Path("/tmp/minni.sock"), Path("/tmp/vault")
    )


def _author_persona(envelope: str, body: str) -> str:
    """Return `envelope` with the agent-authored persona body filled in."""
    sec = propagate._extract_section(envelope, propagate.PERSONA_HEADER)
    assert sec is not None, "template must ship a persona section"
    start, end, _ = sec
    lines = envelope.splitlines(keepends=True)
    return "".join(lines[: start + 1]) + body + "".join(lines[end:])


def test_authored_persona_survives_rerender():
    """The core gate: an authored Persona block is carried into the fresh render."""
    authored_body = (
        "\nI am terse, skeptical, and verify before claiming done.\n"
        "I prefer shell/MCP tools over pixel-clicking.\n\n"
    )
    prior = _author_persona(_rendered(), authored_body)

    # Simulate a propagation re-render: fresh template + preserve step.
    fresh = _rendered()
    merged = propagate.preserve_persona(fresh, prior)

    assert "terse, skeptical, and verify before claiming done" in merged
    body = propagate._extract_section(merged, propagate.PERSONA_HEADER)[2]
    assert "pixel-clicking" in body


def test_placeholder_persona_is_not_preserved():
    """An untouched (placeholder-only) persona yields the fresh template as-is."""
    prior = _rendered()  # placeholder comment only, never authored
    fresh = _rendered()
    merged = propagate.preserve_persona(fresh, prior)
    assert merged == fresh


def test_no_prior_content_is_noop():
    fresh = _rendered()
    assert propagate.preserve_persona(fresh, None) == fresh
    assert propagate.preserve_persona(fresh, "") == fresh


def test_sections_after_persona_survive():
    """Preserving persona must not corrupt the trailing Operating Quirks section."""
    prior = _author_persona(_rendered(), "\nMy authored line.\n\n")
    merged = propagate.preserve_persona(_rendered(), prior)
    assert "My authored line." in merged
    assert "## Operating Quirks (agent-curated launchpad)" in merged
    assert "use_named_minni_capabilities_directly" in merged


def test_rerender_is_idempotent_after_preserve():
    """A second re-render of an already-merged envelope keeps the persona stable."""
    prior = _author_persona(_rendered(), "\nStable persona.\n\n")
    first = propagate.preserve_persona(_rendered(), prior)
    second = propagate.preserve_persona(_rendered(), first)
    assert "Stable persona." in second
    assert second == first


def test_persona_is_authored_detection():
    assert propagate._persona_is_authored("\nreal content\n") is True
    assert propagate._persona_is_authored("<!-- only a comment -->") is False
    assert propagate._persona_is_authored("\n   \n") is False
    assert (
        propagate._persona_is_authored(
            "<!-- Yours to write and revise. Minni imposes no personality; you choose your\n"
            "own here over time. Empty until you author it. -->\n"
        )
        is False
    )
