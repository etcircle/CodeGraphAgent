# CGC New Tools Spec v2 — Agent Productivity Suite

**Goal:** Add 9 MCP tools that eliminate the most common reasons agents shell out to external tools or make multiple round-trips.

**Repo:** `/Users/felixcardix/dev-workspaces/cgc-fork/`
**Branch:** `feat/agent-tools` (off `feat/watcher-overhaul`)

---

## Tools Summary

| # | Tool | Question It Answers | Priority |
|---|------|-------------------|----------|
| 1 | `get_function_context` | "Tell me everything about this function" | Highest |
| 2 | `grep_code` | "Where does this string appear?" | High |
| 3 | `get_file_content` | "Show me the source code" | High |
| 4 | `get_module_overview` | "What does this module do?" | High |
| 5 | `find_references` | "Where is this symbol used?" | Medium-High |
| 6 | `diff_since` | "What changed recently?" | Medium |
| 7 | `explain_path` | "How does A call B?" | Medium |
| 8 | `get_file_structure` | "What's the project layout?" | Low |

Note: `get_watcher_health` (Tool 3 from review) already exists in the fork — wired in tool_definitions.py, server.py, watcher_handlers.py. No work needed.

---

## Design Principles

1. **One call = one complete answer.** Every tool should answer a full question without follow-up calls.
2. **Filesystem is truth for source code.** Never serve function source from graph properties — always read from disk.
3. **Security: scoped to indexed repos only.** No tool may read files or run commands outside indexed repository paths.
4. **Tool descriptions guide selection.** Include "Use X instead of Y" in descriptions to reduce agent misrouting.

---

## Tool 1: `get_function_context`

**One call replaces 4-5 calls** (find_name_substring + read_file + find_callers + find_callees + class info).

```json
{
  "name": "get_function_context",
  "description": "Returns comprehensive context for a function: source code (from filesystem), class membership, callers, callees, imports, and sibling methods. Use this when you need to understand a function before modifying it. Use find_name_substring for simple name lookups. Use grep_code for text/pattern searches.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "function_name": {"type": "string", "description": "Name of the function to analyse."},
      "file_path": {"type": "string", "description": "Optional: file path to disambiguate same-named functions."},
      "repo_path": {"type": "string", "description": "Optional: restrict to a specific repository."},
      "include_source": {"type": "boolean", "description": "Include full source code (always from filesystem). Default true.", "default": true},
      "caller_depth": {"type": "integer", "description": "Levels of callers. Default 1.", "default": 1},
      "callee_depth": {"type": "integer", "description": "Levels of callees. Default 1.", "default": 1},
      "include_sibling_methods": {"type": "boolean", "description": "If method, include other methods on the same class (names + signatures only). Default true.", "default": true}
    },
    "required": ["function_name"]
  }
}
```

### Implementation Notes

- **Source: ALWAYS read from filesystem** using `fn.path` + `fn.line_number` + `fn.end_line`. Never use graph `fn.source` property (can be stale from duplicate history).
- **Sibling methods:** When `fn.context` (class name) is set, query `MATCH (f:File {path})-[:CONTAINS]->(sib:Function) WHERE sib.context = $class_name` and return `name + args` for each.
- **Callers/callees:** Use recursive helper with depth parameter, returning `{name, file, line}` per hop.

---

## Tool 2: `grep_code`

**Eliminates cross-MCP shell-out** for every string search.

```json
{
  "name": "grep_code",
  "description": "Search for a text pattern or regex across indexed repositories. Returns matching lines with file paths, line numbers, and context. Use this for: string literals, error messages, API paths, config keys, TODO/FIXME comments. Use find_name_substring for symbol name lookups. Use find_references for comprehensive 'who uses this symbol' queries.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "pattern": {"type": "string", "description": "Text or regex pattern to search for."},
      "is_regex": {"type": "boolean", "default": false},
      "file_pattern": {"type": "string", "description": "Glob filter (e.g. '*.py', 'test_*.py')."},
      "exclude_pattern": {"type": "string", "description": "Glob to exclude (e.g. 'test_*', '*.migration.py')."},
      "repo_path": {"type": "string", "description": "Optional: restrict to one repository."},
      "context_lines": {"type": "integer", "default": 2},
      "max_results": {"type": "integer", "description": "Max total matches (not per-file). Default 50.", "default": 50},
      "case_sensitive": {"type": "boolean", "default": true}
    },
    "required": ["pattern"]
  }
}
```

### Implementation Notes

- **Use ripgrep (`rg`) if available**, fallback to Python `re`.
- **Respect .gitignore by default** — reuse the `pathspec` logic from the watcher overhaul. Load `.gitignore` + `IGNORE_DIRS` config and apply.
- **Fix max_results semantics:** `rg --max-count` is per-file. Instead, stream rg JSON output and count matches Python-side, killing the subprocess when `max_results` is reached.
- **Include per-match metadata:** `{file, line_number, match_line, context[], language}`.
- **exclude_pattern** applied as `--glob '!{pattern}'` for rg, or `fnmatch` for Python fallback.

---

## Tool 3: `get_file_content`

**Eliminates cross-MCP Desktop Commander calls** for file reads.

```json
{
  "name": "get_file_content",
  "description": "Read source code from a file in an indexed repository. Returns content with line numbers. Security-scoped: only reads files within indexed repos. Use after find_name_substring or grep_code to read actual source.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "path": {"type": "string", "description": "Absolute path to the file."},
      "start_line": {"type": "integer", "description": "First line (1-indexed). Omit for file start."},
      "end_line": {"type": "integer", "description": "Last line (1-indexed). Omit for file end."},
      "around_line": {"type": "integer", "description": "Center the view on this line number. Combine with context_lines."},
      "context_lines": {"type": "integer", "description": "Lines before/after around_line. Default 20.", "default": 20},
      "max_lines": {"type": "integer", "default": 500}
    },
    "required": ["path"]
  }
}
```

### Implementation Notes

- **Security:** Verify file path is within an indexed repo before reading. Cache repo paths (refresh every 60s) to avoid Neo4j query per call.
- **`around_line`:** Converts to `start_line = around_line - context_lines`, `end_line = around_line + context_lines`. Takes precedence over explicit start/end.
- **Language detection** from file extension for the `language` field in response.
- **Use `errors='replace'`** for binary file safety.

---

## Tool 4: `get_module_overview`

**Replaces 3-5 Cypher queries** at session start.

```json
{
  "name": "get_module_overview",
  "description": "Returns a structured summary of a code module: endpoints (with HTTP methods), service classes and methods, models, schemas, and key functions. Use this to understand what a module does before diving into files. Works best with Python backend modules but supports TypeScript too.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "module_path": {"type": "string", "description": "Path to the module directory (absolute or relative to repo root)."},
      "repo_path": {"type": "string", "description": "Optional: repository root for resolving relative paths."}
    },
    "required": ["module_path"]
  }
}
```

### Implementation Notes

Graph-driven queries:

- **Endpoints:** Functions with `@router.*` or `@app.*` decorators. Parse HTTP method + path from decorator string.
- **Services:** Classes in `**/services/**` files. Include method names via `fn.context = class.name`.
- **Models:** Classes in `**/models/**` files.
- **Schemas:** Classes in `**/schemas/**` files.
- **Key functions:** Module-level functions (no class context) sorted by complexity.
- **Summary stats:** Total files, functions, classes per subdirectory.

```python
# Endpoint detection via decorators
endpoints = session.run("""
    MATCH (f:File)-[:CONTAINS]->(fn:Function)
    WHERE f.path STARTS WITH $scope
    AND any(d IN fn.decorators WHERE 
        d CONTAINS 'router.' OR d CONTAINS 'app.' OR
        d CONTAINS '.get' OR d CONTAINS '.post' OR 
        d CONTAINS '.put' OR d CONTAINS '.delete')
    RETURN fn.name AS name, fn.decorators AS decorators,
           fn.line_number AS line, f.path AS file, fn.args AS args
    ORDER BY f.path, fn.line_number
""", scope=resolved)
```

---

## Tool 5: `find_references`

**IDE-style "Find All Usages"** — graph + grep hybrid.

```json
{
  "name": "find_references",
  "description": "Find all references to a symbol: callers, importers, inheritors, type annotations, and text mentions. Broader than find_callers. Use this for comprehensive 'who uses this?' queries. Use find_callers for just the call graph.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "symbol": {"type": "string", "description": "Symbol name (function, class, variable, type)."},
      "repo_path": {"type": "string", "description": "Optional: restrict to one repository."},
      "include_definitions": {"type": "boolean", "default": false}
    },
    "required": ["symbol"]
  }
}
```

### Implementation Notes

Combines 4 graph queries + 1 grep call:

1. **Definitions** (if requested): `MATCH (f:File)-[:CONTAINS]->(n {name: $name})`
2. **Callers:** `MATCH (caller)-[:CALLS]->(callee {name: $name})`
3. **Importers:** `MATCH (f:File)-[:IMPORTS]->(m:Module) WHERE m.name = $name OR m.name ENDS WITH '.' + $name`
4. **Inheritors:** `MATCH (child:Class)-[:INHERITS]->(parent {name: $name})`
5. **Text references** (via `grep_code`): Word-boundary regex `\bsymbol\b`, deduplicated against graph results. Categorise matches as:
   - `type_annotations` — matches like `: Symbol`, `-> Symbol`, `Optional[Symbol]`
   - `other` — remaining text references

Include `match_line` content with each reference so agents don't need a follow-up `get_file_content` call.

---

## Tool 6: `diff_since`

**Agent handoff and review tool.**

```json
{
  "name": "diff_since",
  "description": "Show files changed within a time window or since a commit. Uses git history. Useful for picking up another agent's work, reviewing recent changes, or morning standup context.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "repo_path": {"type": "string", "description": "Repository path."},
      "since": {"type": "string", "description": "Time ref: '1h', '4h', '1d', '3d', a commit SHA, or ISO date."},
      "extensions": {"type": "array", "items": {"type": "string"}, "description": "Filter by extensions."},
      "include_diff": {"type": "boolean", "description": "Include actual diff content. Default false.", "default": false},
      "include_stats": {"type": "boolean", "default": true},
      "include_uncommitted": {"type": "boolean", "description": "Include staged + unstaged changes. Default true.", "default": true}
    },
    "required": ["repo_path", "since"]
  }
}
```

### Implementation Notes

- **Single `git log` call for stats** — NOT per-file subprocess. Use `git log --stat --since=...` and parse combined output.
- **Include commit messages:** `{sha, message_firstline, author, relative_time}` per commit.
- **`include_uncommitted`:** Run `git diff --name-status` (unstaged) + `git diff --cached --name-status` (staged) and merge into results.
- **`since` parsing:** `'1h'` → `--since=1.hour.ago`, `'3d'` → `--since=3.days.ago`, SHA → direct ref, ISO date → `--since=date`.
- **All subprocess calls with `timeout=30`.**

---

## Tool 7: `explain_path`

**Call chain tracer** — uses Neo4j `shortestPath`.

```json
{
  "name": "explain_path",
  "description": "Find the shortest call chain between two functions. Shows how control flows from A to B through the call graph. Use for bug tracing, understanding data flow, and planning refactors.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "from_function": {"type": "string", "description": "Starting function name."},
      "to_function": {"type": "string", "description": "Target function name."},
      "repo_path": {"type": "string", "description": "Optional: restrict to one repository."},
      "max_depth": {"type": "integer", "default": 6}
    },
    "required": ["from_function", "to_function"]
  }
}
```

### Implementation Notes

```cypher
MATCH (start:Function {name: $from}), (end:Function {name: $to})
MATCH path = shortestPath((start)-[:CALLS*1..6]->(end))
RETURN [n IN nodes(path) | {name: n.name, path: n.path, line: n.line_number}] AS chain,
       length(path) AS hops
LIMIT 3
```

- Return up to 3 alternative paths.
- If `repo_path` is set, add `WHERE start.path STARTS WITH $repo AND end.path STARTS WITH $repo`.
- If no path found, try reverse direction and report.
- Include `file:line` for each hop so agents can jump directly.

---

## Tool 8: `get_file_structure`

**Project tree view** from graph nodes.

```json
{
  "name": "get_file_structure",
  "description": "Returns directory tree of an indexed repository with function/class counts per file. Use for understanding project layout. Use get_module_overview for deeper module analysis.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "repo_path": {"type": "string", "description": "Repository path."},
      "directory": {"type": "string", "description": "Optional: subdirectory scope."},
      "extensions": {"type": "array", "items": {"type": "string"}},
      "max_depth": {"type": "integer", "default": 4},
      "include_counts": {"type": "boolean", "description": "Function/class counts per file. Default true.", "default": true}
    },
    "required": ["repo_path"]
  }
}
```

### Implementation Notes

- Query File + Directory nodes from graph — no filesystem walk needed.
- **Include_counts default is true** (changed from spec v1).
- **Directory summary:** Show aggregate counts at each directory level.
- Format as tree output like the `tree` command.

---

## File Organisation

```
src/codegraphcontext/tools/handlers/
├── search_handlers.py      # grep_code, find_references
├── context_handlers.py     # get_function_context, get_module_overview, explain_path
├── file_handlers.py        # get_file_content, get_file_structure, diff_since
├── watcher_handlers.py     # get_watcher_health (existing)
├── indexing_handlers.py     # (existing)
├── analysis_handlers.py     # (existing)
├── management_handlers.py   # (existing)
└── query_handlers.py        # (existing)
```

## Files to Modify

| File | Change |
|------|--------|
| `tools/handlers/search_handlers.py` | **NEW** — grep_code, find_references |
| `tools/handlers/context_handlers.py` | **NEW** — get_function_context, get_module_overview, explain_path |
| `tools/handlers/file_handlers.py` | **NEW** — get_file_content, get_file_structure, diff_since |
| `tool_definitions.py` | Add 8 tool definitions |
| `server.py` | Import handlers, add 8 entries to tool routing map |
| `tests/unit/tools/test_search_handlers.py` | **NEW** |
| `tests/unit/tools/test_context_handlers.py` | **NEW** |
| `tests/unit/tools/test_file_handlers.py` | **NEW** |

## Implementation Order

| # | Tool | Est. Time | Dependencies |
|---|------|-----------|--------------|
| 1 | `grep_code` | 1 hr | None (other tools depend on it) |
| 2 | `get_file_content` | 30 min | None |
| 3 | `get_function_context` | 1.5 hr | `get_file_content` (for source reading) |
| 4 | `get_module_overview` | 1.5 hr | None |
| 5 | `find_references` | 1 hr | `grep_code` (uses internally) |
| 6 | `diff_since` | 1 hr | None |
| 7 | `explain_path` | 45 min | None |
| 8 | `get_file_structure` | 45 min | None |

**Total: ~8 hours of coding agent time**

## Security

- All tools MUST verify paths are within indexed repositories before file I/O.
- Cache repo paths with 60s TTL to avoid per-call Neo4j queries.
- All subprocess calls MUST use `timeout=30`.
- File reads MUST use `errors='replace'`.
- `grep_code` MUST respect .gitignore patterns.
- No tool may access files outside indexed repos.

## Testing

### Unit Tests
- grep_code: literal match, regex, case sensitivity, max_results cap, exclude patterns, .gitignore respect
- get_file_content: full file, line range, around_line, security rejection for non-repo paths
- get_function_context: single match, multiple matches, with/without source, sibling methods
- get_module_overview: endpoints detection, services grouping, empty module
- find_references: deduplication between graph and grep results
- diff_since: time parsing, uncommitted changes, extension filtering
- explain_path: direct path, no path found, multiple paths

### Integration Tests
- get_function_context("store_fact") → verify source matches filesystem
- grep_code("store_fact") paths → feed into get_file_content → verify consistency
- Modify file, wait for watcher, call get_function_context → verify updated source
