# UsageDock

A right-edge macOS rail that shows how much of each provider's quota you have already burned. Inspired by [hivinz_'s post](https://x.com/hivinz_/status/2093651446927626294), not a copy of it.

This file is the plan the app is built against. If a later change fights a decision here, change the file first.

## What the post is actually selling

The screenshot is a tall dark pill hugging the right display edge. Three circular rings (Claude / ChatGPT / Perplexity) sit in a column with a percent under each. Click or hover Claude and a speech-bubble card slides out: "Current session" at 73% (resets in 51 min) and "All models" at 7% (resets Thu 12:00 AM).

That layout is good. The implication that every vendor has the same two windows, and that those percents are live, is not.

## Adversarial decisions

These are the places we refuse the mockup. Each one is a product choice, not a polish item.

1. **Live mode does not invent numbers.** Claude Code's `/usage` data is real (`GET https://api.anthropic.com/api/oauth/usage` with the OAuth token Claude Code already stores). ChatGPT Plus and Perplexity do not publish an equivalent session/weekly utilization API. In live mode those slots render as empty rings plus a reason, not as 21% and 52%. Demo mode exists so you can compare the layout to the post. Demo is labeled. It is never the default after a successful Claude fetch.

2. **No licensed vendor marks.** The post uses official logos. We do not ship those assets. Each provider gets an original geometric mark drawn in Swift. Close enough to recognize at 44pt, not a trademark dump.

3. **Minni palette, not neon-on-void.** Accents come from the repo `DESIGN.md`: persimmon for Claude, verdigris for ChatGPT, mustard for Perplexity, blue for Cursor. The rail uses `ultraThinMaterial` over a near-black fill, not a flat `#000` sticker. It has to sit on a real wallpaper.

4. **The rail does not own the display edge.** 8pt inset from the visible frame. The rail hides for fullscreen apps and for the built-in screen-sharing "do not disturb the presenter" case. A menu-bar extra stays so you can quit, open settings, and still see the hottest percent when the rail is down.

5. **Hover peeks, click pins.** The post is a still. Hover (220ms) opens the card; moving away closes it. Click pins it until click-outside or Escape. A monitor you glance at should not require a click, and a monitor you study should not vanish when the cursor twitches.

6. **Do not copy tokens out of Claude Code's files.** Read `CLAUDE_CODE_OAUTH_TOKEN`, then `~/.claude/.credentials.json`. Do not write a second copy into our container. Do not log the token. Keychain is a user-initiated fallback only, because `security find-generic-password` can pop an auth dialog in the middle of a background poll. Refresh on 401 / expiry writes back to the same file Claude Code owns, under an advisory lock on a sidecar, never on the credentials inode.

7. **Poll slowly.** The usage endpoint is undocumented and shared with Claude Code. Minimum 180s between live fetches. 429s back off for `Retry-After` or 5 minutes. A failed poll keeps the last good snapshot and marks it stale after 15 minutes. We never estimate a missing percent from token counts.

8. **Utilization is normalized, not trusted.** Community payloads send 0–100 most of the time and 0–1 occasionally. Values in `(0, 1)` are treated as fractions. `0` and `1` stay as 0% and 1% (not 100%). Everything is clamped to `[0, 100]` before it hits a ring.

9. **No WidgetKit target in this pass.** A second extension target, an App Group, and a sandboxed reader that cannot see `~/.claude` is a follow-up. The rail *is* the widget.

10. **Unsandboxed v1.** A sandbox that cannot read `~/.claude/.credentials.json` without a file-picker dance is the wrong first ship for a local quota rail. Hardened Runtime stays on so you can sign and notarize later. Revisit sandboxing when there is a real store build.

## Layout (the agreed geometry)

Units are logical points. The rail is a vertical stack, trailing edge of the current screen.

```
                  8pt inset
                 ┌─────────┐
                 │  14pt   │
                 │  [ ◯ ]  │  ring 44, stroke 3.5
                 │   73%   │  11pt semibold, 6pt below ring
                 │  16pt   │
                 │  [ ◯ ]  │
                 │   21%   │
                 │  16pt   │
                 │  [ ◯ ]  │
                 │   52%   │
                 │  14pt   │
                 └─────────┘
                   72 wide
```

The pill is a continuous rounded rect (radius = half width). No concave "bite" cutouts. Those look clever in a mockup and fight window shadows plus Mission Control thumbnails.

The popover is 268 wide, 16 radius, 14pt padding. It sits 10pt to the leading side of the rail, vertically aligned to the selected ring, and never leaves the visible frame. Arrow is 8×10, on the trailing edge, aimed at the ring center.

Popover content, top to bottom:

- Mark + "{Name} Usage" (15pt semibold)
- For each window: label (11pt secondary), reset text (11pt secondary, trailing), 6pt track, "{n}% Used" (11pt)
- Primary window (session) uses the provider accent. Secondary windows use verdigris if under 50%, mustard if 50–79%, persimmon if 80%+.
- Footer: last sync, or the unsupported reason.

Empty / unsupported card: the same chrome, one sentence, no fake bar.

## Data model

A `ProviderKind` is a stable id (`claude`, `chatgpt`, `perplexity`, `cursor`). A `UsageWindow` is a percent plus an optional reset date. A `ProviderSnapshot` is either `.live(windows:)`, `.stale(windows:asOf:)`, `.unavailable(reason:)`, or `.unsupported(reason:)`.

Claude maps:

- `five_hour` / `limits[kind=session]` → "Current session"
- `seven_day` / `limits[kind=weekly_all]` → "All models"
- `limits[kind=weekly_scoped]` → the model's display name (Fable, etc.)

ChatGPT, Perplexity, Cursor ship as `.unsupported` until someone writes a real adapter that returns authoritative percents. A Cursor slot is in the model because this machine actually runs Cursor. It is off by default so the first-run rail matches the three-ring post.

## Toolchain

- Language mode: Swift 6
- Preferred compiler: Swift 6.4 (Xcode 27)
- Also expected to build with Xcode 26.4 / Swift 6.3
- Deployment target: macOS 15
- Concurrency: complete checking, `@MainActor` UI, `actor` for the HTTP client

This Linux environment cannot link AppKit. The core types and the Claude decoder are written so a later `swift test` on a Mac is boring. `tools/check_core.py` is the verification that runs here: same fixtures, same normalization and clock rules.

## What you do on the Mac

Open `UsageDock.xcodeproj`, set your Development Team, run the `UsageDock` scheme. First launch is demo mode so the rail appears immediately. Settings → Live is the real path once Claude Code has signed in on that machine.
