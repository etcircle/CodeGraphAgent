# CodeGraphAgent

**Turn your codebase into a queryable knowledge graph for AI agents.**

An MCP server that indexes local code into Neo4j and exposes high-level tools designed for how AI agents actually work — not raw database queries, but purpose-built operations like "tell me everything about this function" or "what does this module do?"

Fork of [CodeGraphContext](https://github.com/CodeGraphContext/CodeGraphContext) with a fundamentally different focus: **agent productivity over IDE integration.**

---

## Why This Exists

Most code analysis tools are built for humans in IDEs. AI agents have different needs:

- **One call, one complete answer.** Agents shouldn't make 5 tool calls to understand a function.
- **Filesystem is truth.** Graph data can be stale — source code must always come from disk.
- **Self-orienting.** Agents need to know which tool to use without reading documentation.
- **Resilient watchers.** When 4 coding agents edit files simultaneously, the index must keep up.

CodeGraphAgent addresses all of these.

---

## Installation

### Prerequisites

- **Python 3.13+**
- **Neo4j 5.x** — local, Docker, or remote. Community Edition works fine.
- **ripgrep** (`rg`) — optional but recommended (10-100x faster `grep_code`)
- **git** — required for `diff_since` tool

### Option A: Install from Source (Recommended)

```bash
git clone https://github.com/etcircle/CodeGraphAgent.git
cd CodeGraphAgent
pip install -e .
```

Editable install (`-e`) means changes to the source are live immediately — no reinstall needed. This is the recommended approach if you plan to customise or contribute.

### Option B: Install with pipx (Isolated Environment)

```bash
# For a clean isolated install:
pipx install git+https://github.com/etcircle/CodeGraphAgent.git

# Or editable from a local clone:
git clone https://github.com/etcircle/CodeGraphAgent.git
pipx install -e ./CodeGraphAgent
```

### Verify Installation

```bash
cgc --version
# → CodeGraphContext 0.3.1
```

The `cgc` command is your CLI entry point for all operations.

---

## Setup

### 1. Configure Neo4j Connection

Create a `.env` file in your project root or `~/.codegraphcontext/.env`:

```env
NEO4J_URI=bolt://localhost:7687
NEO4J_USERNAME=neo4j
NEO4J_PASSWORD=your-password
# NEO4J_DATABASE=neo4j  # optional, defaults to 'neo4j'
```

Or set environment variables:

```bash
export NEO4J_URI=bolt://localhost:7687
export NEO4J_USERNAME=neo4j
export NEO4J_PASSWORD=your-password
```

**Docker quick start for Neo4j:**

```bash
docker run -d \
  --name neo4j-codegraph \
  -p 7687:7687 -p 7474:7474 \
  -e NEO4J_AUTH=neo4j/your-password \
  neo4j:5-community
```

### 2. Index Your Codebase

```bash
# Index one or more repositories
cgc index /path/to/your/backend
cgc index /path/to/your/frontend
cgc index /path/to/your/extensions

# Force re-index (wipes existing data for that repo and rebuilds)
cgc index --force /path/to/your/backend

# Check what's indexed
cgc list

# Get stats
cgc stats
```

Indexing uses tree-sitter parsing — supports 18 languages and typically processes 500-1000 files per minute.

### 3. Start the File Watchers

Watchers keep the graph in sync as code changes. **This is critical** — without watchers, the graph goes stale as soon as anyone edits a file.

#### Manual Watcher (Foreground)

```bash
# Watch a single directory (runs in foreground, Ctrl+C to stop)
cgc watch /path/to/your/project
```

#### Background Watchers (Production Setup)

For a production setup with multiple repos, create a watcher script:

```bash
#!/bin/bash
# cgc-watch-all.sh — persistent watchers for all your repos

export NEO4J_URI="bolt://your-neo4j:7687"
export NEO4J_USERNAME="neo4j"
export NEO4J_PASSWORD="your-password"
export HOME="${HOME:-/home/youruser}"

PROJECT="/path/to/your/project"
CGC=$(which cgc)
LOG_DIR="/tmp/cgc-watch"
mkdir -p "$LOG_DIR"

# Start watchers in background
nohup "$CGC" watch "$PROJECT/backend/" >> "$LOG_DIR/backend.log" 2>&1 &
echo $! > "$LOG_DIR/backend.pid"

nohup "$CGC" watch "$PROJECT/frontend/" >> "$LOG_DIR/frontend.log" 2>&1 &
echo $! > "$LOG_DIR/frontend.pid"

echo "Watchers started. PIDs:"
cat "$LOG_DIR/backend.pid" "$LOG_DIR/frontend.pid"
```

#### Auto-Restart with cron (Recommended)

The watchers can die if Neo4j restarts or the machine sleeps. Use a keepalive cron:

```bash
# Add to crontab (crontab -e):
*/5 * * * * /path/to/cgc-watch-all.sh >> /tmp/cgc-watch/keepalive.log 2>&1
```

Your watcher script should check if watchers are already running before starting new ones (check PID files).

#### Auto-Watch via MCP Server

Set the `CGC_AUTO_WATCH_PATHS` environment variable and the MCP server will start watchers automatically on launch:

```bash
export CGC_AUTO_WATCH_PATHS="/path/to/backend:/path/to/frontend:/path/to/extensions"
```

#### Verify Watchers Are Running

```bash
# Check health files (written by each watcher every 60s)
cat /tmp/cgc-watch/backend-health.json
# → {"status": "healthy", "cached_files": 452, "total_batches": 12, "total_errors": 0, ...}

# Or via MCP tool:
# Call get_watcher_health from any MCP client
```

### 4. Start the MCP Server

#### Stdio Mode (IDE Integration — Claude Code, Cursor, VS Code)

```bash
cgc mcp start
```

Configure in your IDE's MCP settings (e.g. `claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "codegraph": {
      "command": "cgc",
      "args": ["mcp", "start"],
      "env": {
        "NEO4J_URI": "bolt://localhost:7687",
        "NEO4J_USERNAME": "neo4j",
        "NEO4J_PASSWORD": "your-password"
      }
    }
  }
}
```

#### HTTP Mode (Remote Agents — Claude.ai, Custom Agents)

For remote agents that connect over HTTP (e.g. Claude.ai's MCP integration), use [supergateway](https://github.com/nicepkg/supergateway):

```bash
npx supergateway \
  --stdio "cgc mcp start" \
  --port 8790 \
  --outputTransport streamableHttp \
  --healthEndpoint /healthz
```

This exposes the MCP server on `http://localhost:8790/mcp` via Streamable HTTP.

**As a persistent service (macOS launchd):**

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.codegraphagent.mcp-server</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/local/bin/npx</string>
        <string>supergateway</string>
        <string>--stdio</string>
        <string>cgc mcp start</string>
        <string>--port</string>
        <string>8790</string>
        <string>--outputTransport</string>
        <string>streamableHttp</string>
        <string>--healthEndpoint</string>
        <string>/healthz</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>HOME</key>
        <string>/Users/youruser</string>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:~/.local/bin</string>
        <key>NEO4J_URI</key>
        <string>bolt://your-neo4j:7687</string>
        <key>NEO4J_USERNAME</key>
        <string>neo4j</string>
        <key>NEO4J_PASSWORD</key>
        <string>your-password</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/tmp/cgc-mcp-server.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/cgc-mcp-server-err.log</string>
</dict>
</plist>
```

Save to `~/Library/LaunchAgents/com.codegraphagent.mcp-server.plist`, then:

```bash
launchctl load ~/Library/LaunchAgents/com.codegraphagent.mcp-server.plist
curl http://localhost:8790/healthz  # → "ok"
```

**As a systemd service (Linux):**

```ini
[Unit]
Description=CodeGraphAgent MCP Server
After=network.target

[Service]
Type=simple
User=youruser
Environment=NEO4J_URI=bolt://localhost:7687
Environment=NEO4J_USERNAME=neo4j
Environment=NEO4J_PASSWORD=your-password
ExecStart=/usr/bin/npx supergateway --stdio "cgc mcp start" --port 8790 --outputTransport streamableHttp --healthEndpoint /healthz
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

---

## Tools — The Agent Productivity Suite

### Primary Tools (use these first)

These 7 tools cover ~95% of what an agent needs when exploring or modifying code:

| Tool | What It Does | Example |
|------|-------------|---------|
| **`get_module_overview`** | Structured summary of a module — endpoints, services, models, schemas, key functions with complexity | "What does the knowledge module do?" |
| **`get_function_context`** | Everything about a function in one call — source (from filesystem), callers, callees, class membership, sibling methods | "Tell me about `store_extraction` before I modify it" |
| **`grep_code`** | Text/regex search across indexed repos with context lines | "Find all references to `/api/v1/auth/refresh`" |
| **`find_references`** | All usages of a symbol — callers, importers, inheritors, type annotations, text mentions | "Who uses `UserResponse`?" |
| **`get_file_content`** | Read source code with line numbers, line ranges, and `around_line` centering | "Show me lines 100-150 of extractor.py" |
| **`diff_since`** | Recent changes via git — commits, file list, stats, uncommitted changes | "What changed in the last 4 hours?" |
| **`explain_path`** | Shortest call chain between two functions via graph traversal | "How does the API endpoint reach the database?" |

### Secondary Tools

| Tool | What It Does |
|------|-------------|
| `get_file_structure` | Project directory tree with function/class counts |
| `find_most_complex_functions` | Find refactoring targets by cyclomatic complexity |
| `find_dead_code` | Find unused functions across the codebase |
| `execute_cypher_query` | Raw Cypher fallback for anything the above can't answer |
| `cgc_guide` | Returns the tool routing guide — call at session start for orientation |
| `get_watcher_health` | Watcher status, batch counts, errors, Neo4j connectivity |

### Admin Tools

These manage the index itself — not for code analysis:

`watch_directory` · `unwatch_directory` · `list_watched_paths` · `add_code_to_graph` · `add_package_to_graph` · `delete_repository` · `list_jobs` · `check_job_status` · `list_indexed_repositories` · `get_repository_stats` · `load_bundle` · `search_registry_bundles`

### Legacy Tools (superseded)

These still work but the primary tools are better:

| Legacy Tool | Use Instead |
|-------------|-------------|
| `find_code` | `grep_code` — regex support, context lines, file filtering |
| `analyze_code_relationships` | `find_references` + `get_function_context` |
| `calculate_cyclomatic_complexity` | `get_function_context` or `get_module_overview` (complexity included) |

---

## Agent Workflows

### Exploring a New Module
```
1. get_module_overview(module_path="backend/app/modules/knowledge")
   → Endpoints, services, models, schemas
2. get_function_context(function_name="search_knowledge")
   → Source + callers + callees + class
3. explain_path(from_function="search_knowledge", to_function="store_fact")
   → API endpoint → service → database call chain
```

### Understanding a Function Before Changing It
```
1. get_function_context(function_name="store_extraction", include_source=true, caller_depth=2)
   → Full picture in one call
```

### Picking Up Another Agent's Work
```
1. diff_since(repo_path="/path/to/repo", since="4h")
   → Files changed, commits, uncommitted work
2. get_function_context on modified functions
   → Understand what changed and why
```

### Finding Where Something Is Used
```
1. find_references(symbol="UserResponse")
   → Callers, importers, type annotations, text mentions — all in one call
```

---

## The Watcher System

CodeGraphAgent includes a production-grade file watcher that keeps the graph in sync as code changes. This is the most critical piece of infrastructure — a stale graph gives agents wrong answers.

### What Makes It Production-Grade

| Feature | What It Does | Why It Matters |
|---------|-------------|----------------|
| **Incremental processing** | Only re-parses changed files, only re-links affected edges | A 1-file change doesn't re-process 1000 files |
| **Error isolation** | A parse failure in one file doesn't kill the batch | One broken file doesn't stall the whole watcher |
| **Retry queue** | Failed files are retried with max retry limit | Transient failures (file locked, half-written) self-heal |
| **Circuit breaker** | Stops hammering Neo4j when it's temporarily down | Neo4j restart doesn't kill the watcher process |
| **Periodic reconciliation** | Catches events missed by the OS event buffer | When 4 agents write simultaneously, macOS FSEvents can overflow |
| **Health monitoring** | Per-watcher JSON health files + MCP tool | You can always check if the graph is stale |
| **.gitignore-aware** | Won't index `node_modules`, `.git`, build artifacts | `node_modules` won't eat 3.5 GB of RAM |
| **File stability check** | Waits for editors to finish writing before parsing | No more parsing half-written files |
| **Graceful shutdown** | Persists file state cache on SIGTERM/SIGINT | Fast restart — diff against cache instead of full re-scan |
| **Adaptive debounce** | Scales from 5s to 30s based on batch size | Small edits are fast, bulk changes don't overwhelm |

### Watcher Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `CGC_DEBOUNCE_SECONDS` | `5` | Debounce window for batching file changes |
| `CGC_MAX_DEBOUNCE_SECONDS` | `30` | Maximum debounce window under load |
| `CGC_RECONCILE_INTERVAL` | `300` | Seconds between reconciliation sweeps |
| `CGC_HEALTH_DIR` | `/tmp/cgc-watch` | Directory for health JSON files |
| `CGC_MAX_RETRIES` | `3` | Max retries for failed file processing |
| `CGC_CIRCUIT_BREAKER_THRESHOLD` | `5` | Neo4j failures before circuit opens |
| `CGC_CIRCUIT_BREAKER_RESET` | `60` | Seconds before circuit half-opens |
| `CGC_FILE_CACHE_DIR` | `~/.codegraphcontext/cache` | Persistent file state cache for fast restart |
| `CGC_AUTO_WATCH_PATHS` | (empty) | Colon-separated paths to auto-watch on MCP start |

### Health File Format

Each watcher writes a JSON health file every 60 seconds:

```json
{
  "timestamp": "2026-03-28T13:28:34Z",
  "status": "healthy",
  "watched_path": "/path/to/your/backend",
  "cached_files": 452,
  "last_batch_at": "2026-03-28T13:28:30Z",
  "last_batch_files": 2,
  "total_batches": 12,
  "total_errors": 0,
  "needs_full_relink": false,
  "failed_paths": [],
  "pid": 30005
}
```

Status values: `healthy` (all good), `degraded` (some failed paths), `error` (needs full relink or >10 failures).

---

## Database Options

- **Neo4j** (recommended) — full Cypher support, APOC procedures, browser visualisation
- **FalkorDB** — lightweight alternative (from upstream)
- **Kuzu** — embedded, zero-config (from upstream)

---

## Supported Languages

Python · TypeScript · JavaScript · Go · Java · C · C++ · Rust · Ruby · C# · Kotlin · Scala · Swift · Haskell · Dart · Perl · Elixir · PHP

---

## Architecture

```
┌─────────────────────────────────────────────────┐
│                  MCP Clients                     │
│  (Claude.ai, Claude Code, Cursor, VS Code, ...) │
└──────────────┬──────────────────────────────────┘
               │ MCP Protocol (stdio or HTTP)
┌──────────────▼──────────────────────────────────┐
│              MCP Server (server.py)               │
│  ┌─────────────────────────────────────────────┐ │
│  │  Tool Handlers                               │ │
│  │  search_handlers  · context_handlers         │ │
│  │  file_handlers    · watcher_handlers         │ │
│  │  indexing_handlers · query_handlers           │ │
│  └─────────────────────────────────────────────┘ │
│  ┌──────────────┐  ┌────────────┐  ┌──────────┐ │
│  │ GraphBuilder  │  │ CodeFinder │  │ Watcher  │ │
│  └──────┬───────┘  └─────┬──────┘  └────┬─────┘ │
└─────────┼────────────────┼───────────────┼───────┘
          │                │               │
┌─────────▼────────────────▼───────────────▼───────┐
│                    Neo4j                          │
│  Repositories → Files → Functions/Classes         │
│  CALLS · IMPORTS · INHERITS · HAS_PARAMETER       │
└──────────────────────────────────────────────────┘
```

---

## Development

```bash
# Clone and install in editable mode
git clone https://github.com/etcircle/CodeGraphAgent.git
cd CodeGraphAgent
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Run tests
python -m pytest tests/ -q

# Run just unit tests
python -m pytest tests/unit/ -q
```

---

## Credits

Originally forked from [CodeGraphContext](https://github.com/CodeGraphContext/CodeGraphContext) by [Shashank Shekhar Singh](https://github.com/Shashankss1205). The original project provides the excellent tree-sitter parsing, graph building, and multi-language support that this fork builds upon.

**What this fork adds:**
- 9 agent-focused MCP tools (`get_function_context`, `grep_code`, `get_module_overview`, `cgc_guide`, etc.)
- Production-grade file watcher (circuit breaker, retry queue, incremental processing, reconciliation)
- Agent-oriented system prompt with tool routing
- Comprehensive test suite (190+ tests)

---

## License

MIT License — see [LICENSE](LICENSE) for details.
