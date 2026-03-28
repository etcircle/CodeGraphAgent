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

CodeGraph Agent addresses all of these.

---

## Quick Start

### Prerequisites
- Python 3.13+
- Neo4j 5.x (local or remote)
- ripgrep (`rg`) — optional but recommended (10-100x faster grep)

### Install

```bash
pip install codegraph-agent
# or from source:
git clone https://github.com/etcircle/CodeGraphAgent.git
cd CodeGraphAgent
pip install -e .
```

### Configure Neo4j

```bash
# Create .env or set environment variables
export NEO4J_URI=bolt://localhost:7687
export NEO4J_USERNAME=neo4j
export NEO4J_PASSWORD=your-password
```

### Index Your Code

```bash
# Index a repository
cgc index /path/to/your/project

# Start watching for live changes
cgc watch /path/to/your/project
```

### Start the MCP Server

```bash
# Stdio mode (for IDE integration)
cgc mcp start

# HTTP mode (for remote agents via supergateway)
npx supergateway --stdio "cgc mcp start" --port 8790 --outputTransport streamableHttp
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

CodeGraph Agent includes a production-grade file watcher that keeps the graph in sync as code changes:

- **Incremental processing** — only re-parses changed files, only re-links affected edges
- **Error isolation** — a parse failure in one file doesn't kill the batch
- **Retry queue** — failed files are retried with exponential backoff
- **Circuit breaker** — prevents hammering Neo4j when it's temporarily down
- **Periodic reconciliation** — catches events missed by the OS event buffer (critical when multiple agents write simultaneously)
- **Health monitoring** — per-watcher health files with batch counts, errors, and staleness indicators
- **.gitignore-aware** — won't index `node_modules`, `.git`, build artifacts
- **File stability check** — waits for editors to finish writing before parsing
- **Graceful shutdown** — persists file state cache for fast restart
- **Adaptive debounce** — scales from 5s to 30s based on batch size

### Watcher Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `CGC_DEBOUNCE_SECONDS` | `5` | Debounce window for batching file changes |
| `CGC_RECONCILE_INTERVAL` | `300` | Seconds between reconciliation sweeps |
| `CGC_HEALTH_DIR` | `/tmp/cgc-watch` | Directory for health JSON files |
| `CGC_MAX_RETRIES` | `3` | Max retries for failed file processing |
| `CGC_CIRCUIT_BREAKER_THRESHOLD` | `5` | Neo4j failures before circuit opens |
| `CGC_AUTO_WATCH_PATHS` | (empty) | Colon-separated paths to auto-watch on MCP start |

---

## Database Options

- **Neo4j** (recommended) — full Cypher support, APOC procedures, browser visualisation
- **FalkorDB** — lightweight alternative
- **Kuzu** — embedded, zero-config

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
- 8 agent-focused MCP tools (get_function_context, grep_code, get_module_overview, etc.)
- Production-grade file watcher (circuit breaker, retry queue, incremental processing, reconciliation)
- Agent-oriented system prompt and tool routing
- Comprehensive test suite (190+ tests)

---

## License

MIT License — see [LICENSE](LICENSE) for details.
