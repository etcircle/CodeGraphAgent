# CGC Watcher Overhaul Spec

**Goal:** Make the CodeGraphContext file watcher production-reliable. Currently it silently drops changes, has no observability, no error recovery, and no way to verify graph freshness.

**Repo:** https://github.com/CodeGraphContext/CodeGraphContext (fork to our org)
**Version:** 0.3.1 (MIT License)
**Key file:** `src/codegraphcontext/core/watcher.py` (~200 lines)
**Scope:** Watcher reliability only. MCP server architecture (supergateway spawning, stateful sessions) is a separate workstream.

---

## Current Architecture

```
cgc watch /path/to/project
  → CodeWatcher (Observer thread via python-watchdog)
    → RepositoryEventHandler per watched directory
      → on_created/modified/deleted/moved → _debounce(path)
        → 2s timer → _process_batch()
          → re-parse changed files
          → rebuild imports_map from ALL cached files
          → update_file_in_graph() per changed file
          → _create_all_function_calls() on ALL cached data
          → _create_all_inheritance_links() on ALL cached data
```

**Key assumption:** The watcher assumes `cgc index` was already run. `_initial_scan()` parses files and creates CALLS/INHERITS edges but does NOT call `add_file_to_graph()` to create File/Function/Class nodes. If nodes don't exist, the watcher's edge-linking silently does nothing.

---

## Problems Identified

### P1: Silent Failures (Critical)
- `_process_batch()` has **no try/except** — a single parse error or Neo4j timeout kills the batch, and subsequent events pile up but the handler is effectively dead.
- No logging of *what* was processed. No way to know if a file change was captured or dropped.
- Neo4j connection failures are not retried. If bolt drops momentarily, the watcher dies silently.

### P2: O(N) Re-linking on Every Change (Performance)
- Every batch (even a 1-file change) calls `_create_all_function_calls()` and `_create_all_inheritance_links()` on the **entire cached file set** (960+ files for backend).
- `_pre_scan_for_imports()` reads and parses import statements from every file to build a global symbol-to-file map. This is essentially a separate index that needs its own incremental maintenance.
- A single save triggers a full graph re-link that takes 10-30s, during which watchdog events can overflow the OS buffer.

### P3: No Health/Observability
- No heartbeat, no metrics, no "last successful update" timestamp.
- Our keepalive script checks PID existence, not actual function. A watcher process can be alive but broken.
- No way to ask "is the graph stale?" or "when was file X last indexed?"

### P4: No Persistence of Watch State
- If the process crashes, all `all_file_data` cache is lost.
- Restart requires full re-scan (initial_scan), which for 960 files takes minutes.
- No checkpointing — can't resume from where it left off.

### P5: No Graceful Degradation
- If Neo4j is temporarily unreachable (container restart, network blip), the watcher doesn't pause and retry — it either crashes or produces corrupt partial updates.
- No circuit breaker pattern.

### P6: watchdog Event Buffer Overflow (macOS)
- macOS FSEvents has a limited buffer. When 4 Claude Code agents are simultaneously writing files, the OS event queue overflows and watchdog misses events entirely.
- The watcher has no periodic reconciliation to catch missed events.

### P7: Concurrent Batch Execution Race Condition
- `_process_batch()` runs via `threading.Timer`. If processing takes longer than the 2s debounce, a new timer fires another `_process_batch()` concurrently.
- `self._lock` only protects the pending paths collection, NOT the actual batch processing.
- `update_file_in_graph()` does `delete_file_from_graph()` then `add_file_to_graph()`. Under concurrent batches, this races and can produce duplicate nodes.
- The `all_file_data` dict key is `str(path)` but path resolution isn't consistent (relative vs absolute, symlinks).

### P8: No .gitignore / Exclude Pattern Support
- The watcher uses watchdog's recursive mode which fires events for everything including `node_modules/`, `.git/`, build artifacts.
- The `extensions/office` directory has `node_modules` — thousands of irrelevant events.
- CGC's `index` command reads `IGNORE_DIRS` from config but the watcher doesn't use it.

### P9: Atomic Write Handling
- Claude Code and many editors save files via write-to-temp-then-rename, or rapid sequential writes.
- The watcher can catch a half-written file, fail to parse it, add it to the retry queue, and waste cycles.
- No file stability check before attempting parse.

---

## Proposed Changes

### Phase 0: Project Setup

#### 0.1 Fork & CI
- Fork `CodeGraphContext/CodeGraphContext` to our GitHub org
- Set up CI (GitHub Actions): lint, run existing tests
- Verify existing test suite passes on fork
- Create a `feat/watcher-overhaul` branch

### Phase 1: Reliability (Must-Have)

#### 1.1 Error Isolation in Batch Processing
```python
def _process_batch(self):
    # ... existing path collection ...
    
    for path_str in paths:
        try:
            # existing per-file logic
        except Exception as e:
            error_logger(f"Failed to process {path_str}: {e}")
            self._failed_paths[path_str] = self._failed_paths.get(path_str, 0) + 1
            if self._failed_paths[path_str] >= self._max_retries:
                error_logger(f"Giving up on {path_str} after {self._max_retries} failures")
                del self._failed_paths[path_str]
            continue  # don't kill the whole batch
    
    try:
        # re-linking
        all_data = list(self.all_file_data.values())
        self.graph_builder._create_all_function_calls(all_data, self.imports_map)
        self.graph_builder._create_all_inheritance_links(all_data, self.imports_map)
    except Exception as e:
        error_logger(f"Graph re-linking failed: {e}")
        self._needs_full_relink = True
```

#### 1.2 Processing Lock (Prevents Concurrent Batch Execution)
```python
def __init__(self, ...):
    # ... existing init ...
    self._processing_lock = threading.Lock()

def _process_batch(self):
    if not self._processing_lock.acquire(blocking=False):
        # Another batch is still running — re-queue pending paths
        info_logger("Batch skipped — previous batch still processing")
        return
    try:
        # ... actual batch processing ...
    finally:
        self._processing_lock.release()
```

#### 1.3 Neo4j Connection Resilience
```python
def _with_retry(self, fn, max_retries=3, backoff=2.0):
    """Wrap Neo4j operations with exponential backoff retry."""
    for attempt in range(max_retries):
        try:
            return fn()
        except (ServiceUnavailable, SessionExpired, ConnectionError) as e:
            if attempt == max_retries - 1:
                raise
            wait = backoff * (2 ** attempt)
            warning_logger(f"Neo4j retry {attempt+1}/{max_retries} in {wait}s: {e}")
            time.sleep(wait)
```

Place in `core/database.py` as a method on `DatabaseManager`, usable by both watcher and graph_builder.

#### 1.4 Failed Path Retry Queue
- `self._failed_paths: dict[str, int]` — path → consecutive failure count
- On each batch, prepend failed paths from previous batch
- After `CGC_MAX_RETRIES` (default 3) consecutive failures for a path, log error and drop it
- Reset failure count on successful processing

#### 1.5 Health File Output
```python
def _write_health(self):
    """Write health status every batch + every 60s idle via background timer."""
    health = {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "status": "healthy" | "degraded" | "error",
        "watched_path": str(self.repo_path),
        "cached_files": len(self.all_file_data),
        "last_batch_at": self._last_batch_time,
        "last_batch_files": self._last_batch_count,
        "last_batch_duration_ms": self._last_batch_duration,
        "failed_paths": list(self._failed_paths.keys()),
        "total_batches": self._batch_count,
        "total_files_processed": self._total_files_processed,
        "total_errors": self._error_count,
        "neo4j_reachable": self._check_neo4j(),
        "circuit_breaker_state": self._circuit_breaker.state,
        "pid": os.getpid(),
    }
    # Use hash of full path to avoid collisions between repos named "backend"
    repo_slug = hashlib.md5(str(self.repo_path).encode()).hexdigest()[:8]
    repo_name = self.repo_path.name
    health_path = Path(f"{self._health_dir}/{repo_name}-{repo_slug}-health.json")
    health_path.parent.mkdir(parents=True, exist_ok=True)
    health_path.write_text(json.dumps(health, indent=2))
```

#### 1.6 .gitignore / Exclude Pattern Support
```python
def __init__(self, ...):
    # ... existing init ...
    self._ignore_patterns = self._load_ignore_patterns()

def _load_ignore_patterns(self) -> pathspec.PathSpec:
    """Load ignore patterns from .gitignore + IGNORE_DIRS config."""
    patterns = [
        "__pycache__", "*.pyc", "*.pyo",
        ".git", ".git/**",
        "node_modules", "node_modules/**",
        ".venv", ".venv/**", "venv", "venv/**",
        "dist", "dist/**", "build", "build/**",
        ".next", ".next/**",
    ]
    
    # Add patterns from .gitignore if present
    gitignore = self.repo_path / ".gitignore"
    if gitignore.exists():
        patterns.extend(gitignore.read_text().splitlines())
    
    # Add from CGC config
    ignore_dirs = get_config_value("IGNORE_DIRS") or ""
    if ignore_dirs:
        for d in ignore_dirs.split(","):
            d = d.strip()
            if d:
                patterns.extend([d, f"{d}/**"])
    
    return pathspec.PathSpec.from_lines("gitwildmatch", patterns)

def _should_ignore(self, path_str: str) -> bool:
    """Return True for paths matching ignore patterns."""
    try:
        rel = str(Path(path_str).relative_to(self.repo_path))
        return self._ignore_patterns.match_file(rel)
    except ValueError:
        return False
```

**Dependency:** Add `pathspec` to requirements (already common, pure Python).

#### 1.7 File Stability Check
```python
def _debounce(self, event_path: str):
    """Add path to pending set with stability check."""
    with self._lock:
        self._pending_paths.add(event_path)
        if self._timer is not None:
            self._timer.cancel()
        self._timer = threading.Timer(self.debounce_interval, self._process_batch)
        self._timer.start()

def _process_batch(self):
    # ... collect paths ...
    
    # Stability check: skip files still being written
    stable_paths = set()
    for path_str in paths:
        p = Path(path_str)
        if not p.exists():
            stable_paths.add(path_str)  # deleted — process immediately
            continue
        try:
            mtime = p.stat().st_mtime
            time.sleep(0.3)  # brief pause
            if p.stat().st_mtime == mtime:
                stable_paths.add(path_str)
            else:
                # File still changing — re-queue
                self._debounce(path_str)
        except OSError:
            stable_paths.add(path_str)  # gone — process as deletion
    
    paths = stable_paths
    # ... continue with batch processing ...
```

#### 1.8 Consistent Path Resolution
```python
def _normalize_path(self, path_str: str) -> str:
    """Resolve to absolute, resolve symlinks, normalize."""
    return str(Path(path_str).resolve())
```
Apply consistently in `_debounce`, `_process_batch`, and all `all_file_data` keys.

### Phase 2: Performance (High Priority)

#### 2.1 Incremental Re-linking (Not Full Rebuild)
Instead of `_create_all_function_calls(ALL_DATA)` on every batch:

**Important complexity note:** `_create_all_function_calls` in `graph_builder.py` uses Cypher MERGE operations. Passing a subset of file data won't correctly handle cross-file calls where the *caller* is in an unchanged file but the *callee* was renamed/moved. This requires a two-pass approach.

**Required changes to `graph_builder.py`:**
1. Add `delete_edges_for_files(file_paths: list[str])` — deletes all CALLS and INHERITS edges where source OR target is in the given file set
2. Modify `_create_all_function_calls` to accept an optional `scope_files` parameter — when set, only create edges involving those files

**Watcher-side implementation:**
```python
def _incremental_relink(self, changed_paths: set, all_file_data: dict, imports_map: dict):
    """Two-pass incremental relink: delete stale edges, rebuild affected edges."""
    
    # Pass 1: Identify all affected files
    # A file is "affected" if:
    #   (a) it was directly changed, OR
    #   (b) it imports a symbol from a changed file, OR
    #   (c) a changed file imports a symbol from it
    
    changed_modules = set()
    for p in changed_paths:
        data = all_file_data.get(p)
        if data:
            for func in data.get("functions", []):
                changed_modules.add(func["name"])
            for cls in data.get("classes", []):
                changed_modules.add(cls["name"])
            # Also track the module name (file stem) itself
            changed_modules.add(Path(p).stem)
    
    affected_files = set(changed_paths)
    for path_str, data in all_file_data.items():
        for imp in data.get("imports", []):
            if imp.get("module") in changed_modules or imp.get("name") in changed_modules:
                affected_files.add(path_str)
    
    # Pass 2: Delete all edges involving changed files
    self.graph_builder.delete_edges_for_files(list(changed_paths))
    
    # Pass 3: Rebuild edges for affected files only
    affected_data = [all_file_data[p] for p in affected_files if p in all_file_data]
    self.graph_builder._create_all_function_calls(affected_data, imports_map)
    self.graph_builder._create_all_inheritance_links(affected_data, imports_map)
```

**Fallback:** If incremental relink produces inconsistencies (detected via periodic validation), fall back to full relink and set `self._needs_full_relink = False`.

#### 2.2 Debounce Window Scaling
- Default 2s is too short for Claude Code (rapid multi-file saves)
- Make configurable via env: `CGC_DEBOUNCE_SECONDS=5`
- Auto-scale: if last batch size > 20 files, extend debounce to 10s for next window
- Cap at `CGC_MAX_DEBOUNCE_SECONDS=30`

#### 2.3 Incremental Imports Map Maintenance
`_pre_scan_for_imports()` currently reads and parses import statements from every file. For incremental updates:

```python
def _update_imports_map(self, changed_paths: set):
    """Incrementally update imports_map for changed files only."""
    for path_str in changed_paths:
        p = Path(path_str)
        # Remove old entries for this file
        self.imports_map = {
            k: v for k, v in self.imports_map.items()
            if v.get("defined_in") != path_str
        }
        # Re-scan this file's exports
        if p.exists() and p.suffix in self.graph_builder.parsers:
            file_imports = self.graph_builder._scan_file_imports(p)
            self.imports_map.update(file_imports)
    
    # Full rebuild only when explicitly requested or on initial scan
```

**Note:** This requires understanding the exact structure of `imports_map` from `_pre_scan_for_imports`. The implementation above is approximate — the agent should read the actual method and adapt.

### Phase 3: Resilience (Important)

#### 3.1 Periodic Reconciliation
Every N minutes (configurable, default 5), do a lightweight check:
```python
def _start_reconciliation_timer(self):
    """Start recurring reconciliation timer."""
    self._reconcile_timer = threading.Timer(
        self._reconcile_interval, self._reconcile_and_reschedule
    )
    self._reconcile_timer.daemon = True
    self._reconcile_timer.start()

def _reconcile_and_reschedule(self):
    """Run reconciliation then schedule next one."""
    try:
        self._reconcile()
    except Exception as e:
        error_logger(f"Reconciliation error: {e}")
    finally:
        self._start_reconciliation_timer()

def _reconcile(self):
    """Catch events missed by watchdog (FSEvents overflow)."""
    # Use cached file list for existing files (avoid rglob overhead)
    cached_files = set(self.all_file_data.keys())
    
    # Check for modified files via mtime comparison (fast stat() calls)
    modified_files = set()
    deleted_files = set()
    for f in cached_files:
        p = Path(f)
        if not p.exists():
            deleted_files.add(f)
        elif p.stat().st_mtime > self._file_mtimes.get(f, 0):
            modified_files.add(f)
    
    # New file discovery via rglob — less frequent (every 3rd reconciliation)
    new_files = set()
    self._reconcile_count = getattr(self, '_reconcile_count', 0) + 1
    if self._reconcile_count % 3 == 0:
        current_files = set(
            self._normalize_path(str(f)) for f in self._get_supported_files()
            if not self._should_ignore(str(f))
        )
        new_files = current_files - cached_files
    
    stale = new_files | deleted_files | modified_files
    if stale:
        info_logger(f"Reconciliation found {len(stale)} stale files "
                    f"(new={len(new_files)}, deleted={len(deleted_files)}, "
                    f"modified={len(modified_files)})")
        for p in stale:
            self._debounce(p)
    else:
        debug_log("Reconciliation: all files up to date")
```

Track `self._file_mtimes: dict[str, float]` — updated after each successful per-file processing in batch.

#### 3.2 Circuit Breaker for Neo4j
```python
class Neo4jCircuitBreaker:
    """Prevents hammering a dead Neo4j with requests."""
    def __init__(self, failure_threshold=5, reset_timeout=60):
        self.failures = 0
        self.threshold = failure_threshold
        self.reset_timeout = reset_timeout
        self.last_failure = 0
        self.state = "closed"  # closed | open | half-open
    
    def can_execute(self) -> bool:
        if self.state == "closed":
            return True
        if self.state == "open":
            if time.time() - self.last_failure > self.reset_timeout:
                self.state = "half-open"
                return True
            return False
        return True  # half-open: allow one attempt
    
    def record_success(self):
        self.failures = 0
        self.state = "closed"
    
    def record_failure(self):
        self.failures += 1
        self.last_failure = time.time()
        if self.failures >= self.threshold:
            self.state = "open"
            warning_logger(f"Circuit breaker OPEN — Neo4j failures: {self.failures}")
```

Integrate into `_process_batch`:
```python
def _process_batch(self):
    if not self._circuit_breaker.can_execute():
        info_logger("Circuit breaker OPEN — skipping batch, will retry on reset")
        # Re-queue all pending paths
        with self._lock:
            self._pending_paths.update(paths)
        return
    
    # ... process ...
    # On success:
    self._circuit_breaker.record_success()
    # On Neo4j error:
    self._circuit_breaker.record_failure()
```

#### 3.3 Startup File Hash Cache
On clean shutdown or periodically, persist `{path: (mtime, size)}` to disk.
On startup, compare with filesystem to determine what needs re-indexing instead of full re-scan.

```python
def _save_file_state(self):
    """Persist file state cache to disk."""
    repo_hash = hashlib.md5(str(self.repo_path).encode()).hexdigest()[:12]
    cache_dir = Path(self._file_cache_dir) / repo_hash
    cache_dir.mkdir(parents=True, exist_ok=True)
    
    state = {}
    for path_str, data in self.all_file_data.items():
        p = Path(path_str)
        if p.exists():
            stat = p.stat()
            state[path_str] = {
                "mtime": stat.st_mtime,
                "size": stat.st_size,
            }
    
    (cache_dir / "file_state.json").write_text(json.dumps(state))

def _load_file_state(self) -> dict:
    """Load file state cache from disk. Returns empty dict if not found."""
    repo_hash = hashlib.md5(str(self.repo_path).encode()).hexdigest()[:12]
    cache_path = Path(self._file_cache_dir) / repo_hash / "file_state.json"
    if cache_path.exists():
        try:
            return json.loads(cache_path.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}

def _initial_scan(self):
    """Optimised initial scan using file state cache."""
    cached_state = self._load_file_state()
    
    if not cached_state:
        # No cache — full scan (existing behaviour)
        info_logger(f"No file state cache — performing full initial scan for: {self.repo_path}")
        self._full_initial_scan()
        return
    
    # Diff-based scan: only re-parse changed files
    info_logger(f"File state cache found ({len(cached_state)} files) — performing diff scan")
    all_files = self._get_supported_files()
    current_paths = set(self._normalize_path(str(f)) for f in all_files)
    cached_paths = set(cached_state.keys())
    
    new_files = current_paths - cached_paths
    deleted_files = cached_paths - current_paths
    modified_files = set()
    unchanged_files = set()
    
    for p in current_paths & cached_paths:
        stat = Path(p).stat()
        cached = cached_state.get(p, {})
        if stat.st_mtime != cached.get("mtime") or stat.st_size != cached.get("size"):
            modified_files.add(p)
        else:
            unchanged_files.add(p)
    
    stale = new_files | modified_files
    info_logger(f"Diff scan: {len(unchanged_files)} unchanged, {len(stale)} changed, "
                f"{len(deleted_files)} deleted")
    
    # Parse only changed files; for unchanged, we still need them in cache
    # for relationship linking, but we can skip the expensive parse
    # ... (implementation depends on whether we can serialize parsed data too)
    
    # For now, still do full scan but log the diff for observability
    # TODO: Implement incremental initial scan once parsed data is cached
    self._full_initial_scan()
```

This turns a 3-minute startup into a 5-second diff once parsed data serialisation is added.

### Phase 4: Observability (Nice-to-Have)

#### 4.1 Structured Logging
Replace `info_logger(f"...")` with structured events:
```python
info_logger("batch_processed", 
    files=len(paths), 
    duration_ms=elapsed, 
    errors=error_count,
    repo=self.repo_path.name)
```

#### 4.2 Prometheus-style Metrics (Optional)
Expose via the MCP health endpoint:
- `cgc_watcher_batches_total`
- `cgc_watcher_files_processed_total`
- `cgc_watcher_errors_total`
- `cgc_watcher_last_batch_timestamp`
- `cgc_watcher_cache_size_files`

#### 4.3 CLI Health Command
```bash
cgc watch --status
# Output:
# Backend watcher: healthy (PID 28476)
#   Last batch: 3s ago, 2 files, 0 errors
#   Cached: 962 files, 19614 functions
#   Neo4j: connected (circuit: closed)
# Frontend watcher: healthy (PID 28480)
#   Last batch: 12s ago, 1 file, 0 errors
#   ...
# Office Ext watcher: healthy (PID 15051)
#   Last batch: 45s ago, 1 file, 0 errors
#   ...
```

Reads health JSON files from `CGC_HEALTH_DIR`.

#### 4.4 Startup Verification
```python
def _verify_indexed(self):
    """Check if the repo has been indexed (nodes exist). If not, run full index first."""
    # Quick check: does a Repository node exist for this path?
    with self.graph_builder.db_manager.get_driver().session() as session:
        result = session.run(
            "MATCH (r:Repository {path: $path}) RETURN count(r) as c",
            path=str(self.repo_path)
        )
        count = result.single()["c"]
    
    if count == 0:
        warning_logger(f"No index found for {self.repo_path} — running full index before watching")
        # Trigger full index...
```

---

## Files to Modify

| File | Changes |
|------|---------|
| `core/watcher.py` | Main overhaul: error isolation, processing lock, retry queue, health output, reconciliation, circuit breaker, incremental relink, .gitignore support, file stability check, path normalisation |
| `core/database.py` | Add retry wrapper with exponential backoff, connection health check method |
| `tools/graph_builder.py` | Add `delete_edges_for_files(file_paths)` for incremental edge cleanup. Add optional `scope_files` parameter to `_create_all_function_calls` |
| `cli/main.py` | Add `cgc watch --status` command (reads health JSON files) |
| `server.py` | Expose health metrics via MCP health endpoint |
| `requirements.txt` / `pyproject.toml` | Add `pathspec` dependency |

## Configuration (Environment Variables)

| Variable | Default | Description |
|----------|---------|-------------|
| `CGC_DEBOUNCE_SECONDS` | `5` | Debounce window for file change batching |
| `CGC_MAX_DEBOUNCE_SECONDS` | `30` | Maximum debounce window (auto-scaled under load) |
| `CGC_RECONCILE_INTERVAL` | `300` | Seconds between reconciliation sweeps |
| `CGC_HEALTH_DIR` | `/tmp/cgc-watch` | Directory for health JSON files |
| `CGC_MAX_RETRIES` | `3` | Max retries for failed file processing |
| `CGC_CIRCUIT_BREAKER_THRESHOLD` | `5` | Neo4j failures before circuit opens |
| `CGC_CIRCUIT_BREAKER_RESET` | `60` | Seconds before circuit half-opens |
| `CGC_FILE_CACHE_DIR` | `~/.codegraphcontext/cache` | Persistent file state cache |
| `CGC_FILE_STABILITY_DELAY` | `0.3` | Seconds to wait for file write stability |

## Implementation Order

1. **Phase 0** — Fork repo, set up CI, verify existing tests (30 min)
2. **Phase 1.1 + 1.2 + 1.4** — Error isolation + processing lock + retry queue (45 min)
3. **Phase 1.3** — Neo4j retry wrapper in database.py (20 min)
4. **Phase 1.6** — .gitignore / exclude pattern support (30 min)
5. **Phase 1.7 + 1.8** — File stability check + path normalisation (20 min)
6. **Phase 1.5** — Health file output (30 min)
7. **Phase 3.1** — Periodic reconciliation (45 min)
8. **Phase 3.2** — Circuit breaker (30 min)
9. **Phase 2.1** — Incremental re-linking + graph_builder changes (3-4 hr)
10. **Phase 2.2 + 2.3** — Debounce scaling + incremental imports map (45 min)
11. **Phase 3.3** — Startup file cache (1 hr)
12. **Phase 4** — Observability: structured logging, CLI status, startup verification (1.5 hr)

**Total estimate:** ~10-12 hours of coding agent time

## Testing

### Unit Tests
- Circuit breaker state transitions (closed → open → half-open → closed)
- Retry logic with exponential backoff
- .gitignore pattern matching (node_modules, .git, custom patterns)
- Path normalisation (relative, absolute, symlinks)
- Debounce auto-scaling logic
- File stability check (file still changing vs stable)
- Health file JSON output format

### Integration Tests
- Kill Neo4j mid-batch → verify circuit breaker opens, watcher survives, resumes on reconnect
- Create 50 files rapidly → verify all indexed (no missed events)
- Stress test: 4 parallel writers modifying random files → verify no missed events after reconciliation
- Reconciliation: disable watchdog events, modify file via direct write, verify reconciliation catches it within `CGC_RECONCILE_INTERVAL`
- Concurrent batch: trigger processing that takes >debounce window → verify no duplicate nodes

### Regression Tests
- Full re-index produces identical graph to incremental updates
- Watcher + index produce same results as index alone
- All existing CGC CLI commands work unchanged

## Notes

- Fork from `CodeGraphContext/CodeGraphContext` main branch
- Keep all existing functionality — this is additive, not a rewrite
- The MCP server (`server.py`) and CLI (`main.py`) are thin wrappers — the watcher is self-contained
- Consider upstreaming improvements via PR to the original repo (MIT license)
- MCP server architecture (supergateway per-connection spawning, stateful vs stateless) is a **separate workstream** not covered by this spec
