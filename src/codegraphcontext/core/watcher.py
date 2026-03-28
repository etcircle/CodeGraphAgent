# src/codegraphcontext/core/watcher.py
"""
This module implements the live file-watching functionality using the `watchdog` library.
It observes directories for changes and triggers updates to the code graph.
"""
import threading
from pathlib import Path
import typing
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

if typing.TYPE_CHECKING:
    from codegraphcontext.tools.graph_builder import GraphBuilder
    from codegraphcontext.core.jobs import JobManager

from codegraphcontext.utils.debug_log import debug_log, info_logger, error_logger, warning_logger

class RepositoryEventHandler(FileSystemEventHandler):
    """
    A dedicated event handler for a single repository being watched.

    This handler is stateful. It performs an initial scan of the repository
    to build a baseline and then uses this cached state to perform efficient
    incremental updates when files are changed, created, or deleted.
    """
    def __init__(self, graph_builder: "GraphBuilder", repo_path: Path, debounce_interval=2.0, perform_initial_scan: bool = True):
        """
        Initializes the event handler.

        Args:
            graph_builder: An instance of the GraphBuilder to perform graph operations.
            repo_path: The absolute path to the repository directory to watch.
            debounce_interval: The time in seconds to wait for more changes before processing an event.
            perform_initial_scan: Whether to perform an initial scan of the repository.
        """
        super().__init__()
        self.graph_builder = graph_builder
        self.repo_path = repo_path
        self.debounce_interval = debounce_interval
        self.timers = {} # Kept for backward compatibility.

        # Batched debounce: collects changed paths and processes them together.
        self._pending_paths = set()
        self._timer = None
        self._lock = threading.Lock()

        # Caches for the repository's state.
        # all_file_data is a dict keyed by file path for O(1) incremental updates.
        self.all_file_data = {}
        self.imports_map = {}

        # Perform the initial scan and linking when the watcher is created.
        if perform_initial_scan:
            self._initial_scan()

    @staticmethod
    def _should_ignore(path_str: str) -> bool:
        """Return True for __pycache__, .pyc, and .pyo paths."""
        return ('__pycache__' in path_str
                or path_str.endswith('.pyc')
                or path_str.endswith('.pyo'))

    def _get_supported_files(self):
        """Get all supported source files, excluding __pycache__ and compiled files."""
        supported_extensions = self.graph_builder.parsers.keys()
        return [
            f for f in self.repo_path.rglob("*")
            if f.is_file() and f.suffix in supported_extensions
            and '__pycache__' not in f.parts
        ]

    def _initial_scan(self):
        """Scans the entire repository, parses all files, and builds the initial graph."""
        info_logger(f"Performing initial scan for watcher: {self.repo_path}")
        all_files = self._get_supported_files()

        # 1. Pre-scan all files to get a global map of where every symbol is defined.
        self.imports_map = self.graph_builder._pre_scan_for_imports(all_files)

        # 2. Parse all files in detail and cache the parsed data (keyed by path).
        self.all_file_data = {}
        for f in all_files:
            parsed_data = self.graph_builder.parse_file(self.repo_path, f)
            if "error" not in parsed_data:
                self.all_file_data[str(f)] = parsed_data

        # 3. After all files are parsed, create the relationships between them.
        all_data = list(self.all_file_data.values())
        self.graph_builder._create_all_function_calls(all_data, self.imports_map)
        self.graph_builder._create_all_inheritance_links(all_data, self.imports_map)
        info_logger(f"Initial scan and graph linking complete for: {self.repo_path}")

    def _debounce(self, event_path: str):
        """
        Add a changed path to the pending set and (re)start the batch timer.
        Multiple file changes within the debounce window are processed together.
        """
        with self._lock:
            self._pending_paths.add(event_path)
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self.debounce_interval, self._process_batch)
            self._timer.start()

    def _process_batch(self):
        """
        Process all files that changed during the debounce window.
        Only re-parses changed files; reuses cached data for everything else.
        """
        with self._lock:
            paths = self._pending_paths.copy()
            self._pending_paths.clear()
            self._timer = None

        if not paths:
            return

        info_logger(f"Processing batch of {len(paths)} changed file(s)")
        supported_extensions = self.graph_builder.parsers.keys()

        # 1. Incrementally update only the changed files in our cache.
        for path_str in paths:
            modified_path = Path(path_str)

            if (modified_path.exists() and modified_path.is_file()
                    and modified_path.suffix in supported_extensions):
                parsed_data = self.graph_builder.parse_file(self.repo_path, modified_path)
                if "error" not in parsed_data:
                    self.all_file_data[str(modified_path)] = parsed_data
                else:
                    self.all_file_data.pop(str(modified_path), None)
            else:
                # File was deleted or is not a supported type.
                self.all_file_data.pop(path_str, None)

        # 2. Rebuild imports map from cached known files (no rglob needed).
        known_files = [Path(p) for p in self.all_file_data]
        self.imports_map = self.graph_builder._pre_scan_for_imports(known_files)

        # 3. Update changed files in the graph.
        for path_str in paths:
            self.graph_builder.update_file_in_graph(
                Path(path_str), self.repo_path, self.imports_map
            )

        # 4. Re-link the graph using cached data (no full re-parse needed).
        all_data = list(self.all_file_data.values())
        self.graph_builder._create_all_function_calls(all_data, self.imports_map)
        self.graph_builder._create_all_inheritance_links(all_data, self.imports_map)
        info_logger(f"Batch processing complete for {len(paths)} file(s)")

    # The following methods are called by the watchdog observer when a file event occurs.
    def on_created(self, event):
        if not event.is_directory and not self._should_ignore(event.src_path):
            if Path(event.src_path).suffix in self.graph_builder.parsers:
                self._debounce(event.src_path)

    def on_modified(self, event):
        if not event.is_directory and not self._should_ignore(event.src_path):
            if Path(event.src_path).suffix in self.graph_builder.parsers:
                self._debounce(event.src_path)

    def on_deleted(self, event):
        if not event.is_directory and not self._should_ignore(event.src_path):
            if Path(event.src_path).suffix in self.graph_builder.parsers:
                self._debounce(event.src_path)

    def on_moved(self, event):
        if not event.is_directory:
            if not self._should_ignore(event.src_path) and Path(event.src_path).suffix in self.graph_builder.parsers:
                self._debounce(event.src_path)
            if not self._should_ignore(event.dest_path) and Path(event.dest_path).suffix in self.graph_builder.parsers:
                self._debounce(event.dest_path)


class CodeWatcher:
    """
    Manages the file system observer thread. It can watch multiple directories,
    assigning a separate `RepositoryEventHandler` to each one.
    """
    def __init__(self, graph_builder: "GraphBuilder", job_manager= "JobManager"):
        self.graph_builder = graph_builder
        self.observer = Observer()
        self.watched_paths = set() # Keep track of paths already being watched.
        self.watches = {} # Store watch objects to allow unscheduling

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

        watch = self.watches.pop(path_str, None)
        if watch:
            self.observer.unschedule(watch)
        
        self.watched_paths.discard(path_str)
        info_logger(f"Stopped watching for code changes in: {path_str}")
        return {"message": f"Stopped watching {path_str}."}

    def list_watched_paths(self) -> list:
        """Returns a list of all currently watched directory paths."""
        return list(self.watched_paths)

    def start(self):
        """Starts the observer thread."""
        if not self.observer.is_alive():
            self.observer.start()
            info_logger("Code watcher observer thread started.")

    def stop(self):
        """Stops the observer thread gracefully."""
        if self.observer.is_alive():
            self.observer.stop()
            self.observer.join() # Wait for the thread to terminate.
            info_logger("Code watcher observer thread stopped.")
