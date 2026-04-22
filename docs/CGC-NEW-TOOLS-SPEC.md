# CGC New Tools Spec — Agent Productivity Suite

**Goal:** Add 6 new MCP tools to CodeGraphContext that eliminate the most common reasons agents shell out to external tools or make multiple round-trips. Each tool is designed to answer a specific question an agent asks repeatedly during code exploration.

**Repo:** `/Users/felixcardix/dev-workspaces/cgc-fork/` (fork, branch `feat/watcher-overhaul`)
**Target:** New feature branch `feat/agent-tools` off `feat/watcher-overhaul`

---

## The Problem

Agents using CGC currently hit three friction points:

1. **"CGC found the function but I can't read the file"** — `find_name_substring` returns symbol names/locations, but agents need a separate filesystem tool to read actual source. Round-trip waste.

2. **"I need to search for a string, not a symbol"** — `find_name_substring` matches symbol names. Agents searching for error messages, API paths, config keys, or string literals have to shell out to `grep`/`rg`.

3. **"I need the full picture of this function"** — Understanding a function requires 4+ tool calls: find it, read its source, find callers, find callees, check its class. This burns tokens and time.

---

## Tool 1: `grep_code`

**Question it answers:** "Where does this string/pattern appear in the codebase?"

### Interface

```json
{
  "name": "grep_code",
  "description": "Search for a text pattern or regex across all indexed repositories. Returns matching lines with file paths, line numbers, and surrounding context. Use this for searching string literals, error messages, API endpoints, config keys, TODO comments, or any text that isn't a symbol name. For symbol name searches, prefer find_name_substring instead.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "pattern": {
        "type": "string",
        "description": "Text pattern or regex to search for."
      },
      "is_regex": {
        "type": "boolean",
        "description": "If true, treat pattern as a regex. Default false (literal string match).",
        "default": false
      },
      "file_pattern": {
        "type": "string",
        "description": "Optional glob to filter files (e.g. '*.py', '*.ts', 'test_*.py')."
      },
      "repo_path": {
        "type": "string",
        "description": "Optional: restrict search to a specific indexed repository path."
      },
      "context_lines": {
        "type": "integer",
        "description": "Number of context lines before and after each match. Default 2.",
        "default": 2
      },
      "max_results": {
        "type": "integer",
        "description": "Maximum number of matches to return. Default 50.",
        "default": 50
      },
      "case_sensitive": {
        "type": "boolean",
        "description": "Case-sensitive matching. Default true.",
        "default": true
      }
    },
    "required": ["pattern"]
  }
}
```

### Implementation

```python
import subprocess
import shutil

def grep_code(pattern: str, is_regex: bool = False, file_pattern: str = None,
              repo_path: str = None, context_lines: int = 2, 
              max_results: int = 50, case_sensitive: bool = True) -> dict:
    """
    Search indexed repos using ripgrep (rg) or fallback to Python re.
    """
    # Determine search paths from indexed repos
    if repo_path:
        search_paths = [repo_path]
    else:
        # Query Neo4j for all indexed repo paths
        search_paths = _get_all_repo_paths()
    
    # Prefer ripgrep if available (10-100x faster than Python)
    rg_path = shutil.which("rg")
    if rg_path:
        return _grep_with_ripgrep(rg_path, pattern, search_paths, is_regex,
                                   file_pattern, context_lines, max_results, 
                                   case_sensitive)
    else:
        return _grep_with_python(pattern, search_paths, is_regex,
                                  file_pattern, context_lines, max_results,
                                  case_sensitive)


def _grep_with_ripgrep(rg_path, pattern, search_paths, is_regex,
                        file_pattern, context_lines, max_results, case_sensitive):
    """Shell out to ripgrep for speed."""
    cmd = [rg_path, "--json", f"-C{context_lines}", f"--max-count={max_results}"]
    
    if not is_regex:
        cmd.append("--fixed-strings")
    if not case_sensitive:
        cmd.append("--ignore-case")
    if file_pattern:
        cmd.extend(["--glob", file_pattern])
    
    # Exclude common noise
    cmd.extend([
        "--glob", "!node_modules",
        "--glob", "!.git",
        "--glob", "!__pycache__",
        "--glob", "!*.pyc",
        "--glob", "!.venv",
        "--glob", "!dist",
        "--glob", "!build",
    ])
    
    cmd.append(pattern)
    cmd.extend(search_paths)
    
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    
    # Parse ripgrep JSON output into structured results
    matches = _parse_rg_json(result.stdout, max_results)
    
    return {
        "success": True,
        "pattern": pattern,
        "total_matches": len(matches),
        "matches": matches,
        "truncated": len(matches) >= max_results,
    }


def _grep_with_python(pattern, search_paths, is_regex, file_pattern,
                       context_lines, max_results, case_sensitive):
    """Pure Python fallback using re module."""
    import re
    import fnmatch
    
    flags = 0 if case_sensitive else re.IGNORECASE
    if is_regex:
        compiled = re.compile(pattern, flags)
    else:
        compiled = re.compile(re.escape(pattern), flags)
    
    matches = []
    for repo_path in search_paths:
        for root, dirs, files in os.walk(repo_path):
            # Skip ignored directories
            dirs[:] = [d for d in dirs if d not in {
                'node_modules', '.git', '__pycache__', '.venv', 
                'venv', 'dist', 'build', '.next'
            }]
            
            for fname in files:
                if file_pattern and not fnmatch.fnmatch(fname, file_pattern):
                    continue
                    
                fpath = os.path.join(root, fname)
                try:
                    with open(fpath, 'r', errors='replace') as f:
                        lines = f.readlines()
                except (OSError, UnicodeDecodeError):
                    continue
                
                for i, line in enumerate(lines):
                    if compiled.search(line):
                        start = max(0, i - context_lines)
                        end = min(len(lines), i + context_lines + 1)
                        matches.append({
                            "file": fpath,
                            "line_number": i + 1,
                            "match_line": line.rstrip(),
                            "context": [l.rstrip() for l in lines[start:end]],
                            "context_start_line": start + 1,
                        })
                        if len(matches) >= max_results:
                            return {
                                "success": True,
                                "pattern": pattern,
                                "total_matches": len(matches),
                                "matches": matches,
                                "truncated": True,
                            }
    
    return {
        "success": True,
        "pattern": pattern,
        "total_matches": len(matches),
        "matches": matches,
        "truncated": False,
    }
```

### Example Usage

```
Agent: grep_code(pattern="/api/v1/auth/refresh", file_pattern="*.py")
→ Returns: 3 matches in endpoints.py, auth-di-copilot.ts, test_auth.py with context

Agent: grep_code(pattern="TODO|FIXME|HACK", is_regex=true)
→ Returns: All TODO comments across the codebase

Agent: grep_code(pattern="store_extraction", repo_path="/Users/.../backend")
→ Returns: Every file referencing store_extraction, with surrounding lines
```

---

## Tool 2: `get_file_content`

**Question it answers:** "Show me the source code of this file (or a range of lines)."

### Interface

```json
{
  "name": "get_file_content",
  "description": "Read the content of a source file from an indexed repository. Returns the raw file content with line numbers. Supports reading full files or specific line ranges. Only reads files within indexed repositories for security. Use this after find_name_substring or grep_code to read the actual source.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "path": {
        "type": "string",
        "description": "Absolute path to the file, or path relative to a repo root."
      },
      "start_line": {
        "type": "integer",
        "description": "Optional: first line to return (1-indexed). Omit for start of file."
      },
      "end_line": {
        "type": "integer",
        "description": "Optional: last line to return (1-indexed). Omit for end of file."
      },
      "max_lines": {
        "type": "integer",
        "description": "Maximum lines to return. Default 500. Use start_line/end_line for larger files.",
        "default": 500
      }
    },
    "required": ["path"]
  }
}
```

### Implementation

```python
def get_file_content(path: str, start_line: int = None, end_line: int = None,
                     max_lines: int = 500) -> dict:
    """
    Read file content from an indexed repository.
    Security: only serves files within indexed repo paths.
    """
    resolved = str(Path(path).resolve())
    
    # Security check: file must be within an indexed repository
    repo_paths = _get_all_repo_paths()
    if not any(resolved.startswith(rp) for rp in repo_paths):
        return {
            "success": False,
            "error": f"File is not within any indexed repository. "
                     f"Indexed repos: {repo_paths}"
        }
    
    if not Path(resolved).exists():
        return {"success": False, "error": f"File not found: {resolved}"}
    
    if not Path(resolved).is_file():
        return {"success": False, "error": f"Not a file: {resolved}"}
    
    try:
        with open(resolved, 'r', errors='replace') as f:
            all_lines = f.readlines()
    except OSError as e:
        return {"success": False, "error": str(e)}
    
    total_lines = len(all_lines)
    
    # Apply line range
    start = (start_line - 1) if start_line else 0
    end = end_line if end_line else total_lines
    start = max(0, start)
    end = min(total_lines, end)
    
    # Apply max_lines cap
    if (end - start) > max_lines:
        end = start + max_lines
        truncated = True
    else:
        truncated = False
    
    selected = all_lines[start:end]
    
    # Format with line numbers
    numbered_content = ""
    for i, line in enumerate(selected, start=start + 1):
        numbered_content += f"{i:4d} | {line}"
    
    return {
        "success": True,
        "path": resolved,
        "total_lines": total_lines,
        "start_line": start + 1,
        "end_line": start + len(selected),
        "content": numbered_content,
        "truncated": truncated,
        "language": _detect_language(resolved),
    }
```

### Example Usage

```
Agent: get_file_content(path="/Users/.../backend/app/modules/auth/security.py")
→ Returns: Full file with line numbers, language: "python"

Agent: get_file_content(path=".../extractor.py", start_line=968, end_line=1068)
→ Returns: Just the extraction prompt section

Agent: get_file_content(path=".../ChatPanel.tsx")
→ Returns: First 500 lines (truncated=true), agent can request more with start_line
```

---

## Tool 3: `get_function_context`

**Question it answers:** "Tell me everything about this function in one call."

### Interface

```json
{
  "name": "get_function_context",
  "description": "Returns comprehensive context for a function: its source code, class membership, callers, callees, imports it uses, and the file it belongs to. Combines find_name_substring + analyze_code_relationships into a single call. Use this when you need to understand a function before modifying it.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "function_name": {
        "type": "string",
        "description": "Name of the function to analyse."
      },
      "file_path": {
        "type": "string",
        "description": "Optional: specific file path to disambiguate functions with the same name."
      },
      "repo_path": {
        "type": "string",
        "description": "Optional: restrict to a specific repository."
      },
      "include_source": {
        "type": "boolean",
        "description": "Include full source code of the function. Default true.",
        "default": true
      },
      "caller_depth": {
        "type": "integer",
        "description": "How many levels of callers to include. Default 1 (direct callers only). Use 2+ for transitive callers.",
        "default": 1
      },
      "callee_depth": {
        "type": "integer",
        "description": "How many levels of callees to include. Default 1.",
        "default": 1
      }
    },
    "required": ["function_name"]
  }
}
```

### Implementation

```python
def get_function_context(function_name: str, file_path: str = None,
                          repo_path: str = None, include_source: bool = True,
                          caller_depth: int = 1, callee_depth: int = 1) -> dict:
    """
    Single-call comprehensive function analysis.
    Combines graph queries to build a complete picture.
    """
    with driver.session() as session:
        # 1. Find the function
        match_clause = "MATCH (f:File)-[:CONTAINS]->(fn:Function {name: $name})"
        params = {"name": function_name}
        
        if file_path:
            match_clause += " WHERE f.path = $file_path"
            params["file_path"] = str(Path(file_path).resolve())
        elif repo_path:
            match_clause += " WHERE f.path STARTS WITH $repo_path"
            params["repo_path"] = str(Path(repo_path).resolve())
        
        result = session.run(f"""
            {match_clause}
            RETURN fn.name AS name, fn.path AS path, fn.line_number AS line,
                   fn.end_line AS end_line, fn.source AS source,
                   fn.cyclomatic_complexity AS complexity,
                   fn.decorators AS decorators, fn.args AS args,
                   fn.context AS class_name, f.path AS file_path
        """, **params)
        
        records = list(result)
        if not records:
            return {"success": False, "error": f"Function '{function_name}' not found"}
        
        # If multiple matches, return all with a note
        functions = []
        for rec in records:
            func_info = {
                "name": rec["name"],
                "file": rec["file_path"],
                "line": rec["line"],
                "end_line": rec["end_line"],
                "complexity": rec["complexity"],
                "class": rec["class_name"],
                "decorators": rec["decorators"],
                "args": rec["args"],
            }
            
            if include_source and rec["source"]:
                func_info["source"] = rec["source"]
            elif include_source:
                # Source not in graph — read from filesystem
                func_info["source"] = _read_lines(
                    rec["file_path"], rec["line"], rec["end_line"]
                )
            
            # 2. Find callers (who calls this function)
            callers = _find_callers_recursive(
                session, function_name, rec["file_path"], caller_depth
            )
            func_info["callers"] = callers
            
            # 3. Find callees (what this function calls)
            callees = _find_callees_recursive(
                session, function_name, rec["file_path"], callee_depth
            )
            func_info["callees"] = callees
            
            # 4. Find imports used in the file
            imports_result = session.run("""
                MATCH (f:File {path: $path})-[:IMPORTS]->(m:Module)
                RETURN m.name AS module_name
            """, path=rec["file_path"])
            func_info["file_imports"] = [r["module_name"] for r in imports_result]
            
            # 5. Get class info if method
            if rec["class_name"]:
                class_result = session.run("""
                    MATCH (f:File {path: $path})-[:CONTAINS]->(c:Class {name: $class_name})
                    OPTIONAL MATCH (c)-[:INHERITS]->(parent:Class)
                    RETURN c.name AS name, c.line_number AS line,
                           collect(parent.name) AS bases
                """, path=rec["file_path"], class_name=rec["class_name"])
                class_rec = class_result.single()
                if class_rec:
                    func_info["class_info"] = {
                        "name": class_rec["name"],
                        "line": class_rec["line"],
                        "bases": class_rec["bases"],
                    }
            
            functions.append(func_info)
        
        return {
            "success": True,
            "function_name": function_name,
            "matches": len(functions),
            "functions": functions,
            "note": "Multiple matches found — use file_path to disambiguate" if len(functions) > 1 else None,
        }
```

### Example Usage

```
Agent: get_function_context(function_name="store_extraction")
→ Returns:
  - source: full 50-line function
  - class: None (module-level)  
  - callers: ["extract_and_store_document_chunk", "_extract_email_knowledge"]
  - callees: ["_merge_entities_batch", "store_fact", "embed_batch"]
  - file_imports: ["structlog", "sqlalchemy", "neo4j"]
  - complexity: 12

One call instead of: find_name_substring + get_file_content + find_callers + find_callees
```

---

## Tool 4: `get_file_structure`

**Question it answers:** "What's the project layout? What files are in this directory?"

### Interface

```json
{
  "name": "get_file_structure",
  "description": "Returns the directory tree structure of an indexed repository. Shows files and directories with optional filtering by extension. Use this to understand project layout before diving into specific files.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "repo_path": {
        "type": "string",
        "description": "Path to the indexed repository."
      },
      "directory": {
        "type": "string",
        "description": "Optional: subdirectory to scope the tree to (e.g. 'app/modules/auth')."
      },
      "extensions": {
        "type": "array",
        "items": {"type": "string"},
        "description": "Optional: filter by file extensions (e.g. ['.py', '.ts']). Default: all indexed extensions."
      },
      "max_depth": {
        "type": "integer",
        "description": "Maximum directory depth to show. Default 4.",
        "default": 4
      },
      "include_counts": {
        "type": "boolean",
        "description": "Include function/class counts per file. Default false.",
        "default": false
      }
    },
    "required": ["repo_path"]
  }
}
```

### Implementation

Uses the existing File and Directory nodes in the graph — no filesystem access needed.

```python
def get_file_structure(repo_path: str, directory: str = None,
                        extensions: list = None, max_depth: int = 4,
                        include_counts: bool = False) -> dict:
    """Build a tree view from graph Directory and File nodes."""
    resolved = str(Path(repo_path).resolve())
    scope = resolved
    if directory:
        scope = str(Path(resolved) / directory)
    
    with driver.session() as session:
        if include_counts:
            query = """
                MATCH (f:File)
                WHERE f.path STARTS WITH $scope
                OPTIONAL MATCH (f)-[:CONTAINS]->(fn:Function)
                OPTIONAL MATCH (f)-[:CONTAINS]->(c:Class)
                RETURN f.path AS path, f.relative_path AS rel_path,
                       count(DISTINCT fn) AS functions, count(DISTINCT c) AS classes
                ORDER BY f.path
            """
        else:
            query = """
                MATCH (f:File)
                WHERE f.path STARTS WITH $scope
                RETURN f.path AS path, f.relative_path AS rel_path
                ORDER BY f.path
            """
        
        result = session.run(query, scope=scope)
        files = list(result)
    
    # Filter by extension
    if extensions:
        files = [f for f in files if any(f["path"].endswith(ext) for ext in extensions)]
    
    # Build tree structure
    tree = _build_tree(files, scope, max_depth, include_counts)
    
    return {
        "success": True,
        "repo": repo_path,
        "scope": scope,
        "total_files": len(files),
        "tree": tree,  # formatted string like `tree` command output
    }
```

### Example Output

```
backend/ (452 files)
├── app/
│   ├── core/
│   │   ├── config.py (3 fn, 1 cls)
│   │   ├── exceptions.py (0 fn, 8 cls)
│   │   └── database.py (5 fn, 1 cls)
│   ├── modules/
│   │   ├── auth/
│   │   │   ├── endpoints.py (12 fn)
│   │   │   ├── security.py (5 fn)
│   │   │   └── service.py (8 fn, 1 cls)
│   │   ├── knowledge/
│   │   │   ├── services/
│   │   │   │   ├── extractor.py (24 fn, 3 cls)
│   │   │   │   ├── retrieval_service.py (15 fn, 1 cls)
│   │   │   │   └── ...
```

---

## Tool 5: `find_references`

**Question it answers:** "Where is this symbol used — not just called, but referenced anywhere?"

### Interface

```json
{
  "name": "find_references",
  "description": "Find all references to a symbol across the codebase. Broader than find_callers — includes imports, type annotations, assignments, function arguments, decorator usage, and string references. Combines graph queries with grep for comprehensive results.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "symbol": {
        "type": "string",
        "description": "Symbol name to find references for (function, class, variable, type, constant)."
      },
      "repo_path": {
        "type": "string",
        "description": "Optional: restrict to a specific repository."
      },
      "include_definitions": {
        "type": "boolean",
        "description": "Include the definition site itself. Default false.",
        "default": false
      }
    },
    "required": ["symbol"]
  }
}
```

### Implementation

Combines graph queries (CALLS, IMPORTS, INHERITS, CONTAINS) with a targeted `grep_code` call to catch references the graph doesn't track (type annotations, string references, dict keys).

```python
def find_references(symbol: str, repo_path: str = None,
                     include_definitions: bool = False) -> dict:
    """Comprehensive reference finder — graph + text search."""
    refs = {
        "definitions": [],
        "callers": [],
        "importers": [],
        "inheritors": [],
        "text_references": [],
    }
    
    with driver.session() as session:
        # 1. Definitions (where it's defined)
        if include_definitions:
            defs = session.run("""
                MATCH (f:File)-[:CONTAINS]->(n)
                WHERE n.name = $name
                RETURN f.path AS file, n.name AS name, 
                       labels(n)[0] AS type, n.line_number AS line
            """, name=symbol)
            refs["definitions"] = [dict(r) for r in defs]
        
        # 2. Callers (functions that call it)
        callers = session.run("""
            MATCH (caller:Function)-[:CALLS]->(callee:Function {name: $name})
            MATCH (f:File)-[:CONTAINS]->(caller)
            RETURN f.path AS file, caller.name AS caller_name, 
                   caller.line_number AS line
        """, name=symbol)
        refs["callers"] = [dict(r) for r in callers]
        
        # 3. Importers (files that import it)
        importers = session.run("""
            MATCH (f:File)-[:IMPORTS]->(m:Module)
            WHERE m.name = $name OR m.name ENDS WITH '.' + $name
            RETURN f.path AS file, m.name AS import_path
        """, name=symbol)
        refs["importers"] = [dict(r) for r in importers]
        
        # 4. Inheritors (classes that extend it)
        inheritors = session.run("""
            MATCH (child:Class)-[:INHERITS]->(parent:Class {name: $name})
            MATCH (f:File)-[:CONTAINS]->(child)
            RETURN f.path AS file, child.name AS class_name, 
                   child.line_number AS line
        """, name=symbol)
        refs["inheritors"] = [dict(r) for r in inheritors]
    
    # 5. Text references (catch what graph misses: type hints, string refs, etc.)
    # Use word-boundary regex to avoid partial matches
    grep_result = grep_code(
        pattern=f"\\b{re.escape(symbol)}\\b",
        is_regex=True,
        repo_path=repo_path,
        context_lines=0,
        max_results=100,
    )
    
    # Deduplicate against graph results
    known_locations = set()
    for ref_list in [refs["definitions"], refs["callers"], refs["importers"], refs["inheritors"]]:
        for r in ref_list:
            known_locations.add((r.get("file", ""), r.get("line", 0)))
    
    for match in grep_result.get("matches", []):
        loc = (match["file"], match["line_number"])
        if loc not in known_locations:
            refs["text_references"].append({
                "file": match["file"],
                "line": match["line_number"],
                "content": match["match_line"],
            })
    
    total = sum(len(v) for v in refs.values())
    
    return {
        "success": True,
        "symbol": symbol,
        "total_references": total,
        "references": refs,
    }
```

---

## Tool 6: `diff_since`

**Question it answers:** "What changed recently? What files were modified since I last looked?"

### Interface

```json
{
  "name": "diff_since",
  "description": "Show files changed within a time window or since a specific commit. Uses git history for indexed repositories. Useful for picking up where another agent left off, reviewing recent work, or understanding what changed overnight.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "repo_path": {
        "type": "string",
        "description": "Path to the indexed repository."
      },
      "since": {
        "type": "string",
        "description": "Time reference: '1h' (1 hour), '4h', '1d' (1 day), '3d', or a git commit SHA, or ISO date '2026-03-28'."
      },
      "extensions": {
        "type": "array",
        "items": {"type": "string"},
        "description": "Optional: filter by file extensions (e.g. ['.py', '.ts'])."
      },
      "include_diff": {
        "type": "boolean",
        "description": "Include actual diff content (git diff). Default false — only shows file list.",
        "default": false
      },
      "include_stats": {
        "type": "boolean",
        "description": "Include line count changes per file. Default true.",
        "default": true
      }
    },
    "required": ["repo_path", "since"]
  }
}
```

### Implementation

```python
def diff_since(repo_path: str, since: str, extensions: list = None,
                include_diff: bool = False, include_stats: bool = True) -> dict:
    """Show changes since a time/commit reference using git."""
    resolved = str(Path(repo_path).resolve())
    
    # Parse 'since' into git-compatible format
    git_since = _parse_since(since)  # '1h' → '--since=1.hour.ago', SHA → SHA, ISO → '--since=2026-03-28'
    
    # Get changed files
    if git_since.startswith("--since"):
        cmd = ["git", "-C", resolved, "log", git_since, "--name-status", 
               "--pretty=format:", "--diff-filter=ACDMR"]
    else:
        # Treat as commit SHA
        cmd = ["git", "-C", resolved, "diff", "--name-status", git_since, "HEAD"]
    
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    
    changes = _parse_name_status(result.stdout, resolved)
    
    # Filter by extension
    if extensions:
        changes = [c for c in changes if any(c["file"].endswith(ext) for ext in extensions)]
    
    # Add stats if requested
    if include_stats:
        for change in changes:
            stat_cmd = ["git", "-C", resolved, "diff", "--stat", 
                       f"HEAD~1", "--", change["file"]]
            stat_result = subprocess.run(stat_cmd, capture_output=True, text=True, timeout=5)
            # Parse "+X, -Y" from git stat output
            change["stats"] = _parse_stat_line(stat_result.stdout)
    
    # Add full diff if requested
    if include_diff:
        for change in changes:
            diff_cmd = ["git", "-C", resolved, "diff", "HEAD~1", "--", change["file"]]
            diff_result = subprocess.run(diff_cmd, capture_output=True, text=True, timeout=10)
            change["diff"] = diff_result.stdout[:5000]  # cap at 5KB per file
    
    return {
        "success": True,
        "repo": repo_path,
        "since": since,
        "total_changes": len(changes),
        "changes": changes,
    }
```

### Example Usage

```
Agent: diff_since(repo_path="/Users/.../backend", since="4h", extensions=[".py"])
→ Returns:
  - extractor.py: Modified (+45, -12)
  - neo4j_store.py: Modified (+120, -80)  
  - entity_search_service.py: Added (+95)

Agent: diff_since(repo_path="/Users/.../backend", since="abc123f", include_diff=true)
→ Returns: Full git diff since commit abc123f
```

---

## Files to Create/Modify

| File | Change |
|------|--------|
| `src/codegraphcontext/tools/handlers/search_handlers.py` | **NEW** — `grep_code`, `get_file_content`, `find_references` |
| `src/codegraphcontext/tools/handlers/context_handlers.py` | **NEW** — `get_function_context`, `get_file_structure`, `diff_since` |
| `src/codegraphcontext/tool_definitions.py` | Add 6 new tool definitions |
| `src/codegraphcontext/server.py` | Wire 6 new tool handlers into routing |
| `tests/unit/tools/test_search_handlers.py` | **NEW** — tests for grep, file content, references |
| `tests/unit/tools/test_context_handlers.py` | **NEW** — tests for function context, file structure, diff |

## Dependencies

- `ripgrep` (`rg`) — optional but strongly recommended (10-100x faster grep). Falls back to Python `re` if not available.
- `git` — required for `diff_since`. Already available on all dev machines.
- No new Python package dependencies.

## Implementation Order

| # | Tool | Est. Time | Dependencies |
|---|------|-----------|--------------|
| 1 | `grep_code` | 1 hr | None |
| 2 | `get_file_content` | 30 min | None |
| 3 | `get_function_context` | 1.5 hr | None (uses existing graph queries) |
| 4 | `get_file_structure` | 45 min | None (uses existing graph nodes) |
| 5 | `find_references` | 1 hr | `grep_code` (uses it internally) |
| 6 | `diff_since` | 1 hr | `git` |

**Total: ~6 hours of coding agent time**

## Security Considerations

- `get_file_content` MUST verify the file is within an indexed repository before reading. No arbitrary file access.
- `grep_code` MUST scope searches to indexed repositories only. No searching `/etc/`, `~/.ssh/`, etc.
- `diff_since` MUST only run `git` commands within indexed repository paths.
- All subprocess calls MUST use `timeout` to prevent hanging.
- All file reads MUST use `errors='replace'` to handle binary files gracefully.

---

*Spec for 6 new CGC MCP tools to improve agent productivity.*
*To be implemented on branch `feat/agent-tools` off `feat/watcher-overhaul`.*
