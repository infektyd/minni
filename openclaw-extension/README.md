# Sovereign Memory Bridge

OpenClaw plugin bridge for Sovereign Memory integration.

## Architecture

- **sovrd.py** — Python HTTP daemon over Unix socket (`/tmp/sovereign.sock`)
- **src/bridge.ts** — TypeScript client with reconnect logic
- **src/bridge-process.ts** — Process supervisor for spawning/managing sovrd

## Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/health` | Health check |
| GET | `/recall?q=...` | Recall memories by query |
| GET | `/read?key=...` | Read memory by key |
| GET | `/identity` | Get identity context |
| GET | `/full` | Get full context (identity + memory) |
| POST | `/learn` | Learn new information (JSON body: `{content, category}`) |

## Quick Start

### 1. Start the daemon

```bash
cd ~/.openclaw/plugins/sovereign-memory/
~/.openclaw/sovereign-memory-v3.1/venv/bin/python sovrd.py
```

### 2. Verify with curl

```bash
curl --unix-socket /tmp/sovereign.sock http://localhost/health
# => {"status":"ok","agent":"hermes"}
```

### 3. Use the TypeScript bridge

```bash
npm install
npm run build
npm run test
```

Or programmatically:

```typescript
import { health, recall, learn } from './dist/bridge.js';

// Health check
const status = await health();

// Recall memories
const results = await recall("What is my name?");

// Learn something new
await learn("The user's name is Alex", "personal");
```

## File Structure

```
~/.openclaw/plugins/sovereign-memory/
├── sovrd.py              # Python daemon
├── package.json          # Node.js project config
├── tsconfig.json         # TypeScript config
├── README.md             # This file
├── src/
│   ├── bridge.ts         # Unix socket HTTP client
│   └── bridge-process.ts # Process supervisor
└── dist/                 # Compiled TypeScript (after build)
```

## Constraints

- Do NOT modify `~/.openclaw/sovereign-memory-v3.1/` (read-only dependency)
- Do NOT touch `~/.hermes/`
- Uses existing venv: `~/.openclaw/sovereign-memory-v3.1/venv/bin/python`
