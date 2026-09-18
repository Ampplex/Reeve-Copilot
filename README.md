# Reeve Copilot

**Reeve Copilot** is a VS Code-based AI coding assistant with persistent long-term memory, powered by [Reeve](https://mcp.reeve.co.in). It extends GitHub Copilot Chat with the ability to remember your conversations, preferences, and project context across sessions — so it gets smarter the more you use it.

## How It Works

Reeve Copilot integrates with the **Reeve MCP Server** using the Model Context Protocol (MCP) over Server-Sent Events (SSE). Every time you chat:

1. **Your message is stored** in Reeve's graph-based memory (Neo4j)
2. **Relevant past context** is retrieved and injected into the prompt
3. **The AI response is also stored** — building a bidirectional memory loop

```
┌──────────────────────────────────────────────────┐
│                  Reeve Copilot                   │
│                                                  │
│  User prompt ──► queryMemory() ──► Reeve MCP     │
│       │              │             (SSE + JSON-RPC)
│       │              ▼                           │
│       │     Past context injected                │
│       │         into prompt                      │
│       ▼                                          │
│  Copilot LLM generates response                 │
│       │                                          │
│       ▼                                          │
│  storeMemory() ──► Reeve MCP                     │
│  (user msg + AI response persisted)              │
└──────────────────────────────────────────────────┘
```

## Setup

### Prerequisites

- **Node.js** v22+ (recommend using [fnm](https://github.com/Schniz/fnm))
- **Reeve API Key** — get one from [Reeve](https://mcp.reeve.co.in)

### Build from Source

```bash
# Clone the repository
git clone https://github.com/Ampplex/Reeve-Copilot.git
cd Reeve-Copilot/vscode

# Install dependencies
npm install

# Build the Copilot extension
cd extensions/copilot
npm install
node .esbuild.mts --dev

# Run Reeve Copilot
cd ../..
./scripts/code.sh
```

### Configure Reeve

Open Settings (`Cmd+,`) and search for `reeve`:

| Setting | Description | Default |
|---------|-------------|---------|
| `github.copilot.reeve.enabled` | Enable/disable Reeve memory | `true` |
| `github.copilot.reeve.serverUrl` | Reeve MCP server URL | `https://mcp.reeve.co.in` |
| `github.copilot.reeve.apiKey` | Your Reeve API key / auth token | — |

Alternatively, set the `REEVE_API_KEY` or `REEVE_AUTH_TOKEN` environment variable.

## Where Reeve Copilot Files Live

All Reeve-specific integration code is inside `extensions/copilot/src/`:

```
extensions/copilot/
├── src/
│   ├── platform/reeve/
│   │   ├── common/
│   │   │   └── reeveClient.ts          # IReeveClient interface & types
│   │   ├── node/
│   │   │   └── reeveClient.ts          # MCP SSE client implementation
│   │   └── test/node/
│   │       └── reeveClient.spec.ts     # Unit tests (11 tests)
│   │
│   └── extension/
│       ├── conversation/vscode-node/
│       │   └── chatParticipants.ts     # Memory injection & persistence in chat
│       ├── tools/node/
│       │   ├── reeveSearchMemoryTool.ts    # LLM tool: copilot_reeveSearchMemory
│       │   └── test/
│       │       └── reeveSearchMemoryTool.spec.ts
│       └── extension/vscode-node/
│           └── services.ts            # DI registration for IReeveClient
│
├── package.json                       # Extension manifest & settings schema
└── .esbuild.mts                       # Build script
```

### Key Files

| File | Purpose |
|------|---------|
| [`platform/reeve/node/reeveClient.ts`](vscode/extensions/copilot/src/platform/reeve/node/reeveClient.ts) | Core MCP client — SSE connection, 3-way handshake, `tools/call` JSON-RPC, Bearer auth |
| [`platform/reeve/common/reeveClient.ts`](vscode/extensions/copilot/src/platform/reeve/common/reeveClient.ts) | `IReeveClient` service interface and type definitions |
| [`conversation/vscode-node/chatParticipants.ts`](vscode/extensions/copilot/src/extension/conversation/vscode-node/chatParticipants.ts) | Hooks into Copilot chat — queries memory before each prompt, stores both user messages and AI responses |
| [`tools/node/reeveSearchMemoryTool.ts`](vscode/extensions/copilot/src/extension/tools/node/reeveSearchMemoryTool.ts) | Registers `copilot_reeveSearchMemory` as a tool the LLM can invoke autonomously |
| [`extension/vscode-node/services.ts`](vscode/extensions/copilot/src/extension/extension/vscode-node/services.ts) | Dependency injection — registers `ReeveClient` as the `IReeveClient` singleton |

## Architecture

### MCP over SSE Protocol Flow

```
Client (Reeve Copilot)                    Server (mcp.reeve.co.in)
        │                                          │
        │──── GET /sse ────────────────────────────►│
        │     Authorization: Bearer <token>         │
        │                                          │
        │◄─── SSE: endpoint event ─────────────────│
        │     data: /messages?sessionId=xxx         │
        │                                          │
        │──── POST /messages?sessionId=xxx ────────►│
        │     { "method": "initialize", ... }       │
        │                                          │
        │◄─── SSE: message event ──────────────────│
        │     { "result": { capabilities: ... } }   │
        │                                          │
        │──── POST /messages?sessionId=xxx ────────►│
        │     { "method": "notifications/initialized" }
        │                                          │
        │──── POST /messages?sessionId=xxx ────────►│
        │     { "method": "tools/call",             │
        │       "params": { "name": "retrieve_memory_context",
        │                   "arguments": { ... } } }│
        │                                          │
        │◄─── SSE: message event ──────────────────│
        │     { "result": { "content": [...] } }    │
        └──────────────────────────────────────────┘
```

### Memory Tools

| MCP Tool | Description |
|----------|-------------|
| `retrieve_memory_context` | Query past memories by question + speaker |
| `store_memory` | Persist text (conversations, facts) with speaker partition |
| `query_memory` | Structured memory query |

## Running Tests

```bash
cd extensions/copilot
npx vitest run src/platform/reeve/test/node/reeveClient.spec.ts
npx vitest run src/extension/tools/node/test/reeveSearchMemoryTool.spec.ts
```

## Design Principles

- **Fail-safe**: If Reeve is down, disabled, or times out (1500ms), Copilot works normally
- **Zero auth changes**: No modifications to GitHub Copilot's authentication, token handling, or billing
- **Native protocol**: MCP SSE + JSON-RPC implemented with native `fetch` — no external SDK dependencies
- **Bidirectional memory**: Both user prompts and AI responses are stored for full conversation recall

## License

Copyright (c) 2026 Ankesh Kumar. Licensed under the [MIT License](LICENSE.txt).
