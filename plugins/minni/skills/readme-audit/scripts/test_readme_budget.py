"""Tests for the readme-audit budget meter.

The meter is the only part of the audit that must be mechanical: an agent
eyeballing "is this section over budget?" gets it wrong, and the retirement
rule is only as honest as the number it is applied to. Everything else in the
skill is judgement; this is arithmetic, so it is tested.

Fixtures are synthetic READMEs written to tmp_path -- never the repo's own.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import readme_budget  # noqa: E402


SIMPLE = """# Title

Intro line here.

## Alpha

One two three four five.

## Beta

Six seven.

```bash
this code block is not prose
```

| a | b |
|---|---|
| c | d |
"""


def _measure(tmp_path, text: str):
    path = tmp_path / "README.md"
    path.write_text(text, encoding="utf-8")
    return {s.name: s for s in readme_budget.measure(path)}


def test_preamble_is_its_own_section(tmp_path):
    """Content before the first `##` is the first screen -- it needs a budget too."""
    sections = _measure(tmp_path, SIMPLE)

    assert readme_budget.PREAMBLE in sections
    assert sections[readme_budget.PREAMBLE].words == 3  # "Intro line here."


def test_prose_words_exclude_code_blocks(tmp_path):
    """A quickstart is mostly commands; counting them would make the budget meaningless."""
    beta = _measure(tmp_path, SIMPLE)["Beta"]

    assert beta.words == 2, "only 'Six seven.' is prose"
    assert beta.code_lines == 3, "fenced block lines are counted separately"


def test_table_rows_counted_not_worded(tmp_path):
    """Comparison/documentation sections are tables; rows are the honest unit."""
    beta = _measure(tmp_path, SIMPLE)["Beta"]

    assert beta.table_rows == 1, "header and separator are not content rows"


def test_line_count_is_the_whole_section(tmp_path):
    """Screen real estate is lines, including code and tables."""
    alpha = _measure(tmp_path, SIMPLE)["Alpha"]

    assert alpha.lines == 3, "heading, blank, prose"


def test_mermaid_topology_counted(tmp_path):
    """The diagram gets a node/edge budget of its own, so it must be measurable."""
    text = """# T

## Arch

```mermaid
flowchart TD
    subgraph S["Group"]
      A["Alpha"]
      B["Beta"]
    end
    C["Gamma"]
    D[("Store")]

    S --> C --> D
    A -->|labelled edge| D
```
"""
    path = tmp_path / "README.md"
    path.write_text(text, encoding="utf-8")
    diagrams = readme_budget.measure_mermaid(path)

    assert len(diagrams) == 1
    # A, B, C, D plus the subgraph S itself -- the subgraph is a node you can draw edges to.
    assert diagrams[0].nodes == 5
    # S-->C, C-->D, A-->D
    assert diagrams[0].edges == 3


def test_over_budget_is_reported_with_the_overage(tmp_path):
    """The retirement rule needs 'by how much', not just 'yes'."""
    section = readme_budget.Section(name="Alpha", lines=10, words=200, code_lines=0, table_rows=0)
    budget = readme_budget.Budget(words=150, lines=None, table_rows=None)

    verdict = readme_budget.judge(section, budget)

    assert verdict.over is True
    assert verdict.words_over == 50


def test_unbudgeted_section_is_a_finding(tmp_path):
    """A new `##` nobody budgeted is exactly how a README starts drifting."""
    sections = _measure(tmp_path, SIMPLE)
    report = readme_budget.report(sections.values(), {"Alpha": readme_budget.Budget(words=10)})

    unbudgeted = [v.section.name for v in report if v.budget is None]

    assert "Beta" in unbudgeted
    assert readme_budget.PREAMBLE in unbudgeted


def test_default_budget_table_covers_the_repo_readme():
    """The shipped budget must name every section the real README has today.

    If someone adds a section without a budget line, this fails and forces the
    budget conversation instead of letting the section slip in unmetered.
    """
    repo_readme = Path(__file__).resolve().parents[4] / "README.md"
    if not repo_readme.exists():  # running from a payload copy, not the repo
        return

    names = {s.name for s in readme_budget.measure(repo_readme)}
    budgeted = set(readme_budget.DEFAULT_BUDGETS)

    assert names <= budgeted, f"unbudgeted sections: {sorted(names - budgeted)}"
