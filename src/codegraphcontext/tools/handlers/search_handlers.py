"""Search-oriented tool handlers: grep_code, find_references."""
import fnmatch
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import pathspec

from ...utils.debug_log import debug_log, error_logger
from ...core.watcher import IGNORE_DIRS


# ---------------------------------------------------------------------------
# Shared: cached repo paths for security scoping
# ---------------------------------------------------------------------------

_repo_paths_cache: Dict[str, Any] = {"paths": [], "ts": 0.0}
_CACHE_TTL = 60  # seconds


def _get_indexed_repo_paths(db_manager) -> List[str]:
    """Return list of indexed repo paths, cached for 60s."""
    now = time.time()
    if now - _repo_paths_cache["ts"] < _CACHE_TTL and _repo_paths_cache["paths"]:
        return _repo_paths_cache["paths"]
    try:
        with db_manager.get_driver().session() as session:
            result = session.run("MATCH (r:Repository) RETURN r.path AS path")
            paths = [r["path"] for r in result if r["path"]]
        _repo_paths_cache["paths"] = paths
        _repo_paths_cache["ts"] = now
        return paths
    except Exception as e:
        error_logger(f"Failed to fetch repo paths: {e}")
        return _repo_paths_cache["paths"]  # stale is better than empty


def _is_within_indexed_repo(file_path: str, repo_paths: List[str]) -> bool:
    """Check if a file path falls within any indexed repository."""
    resolved = str(Path(file_path).resolve())
    return any(resolved.startswith(rp) for rp in repo_paths)


# ---------------------------------------------------------------------------
# .gitignore loading (reuses watcher.py pattern)
# ---------------------------------------------------------------------------

def _load_gitignore_spec(repo_path: str) -> pathspec.PathSpec:
    """Load .gitignore + IGNORE_DIRS for a repo root."""
    patterns = list(IGNORE_DIRS)
    gitignore = Path(repo_path) / ".gitignore"
    if gitignore.is_file():
        try:
            patterns.extend(gitignore.read_text().splitlines())
        except OSError:
            pass
    return pathspec.PathSpec.from_lines("gitignore", patterns)


def _should_ignore_path(rel_path: str, spec: pathspec.PathSpec) -> bool:
    """Check if a relative path should be ignored."""
    if rel_path.endswith((".pyc", ".pyo")):
        return True
    parts = Path(rel_path).parts
    for part in parts:
        if part in IGNORE_DIRS:
            return True
    return spec.match_file(rel_path)


# ---------------------------------------------------------------------------
# Language detection
# ---------------------------------------------------------------------------

_EXT_TO_LANG = {
    ".py": "python", ".pyw": "python", ".pyi": "python",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript",
    ".ts": "typescript", ".tsx": "typescript",
    ".java": "java", ".kt": "kotlin",
    ".go": "go",
    ".rs": "rust",
    ".rb": "ruby",
    ".php": "php",
    ".c": "c", ".h": "c",
    ".cpp": "cpp", ".cxx": "cpp", ".cc": "cpp", ".hpp": "cpp",
    ".cs": "csharp",
    ".swift": "swift",
    ".ex": "elixir", ".exs": "elixir",
    ".sh": "shell", ".bash": "shell", ".zsh": "shell",
    ".sql": "sql",
    ".html": "html", ".htm": "html",
    ".css": "css", ".scss": "scss",
    ".json": "json", ".yaml": "yaml", ".yml": "yaml",
    ".xml": "xml",
    ".md": "markdown", ".rst": "rst",
    ".toml": "toml", ".ini": "ini", ".cfg": "ini",
}


def _detect_language(file_path: str) -> Optional[str]:
    ext = Path(file_path).suffix.lower()
    return _EXT_TO_LANG.get(ext)


# ---------------------------------------------------------------------------
# Tool 1: grep_code
# ---------------------------------------------------------------------------

def grep_code(db_manager, **args) -> Dict[str, Any]:
    """
    Search for a text pattern or regex across indexed repositories.
    Uses ripgrep if available, falls back to Python re.
    """
    pattern = args.get("pattern")
    if not pattern:
        return {"error": "pattern is required."}

    is_regex = args.get("is_regex", False)
    file_pattern = args.get("file_pattern")
    exclude_pattern = args.get("exclude_pattern")
    repo_path = args.get("repo_path")
    context_lines = args.get("context_lines", 2)
    max_results = args.get("max_results", 50)
    case_sensitive = args.get("case_sensitive", True)

    # Determine search scope
    repo_paths = _get_indexed_repo_paths(db_manager)
    if not repo_paths:
        return {"error": "No indexed repositories found."}

    if repo_path:
        resolved = str(Path(repo_path).resolve())
        if not any(resolved.startswith(rp) or rp.startswith(resolved) for rp in repo_paths):
            return {"error": f"Path '{repo_path}' is not within any indexed repository."}
        search_paths = [resolved]
    else:
        search_paths = repo_paths

    # Try ripgrep first
    rg_path = shutil.which("rg")
    if rg_path:
        return _grep_with_rg(
            rg_path, pattern, search_paths, is_regex, file_pattern,
            exclude_pattern, context_lines, max_results, case_sensitive
        )
    else:
        return _grep_with_python(
            pattern, search_paths, is_regex, file_pattern,
            exclude_pattern, context_lines, max_results, case_sensitive
        )


def _grep_with_rg(
    rg_path: str, pattern: str, search_paths: List[str],
    is_regex: bool, file_pattern: Optional[str],
    exclude_pattern: Optional[str], context_lines: int,
    max_results: int, case_sensitive: bool
) -> Dict[str, Any]:
    """Run ripgrep and stream JSON output, counting matches Python-side."""
    cmd = [rg_path, "--json"]

    if not is_regex:
        cmd.append("--fixed-strings")
    if not case_sensitive:
        cmd.append("--ignore-case")
    if context_lines > 0:
        cmd.extend(["-C", str(context_lines)])

    # File pattern filtering
    if file_pattern:
        cmd.extend(["--glob", file_pattern])
    if exclude_pattern:
        cmd.extend(["--glob", f"!{exclude_pattern}"])

    cmd.append(pattern)
    cmd.extend(search_paths)

    matches: List[Dict] = []
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True
        )
        try:
            for line in proc.stdout:
                if len(matches) >= max_results:
                    proc.kill()
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if msg.get("type") == "match":
                    data = msg["data"]
                    file_path = data["path"]["text"]
                    for submatch in data.get("submatches", [{}]):
                        if len(matches) >= max_results:
                            break
                        line_num = data["line_number"]
                        match_line = data["lines"]["text"].rstrip("\n")

                        # Gather context from surrounding context messages
                        context = []
                        matches.append({
                            "file": file_path,
                            "line_number": line_num,
                            "match_line": match_line,
                            "language": _detect_language(file_path),
                        })
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
    except Exception as e:
        error_logger(f"ripgrep error: {e}")
        return {"error": f"ripgrep failed: {str(e)}"}

    return {
        "success": True,
        "pattern": pattern,
        "total_matches": len(matches),
        "truncated": len(matches) >= max_results,
        "matches": matches,
    }


def _grep_with_python(
    pattern: str, search_paths: List[str],
    is_regex: bool, file_pattern: Optional[str],
    exclude_pattern: Optional[str], context_lines: int,
    max_results: int, case_sensitive: bool
) -> Dict[str, Any]:
    """Pure Python fallback using re module."""
    flags = 0 if case_sensitive else re.IGNORECASE
    if is_regex:
        try:
            compiled = re.compile(pattern, flags)
        except re.error as e:
            return {"error": f"Invalid regex pattern: {e}"}
    else:
        compiled = re.compile(re.escape(pattern), flags)

    matches: List[Dict] = []
    for repo_root in search_paths:
        spec = _load_gitignore_spec(repo_root)
        root = Path(repo_root)
        for dirpath, dirnames, filenames in os.walk(root):
            # Prune ignored dirs in-place
            dirnames[:] = [
                d for d in dirnames
                if d not in IGNORE_DIRS
                and not _should_ignore_path(
                    str(Path(dirpath, d).relative_to(root)), spec
                )
            ]
            for fname in filenames:
                if len(matches) >= max_results:
                    break
                fpath = Path(dirpath, fname)
                try:
                    rel = str(fpath.relative_to(root))
                except ValueError:
                    continue

                if _should_ignore_path(rel, spec):
                    continue
                if file_pattern and not fnmatch.fnmatch(fname, file_pattern):
                    continue
                if exclude_pattern and fnmatch.fnmatch(fname, exclude_pattern):
                    continue

                try:
                    lines = fpath.read_text(errors="replace").splitlines()
                except (OSError, UnicodeDecodeError):
                    continue

                for i, line_text in enumerate(lines):
                    if len(matches) >= max_results:
                        break
                    if compiled.search(line_text):
                        # Gather context lines
                        ctx_start = max(0, i - context_lines)
                        ctx_end = min(len(lines), i + context_lines + 1)
                        context = [
                            {"line_number": ctx_start + j + 1, "text": lines[ctx_start + j]}
                            for j in range(ctx_end - ctx_start)
                            if ctx_start + j != i
                        ]
                        matches.append({
                            "file": str(fpath),
                            "line_number": i + 1,
                            "match_line": line_text,
                            "context": context,
                            "language": _detect_language(str(fpath)),
                        })
            if len(matches) >= max_results:
                break
        if len(matches) >= max_results:
            break

    return {
        "success": True,
        "pattern": pattern,
        "total_matches": len(matches),
        "truncated": len(matches) >= max_results,
        "matches": matches,
    }


# ---------------------------------------------------------------------------
# Tool 5: find_references
# ---------------------------------------------------------------------------

def find_references(db_manager, **args) -> Dict[str, Any]:
    """
    Find all references to a symbol: callers, importers, inheritors,
    type annotations, and text mentions. Graph + grep hybrid.
    """
    symbol = args.get("symbol")
    if not symbol:
        return {"error": "symbol is required."}

    repo_path = args.get("repo_path")
    include_definitions = args.get("include_definitions", False)

    result: Dict[str, Any] = {
        "success": True,
        "symbol": symbol,
        "definitions": [],
        "callers": [],
        "importers": [],
        "inheritors": [],
        "type_annotations": [],
        "other_references": [],
    }

    # Build WHERE clause for repo scoping
    repo_filter = ""
    params: Dict[str, Any] = {"name": symbol}
    if repo_path:
        resolved = str(Path(repo_path).resolve())
        repo_filter = " AND f.path STARTS WITH $repo"
        params["repo"] = resolved

    seen_locations: set = set()  # (file, line_number) for dedup

    try:
        with db_manager.get_driver().session() as session:
            # 1. Definitions
            if include_definitions:
                q = (
                    f"MATCH (f:File)-[:CONTAINS]->(n {{name: $name}}) "
                    f"WHERE true {repo_filter} "
                    f"RETURN f.path AS file, n.line_number AS line, labels(n) AS labels, "
                    f"n.args AS args"
                )
                for rec in session.run(q, **params):
                    loc = (rec["file"], rec["line"])
                    seen_locations.add(loc)
                    result["definitions"].append({
                        "file": rec["file"],
                        "line_number": rec["line"],
                        "type": rec["labels"][-1] if rec["labels"] else "unknown",
                        "args": rec.get("args"),
                    })

            # 2. Callers
            q = (
                f"MATCH (f:File)-[:CONTAINS]->(caller)-[:CALLS]->(callee {{name: $name}}) "
                f"WHERE true {repo_filter} "
                f"RETURN caller.name AS caller_name, f.path AS file, "
                f"caller.line_number AS line"
            )
            for rec in session.run(q, **params):
                loc = (rec["file"], rec["line"])
                seen_locations.add(loc)
                result["callers"].append({
                    "name": rec["caller_name"],
                    "file": rec["file"],
                    "line_number": rec["line"],
                })

            # 3. Importers
            q = (
                f"MATCH (f:File)-[:IMPORTS]->(m:Module) "
                f"WHERE (m.name = $name OR m.name ENDS WITH '.' + $name) "
                f"{repo_filter} "
                f"RETURN f.path AS file"
            )
            for rec in session.run(q, **params):
                result["importers"].append({"file": rec["file"]})
                seen_locations.add((rec["file"], None))

            # 4. Inheritors
            q = (
                f"MATCH (f:File)-[:CONTAINS]->(child:Class)-[:INHERITS]->(parent {{name: $name}}) "
                f"WHERE true {repo_filter} "
                f"RETURN child.name AS child_name, f.path AS file, "
                f"child.line_number AS line"
            )
            for rec in session.run(q, **params):
                loc = (rec["file"], rec["line"])
                seen_locations.add(loc)
                result["inheritors"].append({
                    "name": rec["child_name"],
                    "file": rec["file"],
                    "line_number": rec["line"],
                })

    except Exception as e:
        error_logger(f"Graph query error in find_references: {e}")
        return {"error": f"Graph query failed: {str(e)}"}

    # 5. Text references via grep_code (word-boundary regex)
    grep_result = grep_code(db_manager, pattern=rf"\b{re.escape(symbol)}\b",
                            is_regex=True, repo_path=repo_path,
                            max_results=200, context_lines=0)

    if grep_result.get("success"):
        type_annotation_pattern = re.compile(
            rf":\s*{re.escape(symbol)}\b|"
            rf"->\s*{re.escape(symbol)}\b|"
            rf"Optional\[{re.escape(symbol)}\]|"
            rf"List\[{re.escape(symbol)}\]|"
            rf"Dict\[.*{re.escape(symbol)}|"
            rf"Union\[.*{re.escape(symbol)}"
        )
        for match in grep_result.get("matches", []):
            loc = (match["file"], match["line_number"])
            if loc in seen_locations:
                continue
            seen_locations.add(loc)

            if type_annotation_pattern.search(match["match_line"]):
                result["type_annotations"].append({
                    "file": match["file"],
                    "line_number": match["line_number"],
                    "match_line": match["match_line"],
                })
            else:
                result["other_references"].append({
                    "file": match["file"],
                    "line_number": match["line_number"],
                    "match_line": match["match_line"],
                })

    # Summary counts
    result["summary"] = {
        "definitions": len(result["definitions"]),
        "callers": len(result["callers"]),
        "importers": len(result["importers"]),
        "inheritors": len(result["inheritors"]),
        "type_annotations": len(result["type_annotations"]),
        "other_references": len(result["other_references"]),
        "total": sum([
            len(result["definitions"]),
            len(result["callers"]),
            len(result["importers"]),
            len(result["inheritors"]),
            len(result["type_annotations"]),
            len(result["other_references"]),
        ]),
    }

    return result
