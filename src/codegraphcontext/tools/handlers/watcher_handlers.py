import json
import os
from pathlib import Path as _Path
from typing import Any, Dict
from ...utils.debug_log import debug_log, error_logger

def list_watched_paths(code_watcher, **args) -> Dict[str, Any]:
    """Tool to list all currently watched directory paths."""
    try:
        paths = code_watcher.list_watched_paths()
        return {"success": True, "watched_paths": paths}
    except Exception as e:
        return {"error": f"Failed to list watched paths: {str(e)}"}

def unwatch_directory(code_watcher, **args) -> Dict[str, Any]:
    """Tool to stop watching a directory."""
    path = args.get("path")
    if not path:
        return {"error": "Path is a required argument."}
    return code_watcher.unwatch_directory(path)

# watch_directory is complex as it depends on other tools and handlers
# We will keep it in server.py or implement it here passing all dependencies.
# Let's implement it here as a pure function accepting dependencies.
# Dependencies: code_watcher, list_repositories_func, add_code_func

def watch_directory(code_watcher, list_repositories_func, add_code_func, **args) -> Dict[str, Any]:
    """
    Tool implementation to start watching a directory for changes.
    It checks if the path exists, if it's already watched, or if it needs indexing.
    """
    path = args.get("path")
    from pathlib import Path

    if not path:
        return {"error": "Path is a required argument."}

    path_obj = Path(path).resolve()
    path_str = str(path_obj)

    # 1. Validate the path
    if not path_obj.is_dir():
        return {
            "success": True,
            "status": "path_not_found",
            "message": f"Path '{path_str}' does not exist or is not a directory."
        }
    try:
        # Check if already watching
        if path_str in code_watcher.watched_paths:
            return {"success": True, "message": f"Already watching directory: {path_str}"}

        # 2. Check if the repository is already indexed
        indexed_repos_result = list_repositories_func()
        indexed_repos = indexed_repos_result.get("repositories", [])
        is_already_indexed = any(Path(repo["path"]).resolve() == path_obj for repo in indexed_repos)

        # 3. Decide whether to perform an initial scan
        if is_already_indexed:
            # If already indexed, just start the watcher without a scan
            code_watcher.watch_directory(path_str, perform_initial_scan=False)
            return {
                "success": True,
                "message": f"Path '{path_str}' is already indexed. Now watching for live changes."
            }
        else:
            # If not indexed, perform the scan AND start the watcher
            scan_job_result = add_code_func(path=path_str, is_dependency=False)

            if "error" in scan_job_result:
                return scan_job_result

            # add_code_func already indexed the files — don't double-scan
            code_watcher.watch_directory(path_str, perform_initial_scan=False)
            
            return {
                "success": True,
                "message": f"Path '{path_str}' was not indexed. Started initial scan and now watching for live changes.",
                "job_id": scan_job_result.get("job_id"),
                "details": "Use check_job_status to monitor the initial scan."
            }
        
    except Exception as e:
        error_logger(f"Failed to start watching directory {path}: {e}")
        return {"error": f"Failed to start watching directory: {str(e)}"}


def get_watcher_health(code_watcher, db_manager, **args) -> Dict[str, Any]:
    """Returns health status for all active watchers + health files from CLI watchers."""
    health = {
        "mcp_watchers": [],
        "cli_watchers": [],
        "neo4j_connected": db_manager.is_connected(),
    }

    # 1. In-process MCP watchers (via handler access)
    for path_str, handler in code_watcher.handlers.items():
        health["mcp_watchers"].append({
            "path": path_str,
            "status": handler._compute_status(),
            "cached_files": len(handler.all_file_data),
            "last_batch_at": handler._last_batch_time,
            "last_batch_files": handler._last_batch_count,
            "total_batches": handler._batch_count,
            "total_errors": handler._error_count,
            "failed_paths_count": len(handler._failed_paths),
        })

    # 2. Health files from CLI watchers (if any)
    health_dir = _Path(os.getenv('CGC_HEALTH_DIR', '/tmp/cgc-watch'))
    if health_dir.exists():
        for hf in health_dir.glob("*-health.json"):
            try:
                data = json.loads(hf.read_text())
                # Skip if this is an MCP-managed watcher (already reported above)
                if data.get("watched_path") not in code_watcher.watched_paths:
                    health["cli_watchers"].append(data)
            except Exception:
                continue

    return {"success": True, "health": health}
