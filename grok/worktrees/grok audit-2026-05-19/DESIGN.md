---
version: alpha
name: Sovereign Memory Console
description: Agent-first operational interface for local memory inspection, context preparation, dry-run learning review, and explicit learning decisions.
colors:
  primary: "#181613"
  ink: "#181613"
  graphite: "#242320"
  graphite-2: "#33312C"
  graphite-3: "#4E4A42"
  bone: "#F4F1EA"
  bone-2: "#E8E2D0"
  panel: "#FFFEFA"
  panel-muted: "#F8F5EE"
  border: "#D6D0C3"
  border-strong: "#AFA696"
  verdigris: "#2F7D68"
  verdigris-dark: "#1E5E4C"
  verdigris-soft: "#D9ECE5"
  persimmon: "#D2603A"
  persimmon-dark: "#A4483F"
  persimmon-soft: "#F7DED6"
  mustard: "#C9A961"
  mustard-dark: "#8C681F"
  mustard-soft: "#F4E8C7"
  blue: "#3D6F95"
  blue-soft: "#DCEAF2"
  private: "#A4483F"
  owner: "#2F7D68"
  team: "#3D6F95"
  system: "#60656A"
  public: "#736B60"
  afm-safe: "#2F7D68"
  learn-candidate: "#2F7D68"
  log-only: "#B9852A"
  do-not-store: "#A4483F"
  success: "#2F7D68"
  warning: "#B9852A"
  danger: "#A4483F"
  info: "#3D6F95"
  focus: "#3D6F95"
  selected: "#E0E8E2"
  hover: "#F0EADF"
  disabled: "#C8C2B8"
  loading: "#7C817B"
  on-ink: "#F8F5EF"
  on-panel: "#181613"
  on-muted: "#4E4A42"
  on-action: "#FFFFFF"
typography:
  display:
    fontFamily: Inter
    fontSize: 34px
    fontWeight: 720
    lineHeight: 1.08
    letterSpacing: 0px
  title:
    fontFamily: Inter
    fontSize: 20px
    fontWeight: 680
    lineHeight: 1.2
    letterSpacing: 0px
  section:
    fontFamily: Inter
    fontSize: 15px
    fontWeight: 700
    lineHeight: 1.25
    letterSpacing: 0px
  body:
    fontFamily: Inter
    fontSize: 15px
    fontWeight: 420
    lineHeight: 1.55
    letterSpacing: 0px
  body-sm:
    fontFamily: Inter
    fontSize: 13px
    fontWeight: 420
    lineHeight: 1.5
    letterSpacing: 0px
  label:
    fontFamily: IBM Plex Mono
    fontSize: 12px
    fontWeight: 650
    lineHeight: 1.25
    letterSpacing: 0px
  evidence:
    fontFamily: IBM Plex Mono
    fontSize: 12px
    fontWeight: 500
    lineHeight: 1.4
    letterSpacing: 0px
rounded:
  xs: 2px
  sm: 4px
  md: 8px
  lg: 10px
spacing:
  xs: 4px
  sm: 8px
  md: 16px
  lg: 24px
  xl: 32px
  xxl: 48px
  rail-width: 260px
  inspector-width: 384px
  touch-target: 44px
components:
  app-shell:
    backgroundColor: "{colors.bone}"
    textColor: "{colors.primary}"
    padding: 24px
  left-rail:
    backgroundColor: "{colors.ink}"
    textColor: "{colors.on-ink}"
    rounded: "{rounded.xs}"
    padding: 16px
    width: 260px
  top-status-band:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.primary}"
    rounded: "{rounded.xs}"
    padding: 12px
    height: 56px
  panel:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.on-panel}"
    rounded: "{rounded.xs}"
    padding: 16px
  panel-muted:
    backgroundColor: "{colors.panel-muted}"
    textColor: "{colors.on-panel}"
    rounded: "{rounded.xs}"
    padding: 16px
  inspector:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.on-panel}"
    rounded: "{rounded.xs}"
    padding: 16px
    width: 384px
  evidence-row:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.on-panel}"
    rounded: "{rounded.xs}"
    padding: 10px
    height: 44px
  evidence-row-selected:
    backgroundColor: "{colors.selected}"
    textColor: "{colors.on-panel}"
    rounded: "{rounded.xs}"
    padding: 10px
    height: 44px
  button-primary:
    backgroundColor: "{colors.verdigris}"
    textColor: "{colors.on-action}"
    rounded: "{rounded.sm}"
    padding: 10px 14px
    height: 44px
  button-secondary:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.ink}"
    rounded: "{rounded.sm}"
    padding: 10px 14px
    height: 44px
  button-danger:
    backgroundColor: "{colors.persimmon-dark}"
    textColor: "{colors.on-action}"
    rounded: "{rounded.sm}"
    padding: 10px 14px
    height: 44px
  input:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.on-panel}"
    rounded: "{rounded.sm}"
    padding: 10px
    height: 44px
  status-chip:
    backgroundColor: "{colors.panel-muted}"
    textColor: "{colors.on-muted}"
    typography: "{typography.label}"
    rounded: "{rounded.sm}"
    padding: 4px 8px
  status-safe:
    backgroundColor: "{colors.verdigris-soft}"
    textColor: "{colors.verdigris-dark}"
    rounded: "{rounded.sm}"
    padding: 4px 8px
  status-warning:
    backgroundColor: "{colors.mustard-soft}"
    textColor: "{colors.ink}"
    rounded: "{rounded.sm}"
    padding: 4px 8px
  status-danger:
    backgroundColor: "{colors.persimmon-soft}"
    textColor: "{colors.persimmon-dark}"
    rounded: "{rounded.sm}"
    padding: 4px 8px
  status-info:
    backgroundColor: "{colors.blue-soft}"
    textColor: "{colors.ink}"
    rounded: "{rounded.sm}"
    padding: 4px 8px
  meter:
    backgroundColor: "{colors.bone-2}"
    textColor: "{colors.ink}"
    rounded: "{rounded.xs}"
    height: 6px
  code-block:
    backgroundColor: "{colors.panel-muted}"
    textColor: "{colors.ink}"
    typography: "{typography.evidence}"
    rounded: "{rounded.xs}"
    padding: 12px
  risk-callout:
    backgroundColor: "{colors.mustard-soft}"
    textColor: "{colors.ink}"
    rounded: "{rounded.xs}"
    padding: 12px
  rail-hover:
    backgroundColor: "{colors.graphite}"
    textColor: "{colors.on-ink}"
    rounded: "{rounded.xs}"
    padding: 8px
  rail-active:
    backgroundColor: "{colors.graphite-2}"
    textColor: "{colors.on-ink}"
    rounded: "{rounded.xs}"
    padding: 8px
  rail-muted-label:
    backgroundColor: "{colors.graphite-3}"
    textColor: "{colors.on-ink}"
    rounded: "{rounded.xs}"
    padding: 4px
  separator:
    backgroundColor: "{colors.border}"
    textColor: "{colors.ink}"
    height: 1px
  strong-separator:
    backgroundColor: "{colors.border-strong}"
    textColor: "{colors.ink}"
    height: 1px
  privacy-private:
    backgroundColor: "{colors.persimmon-soft}"
    textColor: "{colors.private}"
    rounded: "{rounded.sm}"
    padding: 4px 8px
  authority-owner:
    backgroundColor: "{colors.owner}"
    textColor: "{colors.on-action}"
    rounded: "{rounded.sm}"
    padding: 4px 8px
  authority-team:
    backgroundColor: "{colors.team}"
    textColor: "{colors.on-action}"
    rounded: "{rounded.sm}"
    padding: 4px 8px
  authority-system:
    backgroundColor: "{colors.panel-muted}"
    textColor: "{colors.system}"
    rounded: "{rounded.sm}"
    padding: 4px 8px
  authority-public:
    backgroundColor: "{colors.panel-muted}"
    textColor: "{colors.public}"
    rounded: "{rounded.sm}"
    padding: 4px 8px
  afm-safe-chip:
    backgroundColor: "{colors.afm-safe}"
    textColor: "{colors.on-action}"
    rounded: "{rounded.sm}"
    padding: 4px 8px
  learn-candidate-chip:
    backgroundColor: "{colors.learn-candidate}"
    textColor: "{colors.on-action}"
    rounded: "{rounded.sm}"
    padding: 4px 8px
  log-only-chip:
    backgroundColor: "{colors.log-only}"
    textColor: "{colors.ink}"
    rounded: "{rounded.sm}"
    padding: 4px 8px
  do-not-store-chip:
    backgroundColor: "{colors.persimmon-soft}"
    textColor: "{colors.do-not-store}"
    rounded: "{rounded.sm}"
    padding: 4px 8px
  success-toast:
    backgroundColor: "{colors.success}"
    textColor: "{colors.on-action}"
    rounded: "{rounded.sm}"
    padding: 12px
  warning-toast:
    backgroundColor: "{colors.warning}"
    textColor: "{colors.ink}"
    rounded: "{rounded.sm}"
    padding: 12px
  danger-toast:
    backgroundColor: "{colors.persimmon-soft}"
    textColor: "{colors.danger}"
    rounded: "{rounded.sm}"
    padding: 12px
  info-toast:
    backgroundColor: "{colors.info}"
    textColor: "{colors.on-action}"
    rounded: "{rounded.sm}"
    padding: 12px
  focus-ring:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.focus}"
    rounded: "{rounded.sm}"
    padding: 2px
  row-hover:
    backgroundColor: "{colors.hover}"
    textColor: "{colors.ink}"
    rounded: "{rounded.xs}"
    padding: 10px
  disabled-control:
    backgroundColor: "{colors.disabled}"
    textColor: "{colors.ink}"
    rounded: "{rounded.sm}"
    padding: 10px 14px
  loading-indicator:
    backgroundColor: "{colors.ink}"
    textColor: "{colors.loading}"
    rounded: "{rounded.xs}"
    height: 6px
  persimmon-swatch:
    backgroundColor: "{colors.persimmon}"
    textColor: "{colors.ink}"
    rounded: "{rounded.sm}"
    padding: 4px 8px
  mustard-swatch:
    backgroundColor: "{colors.mustard}"
    textColor: "{colors.ink}"
    rounded: "{rounded.sm}"
    padding: 4px 8px
  mustard-dark-label:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.mustard-dark}"
    rounded: "{rounded.sm}"
    padding: 4px 8px
  blue-swatch:
    backgroundColor: "{colors.blue}"
    textColor: "{colors.on-action}"
    rounded: "{rounded.sm}"
    padding: 4px 8px
  research-composer:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.on-panel}"
    rounded: "{rounded.xs}"
    padding: 16px
  run-ledger-row:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.on-panel}"
    rounded: "{rounded.xs}"
    padding: 10px
    height: 64px
  approval-loop:
    backgroundColor: "{colors.panel-muted}"
    textColor: "{colors.ink}"
    rounded: "{rounded.xs}"
    padding: 16px
---

# Sovereign Memory Console

## Overview

Sovereign Memory Console is an operational surface for agents and humans to inspect local memory, prepare context packets, drive Deep Research runs, review evidence, and make explicit learning decisions. It is private by posture and procedural by temperament: the interface should feel like a careful audit desk connected to a local control room.

The design exists to answer six questions quickly: what is the daemon doing, what research is running, what evidence is included, why was it ranked that way, what privacy or AFM constraints apply, and what action is safe next. Decorative motion, dramatic hero layouts, and brand spectacle are out of scope. Evidence is the visual subject.

Use this file as the normative design source. Reference plates under `assets/design/reference-plates/` are illustrative only; when they disagree with this document, this document wins.

## Colors

The palette is warm-paper first, graphite second, and semantic always. The base canvas uses bone paper (`#F4F1EA`) so long reading sessions do not feel clinical. Graphite and ink (`#181613`, `#242320`) provide control-room gravity without turning every surface into dark mode.

Verdigris (`#2F7D68`) is reserved for primary actions, AFM-safe states, learn candidates, and healthy local checks. Persimmon (`#D2603A`) and deep danger clay (`#A4483F`) mean exclusion, risk, destructive action, or do-not-store. Mustard (`#C9A961`) means review, log-only, elevated attention, or incomplete certainty. Blue (`#3D6F95`) is informational and should not compete with safety or danger.

Never rely on color alone. Pair every semantic color with text, an icon, a border treatment, or a label such as `AFM-SAFE`, `PRIVATE`, `LOG-ONLY`, `DO-NOT-STORE`, or `LEARN CANDIDATE`.

## Typography

Use Inter for interface language and IBM Plex Mono for machine evidence. Machine evidence includes source IDs, paths, timestamps, scores, hashes, JSON fields, trace IDs, policy IDs, and status pills. Keep letter spacing at zero; the console should look crisp and engineered, not spaced-out or decorative.

Headings are compact. Large display type is allowed only for product-level context or a major empty state, never inside dense panels. Tables, inspectors, and packet previews use small but readable type with generous line height. Long paths and JSON content must wrap or truncate with affordances rather than overflowing their containers.

## Layout

The default shell is a dense operational dashboard: persistent left rail, top status band, central work area, and optional right inspector. Prefer split panes, tables, research composers, run ledgers, inspectors, status matrices, and audit trays over card-heavy marketing composition.

The left rail is for navigation only. Contextual actions such as approve, include, exclude, export, or retry live beside the evidence they affect. The top band is for daemon state, AFM readiness, active workspace, privacy mode, last dry-run, and system health. It should be glanceable and stable, not a place for workflow decisions.

Use fixed dimensions for recurring operational controls. Source rows, chips, meters, icon buttons, table controls, and review items should not shift size when values change. The minimum interactive target is 44px in either width or height unless a compact data grid cell has a paired larger action target.

## Elevation & Depth

Depth is created with hairline borders, tonal surface changes, and alignment. Shadows are rare and quiet. Avoid glass, translucency, blurred materials, floating page sections, gradient blobs, and ornamental depth.

Panels can sit on the page background, but sections should not become cards inside cards. Repeated rows, modals, inspectors, and review items may use framed surfaces. The detail inspector should feel attached to the selected evidence, not like a separate promotional sidebar.

## Shapes

Use small radii. Chips, inputs, rows, and compact buttons use 4px. Larger panels can use 2px to 8px depending on density, but squared archival panels are preferred over bubbly shapes. Full pills are allowed only where the shape improves scanning for compact status labels.

Focus rings must be visible on every background. Use a 2px outline in `focus` blue with a 2px offset, never a subtle shadow-only focus state.

## Components

Buttons communicate consequence. Primary verdigris buttons advance safe local work such as `Generate Packet`, `Include Source`, or `Approve Selected Learn Candidates`. Secondary buttons inspect, compare, export, or retry. Danger buttons exclude, reject, discard, or mark do-not-store. Destructive actions require a confirmation state with plain language about what will and will not be stored.

Evidence tables must show score, source name or path, source class, privacy, authority, recency, inclusion reason, AFM-safe state, and action. Selected rows use both a checkbox state and selected row background. Private and do-not-store rows must remain readable when disabled.

Source inspectors expose metadata before content: source ID, path, ingested time, modified time, size, locality, score, privacy level, authority, AFM status, content hash, collection, and tags. Evidence excerpts, JSON packet previews, risk annotations, provenance, and recommended handling appear as separate labeled blocks.

Dry-run review surfaces always separate `LEARN CANDIDATES`, `LOG-ONLY`, and `DO-NOT-STORE`. The copy must state when nothing has been stored. Learning actions are explicit, reversible where possible, and never implied by navigation or inspection.

Deep Research surfaces use a three-part workflow: `PLAN`, `APPROVE/REFINE`, and `RUN/STATUS`. The research composer owns prompt, mode, file-search stores, built-in tool toggles, document URIs, and visualization choice. Collaborative planning responses must keep the interaction ID visible beside refine and approve controls. Run ledgers show run ID, prompt, mode, status, result/report availability, and last update. Local-doc/file-store management must make the fixed local document boundary clear before any upload/index action.

Empty, loading, and error states are compact operational states. Empty states suggest the next useful filter or action. Loading states show what is being evaluated. Errors name the failed local boundary, such as daemon connection, vault access, malformed packet, or unavailable AFM bridge.

Reference plates should depict synthetic data only. Use fake source IDs, fake paths, fake timestamps, and fake hashes. Do not include raw session text, adapter paths, DB filenames, private datasets, or vault raw/log content.

## Do's and Don'ts

- Do keep evidence, ranking, privacy, authority, and inclusion reason visible in primary workflows.
- Do make local-only and manual-learning boundaries obvious.
- Do use warm paper, graphite, verdigris, persimmon, mustard, and blue as semantic roles rather than decoration.
- Do design for keyboard focus, large text, and status recognition without color alone.
- Do keep generated plates synthetic and clearly subordinate to this `DESIGN.md`.
- Don't use Apple-style glass, translucency, or platform chrome as the visual identity.
- Don't use large hero sections, decorative gradients, bokeh, or generic SaaS marketing layouts.
- Don't make a global navigation item perform a learning, approval, rejection, or storage action.
- Don't imply that inspecting a packet stores, learns, exports, or publishes anything.
- Don't commit private vault material, adapter files, launchd plists, datasets, DB files, raw logs, or live session content as design assets.
