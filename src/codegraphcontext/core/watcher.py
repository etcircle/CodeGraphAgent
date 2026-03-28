# src/codegraphcontext/core/watcher.py
"""
This module implements the live file-watching functionality using the `watchdog` library.
It observes directories for changes and triggers updates to the code graph.
"""
import hashlib
import json
import os
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
import typing

import pathspec
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

if typing.TYPE_CHECKING:
    from codegraphcontext.tools.graph_builder import GraphBuilder
    from codegraphcontext.core.jobs import JobManager

from codegraphcontext.utils.debug_log import debug_log, info_logger, error_logger, warning_logger

# Directories always ignored regardless of .gitignore
IGNORE_DIRS = {
    '__pycache__', '.git', '.hg', '.svn', 'node_modules', '.tox', '.mypy_cache',
    '.pytest_cache', '.eggs', '*.egg-info', 'dist', 'build', '.venv', 'venv',
    'env', '.env', '.idea', '.vscode',
}

class Neo4jCircuitBreaker:
    """Prevents hammering a dead Neo4j with requests."""

    def __init__(self):
        self.failure_threshold = int(os.getenv('CGC_CIRCUIT_BREAKER_THRESHOLD', '5'))
        self.reset_timeout = int(os.getenv('CGC_CIRCUIT_BREAKER_RESET', '60'))
        self.failures = 0
        self.last_failure = 0.0
        self.state = "closed"  # closed | open | half-open

    def can_execute(self) -> bool:
        if self.state == "closed":
            return True
        if self.state == "open":
            if time.time() - self.last_failure > self.reset_timeout:
                self.state = "half-open"
                info_logger("Circuit breaker half-open — allowing test request")
                return True
            return False
        return True  # half-open: allow one attempt

    def record_success(self):
        if self.state == "half-open":
            info_logger("Circuit breaker closed — Neo4j recovered")
        self.failures = 0
        self.state = "closed"

    def record_failure(self):
        self.failures += 1
        self.last_failure = time.time()
        if self.failures >= self.failure_threshold:
            self.state = "open"
            warning_logger(f"Circuit breaker OPEN — Neo4j failures: {self.failures}")


class RepositoryEventHandler(FileSystemEventHandler):
    """
    A dedicated event handler for a single repository being watched.

    This handler is stateful. It performs an initial scan of the repository
    to build a baseline and then uses this cached state to perform efficient
    incremental updates when files are changed, created, or deleted.
    """
    def __init__(self, graph_builder: "GraphBuilder", repo_path: Path, debounce_interval=None, perform_initial_scan: bool = True):
        """
        Initializes the event handler.

        Args:
            graph_builder: An instance of the GraphBuilder to perform graph operations.
            repo_path: The absolute path to the repository directory to watch.
            debounce_interval: The time in seconds to wait for more changes before processing an event.
                               Defaults to CGC_DEBOUNCE_SECONDS env var, or 5.0.
            perform_initial_scan: Whether to perform an initial scan of the repository.
        """
        super().__init__()
        self.graph_builder = graph_builder
        self.repo_path = repo_path

        # Configurable debounce from env var (spec default: 5s, was 2s)
        self._default_debounce = float(os.getenv('CGC_DEBOUNCE_SECONDS', '5'))
        self.debounce_interval = debounce_interval if debounce_interval is not None else self._default_debounce
        self.timers = {}  # Kept for backward compatibility.

        # Batched debounce: collects changed paths and processes them together.
        self._pending_paths = set()
        self._timer = None
        self._lock = threading.Lock()

        # Caches for the repository's state.
        # all_file_data is a dict keyed by file path for O(1) incremental updates.
        self.all_file_data = {}
        self.imports_map = {}

        # Circuit breaker for Neo4j (Phase 3)
        self._circuit_breaker = Neo4jCircuitBreaker()

        # Retry queue (Phase 1)
        self._failed_paths: set = set()
        self._failure_counts: dict = {}  # path -> consecutive failure count
        self._max_retries = int(os.getenv('CGC_MAX_RETRIES', '3'))

        # Health tracking (Phase 1)
        self._last_batch_time: str = ""
        self._last_batch_count: int = 0
        self._batch_count: int = 0
        self._error_count: int = 0
        self._needs_full_relink: bool = False

        # File mtimes for reconciliation (Phase 3)
        self._file_mtimes: dict = {}

        # Load .gitignore patterns for this repo
        self._gitignore_spec = self._load_gitignore()

        # Perform the initial scan and linking when the watcher is created.
        if perform_initial_scan:
            self._initial_scan()

        # Start health heartbeat (writes health even when idle)
        self._health_timer = None
        self._schedule_health_heartbeat()

        # Start periodic reconciliation (catches missed FSEvents)
        self._reconcile_timer = None
        self._start_reconciliation_timer()

    def _load_gitignore(self) -> pathspec.PathSpec:
        """Load .gitignore patterns from the repo root, combined with IGNORE_DIRS."""
        patterns = list(IGNORE_DIRS)
        gitignore_path = self.repo_path / '.gitignore'
        if gitignore_path.is_file():
            try:
                patterns.extend(gitignore_path.read_text().splitlines())
            except OSError:
                pass
        return pathspec.PathSpec.from_lines("gitignore", patterns)

    def _should_ignore(self, path_str: str) -> bool:
        """Return True if the path should be ignored (compiled files, .gitignore, IGNORE_DIRS)."""
        if path_str.endswith('.pyc') or path_str.endswith('.pyo'):
            return True
        # Check against IGNORE_DIRS by examining path parts
        parts = Path(path_str).parts
        for part in parts:
            if part in IGNORE_DIRS:
                return True
        # Check against .gitignore spec using relative path
        try:
            rel = str(Path(path_str).relative_to(self.repo_path))
            if self._gitignore_spec.match_file(rel):
                return True
        except ValueError:
            pass
        return False

    @staticmethod
    def _is_file_stable(path: Path, wait_ms: int = 300) -> bool:
        """Check if a file's mtime has stabilised (editors do write-to-temp-then-rename)."""
        try:
            mtime1 = path.stat().st_mtime
            time.sleep(wait_ms / 1000.0)
            mtime2 = path.stat().st_mtime
            return mtime1 == mtime2
        except OSError:
            return False

    def _get_supported_files(self):
        """Get all supported source files, excluding ignored paths."""
        supported_extensions = self.graph_builder.parsers.keys()
        return [
            f for f in self.repo_path.rglob("*")
            if f.is_file() and f.suffix in supported_extensions
            and not self._should_ignore(str(f))
        ]

    def _initial_scan(self):
        """Scans the repository, using file state cache for incremental startup."""
        cached_state = self._load_file_state()

        if cached_state:
            info_logger(f"Found cached state for {len(cached_state)} files — doing incremental startup")
            all_files = self._get_supported_files()
            current_paths = {self._normalise_path(str(f)) for f in all_files}
            cached_paths = set(cached_state.keys())

            # Identify what changed since last run
            new_files = current_paths - cached_paths
            deleted_files = cached_paths - current_paths
            modified_files = set()
            unchanged_files = set()

            for f_str in current_paths & cached_paths:
                try:
                    stat = Path(f_str).stat()
                    cached = cached_state[f_str]
                    if stat.st_mtime != cached["mtime"] or stat.st_size != cached["size"]:
                        modified_files.add(f_str)
                    else:
                        unchanged_files.add(f_str)
                except OSError:
                    modified_files.add(f_str)

            files_to_parse = new_files | modified_files
            info_logger(f"Incremental startup: {len(files_to_parse)} to parse, "
                       f"{len(unchanged_files)} unchanged, {len(deleted_files)} deleted")

            if len(files_to_parse) < len(current_paths) * 0.5:
                # Less than 50% changed — incremental is worth it
                for f_str in files_to_parse:
                    parsed = self.graph_builder.parse_file(self.repo_path, Path(f_str))
                    if "error" not in parsed:
                        self.all_file_data[f_str] = parsed

                # Also parse unchanged files for the in-memory cache (needed for re-linking)
                for f_str in unchanged_files:
                    parsed = self.graph_builder.parse_file(self.repo_path, Path(f_str))
                    if "error" not in parsed:
                        self.all_file_data[f_str] = parsed

                self.imports_map = self.graph_builder._pre_scan_for_imports(
                    [Path(p) for p in self.all_file_data]
                )
                all_data = list(self.all_file_data.values())
                self.graph_builder._create_all_function_calls(all_data, self.imports_map)
                self.graph_builder._create_all_inheritance_links(all_data, self.imports_map)

                self._save_file_state()
                info_logger(f"Incremental startup complete for: {self.repo_path}")
                return

        # Fallback: full scan (same as original)
        info_logger(f"Performing full initial scan for watcher: {self.repo_path}")
        all_files = self._get_supported_files()
        self.imports_map = self.graph_builder._pre_scan_for_imports(all_files)

        self.all_file_data = {}
        for f in all_files:
            f_str = self._normalise_path(str(f))
            parsed_data = self.graph_builder.parse_file(self.repo_path, f)
            if "error" not in parsed_data:
                self.all_file_data[f_str] = parsed_data

        all_data = list(self.all_file_data.values())
        self.graph_builder._create_all_function_calls(all_data, self.imports_map)
        self.graph_builder._create_all_inheritance_links(all_data, self.imports_map)

        self._save_file_state()
        info_logger(f"Initial scan and graph linking complete for: {self.repo_path}")

    def _update_imports_map_incrementally(self, changed_paths: set):
        """Update imports_map only for changed files, not the entire cache."""
        # 1. Remove old entries for changed files
        for path_str in changed_paths:
            resolved = str(Path(path_str).resolve())
            for symbol, paths_list in list(self.imports_map.items()):
                if resolved in paths_list:
                    paths_list.remove(resolved)
                if not paths_list:
                    del self.imports_map[symbol]

        # 2. Re-scan ONLY changed files for new symbols
        changed_files = [Path(p) for p in changed_paths if p in self.all_file_data]
        if changed_files:
            partial_imports = self.graph_builder._pre_scan_for_imports(changed_files)
            # 3. Merge into existing map
            for symbol, paths_list in partial_imports.items():
                if symbol in self.imports_map:
                    existing = set(self.imports_map[symbol])
                    existing.update(paths_list)
                    self.imports_map[symbol] = list(existing)
                else:
                    self.imports_map[symbol] = paths_list

    def _incremental_relink(self, changed_paths: set):
        """Re-link only edges involving changed files + files that import changed symbols."""
        # 1. Identify symbols defined in changed files
        changed_symbols = set()
        for p in changed_paths:
            data = self.all_file_data.get(p) or self.all_file_data.get(str(Path(p).resolve()))
            if data:
                for func in data.get("functions", []):
                    changed_symbols.add(func["name"])
                for cls in data.get("classes", []):
                    changed_symbols.add(cls["name"])

        # 2. Find affected files: changed files + files that import changed symbols
        affected_paths = set(changed_paths)
        for path_str, data in self.all_file_data.items():
            for imp in data.get("imports", []):
                imp_name = imp.get("alias") or imp["name"].split(".")[-1]
                if imp_name in changed_symbols or imp.get("module") in changed_symbols:
                    affected_paths.add(path_str)

        # 3. Delete CALLS and INHERITS edges originating from affected files
        for path_str in affected_paths:
            self.graph_builder.delete_edges_for_file(str(Path(path_str).resolve()))

        # 4. Re-create edges only for affected subset
        affected_data = [
            self.all_file_data[p]
            for p in affected_paths
            if p in self.all_file_data
        ]
        if affected_data:
            self.graph_builder._create_all_function_calls(affected_data, self.imports_map)
            self.graph_builder._create_all_inheritance_links(affected_data, self.imports_map)

        info_logger(
            f"Incremental relink: {len(changed_paths)} changed, "
            f"{len(affected_paths)} affected, "
            f"{len(changed_symbols)} symbols"
        )

    def _debounce(self, event_path: str):
        """
        Add a changed path to the pending set and (re)start the batch timer.
        Multiple file changes within the debounce window are processed together.
        """
        normalised = self._normalise_path(event_path)
        with self._lock:
            self._pending_paths.add(normalised)
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self.debounce_interval, self._process_batch)
            self._timer.start()

    def _process_batch(self):
        """Process all files that changed during the debounce window, with error isolation."""
        with self._lock:
            paths = self._pending_paths.copy()
            self._pending_paths.clear()
            self._timer = None

        if not paths:
            return

        # Circuit breaker check
        if not self._circuit_breaker.can_execute():
            warning_logger("Circuit breaker open — skipping batch, queuing for retry")
            with self._lock:
                self._pending_paths.update(paths)
            return

        # Prepend any previously failed paths (with retry limit)
        retry_paths = set()
        for p in list(self._failed_paths):
            count = self._failure_counts.get(p, 0)
            if count < self._max_retries:
                retry_paths.add(p)
            else:
                error_logger(f"Dropping {p} after {self._max_retries} consecutive failures")
                self._failed_paths.discard(p)
                self._failure_counts.pop(p, None)

        all_paths = paths | retry_paths
        info_logger(f"Processing batch of {len(all_paths)} file(s) ({len(retry_paths)} retries)")

        supported_extensions = self.graph_builder.parsers.keys()
        batch_errors = 0
        successfully_processed = set()

        # 1. Per-file parse + cache update — each file isolated
        for path_str in all_paths:
            try:
                modified_path = Path(path_str)
                if (modified_path.exists() and modified_path.is_file()
                        and modified_path.suffix in supported_extensions):
                    # File stability check: wait for editor save to finish
                    if not self._is_file_stable(modified_path):
                        warning_logger(f"File not stable yet, deferring: {path_str}")
                        self._failed_paths.add(path_str)
                        self._failure_counts[path_str] = self._failure_counts.get(path_str, 0) + 1
                        batch_errors += 1
                        continue

                    parsed_data = self.graph_builder.parse_file(self.repo_path, modified_path)
                    if "error" not in parsed_data:
                        self.all_file_data[str(modified_path)] = parsed_data
                    else:
                        self.all_file_data.pop(str(modified_path), None)
                else:
                    self.all_file_data.pop(path_str, None)
                successfully_processed.add(path_str)
            except Exception as e:
                error_logger(f"Failed to process {path_str}: {e}")
                self._failed_paths.add(path_str)
                self._failure_counts[path_str] = self._failure_counts.get(path_str, 0) + 1
                batch_errors += 1
                continue

        # Clear failure state for successfully processed paths
        for p in successfully_processed:
            self._failed_paths.discard(p)
            self._failure_counts.pop(p, None)

        # 2-4. Incremental graph update
        try:
            # 2. Incremental imports map update
            self._update_imports_map_incrementally(successfully_processed)

            # 3. Update changed files in graph (node-level)
            for path_str in successfully_processed:
                self.graph_builder.update_file_in_graph(
                    Path(path_str), self.repo_path, self.imports_map
                )

            # 4. Incremental re-link (edges only for changed + affected)
            if self._needs_full_relink:
                # Recovery: do full re-link once, then clear flag
                all_data = list(self.all_file_data.values())
                self.graph_builder._create_all_function_calls(all_data, self.imports_map)
                self.graph_builder._create_all_inheritance_links(all_data, self.imports_map)
                self._needs_full_relink = False
                info_logger("Full re-link recovery completed")
            else:
                self._incremental_relink(successfully_processed)
            self._circuit_breaker.record_success()
        except Exception as e:
            error_logger(f"Graph update failed: {e}")
            self._needs_full_relink = True
            self._circuit_breaker.record_failure()

        # 5. Update file mtimes for reconciliation
        for p in successfully_processed:
            try:
                self._file_mtimes[p] = Path(p).stat().st_mtime
            except OSError:
                self._file_mtimes.pop(p, None)

        # 6. Update health + metrics
        self._last_batch_time = datetime.now(tz=timezone.utc).isoformat()
        self._last_batch_count = len(successfully_processed)
        self._batch_count += 1
        self._error_count += batch_errors
        self._write_health()

        info_logger(f"Batch complete: {len(successfully_processed)} OK, {batch_errors} errors")

        # 7. Adaptive debounce: scale window based on batch size
        if len(all_paths) > 20:
            self.debounce_interval = min(self.debounce_interval * 1.5, 30.0)
            info_logger(f"Large batch — debounce increased to {self.debounce_interval}s")
        elif len(all_paths) <= 3 and self.debounce_interval > self._default_debounce:
            self.debounce_interval = max(self.debounce_interval * 0.75, self._default_debounce)

    def _write_health(self):
        """Write watcher health to a JSON file for external monitoring."""
        health_dir = Path(os.getenv('CGC_HEALTH_DIR', '/tmp/cgc-watch'))
        health_dir.mkdir(parents=True, exist_ok=True)

        health = {
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            "status": self._compute_status(),
            "watched_path": str(self.repo_path),
            "cached_files": len(self.all_file_data),
            "last_batch_at": self._last_batch_time,
            "last_batch_files": self._last_batch_count,
            "failed_paths": list(self._failed_paths)[:20],
            "total_batches": self._batch_count,
            "total_errors": self._error_count,
            "needs_full_relink": self._needs_full_relink,
            "pid": os.getpid(),
        }

        health_path = health_dir / f"{self.repo_path.name}-health.json"
        try:
            health_path.write_text(json.dumps(health, indent=2))
        except Exception as e:
            error_logger(f"Failed to write health file: {e}")

    def _compute_status(self) -> str:
        if self._needs_full_relink or len(self._failed_paths) > 10:
            return "error"
        elif len(self._failed_paths) > 0:
            return "degraded"
        return "healthy"

    def _schedule_health_heartbeat(self):
        """Write health every 60s even when idle."""
        self._write_health()
        self._health_timer = threading.Timer(60.0, self._schedule_health_heartbeat)
        self._health_timer.daemon = True
        self._health_timer.start()

    # --- Path normalisation (Phase 3.5) ---

    @staticmethod
    def _normalise_path(path_str: str) -> str:
        """Normalise all paths to resolved absolute form."""
        return str(Path(path_str).resolve())

    # --- Periodic reconciliation (Phase 3.2) ---

    def _start_reconciliation_timer(self):
        """Periodically check for missed file events (FSEvents overflow)."""
        interval = int(os.getenv('CGC_RECONCILE_INTERVAL', '300'))
        self._reconcile_timer = threading.Timer(interval, self._reconcile_and_reschedule)
        self._reconcile_timer.daemon = True
        self._reconcile_timer.start()

    def _reconcile_and_reschedule(self):
        """Catch events missed by watchdog."""
        try:
            current_files = {str(f) for f in self._get_supported_files()}
            cached_files = set(self.all_file_data.keys())

            new_files = current_files - cached_files
            deleted_files = cached_files - current_files

            # Check mtime for modified files
            modified_files = set()
            for f in current_files & cached_files:
                try:
                    mtime = Path(f).stat().st_mtime
                    if mtime > self._file_mtimes.get(f, 0):
                        modified_files.add(f)
                except OSError:
                    continue

            stale = new_files | deleted_files | modified_files
            if stale:
                info_logger(f"Reconciliation found {len(stale)} stale files "
                           f"({len(new_files)} new, {len(deleted_files)} deleted, "
                           f"{len(modified_files)} modified)")
                for p in stale:
                    self._debounce(p)
        except Exception as e:
            error_logger(f"Reconciliation failed: {e}")
        finally:
            self._start_reconciliation_timer()

    # --- Startup file cache (Phase 3.3) ---

    def _get_cache_dir(self) -> Path:
        base = Path(os.getenv('CGC_FILE_CACHE_DIR',
                              os.path.expanduser('~/.codegraphcontext/cache')))
        repo_hash = hashlib.md5(str(self.repo_path).encode()).hexdigest()[:12]
        cache_dir = base / repo_hash
        cache_dir.mkdir(parents=True, exist_ok=True)
        return cache_dir

    def _save_file_state(self):
        """Persist file mtimes + sizes for fast restart diff."""
        state = {}
        for path_str in self.all_file_data:
            try:
                p = Path(path_str)
                stat = p.stat()
                state[path_str] = {"mtime": stat.st_mtime, "size": stat.st_size}
            except OSError:
                continue

        cache_path = self._get_cache_dir() / "file_state.json"
        cache_path.write_text(json.dumps(state))
        info_logger(f"Saved file state cache: {len(state)} files")

    def _load_file_state(self) -> dict:
        """Load persisted file state for diffing against current filesystem."""
        cache_path = self._get_cache_dir() / "file_state.json"
        if cache_path.exists():
            try:
                return json.loads(cache_path.read_text())
            except Exception:
                return {}
        return {}

    # The following methods are called by the watchdog observer when a file event occurs.
    # All paths are normalised to resolved absolute form (Phase 3.5 / 5.1).
    def on_created(self, event):
        if not event.is_directory and not self._should_ignore(event.src_path):
            path = self._normalise_path(event.src_path)
            if Path(path).suffix in self.graph_builder.parsers:
                self._debounce(path)

    def on_modified(self, event):
        if not event.is_directory and not self._should_ignore(event.src_path):
            path = self._normalise_path(event.src_path)
            if Path(path).suffix in self.graph_builder.parsers:
                self._debounce(path)

    def on_deleted(self, event):
        if not event.is_directory and not self._should_ignore(event.src_path):
            path = self._normalise_path(event.src_path)
            if Path(path).suffix in self.graph_builder.parsers:
                self._debounce(path)

    def on_moved(self, event):
        if not event.is_directory:
            if not self._should_ignore(event.src_path):
                path = self._normalise_path(event.src_path)
                if Path(path).suffix in self.graph_builder.parsers:
                    self._debounce(path)
            if not self._should_ignore(event.dest_path):
                dest = self._normalise_path(event.dest_path)
                if Path(dest).suffix in self.graph_builder.parsers:
                    self._debounce(dest)


class CodeWatcher:
    """
    Manages the file system observer thread. It can watch multiple directories,
    assigning a separate `RepositoryEventHandler` to each one.
    """
    def __init__(self, graph_builder: "GraphBuilder", job_manager="JobManager"):
        self.graph_builder = graph_builder
        self.observer = Observer()
        self.watched_paths = set()  # Keep track of paths already being watched.
        self.watches = {}  # Store watch objects to allow unscheduling
        self.handlers: dict[str, RepositoryEventHandler] = {}  # Phase 4.2: handler access

    def watch_directory(self, path: str, perform_initial_scan: bool = True):
        """Schedules a directory to be watched for changes."""
        path_obj = Path(path).resolve()
        path_str = str(path_obj)

        if path_str in self.watched_paths:
            info_logger(f"Path already being watched: {path_str}")
            return {"message": f"Path already being watched: {path_str}"}

        # Create a new, dedicated event handler for this specific repository path.
        event_handler = RepositoryEventHandler(self.graph_builder, path_obj, perform_initial_scan=perform_initial_scan)

        watch = self.observer.schedule(event_handler, path_str, recursive=True)
        self.watches[path_str] = watch
        self.handlers[path_str] = event_handler
        self.watched_paths.add(path_str)
        info_logger(f"Started watching for code changes in: {path_str}")

        return {"message": f"Started watching {path_str}."}

    def unwatch_directory(self, path: str):
        """Stops watching a directory for changes."""
        path_obj = Path(path).resolve()
        path_str = str(path_obj)

        if path_str not in self.watched_paths:
            warning_logger(f"Attempted to unwatch a path that is not being watched: {path_str}")
            return {"error": f"Path not currently being watched: {path_str}"}

        # Save state before unwatching
        handler = self.handlers.get(path_str)
        if handler:
            handler._save_file_state()
            handler._write_health()

        watch = self.watches.pop(path_str, None)
        if watch:
            self.observer.unschedule(watch)

        self.handlers.pop(path_str, None)
        self.watched_paths.discard(path_str)
        info_logger(f"Stopped watching for code changes in: {path_str}")
        return {"message": f"Stopped watching {path_str}."}

    def list_watched_paths(self) -> list:
        """Returns a list of all currently watched directory paths."""
        return list(self.watched_paths)

    def start(self):
        """Starts the observer thread and registers signal handlers."""
        if not self.observer.is_alive():
            self.observer.start()
            info_logger("Code watcher observer thread started.")

        # Register graceful shutdown handlers (only from main thread)
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGTERM, signal.SIGINT):
                signal.signal(sig, self._handle_shutdown_signal)

    def _handle_shutdown_signal(self, signum, frame):
        """Gracefully shut down on SIGTERM/SIGINT — persist state before exit."""
        info_logger(f"Received signal {signum}, shutting down gracefully")
        for path_str, handler in self.handlers.items():
            handler._save_file_state()
            handler._write_health()
        self.stop()
        sys.exit(0)

    def stop(self):
        """Stops the observer thread gracefully."""
        if self.observer.is_alive():
            self.observer.stop()
            self.observer.join() # Wait for the thread to terminate.
            info_logger("Code watcher observer thread stopped.")
