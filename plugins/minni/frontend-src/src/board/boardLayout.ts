// ============================================================================
// Minni Memory Board — link geometry + overview card layout
//
// The overview mini-cards previously used inline magic-number coordinates. They
// now derive from the `OVERVIEW_LAYOUT` table below (pixel-faithful defaults),
// and the staged wall goes through `stagedSlot()` which falls back to a real
// two-column grid so a variable number of cards can never silently overlap.
// ============================================================================

import { BOARD_ORDER } from "./boardData";
import { stagedSlot, type CardSlot } from "./boardLogic";

// Bezier link geometry (Point, Link, linkCurve, computeLinks, orderedAgentLinks)
// lives in the framework-free `boardLogic` module so node:test can import it;
// re-exported at the bottom so existing consumers keep importing from here.

// ── overview card layout table ──────────────────────────────────────────────
// The staged-wall slot grid (`stagedSlot`) and `CardSlot` now live in the
// framework-free `boardLogic` module so they are unit-testable; re-exported
// here so existing consumers keep importing from `boardLayout`.

export const OVERVIEW_LAYOUT = {
  agents: {
    nodeX: 22,
    nodeY0: 30,
    nodeGap: 120,
    threadTag: { x: 84, y: 126 },
  },
  hub: {
    card: { x: 24, y: 38 },
  },
  staged: {
    moreChip: { x: 24, y: 366 },
  },
  logs: {
    card: { x: 24, y: 32, w: 380 } as CardSlot,
    moreChip: { x: 420, y: 54 },
  },
  quarantine: {
    card: { x: 24, y: 32, w: 266 } as CardSlot,
  },
  recall: {
    svgPaths: ["M 170 150 C 135 170, 115 178, 100 190", "M 330 152 C 340 175, 345 192, 348 212"],
    qcard: { x: 110, y: 40 },
    cards: [
      { x: 24, y: 190, w: 205 } as CardSlot,
      { x: 245, y: 212, w: 205 } as CardSlot,
      { x: 24, y: 306, w: 205 } as CardSlot,
    ],
  },
} as const;

export { BOARD_ORDER };
export { stagedSlot, type CardSlot };
export {
  computeLinks,
  linkCurve,
  orderedAgentLinks,
  type Link,
  type Point,
} from "./boardLogic";
