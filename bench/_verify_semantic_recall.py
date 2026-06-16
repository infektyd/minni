"""Standalone proof: the minni adapter now recalls MULTIPLE semantically-relevant
ranked docs via the throwaway daemon's SEMANTIC `results` stream (gap fixed).

Stands up the real isolated throwaway minnid (over a temp MINNI_HOME), ingests a
small synthetic corpus through the public learn->resolve_candidate(accept) path,
then queries and prints the ranked recall. NOT a pytest — run directly with the
engine venv python. Touches NO live ~/.minni (the adapter's data-safety guard +
temp home enforce this).
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from membench.adapters.minni_adapter import MinniAdapter
from membench.corpus import DirectoryFrozenCorpus, compute_content_hash
from membench.contract import TokenBudget

# 12 short synthetic docs across distinct topics; the query targets the "battery"
# cluster, several of which should co-rank semantically.
DOCS = {
    "battery_chem.md": (
        "# Lithium-Ion Battery Chemistry\n\n"
        "A lithium-ion battery stores electrical energy by shuttling lithium ions "
        "between a graphite anode and a metal-oxide cathode through a liquid "
        "electrolyte. Charging drives the ions into the anode; discharging lets "
        "them flow back, releasing energy to power the device. Energy density, "
        "cycle life, and thermal stability are the key trade-offs cell designers "
        "balance when choosing cathode materials such as NMC or LFP.\n"
    ),
    "battery_safety.md": (
        "# Battery Safety and Thermal Runaway\n\n"
        "When a lithium-ion cell overheats, it can enter thermal runaway: an "
        "exothermic chain reaction that vents flammable gas and can ignite. Battery "
        "management systems monitor cell voltage and temperature, balance charge "
        "across the pack, and cut the circuit before an overcharged or shorted cell "
        "fails. Good pack design isolates a failing cell so it cannot cascade to "
        "its neighbors.\n"
    ),
    "battery_charging.md": (
        "# Charging a Lithium Battery\n\n"
        "Lithium batteries charge in two stages: a constant-current phase that "
        "rapidly fills most of the capacity, followed by a constant-voltage phase "
        "that tops off the cell while tapering the current. Charging too fast or at "
        "low temperature plates lithium metal on the anode, permanently reducing "
        "capacity and raising the risk of an internal short.\n"
    ),
    "tides.md": (
        "# Ocean Tides\n\n"
        "Tides are the regular rise and fall of sea level driven by the "
        "gravitational pull of the moon and sun on the rotating earth, producing two "
        "high tides and two low tides across most coastlines each lunar day, with "
        "spring and neap variation across the month.\n"
    ),
    "photosynthesis.md": (
        "# Photosynthesis\n\n"
        "Green plants convert sunlight, carbon dioxide, and water into glucose and "
        "oxygen using chlorophyll in their chloroplasts, fixing carbon and feeding "
        "nearly every food chain on the planet while replenishing atmospheric "
        "oxygen.\n"
    ),
    "volcano.md": (
        "# Volcanoes\n\n"
        "A volcano forms where molten rock, gas, and ash escape from a magma "
        "chamber through a vent in the crust, building cones of lava and tephra and "
        "occasionally erupting explosively when dissolved gases come out of "
        "solution under falling pressure.\n"
    ),
    "compiler.md": (
        "# Compilers\n\n"
        "A compiler translates source code in a high-level language into machine "
        "code through lexing, parsing, semantic analysis, optimization, and code "
        "generation, reporting type errors and emitting an executable the operating "
        "system can load and run directly.\n"
    ),
    "tcp.md": (
        "# TCP\n\n"
        "The Transmission Control Protocol provides a reliable, ordered byte stream "
        "over an unreliable network by numbering segments, acknowledging receipt, "
        "retransmitting lost data, and adjusting its sending rate with congestion "
        "control to avoid overwhelming the path.\n"
    ),
    "coffee.md": (
        "# Coffee Roasting\n\n"
        "Roasting green coffee beans drives Maillard browning and caramelization, "
        "developing the aromatic compounds that define a cup; lighter roasts keep "
        "bright acidity while darker roasts deepen body and bitterness as the beans "
        "lose mass and moisture.\n"
    ),
    "glacier.md": (
        "# Glaciers\n\n"
        "A glacier is a persistent body of dense ice that forms where snowfall "
        "exceeds melt over many years; under its own weight it flows slowly "
        "downhill, carving valleys and depositing rock debris as moraine along its "
        "margins.\n"
    ),
    "violin.md": (
        "# The Violin\n\n"
        "A violin produces sound when a bowed or plucked string vibrates and the "
        "bridge transmits that vibration to the hollow wooden body, which amplifies "
        "the tone; the player changes pitch by pressing the strings against the "
        "fingerboard to shorten their vibrating length.\n"
    ),
    "bread.md": (
        "# Bread Baking\n\n"
        "Yeast ferments the sugars in bread dough, producing carbon dioxide that "
        "gluten strands trap as bubbles, leavening the loaf; baking heat sets the "
        "structure, gelatinizes the starch, and browns the crust through the "
        "Maillard reaction.\n"
    ),
}


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="membench-verify-corpus-"))
    for name, body in DOCS.items():
        (tmp / name).write_text(body, encoding="utf-8")
    corpus = DirectoryFrozenCorpus(tmp, compute_content_hash(tmp), scrubbed=False)

    adapter = MinniAdapter()
    try:
        report = adapter.ingest(corpus)
        print(f"ingest: promoted={report.doc_count} skipped={report.skipped_doc_count}")

        query = "how does a rechargeable lithium cell store and release energy"
        result = adapter.query(query, TokenBudget(max_tokens=4096, max_docs=5))
        print(f"\nquery: {query!r}")
        print(f"ranked recall ({len(result.ranked_results)} docs):")
        for i, rd in enumerate(result.ranked_results, 1):
            print(f"  {i}. {rd.doc_id:24s} score={rd.score:.4f}")

        battery_hits = [r for r in result.ranked_results if r.doc_id.startswith("battery")]
        print(f"\nbattery-cluster hits: {len(battery_hits)} / {len(result.ranked_results)}")
        if len(result.ranked_results) >= 2 and len(battery_hits) >= 2:
            print("PROOF OK: multiple semantically-relevant ranked results returned.")
            return 0
        print("PROOF FAILED: expected >=2 results with >=2 battery-cluster hits.")
        return 1
    finally:
        adapter.teardown()
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
