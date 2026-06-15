"""Tests for persona-durability: seed_hosted must PRESERVE an agent-authored
``## Persona`` block across a template re-render.

Root cause (pre-fix): seed_hosted rendered the hosted envelope from a pure
template on every run and overwrote both the source file and the DB doc, with
no read-back of the existing persona section. Any persona an agent grew across
sessions was silently wiped on the next propagation. main preserves it by
reading the prior envelope and passing it as ``existing_content`` to
``render_hosted_envelope``, which splices the authored persona back in via
``extract_agent_persona``. These tests pin that behavior — including the
``## ``-subheading bounding hardening (persona is bounded by the Operating
Quirks header, not the first ``## ``).
"""

import re
import sys
from pathlib import Path

SCRIPTS_DIR = str(
    Path(__file__).resolve().parent.parent
    / "plugins" / "minni" / "skills" / "minni-install" / "scripts"
)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import propagate  # noqa: E402

QUIRKS_HEADER = "## Operating Quirks (agent-curated launchpad)"


def _rendered(agent: str = "claude-code", existing: str | None = None) -> str:
    return propagate.render_hosted_envelope(
        agent,
        "workspace-test",
        Path("/tmp/minni.sock"),
        Path("/tmp/vault"),
        existing_content=existing,
    )


def _author_persona(envelope: str, body: str) -> str:
    """Return `envelope` with the agent-authored persona body filled in,
    bounded by the Operating Quirks header (so a body containing `## `
    subheadings is inserted whole)."""
    pattern = re.compile(
        r"(?ms)(^## Persona \(agent-authored\)[ \t\r]*\n).*?(?=^## Operating Quirks)"
    )
    new, n = pattern.subn(lambda m: m.group(1) + body + "\n\n", envelope)
    assert n == 1, "template must ship a persona section bounded by Operating Quirks"
    return new


def test_authored_persona_survives_rerender():
    """The core gate: an authored Persona block is carried into the fresh render."""
    authored_body = (
        "I am terse, skeptical, and verify before claiming done.\n"
        "I prefer shell/MCP tools over pixel-clicking."
    )
    prior = _author_persona(_rendered(), authored_body)

    # Simulate a propagation re-render: fresh template + prior preserved.
    merged = _rendered(existing=prior)

    assert "terse, skeptical, and verify before claiming done" in merged
    assert "pixel-clicking" in propagate.extract_agent_persona(merged)


def test_placeholder_persona_is_not_preserved():
    """An untouched (placeholder-only) persona yields the fresh template as-is."""
    prior = _rendered()  # placeholder comment only, never authored
    merged = _rendered(existing=prior)
    assert merged == _rendered()
    assert propagate.extract_agent_persona(prior) == ""


def test_no_prior_content_is_noop():
    assert _rendered(existing=None) == _rendered()
    assert _rendered(existing="") == _rendered()


def test_sections_after_persona_survive():
    """Preserving persona must not corrupt the trailing Operating Quirks section."""
    prior = _author_persona(_rendered(), "My authored line.")
    merged = _rendered(existing=prior)
    assert "My authored line." in merged
    assert QUIRKS_HEADER in merged
    assert "use_named_minni_capabilities_directly" in merged


def test_rerender_is_idempotent_after_preserve():
    """A second re-render of an already-merged envelope keeps the persona stable."""
    prior = _author_persona(_rendered(), "Stable persona.")
    first = _rendered(existing=prior)
    second = _rendered(existing=first)
    assert "Stable persona." in second
    assert second == first


def test_persona_with_h2_subheadings_survives_intact():
    """An agent may structure their persona with `## ` subheadings; the whole
    body (not just the part before the first subheading) must be preserved.

    Regression: main's original boundary stopped at the first `## ` after the
    persona header, silently dropping everything from an agent's first h2
    onward. Persona is now bounded by the Operating Quirks header instead.
    """
    authored = "Intro line.\n\n## Voice\nterse, skeptical.\n\n## Style\nshell over pixels."
    prior = _author_persona(_rendered(), authored)
    merged = _rendered(existing=prior)

    assert "Intro line." in merged
    assert "## Voice" in merged and "terse, skeptical." in merged
    assert "## Style" in merged and "shell over pixels." in merged
    # And the template's own trailing section is untouched.
    assert QUIRKS_HEADER in merged
    assert "use_named_minni_capabilities_directly" in merged


def test_extract_agent_persona_detection():
    authored = _author_persona(_rendered(), "real content")
    assert "real content" in propagate.extract_agent_persona(authored)
    assert propagate.extract_agent_persona(_rendered()) == ""  # placeholder/comment-only
    assert propagate.extract_agent_persona(None) == ""
    assert propagate.extract_agent_persona("") == ""
