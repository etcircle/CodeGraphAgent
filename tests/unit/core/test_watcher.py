"""
Unit tests for watcher overhaul: circuit breaker, retry logic,
path normalisation, health file output, adaptive debounce.
"""
import json
import os
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Neo4jCircuitBreaker
# ---------------------------------------------------------------------------

class TestNeo4jCircuitBreaker:
    """Tests for the Neo4jCircuitBreaker state machine."""

    def _make_breaker(self, threshold=3, reset=1):
        from codegraphcontext.core.watcher import Neo4jCircuitBreaker
        with patch.dict(os.environ, {
            'CGC_CIRCUIT_BREAKER_THRESHOLD': str(threshold),
            'CGC_CIRCUIT_BREAKER_RESET': str(reset),
        }):
            return Neo4jCircuitBreaker()

    def test_starts_closed(self):
        cb = self._make_breaker()
        assert cb.state == "closed"
        assert cb.can_execute() is True

    def test_stays_closed_below_threshold(self):
        cb = self._make_breaker(threshold=3)
        cb.record_failure()
        cb.record_failure()
        assert cb.state == "closed"
        assert cb.can_execute() is True

    def test_opens_at_threshold(self):
        cb = self._make_breaker(threshold=3)
        for _ in range(3):
            cb.record_failure()
        assert cb.state == "open"
        assert cb.can_execute() is False

    def test_half_open_after_reset_timeout(self):
        cb = self._make_breaker(threshold=2, reset=1)
        cb.record_failure()
        cb.record_failure()
        assert cb.state == "open"

        # Manually backdate the last failure so the reset window has passed
        cb.last_failure = time.time() - 2
        assert cb.can_execute() is True
        assert cb.state == "half-open"

    def test_closes_on_success_after_half_open(self):
        cb = self._make_breaker(threshold=1, reset=0)
        cb.record_failure()
        assert cb.state == "open"

        cb.last_failure = time.time() - 1
        cb.can_execute()  # transitions to half-open
        assert cb.state == "half-open"

        cb.record_success()
        assert cb.state == "closed"
        assert cb.failures == 0

    def test_reopens_on_failure_in_half_open(self):
        cb = self._make_breaker(threshold=1, reset=0)
        cb.record_failure()
        cb.last_failure = time.time() - 1
        cb.can_execute()  # half-open
        cb.record_failure()
        assert cb.state == "open"

    def test_success_resets_failures(self):
        cb = self._make_breaker(threshold=5)
        cb.record_failure()
        cb.record_failure()
        assert cb.failures == 2
        cb.record_success()
        assert cb.failures == 0


# ---------------------------------------------------------------------------
# Path normalisation
# ---------------------------------------------------------------------------

class TestPathNormalisation:
    """Tests for RepositoryEventHandler._normalise_path."""

    def test_resolves_relative_path(self):
        from codegraphcontext.core.watcher import RepositoryEventHandler
        # A relative path should become absolute
        result = RepositoryEventHandler._normalise_path("some/relative/path.py")
        assert os.path.isabs(result)

    def test_resolves_symlinks(self, tmp_path):
        from codegraphcontext.core.watcher import RepositoryEventHandler
        real_file = tmp_path / "real.py"
        real_file.write_text("# real")
        link = tmp_path / "link.py"
        link.symlink_to(real_file)

        result = RepositoryEventHandler._normalise_path(str(link))
        assert result == str(real_file.resolve())

    def test_consistent_for_same_file(self, tmp_path):
        from codegraphcontext.core.watcher import RepositoryEventHandler
        file = tmp_path / "test.py"
        file.write_text("# test")

        # Two different string representations of the same path
        path1 = str(file)
        path2 = str(tmp_path / "." / "test.py")

        assert RepositoryEventHandler._normalise_path(path1) == \
               RepositoryEventHandler._normalise_path(path2)


# ---------------------------------------------------------------------------
# Ignore rules
# ---------------------------------------------------------------------------

class TestIgnoreRules:
    """Watcher should honour .cgcignore and built-in ignore rules."""

    def _make_handler(self, tmp_path):
        from codegraphcontext.core.watcher import RepositoryEventHandler

        mock_gb = MagicMock()
        mock_gb.parsers = {".py": MagicMock()}
        mock_gb._pre_scan_for_imports.return_value = {}
        mock_gb._create_all_function_calls.return_value = None
        mock_gb._create_all_inheritance_links.return_value = None

        health_dir = tmp_path / "health"
        health_dir.mkdir(exist_ok=True)

        with patch.dict(os.environ, {'CGC_HEALTH_DIR': str(health_dir)}):
            handler = RepositoryEventHandler(
                mock_gb, tmp_path, perform_initial_scan=False
            )
            if handler._health_timer:
                handler._health_timer.cancel()
            if handler._reconcile_timer:
                handler._reconcile_timer.cancel()
        return handler

    def test_builtin_ignore_dirs_exclude_paths(self, tmp_path):
        ignored_file = tmp_path / "node_modules" / "skip.py"
        ignored_file.parent.mkdir()
        ignored_file.write_text("print('skip')")

        handler = self._make_handler(tmp_path)
        assert handler._should_ignore(str(ignored_file)) is True

    def test_cgcignore_excludes_paths(self, tmp_path):
        (tmp_path / ".cgcignore").write_text("plugins/memory/\n")
        ignored_file = tmp_path / "plugins" / "memory" / "secret.py"
        ignored_file.parent.mkdir(parents=True)
        ignored_file.write_text("print('secret')")
        kept_file = tmp_path / "tools" / "tool.py"
        kept_file.parent.mkdir()
        kept_file.write_text("print('keep')")

        handler = self._make_handler(tmp_path)
        assert handler._should_ignore(str(ignored_file)) is True
        assert handler._should_ignore(str(kept_file)) is False


class TestUnsupportedFileWatching:
    """Watcher should keep unsupported files in sync via minimal File nodes."""

    def _make_handler(self, tmp_path):
        from codegraphcontext.core.watcher import RepositoryEventHandler

        mock_gb = MagicMock()
        mock_gb.parsers = {".py": MagicMock()}
        mock_gb._pre_scan_for_imports.return_value = {}
        mock_gb._create_all_function_calls.return_value = None
        mock_gb._create_all_inheritance_links.return_value = None
        mock_gb.parse_file.return_value = {"path": str((tmp_path / "notes.md").resolve()), "error": "No parser for .md"}
        mock_gb.update_file_in_graph.return_value = {"path": str((tmp_path / "notes.md").resolve())}

        health_dir = tmp_path / "health"
        health_dir.mkdir(exist_ok=True)

        with patch.dict(os.environ, {'CGC_HEALTH_DIR': str(health_dir)}):
            handler = RepositoryEventHandler(
                mock_gb, tmp_path, perform_initial_scan=False
            )
            if handler._health_timer:
                handler._health_timer.cancel()
            if handler._reconcile_timer:
                handler._reconcile_timer.cancel()
        return handler, mock_gb

    def test_get_supported_files_includes_markdown(self, tmp_path):
        md_file = tmp_path / "notes.md"
        md_file.write_text("hello")
        handler, _ = self._make_handler(tmp_path)

        files = handler._get_supported_files()
        assert md_file in files

    def test_process_batch_tracks_markdown_and_updates_graph(self, tmp_path):
        md_file = tmp_path / "notes.md"
        md_file.write_text("hello")
        handler, mock_gb = self._make_handler(tmp_path)

        with patch.object(handler, '_is_file_stable', return_value=True):
            handler._pending_paths.add(str(md_file.resolve()))
            handler._process_batch()

        assert str(md_file.resolve()) in handler.all_file_data
        assert handler.all_file_data[str(md_file.resolve())]["functions"] == []
        mock_gb.update_file_in_graph.assert_called_once()


# ---------------------------------------------------------------------------
# Health file output
# ---------------------------------------------------------------------------

class TestHealthOutput:
    """Tests for _write_health and _compute_status."""

    def _make_handler(self, tmp_path):
        """Create a RepositoryEventHandler with mocked graph_builder."""
        from codegraphcontext.core.watcher import RepositoryEventHandler

        mock_gb = MagicMock()
        mock_gb.parsers = {".py": MagicMock()}
        mock_gb._pre_scan_for_imports.return_value = {}
        mock_gb._create_all_function_calls.return_value = None
        mock_gb._create_all_inheritance_links.return_value = None

        health_dir = tmp_path / "health"
        health_dir.mkdir()

        with patch.dict(os.environ, {'CGC_HEALTH_DIR': str(health_dir)}):
            handler = RepositoryEventHandler(
                mock_gb, tmp_path, perform_initial_scan=False
            )
            # Stop the timers to avoid thread leaks in tests
            if handler._health_timer:
                handler._health_timer.cancel()
            if handler._reconcile_timer:
                handler._reconcile_timer.cancel()
        return handler, health_dir

    def test_healthy_status(self, tmp_path):
        handler, _ = self._make_handler(tmp_path)
        assert handler._compute_status() == "healthy"

    def test_degraded_status_with_failures(self, tmp_path):
        handler, _ = self._make_handler(tmp_path)
        handler._failed_paths.add("/some/path.py")
        assert handler._compute_status() == "degraded"

    def test_error_status_needs_relink(self, tmp_path):
        handler, _ = self._make_handler(tmp_path)
        handler._needs_full_relink = True
        assert handler._compute_status() == "error"

    def test_error_status_many_failures(self, tmp_path):
        handler, _ = self._make_handler(tmp_path)
        for i in range(11):
            handler._failed_paths.add(f"/path/{i}.py")
        assert handler._compute_status() == "error"

    def test_write_health_creates_file(self, tmp_path):
        handler, health_dir = self._make_handler(tmp_path)

        with patch.dict(os.environ, {'CGC_HEALTH_DIR': str(health_dir)}):
            handler._write_health()

        health_files = list(health_dir.glob("*-health.json"))
        assert len(health_files) == 1

        data = json.loads(health_files[0].read_text())
        assert data["status"] == "healthy"
        assert data["watched_path"] == str(tmp_path)
        assert data["pid"] == os.getpid()
        assert "timestamp" in data

    def test_health_file_reflects_metrics(self, tmp_path):
        handler, health_dir = self._make_handler(tmp_path)
        handler._batch_count = 5
        handler._error_count = 2
        handler._last_batch_time = "2026-03-28T10:00:00Z"

        with patch.dict(os.environ, {'CGC_HEALTH_DIR': str(health_dir)}):
            handler._write_health()

        data = json.loads(list(health_dir.glob("*-health.json"))[0].read_text())
        assert data["total_batches"] == 5
        assert data["total_errors"] == 2
        assert data["last_batch_at"] == "2026-03-28T10:00:00Z"


# ---------------------------------------------------------------------------
# Retry logic
# ---------------------------------------------------------------------------

class TestRetryLogic:
    """Tests for the retry queue in _process_batch."""

    def _make_handler(self, tmp_path):
        from codegraphcontext.core.watcher import RepositoryEventHandler

        mock_gb = MagicMock()
        mock_gb.parsers = {".py": MagicMock()}
        mock_gb._pre_scan_for_imports.return_value = {}
        mock_gb._create_all_function_calls.return_value = None
        mock_gb._create_all_inheritance_links.return_value = None
        mock_gb.parse_file.return_value = {"path": str(tmp_path / "test.py"), "functions": [], "classes": [], "imports": []}
        mock_gb.update_file_in_graph.return_value = None
        mock_gb.delete_edges_for_file.return_value = None

        with patch.dict(os.environ, {'CGC_HEALTH_DIR': str(tmp_path / "health")}):
            handler = RepositoryEventHandler(
                mock_gb, tmp_path, perform_initial_scan=False
            )
            if handler._health_timer:
                handler._health_timer.cancel()
            if handler._reconcile_timer:
                handler._reconcile_timer.cancel()
        return handler

    def test_failed_path_goes_to_retry_queue(self, tmp_path):
        handler = self._make_handler(tmp_path)
        handler._max_retries = 3

        # A file that doesn't exist should still be processed (as deletion)
        # but let's simulate a parse error
        handler.graph_builder.parse_file.side_effect = RuntimeError("parse error")

        test_file = tmp_path / "test.py"
        test_file.write_text("# test")

        handler._pending_paths.add(str(test_file))
        handler._process_batch()

        assert str(test_file) in handler._failed_paths
        assert handler._failure_counts[str(test_file)] == 1

    def test_path_dropped_after_max_retries(self, tmp_path):
        handler = self._make_handler(tmp_path)
        handler._max_retries = 2

        path = "/some/failing/file.py"
        handler._failed_paths.add(path)
        handler._failure_counts[path] = 2  # Already at max

        # Process an empty batch — this should trigger retry logic and drop the path
        handler._pending_paths.add(str(tmp_path / "dummy.py"))
        handler._process_batch()

        assert path not in handler._failed_paths

    def test_successful_processing_clears_failure_state(self, tmp_path):
        handler = self._make_handler(tmp_path)

        test_file = tmp_path / "good.py"
        test_file.write_text("x = 1")

        handler._failed_paths.add(str(test_file))
        handler._failure_counts[str(test_file)] = 1

        handler._pending_paths.add(str(test_file))

        # Mock _is_file_stable to return True
        with patch.object(handler, '_is_file_stable', return_value=True):
            handler._process_batch()

        assert str(test_file) not in handler._failed_paths
        assert str(test_file) not in handler._failure_counts


# ---------------------------------------------------------------------------
# Adaptive debounce
# ---------------------------------------------------------------------------

class TestAdaptiveDebounce:
    """Tests for adaptive debounce scaling."""

    def _make_handler(self, tmp_path, default_debounce=5.0):
        from codegraphcontext.core.watcher import RepositoryEventHandler

        mock_gb = MagicMock()
        mock_gb.parsers = {".py": MagicMock()}
        mock_gb._pre_scan_for_imports.return_value = {}
        mock_gb._create_all_function_calls.return_value = None
        mock_gb._create_all_inheritance_links.return_value = None
        mock_gb.parse_file.return_value = {"path": "x", "functions": [], "classes": [], "imports": []}
        mock_gb.update_file_in_graph.return_value = None
        mock_gb.delete_edges_for_file.return_value = None

        with patch.dict(os.environ, {
            'CGC_HEALTH_DIR': str(tmp_path / "health"),
            'CGC_DEBOUNCE_SECONDS': str(default_debounce),
        }):
            handler = RepositoryEventHandler(
                mock_gb, tmp_path, perform_initial_scan=False
            )
            if handler._health_timer:
                handler._health_timer.cancel()
            if handler._reconcile_timer:
                handler._reconcile_timer.cancel()
        return handler

    def test_large_batch_increases_debounce(self, tmp_path):
        handler = self._make_handler(tmp_path, default_debounce=5.0)
        original = handler.debounce_interval

        # Simulate a large batch (> 20 files)
        for i in range(25):
            f = tmp_path / f"file_{i}.py"
            f.write_text(f"x = {i}")
            handler._pending_paths.add(str(f))

        with patch.object(handler, '_is_file_stable', return_value=True):
            handler._process_batch()

        assert handler.debounce_interval > original
        assert handler.debounce_interval <= 30.0

    def test_small_batch_decreases_debounce(self, tmp_path):
        handler = self._make_handler(tmp_path, default_debounce=5.0)
        handler.debounce_interval = 15.0  # Artificially high

        f = tmp_path / "single.py"
        f.write_text("x = 1")
        handler._pending_paths.add(str(f))

        with patch.object(handler, '_is_file_stable', return_value=True):
            handler._process_batch()

        assert handler.debounce_interval < 15.0
        assert handler.debounce_interval >= 5.0


# ---------------------------------------------------------------------------
# File stability check
# ---------------------------------------------------------------------------

class TestFileStability:
    """Tests for _is_file_stable."""

    def test_stable_file_returns_true(self, tmp_path):
        from codegraphcontext.core.watcher import RepositoryEventHandler
        f = tmp_path / "stable.py"
        f.write_text("# stable")

        # Use very short wait to keep test fast
        assert RepositoryEventHandler._is_file_stable(f, wait_ms=50) is True

    def test_missing_file_returns_false(self, tmp_path):
        from codegraphcontext.core.watcher import RepositoryEventHandler
        f = tmp_path / "nonexistent.py"
        assert RepositoryEventHandler._is_file_stable(f, wait_ms=50) is False


# ---------------------------------------------------------------------------
# .gitignore / _should_ignore
# ---------------------------------------------------------------------------

class TestShouldIgnore:
    """Tests for gitignore + IGNORE_DIRS filtering."""

    def _make_handler(self, tmp_path, gitignore_content=None):
        from codegraphcontext.core.watcher import RepositoryEventHandler

        if gitignore_content:
            (tmp_path / ".gitignore").write_text(gitignore_content)

        mock_gb = MagicMock()
        mock_gb.parsers = {".py": MagicMock()}
        mock_gb._pre_scan_for_imports.return_value = {}
        mock_gb._create_all_function_calls.return_value = None
        mock_gb._create_all_inheritance_links.return_value = None

        with patch.dict(os.environ, {'CGC_HEALTH_DIR': str(tmp_path / "health")}):
            handler = RepositoryEventHandler(
                mock_gb, tmp_path, perform_initial_scan=False
            )
            if handler._health_timer:
                handler._health_timer.cancel()
            if handler._reconcile_timer:
                handler._reconcile_timer.cancel()
        return handler

    def test_ignores_pycache(self, tmp_path):
        handler = self._make_handler(tmp_path)
        assert handler._should_ignore(str(tmp_path / "__pycache__" / "mod.pyc")) is True

    def test_ignores_pyc_files(self, tmp_path):
        handler = self._make_handler(tmp_path)
        assert handler._should_ignore(str(tmp_path / "module.pyc")) is True

    def test_ignores_node_modules(self, tmp_path):
        handler = self._make_handler(tmp_path)
        assert handler._should_ignore(str(tmp_path / "node_modules" / "pkg" / "index.js")) is True

    def test_ignores_gitignore_patterns(self, tmp_path):
        handler = self._make_handler(tmp_path, gitignore_content="*.log\nbuild/")
        assert handler._should_ignore(str(tmp_path / "app.log")) is True

    def test_does_not_ignore_normal_files(self, tmp_path):
        handler = self._make_handler(tmp_path)
        assert handler._should_ignore(str(tmp_path / "app.py")) is False


# ---------------------------------------------------------------------------
# Neo4j retry wrapper
# ---------------------------------------------------------------------------

class TestNeo4jRetryWrapper:
    """Tests for DatabaseManager.execute_with_retry."""

    def test_succeeds_first_try(self):
        from codegraphcontext.core.database import DatabaseManager
        dm = DatabaseManager.__new__(DatabaseManager)
        result = dm.execute_with_retry(lambda: 42)
        assert result == 42

    def test_retries_on_transient_error(self):
        from codegraphcontext.core.database import DatabaseManager
        from neo4j.exceptions import ServiceUnavailable

        dm = DatabaseManager.__new__(DatabaseManager)
        call_count = 0

        def flaky():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ServiceUnavailable("gone")
            return "ok"

        result = dm.execute_with_retry(flaky, max_retries=3, backoff=0.01)
        assert result == "ok"
        assert call_count == 3

    def test_raises_after_max_retries(self):
        from codegraphcontext.core.database import DatabaseManager
        from neo4j.exceptions import ServiceUnavailable

        dm = DatabaseManager.__new__(DatabaseManager)

        def always_fails():
            raise ServiceUnavailable("down")

        with pytest.raises(ServiceUnavailable):
            dm.execute_with_retry(always_fails, max_retries=2, backoff=0.01)
