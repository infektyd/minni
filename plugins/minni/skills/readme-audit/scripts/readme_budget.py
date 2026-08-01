#!/usr/bin/env python3
"""Measure a README against the readme-audit budget table.

Why this is a script and not a checklist item: the retirement rule in SKILL.md
is arithmetic ("an addition of N prose words to a section at or over budget must
retire >= N words from that section"), and an agent estimating word counts by eye
gets it wrong in the direction that always favours adding. This measures.

It measures only. It never edits the README and never decides what to retire --
those are judgement calls the skill's procedure and the operator make.

    python3 readme_budget.py README.md
    python3 readme_budget.py README.md --json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

PREAMBLE = "(preamble)"
TOTAL = "(total)"

# Arrow forms mermaid uses for an edge.
_ARROW = re.compile(r"-{2,3}>|-\.-+>|={2,3}>|-{3,}(?![>-])")
# `ID[`, `ID[(`, `ID((`, `ID{`, `ID>` -- a node declaration with a shape.
_NODE_DECL = re.compile(r"\b([A-Za-z_][\w-]*)\s*(?:\[\(|\(\(|\[|\(|\{|>[^=])")
_EDGE_LABEL = re.compile(r"\|[^|]*\|")
_IDENT = re.compile(r"[A-Za-z_][\w-]*")


@dataclass
class Section:
    """One `##` section of the README, measured."""

    name: str
    lines: int
    words: int          # prose only: no headings, code blocks, or table rows
    code_lines: int
    table_rows: int     # content rows, excluding header and separator


@dataclass
class Diagram:
    """One ```mermaid block, measured as a topology."""

    start_line: int
    nodes: int
    edges: int
    node_names: list[str] = field(default_factory=list)


@dataclass
class Budget:
    """The ceiling for one section. `None` means that dimension is unbudgeted."""

    words: int | None = None
    lines: int | None = None
    table_rows: int | None = None


@dataclass
class Verdict:
    section: Section
    budget: Budget | None
    over: bool
    words_over: int
    lines_over: int
    rows_over: int


# ── The budget table ───────────────────────────────────────────────────────
#
# Derived from the README as measured on 2026-08-01: 175 lines, 1,429 prose
# words, 13 table rows, one 17-node diagram. Each section's ceiling is its
# current size rounded up to the next sensible stop, so the headroom is thin on
# purpose -- a cap the README is already ~89% into bites on the very next
# addition, which is the whole point. A generous cap is not a budget, it is a
# deferral.
#
# The section ceilings sum to 1,670, but TOTAL is 1,600: you cannot max every
# section at once. Local slack, global scarcity.
#
# See references/budget-rationale.md for the per-section reasoning.
DEFAULT_BUDGETS: dict[str, Budget] = {
    PREAMBLE: Budget(words=40, lines=10),
    "The problem": Budget(words=110),
    "What Minni is": Budget(words=300),
    "Recall is evidence, not instruction": Budget(words=140),
    "How it compares": Budget(words=140, table_rows=6),
    "Quickstart": Budget(words=550, lines=70),
    "Architecture at a glance": Budget(words=90, lines=45),
    "Status": Budget(words=240),
    "Documentation": Budget(words=20, table_rows=10),
    "Support": Budget(words=40),
    TOTAL: Budget(words=1600, lines=190),
}

# The diagram's own budget: it competes for the same one screen.
DIAGRAM_NODE_BUDGET = 18
DIAGRAM_EDGE_BUDGET = 16


def _split_sections(lines: list[str]) -> list[tuple[str, list[str]]]:
    """Split on `##` headings; content before the first one is the preamble."""
    sections: list[tuple[str, list[str]]] = [(PREAMBLE, [])]
    in_fence = False
    for line in lines:
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        if not in_fence and line.startswith("## "):
            sections.append((line[3:].strip(), [line]))
        else:
            sections[-1][1].append(line)
    return sections


def _measure_body(body: list[str]) -> tuple[int, int, int, int]:
    """Return (lines, prose words, code lines, table content rows)."""
    while body and not body[-1].strip():
        body.pop()

    words = code_lines = table_rows = 0
    in_fence = False
    for line in body:
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            code_lines += 1
            continue
        if in_fence:
            code_lines += 1
            continue
        if stripped.startswith("#"):
            continue
        if stripped.startswith("|") and stripped.endswith("|"):
            # A separator row (|---|---|) is structure, not content.
            if not re.fullmatch(r"\|[\s:\-|]+\|", stripped):
                table_rows += 1
            continue
        words += len(stripped.split())

    # A markdown table's first content row is its header.
    if table_rows:
        table_rows -= 1

    return len(body), words, code_lines, table_rows


def measure(path: Path) -> list[Section]:
    """Measure every section of a README."""
    lines = path.read_text(encoding="utf-8").splitlines()
    out = []
    for name, body in _split_sections(lines):
        n_lines, words, code_lines, table_rows = _measure_body(list(body))
        if name == PREAMBLE and n_lines == 0:
            continue
        out.append(Section(name, n_lines, words, code_lines, table_rows))
    return out


def measure_mermaid(path: Path) -> list[Diagram]:
    """Measure every ```mermaid block as a node/edge topology."""
    lines = path.read_text(encoding="utf-8").splitlines()
    diagrams: list[Diagram] = []
    block: list[str] | None = None
    start = 0

    for idx, line in enumerate(lines, start=1):
        stripped = line.strip()
        if block is None:
            if stripped.startswith("```mermaid"):
                block, start = [], idx
            continue
        if stripped.startswith("```"):
            diagrams.append(_measure_topology(block, start))
            block = None
            continue
        block.append(line)

    return diagrams


def _measure_topology(block: list[str], start_line: int) -> Diagram:
    nodes: dict[str, None] = {}
    edges = 0

    for line in block:
        stripped = line.strip()
        if not stripped or stripped.startswith("%%") or stripped == "end":
            continue
        if re.match(r"(flowchart|graph|sequenceDiagram|classDiagram)\b", stripped):
            continue

        for match in _NODE_DECL.finditer(stripped):
            nodes.setdefault(match.group(1), None)

        bare = _EDGE_LABEL.sub(" ", _strip_labels(stripped))
        arrows = _ARROW.findall(bare)
        if not arrows:
            continue
        edges += len(arrows)
        for endpoint in _ARROW.split(bare):
            ident = _IDENT.match(endpoint.strip())
            if ident:
                nodes.setdefault(ident.group(0), None)

    return Diagram(start_line=start_line, nodes=len(nodes), edges=edges,
                   node_names=sorted(nodes))


def _strip_labels(line: str) -> str:
    """Blank out `["..."]` shape labels so arrows inside a label are not edges."""
    return re.sub(r"[\[\(\{][^\[\]{}]*[\]\)\}]", " ", line)


def judge(section: Section, budget: Budget | None) -> Verdict:
    """Compare one section to its budget, reporting the overage, not just a flag."""
    if budget is None:
        return Verdict(section, None, False, 0, 0, 0)

    words_over = max(0, section.words - budget.words) if budget.words else 0
    lines_over = max(0, section.lines - budget.lines) if budget.lines else 0
    rows_over = max(0, section.table_rows - budget.table_rows) if budget.table_rows else 0

    return Verdict(section, budget, bool(words_over or lines_over or rows_over),
                   words_over, lines_over, rows_over)


def report(sections, budgets: dict[str, Budget]) -> list[Verdict]:
    return [judge(s, budgets.get(s.name)) for s in sections]


def _total(sections: list[Section]) -> Section:
    return Section(
        name=TOTAL,
        lines=sum(s.lines for s in sections),
        words=sum(s.words for s in sections),
        code_lines=sum(s.code_lines for s in sections),
        table_rows=sum(s.table_rows for s in sections),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("readme", type=Path, nargs="?", default=Path("README.md"))
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    if not args.readme.exists():
        print(f"no such file: {args.readme}", file=sys.stderr)
        return 2

    sections = measure(args.readme)
    verdicts = report([*sections, _total(sections)], DEFAULT_BUDGETS)
    diagrams = measure_mermaid(args.readme)

    if args.json:
        print(json.dumps({
            "sections": [asdict(v.section) | {
                "over": v.over, "words_over": v.words_over,
                "lines_over": v.lines_over, "rows_over": v.rows_over,
                "budgeted": v.budget is not None,
            } for v in verdicts],
            "diagrams": [asdict(d) for d in diagrams],
            "diagram_budget": {"nodes": DIAGRAM_NODE_BUDGET, "edges": DIAGRAM_EDGE_BUDGET},
        }, indent=2))
        return 0

    print(f"{'section':<38} {'lines':>7} {'words':>13} {'rows':>9}")
    print("-" * 70)
    for v in verdicts:
        s, b = v.section, v.budget
        if b is None:
            print(f"{s.name:<38} {s.lines:>7} {s.words:>13} {s.table_rows:>9}   UNBUDGETED")
            continue
        line_cell = f"{s.lines}/{b.lines}" if b.lines else str(s.lines)
        word_cell = f"{s.words}/{b.words}" if b.words else str(s.words)
        row_cell = f"{s.table_rows}/{b.table_rows}" if b.table_rows else str(s.table_rows)
        flag = f"   OVER by {v.words_over}w {v.lines_over}l {v.rows_over}r" if v.over else ""
        print(f"{s.name:<38} {line_cell:>7} {word_cell:>13} {row_cell:>9}{flag}")

    for d in diagrams:
        over = d.nodes > DIAGRAM_NODE_BUDGET or d.edges > DIAGRAM_EDGE_BUDGET
        print(f"\nmermaid @ line {d.start_line}: "
              f"{d.nodes}/{DIAGRAM_NODE_BUDGET} nodes, "
              f"{d.edges}/{DIAGRAM_EDGE_BUDGET} edges"
              f"{'   OVER' if over else ''}")
        print(f"  nodes: {', '.join(d.node_names)}")

    unbudgeted = [v.section.name for v in verdicts if v.budget is None]
    if unbudgeted:
        print(f"\nUNBUDGETED SECTIONS (a finding, not a pass): {', '.join(unbudgeted)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
