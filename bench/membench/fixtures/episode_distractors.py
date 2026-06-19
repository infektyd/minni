"""Deterministic DISTRACTOR session pool for the Layer-2 episode fixtures (fix 1).

WHY THIS EXISTS (the headline fairness fix): the original public episode
fixtures co-ingested ~3 tiny sessions (~50 tokens total) into a corpus far under
the ``config.DEFAULT_MAX_TOKENS`` (2048) budget. EVERY adapter — including
``native_platform`` (pure recency truncation) and even ``sanity_random`` —
trivially stuffed the whole corpus (gold fact included) into context, so Layer-2
task-success was NON-DISCRIMINATIVE: a broken retriever scored as well as a real
one. There was no MEMORY PRESSURE.

This module injects a large pool of realistic, topically-UNRELATED distractor
("noise") sessions into every episode so the per-episode CANDIDATE POOL grows to
~31 sessions, of which only ONE carries the gold fact. The single establishing
session must now actually be RETRIEVED and RANKED into the budgeted top-K to land
in context — pure recency/random selection no longer suffices, because a random
top-K pick out of ~31 candidates surfaces the gold session only ~K/num_docs of
the time. This is the substrate for the negative-control self-test
(``sanity_random`` must score materially BELOW the real retrieval adapters on
Layer-2 task-success). NOTE: the mechanism is RETRIEVAL SELECTIVITY over a large
candidate pool, NOT budget exclusion — a K=10 pick (~1750 tokens) fits the 2048
budget fine; it fails because random ranking rarely puts the gold doc in the
top-K (see DISTRACTORS_PER_EPISODE below for the full corrected rationale).

INVARIANTS this pool preserves (so the episode leak guards still hold):
- A distractor's content is generic operations/logistics prose. It NEVER contains
  any episode's ``gold_fact`` substring, so the cross-session leak guard
  (``episodes.check_episode``) does not trip. ``assert_distractors_fact_free``
  enforces this against the live episode set at generation time.
- Distractors carry ``dN-...`` session ids that cannot collide with an episode's
  own ``sN`` ids.
- Distractors are inserted in the MIDDLE — after the establishing session, before
  the final question session — so the fact session is never last and the question
  session stays last (both episode invariants hold). They are pure noise: a
  correct memory system ranks the establishing session above them.

Everything is deterministic (fixed content, fixed order) so the augmented fixture
and every test over it is byte-reproducible (§3.2). This is a TEST/fixture
generator; it reads no real data and is import-isolated from ``engine/``.
"""

from __future__ import annotations

# A pool of self-contained, topically-DISJOINT noise paragraphs. Each is dense,
# realistic operations/logistics/facilities prose with NO overlap with any
# episode's establishing fact. Deliberately verbose (~45-60 tokens each) so a
# handful per session pushes the per-episode corpus well past the 2048-token
# budget. None of these strings contains a gold_fact of any fixture episode
# (asserted by assert_distractors_fact_free at generation time).
_NOISE_PARAGRAPHS: tuple[str, ...] = (
    "The quarterly logistics review covered warehouse throughput, pallet rotation "
    "cadence, and the seasonal staffing plan for the southern distribution hub. "
    "Forklift maintenance windows were rescheduled to avoid the afternoon loading "
    "peak, and the loading-dock scheduler was migrated to the new dispatch board.",
    "Cafeteria operations rotated the lunch menu to a four-week cycle and added a "
    "self-serve salad station near the courtyard entrance. Compostable trays "
    "replaced the older plastic ones, and the dishwashing line was rebalanced to "
    "cut the evening backlog that built up after the all-hands gatherings.",
    "The grounds crew resealed the parking-deck expansion joints and repainted the "
    "accessible stalls on the lower level. A drainage survey flagged two clogged "
    "catch basins behind the loading ramp, which were cleared before the rainy "
    "season, and the perimeter lighting was switched to a dusk-triggered timer.",
    "Procurement consolidated three office-supply vendors into a single framework "
    "contract to simplify reordering of toner, whiteboard markers, and standing-desk "
    "risers. The new catalog enforces a per-team monthly cap, and bulk orders now "
    "route through a shared approval queue instead of individual purchase cards.",
    "The travel desk renegotiated the preferred-hotel block for the eastern region "
    "and added a low-cost rail option for trips under four hours. Expense reports now "
    "auto-categorize ground transport, and per-diem rates were refreshed against the "
    "latest cost-of-living tables for the three most-visited cities.",
    "Facilities tested the backup generator under a simulated load and replaced the "
    "aging transfer switch on the east riser. The fire-suppression panel passed its "
    "annual inspection, and the freight elevator was recertified after a slow-close "
    "door sensor was swapped for a faster optical unit on the mezzanine landing.",
    "The events team booked the rooftop terrace for the summer mixer and arranged a "
    "rain contingency in the second-floor atrium. Catering will run two beverage "
    "stations to spread the queues, and the sound rental includes a wireless lapel "
    "kit so the welcome remarks carry over the open-air courtyard noise.",
    "The shipping bay reorganized its inbound staging lanes by carrier and added "
    "color-coded floor tape to speed the sorter handoff. Returns processing moved to "
    "the quieter north corner, and a barcode-tunnel scanner replaced the handheld "
    "guns that kept losing pairing during the busy mid-morning intake window.",
    "Groundskeeping planted drought-tolerant beds along the main walkway and "
    "installed a drip line on a dawn schedule to cut water use. The seasonal wreath "
    "order was placed early this year, and the lobby's living wall was switched to a "
    "hardier fern mix after the previous planting struggled under the skylight.",
    "The print shop retired two end-of-life plotters and standardized on a single "
    "wide-format unit shared across the design pods. Color profiles were recalibrated "
    "against a reference swatch book, and a self-service binding station was added so "
    "small jobs no longer wait behind the large overnight production runs.",
    "The mailroom adjusted its courier pickup windows to better match the outbound "
    "volume and added a locked overflow cabinet for oversized parcels. Internal "
    "transfers now use reusable totes with a deposit tag, and the postage meter was "
    "swapped for a metered account that reconciles against the monthly carrier "
    "statement automatically.",
    "The wellness program opened a second quiet room on the fourth floor and extended "
    "the on-site clinic hours into early evening. A standing-desk loaner pool was set "
    "up near reception, and the stairwell signage was refreshed to encourage walking "
    "between adjacent floors during the midday break.",
    "The audiovisual team re-cabled the largest conference room with a single "
    "pull-through trunk and replaced the flickering projector lamp with a laser unit. "
    "Room-booking displays were remounted at a consistent height, and a tabletop "
    "puck now starts the call with one tap instead of the old three-remote dance.",
    "Security rotated the visitor-badge stock and updated the front-desk script for "
    "after-hours deliveries. The bicycle cage gained a dozen new hooks, and the "
    "turnstile firmware was patched to clear a rare double-read that occasionally "
    "logged a single entry as two separate swipes during the morning rush.",
)


def _distractor_sessions(start_index: int, count: int) -> list[dict[str, str]]:
    """``count`` deterministic distractor session dicts, cycling the noise pool.

    Each distractor session packs SEVERAL noise paragraphs so a single session is
    already large; ``count`` such sessions then push the episode corpus well past
    the token budget. Session ids are ``dN-noise`` (disjoint from episode ``sN``
    ids). Deterministic: same (start_index, count) -> same content/order.
    """
    pool = _NOISE_PARAGRAPHS
    out: list[dict[str, str]] = []
    for i in range(count):
        # Pack 3 distinct pool paragraphs per session (deterministic rotation) so
        # each distractor session is dense (~150-180 tokens) — a few sessions then
        # dominate the budget.
        base = (start_index + i * 3) % len(pool)
        body = " ".join(
            pool[(base + j) % len(pool)] for j in range(3)
        )
        out.append(
            {
                "session_id": f"d{i + 1}-noise",
                "content": f"Operations log entry {i + 1}. {body}",
            }
        )
    return out


# How many distractor sessions to inject per episode. 28 sessions x ~165 tokens
# ~= ~4600 tokens of PURE NOISE, on top of the episode's own sessions, so the
# total per-episode corpus is well past the 2048-token budget (fix 1a).
#
# MECHANISM of the negative control (corrected, review fix 1a). The distractors do
# NOT work by "budget pressure" — that mechanism is FALSE. A relevance-random
# adapter picks K=10 docs (~1750 tokens) which DO fit under the 2048 budget, so
# budget exclusion is not why it fails. The REAL reason real retrieval adapters
# beat sanity_random is RETRIEVAL SELECTIVITY over a LARGER CANDIDATE POOL:
#   - Each episode has ~31 candidate session docs and only ONE carries the gold
#     fact. A real adapter (naive_rag, markdown_grep) RANKS that gold session into
#     its top-K, so the fact reliably lands in the budgeted context.
#   - sanity_random ranks by sha256(question || doc_id), which is INDEPENDENT of
#     relevance. It surfaces the gold session in its top-K only ~K/num_docs ≈
#     10/31 ≈ 0.32 of the time in expectation; on the actual fixture the gold-fact
#     SUBSTRING lands in the budgeted top-K context even more rarely (it is not
#     always the establishing session that carries the answer substring — e.g. the
#     correction episodes), so the observed sanity_random task-success is ~0.
# The job of a LARGE distractor pool is therefore to make the candidate pool big
# enough that random selection is selective-by-luck only — it is the pool SIZE
# (denominator of K/num_docs), not budget exclusion, that suppresses the random
# control. More distractors -> larger num_docs -> lower random hit rate.
DISTRACTORS_PER_EPISODE = 28


def assert_distractors_fact_free(gold_facts: list[str]) -> None:
    """Abort generation if ANY distractor contains ANY episode's ``gold_fact``.

    The cross-session leak guard (``episodes.check_episode``) rejects an episode
    whose gold_fact appears in a non-establishing session. Injecting a distractor
    that happens to contain a gold_fact would trip it — so we fail LOUD at
    generation time instead of producing an invalid fixture.
    """
    joined = "\n".join(_NOISE_PARAGRAPHS)
    for fact in gold_facts:
        if fact and fact in joined:
            raise ValueError(
                f"distractor pool contains gold_fact {fact!r} — it would trip the "
                "cross-session leak guard; pick disjoint distractor prose (fix 1)."
            )


def augment_episode_dict(ep: dict, *, distractors: int = DISTRACTORS_PER_EPISODE) -> dict:
    """Return a copy of ``ep`` with distractor sessions injected mid-episode.

    Distractors are inserted AFTER the establishing (fact) session and BEFORE the
    final question session, so the fact session is never last and the question
    session stays last (both episode invariants hold). The establishing fact must
    now be RETRIEVED out of a budget-exceeding sea of noise — real memory pressure.
    Idempotent in spirit: it appends fresh ``dN-noise`` ids; do not call twice.
    """
    sessions = list(ep["sessions"])
    if len(sessions) < 2:
        raise ValueError(f"episode {ep.get('id')!r} has < 2 sessions; cannot augment")
    fact_sid = ep["fact_session_id"]
    fact_pos = next(
        (i for i, s in enumerate(sessions) if s["session_id"] == fact_sid), None
    )
    if fact_pos is None:
        raise ValueError(
            f"episode {ep.get('id')!r}: fact_session_id {fact_sid!r} not found"
        )
    # Insert distractors immediately AFTER the establishing session (but the
    # original final/question session always remains last).
    insert_at = max(fact_pos + 1, 1)
    insert_at = min(insert_at, len(sessions) - 1)  # keep the last session last
    # Derive a per-episode start offset from the id so different episodes draw
    # different (but deterministic) distractor orderings — more realistic noise.
    start_index = sum(ord(c) for c in ep["id"]) % len(_NOISE_PARAGRAPHS)
    noise = _distractor_sessions(start_index, distractors)
    new_sessions = sessions[:insert_at] + noise + sessions[insert_at:]
    out = dict(ep)
    out["sessions"] = new_sessions
    return out
