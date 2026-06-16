"""Membench report renderer (§7.7 report header + every required metric section).

Renders ONE Markdown report from the orchestration's results. Matplotlib PNG
plots are included ONLY if matplotlib imports; otherwise the report degrades to
Markdown/ASCII tables and NEVER hard-fails on a missing matplotlib (§7.7 / task
constraint). The report carries:

- the RUN MANIFEST header pinning everything needed to reproduce (§6.9.7),
- per-adapter Layer-1 table (recall@k / precision@k / nDCG@k / MRR / refusal
  rates / token_cost / latency p50/p95) + per-band breakdown,
- the Layer-2 table (task-success with 95% CI, tokens-to-model, between-trial
  reliability),
- the significance matrix (pairwise Wilcoxon q-values after BH-FDR),
- the §6.7 token-efficiency composite,
- the §6.8 ingest-cost table,
- a FAILED-ADAPTERS section (per-adapter error isolation — redacted errors),
- the threats-to-validity pointer + the honesty caveats (public-fixture Layer-2
  is plumbing / negative-control; real differentiation needs the real run).

The renderer is PURE given its inputs and makes no network call. The report is
NOT the byte-identity gate (Layer-2 + timing are CI-only); the Layer-1 scorecard
JSON is the byte-reproducible artifact (§T-e).
"""

from __future__ import annotations


def matplotlib_available() -> bool:
    """True iff matplotlib imports — gates PNG plots vs ASCII/Markdown tables.

    NEVER raises: a missing matplotlib degrades the report to tables (§7.7).
    """
    try:
        import matplotlib  # noqa: F401
    except Exception:
        return False
    return True


def _fmt(value, places: int = 4) -> str:
    """Format a number for a table cell; pass non-numerics through as ``str``."""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (int, float)):
        return f"{value:.{places}f}"
    return str(value)


def _esc_cell(value: str) -> str:
    """Escape a Markdown table cell so a literal ``|`` can't corrupt the table.

    A raw ``|`` in cell content (e.g. inside a redacted error message) would be
    parsed as a column separator and shift every following cell. Escape it to
    ``\\|`` per GFM so the table structure stays intact.
    """
    return str(value).replace("|", "\\|")


def _md_table(headers: list[str], rows: list[list[str]]) -> str:
    """Render a GitHub-flavoured Markdown table (deterministic column order)."""
    head = "| " + " | ".join(_esc_cell(h) for h in headers) + " |"
    sep = "| " + " | ".join("---" for _ in headers) + " |"
    body = ["| " + " | ".join(_esc_cell(c) for c in r) + " |" for r in rows]
    return "\n".join([head, sep, *body])


# ---------------------------------------------------------------------------
# Manifest header (§6.9.7 — pins EVERYTHING needed to reproduce)
# ---------------------------------------------------------------------------
def render_manifest(manifest: dict) -> str:
    """Render the run-manifest header as a Markdown block.

    The manifest dict is built by ``run_bench.build_manifest`` and embeds the
    corpus content-hash, scrub status, adapter configs, embedder/tokenizer ids,
    k/budget/N/seeds, model ids+families, episode hashes, and which adapters are
    degraded / stub vs real. Every key is printed so a run is reproducible.
    """
    lines: list[str] = ["## Run manifest (pinned — §6.9.7)", ""]
    lines.append(_md_table(["field", "value"], [
        [k, _scalar(v)] for k, v in _flatten(manifest)
    ]))
    return "\n".join(lines)


def _scalar(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if v is None:
        return ""
    return str(v)


def _flatten(d: dict, prefix: str = "") -> list[tuple[str, object]]:
    """Flatten a nested manifest to sorted ``dotted.key -> scalar`` pairs."""
    out: list[tuple[str, object]] = []
    for key in sorted(d):
        val = d[key]
        full = f"{prefix}{key}"
        if isinstance(val, dict):
            out.extend(_flatten(val, prefix=f"{full}."))
        elif isinstance(val, list):
            out.append((full, ", ".join(str(x) for x in val)))
        else:
            out.append((full, val))
    return out


# ---------------------------------------------------------------------------
# Layer-1 tables (§6.1–6.6)
# ---------------------------------------------------------------------------
_L1_COLS = [
    ("recall_at_k", "recall@k"),
    ("precision_at_k", "prec@k"),
    ("ndcg_at_k", "ndcg@k"),
    ("mrr", "mrr"),
    ("correct_refusal_rate", "corr_ref"),
    ("false_refusal_rate", "false_ref"),
    ("token_cost", "tok_cost"),
]


def render_layer1(scorecards: dict) -> str:
    """Per-adapter overall Layer-1 table + per-band breakdown (§6.1–6.6)."""
    k = scorecards.get("k")
    lines = [f"## Layer 1 — retrieval scorecards (k={k})", ""]

    headers = ["adapter"] + [label for _, label in _L1_COLS] + ["p50ms", "p95ms"]
    rows: list[list[str]] = []
    for name, card in sorted(scorecards.get("adapters", {}).items()):
        ov = card["overall"]
        lat = card.get("latency_ms", {})
        row = [name]
        row += [_fmt(ov.get(key, 0.0)) for key, _ in _L1_COLS]
        row += [_fmt(lat.get("p50", 0.0), 2), _fmt(lat.get("p95", 0.0), 2)]
        rows.append(row)
    lines.append(_md_table(headers, rows))

    # Per-band breakdown.
    lines += ["", "### Per-band breakdown", ""]
    band_headers = ["adapter", "band", "n"] + [label for _, label in _L1_COLS]
    band_rows: list[list[str]] = []
    for name, card in sorted(scorecards.get("adapters", {}).items()):
        for band, block in sorted(card.get("per_band", {}).items()):
            row = [name, band, str(block.get("n", 0))]
            row += [_fmt(block.get(key, ""), 4) if key in block else "—"
                    for key, _ in _L1_COLS]
            band_rows.append(row)
    lines.append(_md_table(band_headers, band_rows))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Layer-2 table (§6.7 task-success + CI + tokens + reliability)
# ---------------------------------------------------------------------------
def render_layer2(layer2: dict) -> str:
    """Per-adapter Layer-2 table: task-success+95%CI, tokens, reliability (§3.3)."""
    lines = ["## Layer 2 — agent-in-the-loop (N-trial)", ""]
    lines.append(
        "> **Honesty caveat:** on the PUBLIC synthetic fixture with the offline "
        "StubAgent/StubJudge, Layer-2 is a PLUMBING / negative-control run — it "
        "verifies the pipeline, it is **NOT** the headline result. Real adapter "
        "differentiation requires the real-LLM run on the private corpus (s8). "
        "Layer-2 is CI-only (N-trial means + 95% CI), never byte-reproducible "
        "(§T-e)."
    )
    lines.append("")
    n_trials = layer2.get("n_trials")
    am = layer2.get("agent_model", {})
    jm = layer2.get("judge_model", {})
    lines.append(
        f"agent: `{am.get('model_id','')}` ({am.get('model_family','')}) · "
        f"judge: `{jm.get('model_id','')}` ({jm.get('model_family','')}) · "
        f"N={n_trials} trials/episode"
    )
    lines.append("")

    headers = [
        "adapter", "task_success", "ci95_low", "ci95_high", "n_eps",
        "tokens_to_model", "ctx_tokens", "between_trial_var(mean)",
    ]
    rows: list[list[str]] = []
    for name, block in sorted(layer2.get("adapters", {}).items()):
        ts = block["task_success"]
        ci = ts["ci95"]
        ttm = block["tokens_to_model"]["point"]
        ctx = block["ctx_tokens"]["point"]
        rel = ts["between_trial_reliability"]["mean_within_episode_variance"]
        rows.append([
            name,
            _fmt(ts["point"]),
            _fmt(ci["low"]),
            _fmt(ci["high"]),
            str(ts["n_episodes"]),
            _fmt(ttm, 1),
            _fmt(ctx, 1),
            _fmt(rel, 6),
        ])
    lines.append(_md_table(headers, rows))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Significance matrix (§6.9 — pairwise Wilcoxon q after BH-FDR)
# ---------------------------------------------------------------------------
def render_significance(layer2: dict) -> str:
    """Pairwise Wilcoxon q-value matrix after BH-FDR (§6.9)."""
    sig = layer2.get("significance", {})
    lines = ["## Significance — pairwise task success (§6.9)", ""]
    lines.append(
        f"test: {sig.get('test','')} · family: {sig.get('confirmatory_family','')} "
        f"· q={sig.get('q','')} · unit: {sig.get('unit','')}"
    )
    lines.append("")
    headers = [
        "A", "B", "n_eps", "mean_A", "mean_B", "p_raw", "q (BH-FDR)",
        "significant", "winner",
    ]
    rows: list[list[str]] = []
    for c in sig.get("pairwise", []):
        rows.append([
            c["adapter_a"], c["adapter_b"], str(c["n_episodes"]),
            _fmt(c["mean_a"]), _fmt(c["mean_b"]),
            _fmt(c["p_value"], 4), _fmt(c["p_adjusted"], 4),
            "yes" if c["significant"] else "no",
            c["winner"] or "—",
        ])
    if not rows:
        lines.append("_no pairwise comparisons (need ≥ 2 adapters)._")
    else:
        lines.append(_md_table(headers, rows))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# §6.7 efficiency composite
# ---------------------------------------------------------------------------
def render_efficiency(efficiency: dict) -> str:
    """§6.7 token-efficiency composite table."""
    lines = ["## Token-efficiency composite (§6.7)", ""]
    lines.append(f"unit: {efficiency.get('unit','')}")
    lines.append("")
    lines.append(f"> {efficiency.get('note','')}")
    lines.append("")
    headers = [
        "adapter", "task_success", "mean_ctx_tokens",
        "eff (success/1k ctx-tok)", "no_context?",
    ]
    rows: list[list[str]] = []
    for name, block in sorted(efficiency.get("per_adapter", {}).items()):
        rows.append([
            name,
            _fmt(block["mean_task_success"]),
            _fmt(block["mean_ctx_tokens"], 1),
            _fmt(block["efficiency_per_1k_ctx_tokens"], 3),
            "yes — not meaningful" if block["no_context_flag"] else "no",
        ])
    lines.append(_md_table(headers, rows))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# §6.8 ingest-cost table
# ---------------------------------------------------------------------------
def render_ingest_cost(ingest_cost: dict) -> str:
    """§6.8 dedicated ingest-cost table (0 for non-LLM adapters)."""
    lines = ["## Ingest cost (§6.8 — reported separately, NOT in the composite)",
             ""]
    headers = ["adapter", "ingest_tokens", "doc_count"]
    rows = [
        [name, str(block.get("ingest_tokens_used", 0)),
         str(block.get("doc_count", 0))]
        for name, block in sorted(ingest_cost.items())
    ]
    lines.append(_md_table(headers, rows))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Failed-adapters section (per-adapter error isolation)
# ---------------------------------------------------------------------------
def render_failures(failures: dict) -> str:
    """Failed-adapter section — redacted errors; run continued for survivors."""
    lines = ["## Failed adapters (error-isolated — §run-isolation)", ""]
    if not failures:
        lines.append("_none — every adapter ran to completion._")
        return "\n".join(lines)
    lines.append(
        "> The following adapters raised during ingest/query/teardown. Each "
        "failure is ISOLATED: the adapter is marked FAILED with a redacted "
        "error and the run CONTINUED scoring the survivors. These adapters are "
        "absent from the scorecards above."
    )
    lines.append("")
    headers = ["adapter", "phase", "error (redacted)"]
    rows = []
    for name, info in sorted(failures.items()):
        err = info.get("error", "")
        # A compound failure (failed phase AND teardown also raised) carries a
        # supplementary redacted teardown error — surface it so the operator
        # sees both, never silently swallowing the teardown crash.
        teardown_err = info.get("teardown_error", "")
        if teardown_err:
            err = f"{err} [+teardown: {teardown_err}]"
        rows.append([name, info.get("phase", "?"), err])
    lines.append(_md_table(headers, rows))
    return "\n".join(lines)


def render_partial_failures(partial: dict) -> str:
    """Partial-failure section — adapters whose Layer-2 crashed AFTER Layer-1 OK.

    These adapters STAY in the Layer-1 scorecards (their retrieval scoring is
    valid and preserved); only their Layer-2 result was dropped. Surfaced so the
    Layer-2 crash is never silently swallowed.
    """
    lines = ["## Partial failures (Layer-2 only — Layer-1 preserved)", ""]
    if not partial:
        lines.append("_none — no adapter crashed after a successful Layer 1._")
        return "\n".join(lines)
    lines.append(
        "> These adapters scored Layer 1 successfully (still present in the "
        "scorecards above) but raised during Layer 2. The Layer-2 crash is "
        "isolated and redacted; the valid Layer-1 records are PRESERVED."
    )
    lines.append("")
    headers = ["adapter", "phase", "error (redacted)"]
    rows = []
    for name, info in sorted(partial.items()):
        err = info.get("error", "")
        # A partial failure whose teardown ALSO raised carries a supplementary
        # redacted teardown error — surface it so the operator sees both, never
        # silently swallowing the teardown crash, while the adapter still stays
        # a survivor (its Layer-1 records are preserved in the scorecards).
        teardown_err = info.get("teardown_error", "")
        if teardown_err:
            err = f"{err} [+teardown: {teardown_err}]"
        rows.append([name, info.get("phase", "layer2"), err])
    lines.append(_md_table(headers, rows))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Threats-to-validity + honesty caveats
# ---------------------------------------------------------------------------
def render_caveats(manifest: dict) -> str:
    """Threats-to-validity pointer + honesty caveats carried INTO the report."""
    is_fixture = manifest.get("run", {}).get("is_fixture_run", True)
    real_llm = manifest.get("run", {}).get("real_llm", False)
    lines = ["## Threats to validity & honesty caveats", ""]
    if is_fixture or not real_llm:
        lines.append(
            "- **This is NOT the headline result.** This run used the PUBLIC "
            "synthetic fixture corpus and/or the offline StubAgent/StubJudge. "
            "It exercises and verifies the METHOD; it does **not** measure real "
            "adapter quality. The headline numbers come from the real run on the "
            "private corpus with the real LLM agent/judge (s8)."
        )
    lines.append(
        "- **Reproducibility scope (§T-e).** Layer-1 on the fixture is "
        "**byte-reproducible** (deterministic; the scorecard JSON is byte-identical "
        "across two runs after the timing strip). Layer-2 is **CI-only** — N-trial "
        "means with 95% CIs and the pre-registered Wilcoxon+BH-FDR comparison; the "
        "LLM agent/judge are non-deterministic so Layer-2 is never a single "
        "byte-identical artifact."
    )
    lines.append(
        "- **Private-corpus numbers are not externally reproducible** — only the "
        "METHOD is. The corpus content-hash is published so the operator can prove "
        "a later run used the same bytes."
    )
    lines.append(
        "- **`native_platform` is fidelity-limited** (lowest-confidence adapter) "
        "and **degraded** adapters are flagged in the manifest."
    )
    lines.append(
        "- Full threats-to-validity analysis: see the design spec "
        "`docs/superpowers/specs/2026-06-15-membench-design.md` §-threats-to-validity."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Top-level report
# ---------------------------------------------------------------------------
def render_report(results: dict, *, plots_dir=None) -> str:
    """Render the WHOLE Markdown report from the orchestration results dict.

    ``results`` is the machine-readable artifact produced by
    ``run_bench.build_results`` (manifest + scorecards + layer2 + efficiency +
    ingest_cost + failures). ``plots_dir`` is honored only when matplotlib is
    available; otherwise the report is tables-only and never hard-fails (§7.7).
    """
    manifest = results.get("manifest", {})
    title = "# membench report"
    run = manifest.get("run", {})
    if run.get("is_fixture_run", True) or not run.get("real_llm", False):
        title += "  — FIXTURE / STUB RUN (not the headline result)"

    sections = [
        title,
        "",
        f"_plots: {'matplotlib PNGs' if matplotlib_available() else 'ASCII/Markdown tables (matplotlib not installed)'}_",
        "",
        render_manifest(manifest),
        "",
        render_layer1(results.get("scorecards", {})),
        "",
        render_layer2(results.get("layer2", {})),
        "",
        render_significance(results.get("layer2", {})),
        "",
        render_efficiency(results.get("efficiency", {})),
        "",
        render_ingest_cost(results.get("ingest_cost", {})),
        "",
        render_failures(results.get("failures", {})),
        "",
        render_partial_failures(results.get("partial_failures", {})),
        "",
        render_caveats(manifest),
        "",
    ]
    report = "\n".join(sections)

    # Optional matplotlib plots — never load-bearing; absence degrades to tables.
    if matplotlib_available() and plots_dir is not None:
        try:
            png = _render_plots(results, plots_dir)
            if png:
                report += "\n## Plots\n\n" + "\n".join(
                    f"![{p.name}]({p.name})" for p in png
                ) + "\n"
        except Exception as exc:  # plotting must never break the report
            report += f"\n_(plot rendering skipped: {type(exc).__name__})_\n"
    return report


def _render_plots(results: dict, plots_dir):
    """Render a small set of PNG plots when matplotlib is present (best-effort)."""
    from pathlib import Path

    import matplotlib

    matplotlib.use("Agg")  # headless, deterministic backend
    import matplotlib.pyplot as plt

    plots_dir = Path(plots_dir)
    plots_dir.mkdir(parents=True, exist_ok=True)
    out: list = []

    eff = results.get("efficiency", {}).get("per_adapter", {})
    if eff:
        names = sorted(eff)
        vals = [eff[n]["efficiency_per_1k_ctx_tokens"] for n in names]
        fig, ax = plt.subplots()
        ax.barh(names, vals)
        ax.set_xlabel("task-success per 1k context tokens (§6.7)")
        ax.set_title("Token-efficiency composite")
        fig.tight_layout()
        path = plots_dir / "efficiency.png"
        fig.savefig(path)
        plt.close(fig)
        out.append(path)
    return out
