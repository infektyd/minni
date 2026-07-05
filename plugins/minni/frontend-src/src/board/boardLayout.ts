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
    threadTag: { x: 110, y: 126 },
  },
  hub: {
    card: { x: 24, y: 38 },
  },
  staged: {
    moreChip: { x: 138, y: 356 },
  },
  logs: {
    card: { x: 24, y: 32, w: 380 } as CardSlot,
    moreChip: { x: 420, y: 54 },
  },
  quarantine: {
    card: { x: 24, y: 32, w: 266 } as CardSlot,
  },
  recall: {
    svgPaths: ["M 170 130 C 110 180, 90 200, 62 226", "M 320 170 C 330 220, 338 230, 344 250"],
    qcard: { x: 110, y: 40 },
    cards: [
      { x: 20, y: 226, w: 270 } as CardSlot,
      { x: 206, y: 318, w: 258 } as CardSlot,
      { x: 300, y: 250, w: 170 } as CardSlot,
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
