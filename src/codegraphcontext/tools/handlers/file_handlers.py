"""File-oriented tool handlers: get_file_content, get_file_structure, diff_since."""
import os
import re
import subprocess
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

from ...utils.debug_log import debug_log, error_logger
from .search_handlers import _get_indexed_repo_paths, _is_within_indexed_repo, _detect_language


# ---------------------------------------------------------------------------
# Tool 3: get_file_content
# ---------------------------------------------------------------------------

def get_file_content(db_manager, **args) -> Dict[str, Any]:
    """
    Read source code from a file in an indexed repository.
    Security-scoped: only reads files within indexed repos.
    """
    path = args.get("path")
    if not path:
        return {"error": "path is required."}

    resolved = str(Path(path).resolve())

    # Security check: must be within an indexed repo
    repo_paths = _get_indexed_repo_paths(db_manager)
    if not _is_within_indexed_repo(resolved, repo_paths):
        return {"error": f"Access denied: '{path}' is not within any indexed repository."}

    if not Path(resolved).is_file():
        return {"error": f"File not found: '{path}'"}

    start_line = args.get("start_line")
    end_line = args.get("end_line")
    around_line = args.get("around_line")
    context_lines = args.get("context_lines", 20)
    max_lines = args.get("max_lines", 500)

    # around_line takes precedence
    if around_line is not None:
        start_line = max(1, around_line - context_lines)
        end_line = around_line + context_lines

    try:
        with open(resolved, "r", errors="replace") as f:
            all_lines = f.readlines()
    except OSError as e:
        return {"error": f"Failed to read file: {e}"}

    total_lines = len(all_lines)

    # Apply line range (1-indexed)
    if start_line is not None:
        start_idx = max(0, start_line - 1)
    else:
        start_idx = 0

    if end_line is not None:
        end_idx = min(total_lines, end_line)
    else:
        end_idx = total_lines

    # Enforce max_lines
    if end_idx - start_idx > max_lines:
        end_idx = start_idx + max_lines

    selected = all_lines[start_idx:end_idx]

    # Format with line numbers
    numbered_lines = []
    for i, line in enumerate(selected):
        numbered_lines.append(f"{start_idx + i + 1:>6} | {line.rstrip()}")

    content = "\n".join(numbered_lines)
    language = _detect_language(resolved)

    return {
        "success": True,
        "path": resolved,
        "language": language,
        "total_lines": total_lines,
        "start_line": start_idx + 1,
        "end_line": start_idx + len(selected),
        "truncated": (end_idx - start_idx) < (total_lines if start_line is None and end_line is None else end_idx - start_idx + 1),
        "content": content,
    }


# ---------------------------------------------------------------------------
# Tool 6: diff_since
# ---------------------------------------------------------------------------

_TIME_PATTERN = re.compile(r"^(\d+)(h|d|w|m)$")
_TIME_UNITS = {"h": "hours", "d": "days", "w": "weeks", "m": "months"}


def _parse_since(since: str) -> str:
    """Convert since param to git --since argument or a commit ref."""
    m = _TIME_PATTERN.match(since.strip())
    if m:
        num, unit = m.group(1), m.group(2)
        return f"--since={num}.{_TIME_UNITS[unit]}.ago"

    # ISO date check (contains a dash and digits)
    if re.match(r"^\d{4}-\d{2}", since):
        return f"--since={since}"

    # Treat as commit SHA — use as ref directly
    return since


def diff_since(db_manager, **args) -> Dict[str, Any]:
    """
    Show files changed within a time window or since a commit.
    Uses git history.
    """
    repo_path = args.get("repo_path")
    since = args.get("since")

    if not repo_path:
        return {"error": "repo_path is required."}
    if not since:
        return {"error": "since is required."}

    resolved = str(Path(repo_path).resolve())

    # Security: verify repo is indexed
    repo_paths = _get_indexed_repo_paths(db_manager)
    if not any(resolved.startswith(rp) or rp.startswith(resolved) for rp in repo_paths):
        return {"error": f"Path '{repo_path}' is not within any indexed repository."}

    if not Path(resolved).is_dir():
        return {"error": f"Directory not found: '{repo_path}'"}

    extensions = args.get("extensions")
    include_diff = args.get("include_diff", False)
    include_stats = args.get("include_stats", True)
    include_uncommitted = args.get("include_uncommitted", True)

    since_arg = _parse_since(since)
    is_commit_ref = not since_arg.startswith("--since=")

    result: Dict[str, Any] = {
        "success": True,
        "repo_path": resolved,
        "since": since,
        "commits": [],
        "changed_files": [],
    }

    try:
        # Build git log command
        if is_commit_ref:
            log_cmd = [
                "git", "-C", resolved, "log",
                f"{since_arg}..HEAD",
                "--format=%H|%s|%an|%ar",
            ]
            if include_stats:
                log_cmd.append("--stat")
        else:
            log_cmd = [
                "git", "-C", resolved, "log",
                since_arg,
                "--format=%H|%s|%an|%ar",
            ]
            if include_stats:
                log_cmd.append("--stat")

        log_output = subprocess.run(
            log_cmd, capture_output=True, text=True, timeout=30
        )

        if log_output.returncode != 0 and log_output.stderr:
            debug_log(f"git log stderr: {log_output.stderr}")

        # Parse git log output
        commits = []
        changed_files_set: set = set()
        current_commit = None

        for line in log_output.stdout.splitlines():
            line = line.strip()
            if not line:
                continue

            # Check if this is a commit header line
            if "|" in line and len(line.split("|")) >= 4:
                parts = line.split("|", 3)
                if len(parts[0]) >= 7 and all(c in "0123456789abcdef" for c in parts[0][:7]):
                    current_commit = {
                        "sha": parts[0][:8],
                        "message": parts[1],
                        "author": parts[2],
                        "relative_time": parts[3],
                    }
                    commits.append(current_commit)
                    continue

            # Stat lines look like: " file.py | 5 ++-"
            if "|" in line and current_commit is not None:
                stat_parts = line.split("|")
                if len(stat_parts) == 2:
                    fname = stat_parts[0].strip()
                    if fname and not fname.startswith(" ") or fname.strip():
                        fname = fname.strip()
                        if fname and not fname.endswith("changed"):
                            changed_files_set.add(fname)

        result["commits"] = commits

        # Get diff of changed files (using git diff for better file list)
        if is_commit_ref:
            diff_names_cmd = [
                "git", "-C", resolved, "diff", "--name-status", f"{since_arg}..HEAD"
            ]
        else:
            diff_names_cmd = [
                "git", "-C", resolved, "log", since_arg, "--name-status", "--format="
            ]

        diff_names_output = subprocess.run(
            diff_names_cmd, capture_output=True, text=True, timeout=30
        )

        changed_files = []
        for line in diff_names_output.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split("\t")
            if len(parts) >= 2:
                status = parts[0][0] if parts[0] else "M"
                fname = parts[-1]
                status_map = {"A": "added", "M": "modified", "D": "deleted", "R": "renamed"}
                changed_files.append({
                    "file": fname,
                    "status": status_map.get(status, "modified"),
                })

        # Filter by extensions
        if extensions:
            ext_set = {e if e.startswith(".") else f".{e}" for e in extensions}
            changed_files = [
                f for f in changed_files
                if Path(f["file"]).suffix in ext_set
            ]

        result["changed_files"] = changed_files

        # Include diff content if requested
        if include_diff:
            if is_commit_ref:
                diff_cmd = ["git", "-C", resolved, "diff", f"{since_arg}..HEAD"]
            else:
                diff_cmd = ["git", "-C", resolved, "log", since_arg, "-p", "--format="]

            diff_output = subprocess.run(
                diff_cmd, capture_output=True, text=True, timeout=30
            )
            result["diff_content"] = diff_output.stdout[:50000]  # Cap at 50KB

        # Uncommitted changes
        if include_uncommitted:
            unstaged = subprocess.run(
                ["git", "-C", resolved, "diff", "--name-status"],
                capture_output=True, text=True, timeout=30
            )
            staged = subprocess.run(
                ["git", "-C", resolved, "diff", "--cached", "--name-status"],
                capture_output=True, text=True, timeout=30
            )

            uncommitted = []
            for output, label in [(unstaged.stdout, "unstaged"), (staged.stdout, "staged")]:
                for line in output.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    parts = line.split("\t")
                    if len(parts) >= 2:
                        status = parts[0][0] if parts[0] else "M"
                        fname = parts[-1]
                        status_map = {"A": "added", "M": "modified", "D": "deleted"}
                        uncommitted.append({
                            "file": fname,
                            "status": status_map.get(status, "modified"),
                            "stage": label,
                        })

            if extensions:
                uncommitted = [
                    f for f in uncommitted
                    if Path(f["file"]).suffix in ext_set
                ]

            result["uncommitted"] = uncommitted

        # Summary stats
        if include_stats:
            result["summary"] = {
                "total_commits": len(commits),
                "total_files_changed": len(changed_files),
                "uncommitted_changes": len(result.get("uncommitted", [])),
            }

    except subprocess.TimeoutExpired:
        return {"error": "Git command timed out (30s limit)."}
    except Exception as e:
        error_logger(f"diff_since error: {e}")
        return {"error": f"Failed to get diff: {str(e)}"}

    return result


# ---------------------------------------------------------------------------
# Tool 8: get_file_structure
# ---------------------------------------------------------------------------

def get_file_structure(db_manager, **args) -> Dict[str, Any]:
    """
    Returns directory tree of an indexed repository with function/class counts.
    Uses graph data (no filesystem walk).
    """
    repo_path = args.get("repo_path")
    if not repo_path:
        return {"error": "repo_path is required."}

    resolved = str(Path(repo_path).resolve())
    directory = args.get("directory")
    extensions = args.get("extensions")
    max_depth = args.get("max_depth", 4)
    include_counts = args.get("include_counts", True)

    # Security check
    repo_paths = _get_indexed_repo_paths(db_manager)
    if not any(resolved.startswith(rp) or rp.startswith(resolved) for rp in repo_paths):
        return {"error": f"Path '{repo_path}' is not within any indexed repository."}

    scope = resolved
    if directory:
        scope = str(Path(resolved) / directory)

    try:
        with db_manager.get_driver().session() as session:
            # Query all files in scope
            query = (
                "MATCH (f:File) WHERE f.path STARTS WITH $scope "
                "RETURN f.path AS path"
            )
            file_records = list(session.run(query, scope=scope))

            # Query function and class counts per file
            counts_by_file: Dict[str, Dict] = {}
            if include_counts:
                count_query = (
                    "MATCH (f:File)-[:CONTAINS]->(n) "
                    "WHERE f.path STARTS WITH $scope "
                    "RETURN f.path AS path, labels(n) AS labels, count(n) AS cnt"
                )
                for rec in session.run(count_query, scope=scope):
                    fpath = rec["path"]
                    if fpath not in counts_by_file:
                        counts_by_file[fpath] = {"functions": 0, "classes": 0}
                    label_list = rec["labels"]
                    if "Function" in label_list:
                        counts_by_file[fpath]["functions"] = rec["cnt"]
                    elif "Class" in label_list:
                        counts_by_file[fpath]["classes"] = rec["cnt"]

        # Build tree structure
        tree: Dict = {}
        for rec in file_records:
            fpath = rec["path"]

            # Filter by extensions
            if extensions:
                ext_set = {e if e.startswith(".") else f".{e}" for e in extensions}
                if Path(fpath).suffix not in ext_set:
                    continue

            # Get path relative to scope
            try:
                rel = str(Path(fpath).relative_to(scope))
            except ValueError:
                continue

            parts = Path(rel).parts

            # Enforce max_depth
            if len(parts) > max_depth + 1:  # +1 for the file itself
                continue

            # Insert into tree
            node = tree
            for part in parts[:-1]:
                if part not in node:
                    node[part] = {}
                node = node[part]

            # Leaf node (file)
            fname = parts[-1]
            file_info = {"__file__": True}
            if include_counts and fpath in counts_by_file:
                file_info["functions"] = counts_by_file[fpath]["functions"]
                file_info["classes"] = counts_by_file[fpath]["classes"]
            node[fname] = file_info

        # Format as tree text
        tree_lines = _format_tree(tree, "", include_counts)

        # Aggregate summary
        total_files = len(file_records)
        total_functions = sum(c.get("functions", 0) for c in counts_by_file.values())
        total_classes = sum(c.get("classes", 0) for c in counts_by_file.values())

        return {
            "success": True,
            "repo_path": resolved,
            "scope": scope,
            "tree": "\n".join(tree_lines),
            "summary": {
                "total_files": total_files,
                "total_functions": total_functions,
                "total_classes": total_classes,
            },
        }

    except Exception as e:
        error_logger(f"get_file_structure error: {e}")
        return {"error": f"Failed to get file structure: {str(e)}"}


def _format_tree(node: Dict, prefix: str, include_counts: bool) -> List[str]:
    """Format a tree dict into visual tree lines."""
    lines = []
    entries = sorted(node.keys())
    for i, key in enumerate(entries):
        if key == "__file__":
            continue
        is_last = i == len(entries) - 1
        connector = "└── " if is_last else "├── "
        child = node[key]

        if isinstance(child, dict) and child.get("__file__"):
            # File entry
            label = key
            if include_counts:
                funcs = child.get("functions", 0)
                classes = child.get("classes", 0)
                if funcs or classes:
                    parts = []
                    if funcs:
                        parts.append(f"{funcs}f")
                    if classes:
                        parts.append(f"{classes}c")
                    label += f" ({', '.join(parts)})"
            lines.append(f"{prefix}{connector}{label}")
        elif isinstance(child, dict):
            # Directory
            # Count files in subtree
            file_count = _count_files(child)
            label = f"{key}/ [{file_count} files]" if file_count else f"{key}/"
            lines.append(f"{prefix}{connector}{label}")
            extension = "    " if is_last else "│   "
            lines.extend(_format_tree(child, prefix + extension, include_counts))

    return lines


def _count_files(node: Dict) -> int:
    """Count files in a tree dict."""
    count = 0
    for key, val in node.items():
        if key == "__file__":
            continue
        if isinstance(val, dict):
            if val.get("__file__"):
                count += 1
            else:
                count += _count_files(val)
    return count
