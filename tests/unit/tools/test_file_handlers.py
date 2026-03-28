"""Unit tests for file_handlers: get_file_content, diff_since, get_file_structure."""
import os
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from codegraphcontext.tools.handlers.file_handlers import (
    get_file_content,
    diff_since,
    get_file_structure,
    _parse_since,
    _format_tree,
    _count_files,
)
from codegraphcontext.tools.handlers.search_handlers import _repo_paths_cache


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def temp_repo_with_git(tmp_path):
    """Create a temp repo with git init and a known file."""
    subprocess.run(["git", "init", str(tmp_path)], capture_output=True, timeout=10)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "test@test.com"],
        capture_output=True, timeout=10,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.name", "Test"],
        capture_output=True, timeout=10,
    )
    src = tmp_path / "app.py"
    src.write_text("def main():\n    print('hello')\n")
    subprocess.run(
        ["git", "-C", str(tmp_path), "add", "."],
        capture_output=True, timeout=10,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-m", "initial"],
        capture_output=True, timeout=10,
    )
    return tmp_path, src


@pytest.fixture
def mock_db_file(tmp_path):
    """Mock db_manager for file content tests."""
    src = tmp_path / "code.py"
    src.write_text(
        "line1\nline2\nline3\nline4\nline5\n"
        "line6\nline7\nline8\nline9\nline10\n"
        "line11\nline12\nline13\nline14\nline15\n"
    )

    _repo_paths_cache["paths"] = [str(tmp_path)]
    _repo_paths_cache["ts"] = 9999999999.0

    db = MagicMock()
    return db, tmp_path, src


# ---------------------------------------------------------------------------
# get_file_content tests
# ---------------------------------------------------------------------------

class TestGetFileContent:
    def test_path_required(self):
        db = MagicMock()
        result = get_file_content(db)
        assert "error" in result

    def test_full_file_read(self, mock_db_file):
        db, tmp_path, src = mock_db_file
        result = get_file_content(db, path=str(src))
        assert result["success"] is True
        assert result["total_lines"] == 15
        assert "line1" in result["content"]
        assert "line15" in result["content"]

    def test_line_range(self, mock_db_file):
        db, tmp_path, src = mock_db_file
        result = get_file_content(db, path=str(src), start_line=3, end_line=5)
        assert result["success"] is True
        assert result["start_line"] == 3
        assert result["end_line"] == 5
        assert "line3" in result["content"]
        assert "line5" in result["content"]
        assert "line1" not in result["content"]
        assert "line6" not in result["content"]

    def test_around_line(self, mock_db_file):
        db, tmp_path, src = mock_db_file
        result = get_file_content(db, path=str(src), around_line=8, context_lines=2)
        assert result["success"] is True
        assert result["start_line"] == 6
        assert "line6" in result["content"]
        assert "line10" in result["content"]

    def test_security_rejection(self, mock_db_file):
        db, tmp_path, src = mock_db_file
        result = get_file_content(db, path="/etc/passwd")
        assert "error" in result
        assert "Access denied" in result["error"]

    def test_file_not_found(self, mock_db_file):
        db, tmp_path, src = mock_db_file
        result = get_file_content(db, path=str(tmp_path / "nonexistent.py"))
        assert "error" in result
        assert "not found" in result["error"].lower()

    def test_max_lines_cap(self, mock_db_file):
        db, tmp_path, src = mock_db_file
        result = get_file_content(db, path=str(src), max_lines=3)
        assert result["success"] is True
        lines = result["content"].split("\n")
        assert len(lines) <= 3

    def test_language_detection(self, mock_db_file):
        db, tmp_path, src = mock_db_file
        result = get_file_content(db, path=str(src))
        assert result["language"] == "python"


# ---------------------------------------------------------------------------
# _parse_since tests
# ---------------------------------------------------------------------------

class TestParseSince:
    def test_hours(self):
        assert _parse_since("1h") == "--since=1.hours.ago"

    def test_days(self):
        assert _parse_since("3d") == "--since=3.days.ago"

    def test_weeks(self):
        assert _parse_since("2w") == "--since=2.weeks.ago"

    def test_iso_date(self):
        assert _parse_since("2024-01-15") == "--since=2024-01-15"

    def test_commit_sha(self):
        result = _parse_since("abc1234")
        assert result == "abc1234"  # Passed through as commit ref


# ---------------------------------------------------------------------------
# diff_since tests
# ---------------------------------------------------------------------------

class TestDiffSince:
    def test_repo_path_required(self):
        db = MagicMock()
        result = diff_since(db, since="1h")
        assert "error" in result

    def test_since_required(self):
        db = MagicMock()
        result = diff_since(db, repo_path="/tmp")
        assert "error" in result

    def test_basic_diff(self, temp_repo_with_git):
        tmp_path, src = temp_repo_with_git
        _repo_paths_cache["paths"] = [str(tmp_path)]
        _repo_paths_cache["ts"] = 9999999999.0
        db = MagicMock()

        # Make a change and commit
        src.write_text("def main():\n    print('updated')\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "."],
            capture_output=True, timeout=10,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "update"],
            capture_output=True, timeout=10,
        )

        result = diff_since(db, repo_path=str(tmp_path), since="1d")
        assert result["success"] is True
        assert "commits" in result

    def test_uncommitted_changes(self, temp_repo_with_git):
        tmp_path, src = temp_repo_with_git
        _repo_paths_cache["paths"] = [str(tmp_path)]
        _repo_paths_cache["ts"] = 9999999999.0
        db = MagicMock()

        # Make uncommitted change
        src.write_text("def main():\n    print('dirty')\n")

        result = diff_since(
            db, repo_path=str(tmp_path), since="1d",
            include_uncommitted=True
        )
        assert result["success"] is True
        assert "uncommitted" in result
        # Should have at least one uncommitted change
        assert len(result["uncommitted"]) >= 1

    def test_extension_filtering(self, temp_repo_with_git):
        tmp_path, src = temp_repo_with_git
        _repo_paths_cache["paths"] = [str(tmp_path)]
        _repo_paths_cache["ts"] = 9999999999.0
        db = MagicMock()

        # Add a non-Python file
        (tmp_path / "readme.md").write_text("# Hello\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "."],
            capture_output=True, timeout=10,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "add readme"],
            capture_output=True, timeout=10,
        )

        result = diff_since(
            db, repo_path=str(tmp_path), since="1d",
            extensions=["py"]
        )
        assert result["success"] is True
        # Only .py files should be in changed_files
        for f in result["changed_files"]:
            assert f["file"].endswith(".py")

    def test_security_rejection(self):
        _repo_paths_cache["paths"] = ["/some/repo"]
        _repo_paths_cache["ts"] = 9999999999.0
        db = MagicMock()

        result = diff_since(db, repo_path="/etc", since="1d")
        assert "error" in result


# ---------------------------------------------------------------------------
# get_file_structure tests
# ---------------------------------------------------------------------------

class TestGetFileStructure:
    def test_repo_path_required(self):
        db = MagicMock()
        result = get_file_structure(db)
        assert "error" in result

    def test_basic_tree(self):
        db = MagicMock()
        session = MagicMock()

        repo = "/myrepo"
        _repo_paths_cache["paths"] = [repo]
        _repo_paths_cache["ts"] = 9999999999.0

        file_records = [
            {"path": f"{repo}/src/main.py"},
            {"path": f"{repo}/src/utils.py"},
            {"path": f"{repo}/tests/test_main.py"},
        ]

        count_records = [
            {"path": f"{repo}/src/main.py", "labels": ["Function"], "cnt": 5},
            {"path": f"{repo}/src/main.py", "labels": ["Class"], "cnt": 1},
            {"path": f"{repo}/src/utils.py", "labels": ["Function"], "cnt": 3},
        ]

        def mock_run(query, **params):
            r = MagicMock()
            if "count(n)" in query or "count(DISTINCT" in query.lower():
                r.__iter__ = MagicMock(return_value=iter(count_records))
            else:
                r.__iter__ = MagicMock(return_value=iter(file_records))
            return r

        session.run = mock_run
        session.__enter__ = MagicMock(return_value=session)
        session.__exit__ = MagicMock(return_value=False)
        driver = MagicMock()
        driver.session = MagicMock(return_value=session)
        db.get_driver = MagicMock(return_value=driver)

        result = get_file_structure(db, repo_path=repo)
        assert result["success"] is True
        assert "tree" in result
        assert "src" in result["tree"]
        assert "tests" in result["tree"]

    def test_security_rejection(self):
        _repo_paths_cache["paths"] = ["/safe/repo"]
        _repo_paths_cache["ts"] = 9999999999.0
        db = MagicMock()

        result = get_file_structure(db, repo_path="/etc")
        assert "error" in result


# ---------------------------------------------------------------------------
# Tree formatting tests
# ---------------------------------------------------------------------------

class TestFormatTree:
    def test_simple_tree(self):
        tree = {
            "main.py": {"__file__": True, "functions": 3, "classes": 1},
            "utils.py": {"__file__": True, "functions": 2},
        }
        lines = _format_tree(tree, "", True)
        assert len(lines) == 2
        assert "main.py" in lines[0]
        assert "3f" in lines[0]
        assert "1c" in lines[0]

    def test_nested_tree(self):
        tree = {
            "src": {
                "app.py": {"__file__": True, "functions": 5},
            },
        }
        lines = _format_tree(tree, "", True)
        assert any("src/" in line for line in lines)
        assert any("app.py" in line for line in lines)


class TestCountFiles:
    def test_flat(self):
        tree = {
            "a.py": {"__file__": True},
            "b.py": {"__file__": True},
        }
        assert _count_files(tree) == 2

    def test_nested(self):
        tree = {
            "dir": {
                "a.py": {"__file__": True},
                "sub": {
                    "b.py": {"__file__": True},
                },
            },
        }
        assert _count_files(tree) == 2
