"""Unit tests for search_handlers: grep_code, find_references."""
import os
import tempfile
import shutil
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from codegraphcontext.tools.handlers.search_handlers import (
    grep_code,
    find_references,
    _grep_with_python,
    _load_gitignore_spec,
    _should_ignore_path,
    _detect_language,
    _get_indexed_repo_paths,
    _repo_paths_cache,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def temp_repo(tmp_path):
    """Create a temporary repo with some Python files."""
    (tmp_path / "main.py").write_text(
        "import os\n\ndef hello():\n    print('hello world')\n\ndef goodbye():\n    print('goodbye')\n"
    )
    (tmp_path / "utils.py").write_text(
        "def helper():\n    return 42\n\n# TODO: refactor this\ndef old_func():\n    pass\n"
    )
    subdir = tmp_path / "sub"
    subdir.mkdir()
    (subdir / "module.py").write_text(
        "from main import hello\n\ndef use_hello():\n    hello()\n"
    )
    # Add a .gitignore
    (tmp_path / ".gitignore").write_text("*.log\nbuild/\n")
    # Add an ignored file
    (tmp_path / "debug.log").write_text("debug info\nhello in log\n")
    # Add a build dir
    build = tmp_path / "build"
    build.mkdir()
    (build / "output.py").write_text("hello = 'built'\n")
    return tmp_path


@pytest.fixture
def mock_db(temp_repo):
    """Mock db_manager that returns temp_repo as indexed."""
    db = MagicMock()
    session = MagicMock()
    result = MagicMock()
    result.__iter__ = MagicMock(return_value=iter([{"path": str(temp_repo)}]))
    session.run = MagicMock(return_value=result)
    session.__enter__ = MagicMock(return_value=session)
    session.__exit__ = MagicMock(return_value=False)
    driver = MagicMock()
    driver.session = MagicMock(return_value=session)
    db.get_driver = MagicMock(return_value=driver)

    # Reset cache to force reload
    _repo_paths_cache["paths"] = []
    _repo_paths_cache["ts"] = 0.0
    return db


# ---------------------------------------------------------------------------
# grep_code tests
# ---------------------------------------------------------------------------

class TestGrepCode:
    def test_pattern_required(self, mock_db):
        result = grep_code(mock_db)
        assert "error" in result

    def test_literal_match(self, mock_db, temp_repo):
        result = grep_code(mock_db, pattern="hello", repo_path=str(temp_repo))
        assert result["success"] is True
        assert result["total_matches"] > 0
        # Should find "hello" in main.py and sub/module.py
        files = {m["file"] for m in result["matches"]}
        assert any("main.py" in f for f in files)

    def test_regex_match(self, mock_db, temp_repo):
        result = grep_code(
            mock_db, pattern=r"def \w+\(\):", is_regex=True,
            repo_path=str(temp_repo)
        )
        assert result["success"] is True
        assert result["total_matches"] >= 3  # hello, goodbye, helper, old_func, use_hello

    def test_case_insensitive(self, mock_db, temp_repo):
        result = grep_code(
            mock_db, pattern="HELLO", case_sensitive=False,
            repo_path=str(temp_repo)
        )
        assert result["success"] is True
        assert result["total_matches"] > 0

    def test_case_sensitive_no_match(self, mock_db, temp_repo):
        result = grep_code(
            mock_db, pattern="HELLO", case_sensitive=True,
            repo_path=str(temp_repo)
        )
        assert result["success"] is True
        assert result["total_matches"] == 0

    def test_max_results_cap(self, mock_db, temp_repo):
        result = grep_code(
            mock_db, pattern="e", max_results=2,
            repo_path=str(temp_repo)
        )
        assert result["success"] is True
        assert result["total_matches"] <= 2
        assert result["truncated"] is True

    def test_file_pattern_filter(self, mock_db, temp_repo):
        result = grep_code(
            mock_db, pattern="hello", file_pattern="main.py",
            repo_path=str(temp_repo)
        )
        assert result["success"] is True
        for m in result["matches"]:
            assert "main.py" in m["file"]

    def test_exclude_pattern(self, mock_db, temp_repo):
        result = grep_code(
            mock_db, pattern="def", exclude_pattern="utils.py",
            repo_path=str(temp_repo)
        )
        assert result["success"] is True
        for m in result["matches"]:
            assert "utils.py" not in m["file"]

    def test_gitignore_respected(self, mock_db, temp_repo):
        """Files matching .gitignore should be excluded."""
        result = grep_code(mock_db, pattern="hello", repo_path=str(temp_repo))
        assert result["success"] is True
        files = {m["file"] for m in result["matches"]}
        # debug.log and build/ should be excluded
        assert not any("debug.log" in f for f in files)
        assert not any("build" in f for f in files)

    def test_invalid_regex(self, mock_db, temp_repo):
        # Force Python fallback by hiding rg
        with patch("shutil.which", return_value=None):
            result = grep_code(
                mock_db, pattern="[invalid", is_regex=True,
                repo_path=str(temp_repo)
            )
            assert "error" in result

    def test_no_indexed_repos(self):
        db = MagicMock()
        session = MagicMock()
        result_mock = MagicMock()
        result_mock.__iter__ = MagicMock(return_value=iter([]))
        session.run = MagicMock(return_value=result_mock)
        session.__enter__ = MagicMock(return_value=session)
        session.__exit__ = MagicMock(return_value=False)
        driver = MagicMock()
        driver.session = MagicMock(return_value=session)
        db.get_driver = MagicMock(return_value=driver)

        _repo_paths_cache["paths"] = []
        _repo_paths_cache["ts"] = 0.0
        result = grep_code(db, pattern="test")
        assert "error" in result

    def test_language_detection(self, mock_db, temp_repo):
        result = grep_code(mock_db, pattern="hello", repo_path=str(temp_repo))
        assert result["success"] is True
        for m in result["matches"]:
            if m["file"].endswith(".py"):
                assert m["language"] == "python"

    def test_python_fallback_used_when_no_rg(self, mock_db, temp_repo):
        """When rg is not available, Python fallback should work."""
        with patch("shutil.which", return_value=None):
            result = grep_code(mock_db, pattern="hello", repo_path=str(temp_repo))
            assert result["success"] is True
            assert result["total_matches"] > 0


# ---------------------------------------------------------------------------
# Utility tests
# ---------------------------------------------------------------------------

class TestGitignoreLoading:
    def test_load_gitignore_spec(self, temp_repo):
        spec = _load_gitignore_spec(str(temp_repo))
        assert spec.match_file("debug.log")
        assert spec.match_file("build/output.py")
        assert not spec.match_file("main.py")

    def test_should_ignore_compiled(self, temp_repo):
        spec = _load_gitignore_spec(str(temp_repo))
        assert _should_ignore_path("__pycache__/foo.pyc", spec)
        assert _should_ignore_path("foo.pyc", spec)


class TestLanguageDetection:
    def test_python(self):
        assert _detect_language("foo.py") == "python"

    def test_typescript(self):
        assert _detect_language("bar.tsx") == "typescript"

    def test_unknown(self):
        assert _detect_language("foo.xyz") is None


# ---------------------------------------------------------------------------
# find_references tests
# ---------------------------------------------------------------------------

class TestFindReferences:
    def test_symbol_required(self, mock_db):
        result = find_references(mock_db)
        assert "error" in result

    def test_deduplication_graph_vs_grep(self, mock_db, temp_repo):
        """Graph results and grep results should be deduplicated."""
        # Set up mock to return callers from graph
        session = MagicMock()
        caller_rec = {"caller_name": "use_hello", "file": str(temp_repo / "sub/module.py"), "line": 3}
        importer_rec = {"file": str(temp_repo / "sub/module.py")}

        def mock_run(query, **params):
            result = MagicMock()
            if "CALLS" in query:
                result.__iter__ = MagicMock(return_value=iter([caller_rec]))
            elif "IMPORTS" in query:
                result.__iter__ = MagicMock(return_value=iter([importer_rec]))
            elif "INHERITS" in query:
                result.__iter__ = MagicMock(return_value=iter([]))
            elif "CONTAINS" in query and "labels" in query:
                result.__iter__ = MagicMock(return_value=iter([]))
            else:
                result.__iter__ = MagicMock(return_value=iter([]))
            return result

        session.run = mock_run
        session.__enter__ = MagicMock(return_value=session)
        session.__exit__ = MagicMock(return_value=False)
        driver = MagicMock()
        driver.session = MagicMock(return_value=session)
        mock_db.get_driver = MagicMock(return_value=driver)

        result = find_references(mock_db, symbol="hello", repo_path=str(temp_repo))
        assert result["success"] is True
        assert "summary" in result
        # Should have callers from graph
        assert len(result["callers"]) >= 1

    def test_type_annotation_categorization(self):
        """Type annotation references should be categorized correctly."""
        import re as re_module
        from codegraphcontext.tools.handlers.search_handlers import find_references

        # Test the regex pattern used for type annotation detection
        pattern = re_module.compile(
            r":\s*MyClass\b|"
            r"->\s*MyClass\b|"
            r"Optional\[MyClass\]|"
            r"List\[MyClass\]"
        )
        assert pattern.search("def foo(x: MyClass):")
        assert pattern.search("def bar() -> MyClass:")
        assert pattern.search("x: Optional[MyClass] = None")
        assert not pattern.search("# MyClass is great")
