# UsageDock

macOS edge rail for provider quota. SwiftUI + AppKit panel, Swift 6, macOS 15+.

Read `DESIGN.md` before changing layout or adapters. That file is the product contract.

## Open on a Mac

```bash
open macos/UsageDock/UsageDock.xcodeproj
```

Scheme: `UsageDock`. Set a Development Team on the app target (Signing & Capabilities) and Run.

Xcode 27 / Swift 6.4 is what this was written against. Xcode 26.4 / Swift 6.3 should compile the same sources.

If you prefer XcodeGen:

```bash
brew install xcodegen
cd macos/UsageDock && xcodegen generate
```

## What runs where

| Surface | This Linux VM | Your Mac |
|---|---|---|
| Core rules (`tools/check_core.py`) | yes | yes |
| HTML layout preview (`Preview/index.html`) | yes | yes |
| AppKit rail, live Claude fetch, Keychain | no | yes |

```bash
python3 macos/UsageDock/tools/check_core.py
```

## Live Claude data

The app reads the same OAuth token Claude Code already has.

1. `CLAUDE_CODE_OAUTH_TOKEN` if set
2. `~/.claude/.credentials.json` → `claudeAiOauth.accessToken`

It then calls `GET https://api.anthropic.com/api/oauth/usage`. That endpoint is undocumented. When it moves, the rail shows the last good snapshot as stale, or unavailable. It does not invent a percent.

ChatGPT and Perplexity have no equivalent public utilization API. Live mode leaves those rings empty on purpose. Demo mode is how you compare the layout to the original post.

## Settings

Menu-bar extra → Settings.

- Mode: Demo / Live
- Enabled providers
- Rail edge: trailing (default) or leading
- Launch at login
- Poll interval (floor 180s)

## Layout of this folder

```
DESIGN.md                 contract
UsageDock.xcodeproj       open this
project.yml               XcodeGen spec
Sources/UsageDockCore     models, decoder, clock (no AppKit)
Sources/UsageDock         app, adapters, views
Tests/UsageDockCoreTests  Swift Testing (run on Mac)
Fixtures                  anonymized payloads
Preview/index.html        visual twin
tools/check_core.py       Linux-verifiable rules
```
