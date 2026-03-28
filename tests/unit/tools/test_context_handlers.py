"""Unit tests for context_handlers: get_function_context, get_module_overview, explain_path."""
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from codegraphcontext.tools.handlers.context_handlers import (
    get_function_context,
    get_module_overview,
    explain_path,
    _read_source_from_file,
    _parse_endpoint_decorator,
)
from codegraphcontext.tools.handlers.search_handlers import _repo_paths_cache


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def temp_source(tmp_path):
    """Create a Python file with known content for source reading tests."""
    src = tmp_path / "example.py"
    src.write_text(
        "class MyService:\n"
        "    def __init__(self):\n"
        "        self.value = 0\n"
        "\n"
        "    def process(self, data):\n"
        "        return data * 2\n"
        "\n"
        "    def validate(self, data):\n"
        "        return data is not None\n"
        "\n"
        "def standalone():\n"
        "    svc = MyService()\n"
        "    return svc.process(42)\n"
    )
    return tmp_path, src


@pytest.fixture
def mock_db_for_context(temp_source):
    """Mock db_manager with graph data for function context tests."""
    tmp_path, src = temp_source
    db = MagicMock()

    # Reset cache
    _repo_paths_cache["paths"] = [str(tmp_path)]
    _repo_paths_cache["ts"] = 9999999999.0

    fn_record = {
        "name": "process",
        "path": str(src),
        "line_number": 5,
        "end_line": 6,
        "args": "self, data",
        "decorators": None,
        "class_name": "MyService",
        "complexity": 1,
    }

    sibling_record = {
        "name": "validate",
        "args": "self, data",
        "line": 8,
        "decorators": None,
    }

    caller_record = {
        "name": "standalone",
        "file": str(src),
        "line": 11,
    }

    def mock_run(query, **params):
        result = MagicMock()
        if "fn.name = $name" in query and "CONTAINS" in query:
            result.__iter__ = MagicMock(return_value=iter([fn_record]))
            result.data = MagicMock(return_value=[fn_record])
        elif "sib.context" in query:
            result.__iter__ = MagicMock(return_value=iter([sibling_record]))
        elif "CALLS" in query and "caller" in query.lower():
            result.__iter__ = MagicMock(return_value=iter([caller_record]))
        elif "CALLS" in query:
            result.__iter__ = MagicMock(return_value=iter([]))
        else:
            result.__iter__ = MagicMock(return_value=iter([]))
        return result

    session = MagicMock()
    session.run = mock_run
    session.__enter__ = MagicMock(return_value=session)
    session.__exit__ = MagicMock(return_value=False)
    driver = MagicMock()
    driver.session = MagicMock(return_value=session)
    db.get_driver = MagicMock(return_value=driver)
    return db, tmp_path, src


# ---------------------------------------------------------------------------
# _read_source_from_file tests
# ---------------------------------------------------------------------------

class TestReadSourceFromFile:
    def test_reads_correct_lines(self, temp_source):
        _, src = temp_source
        source = _read_source_from_file(str(src), 5, 6)
        assert "def process" in source
        assert "return data * 2" in source

    def test_handles_missing_end_line(self, temp_source):
        _, src = temp_source
        source = _read_source_from_file(str(src), 1, None)
        assert source is not None
        assert "class MyService" in source

    def test_handles_missing_file(self):
        source = _read_source_from_file("/nonexistent/file.py", 1, 10)
        assert source is None


# ---------------------------------------------------------------------------
# get_function_context tests
# ---------------------------------------------------------------------------

class TestGetFunctionContext:
    def test_function_name_required(self):
        db = MagicMock()
        result = get_function_context(db)
        assert "error" in result

    def test_single_match_with_source(self, mock_db_for_context):
        db, tmp_path, src = mock_db_for_context
        result = get_function_context(db, function_name="process")
        assert result["success"] is True
        assert result["found"] is True
        assert result["match_count"] == 1
        fn = result["functions"][0]
        assert fn["name"] == "process"
        assert fn["class_name"] == "MyService"
        # Source should come from filesystem
        assert "def process" in fn["source"]

    def test_sibling_methods_included(self, mock_db_for_context):
        db, tmp_path, src = mock_db_for_context
        result = get_function_context(db, function_name="process")
        fn = result["functions"][0]
        assert "sibling_methods" in fn
        assert len(fn["sibling_methods"]) >= 1
        sibling_names = [s["name"] for s in fn["sibling_methods"]]
        assert "validate" in sibling_names

    def test_without_source(self, mock_db_for_context):
        db, tmp_path, src = mock_db_for_context
        result = get_function_context(db, function_name="process", include_source=False)
        fn = result["functions"][0]
        assert "source" not in fn

    def test_not_found(self):
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

        result = get_function_context(db, function_name="nonexistent_fn")
        assert result["success"] is True
        assert result["found"] is False

    def test_callers_returned(self, mock_db_for_context):
        db, tmp_path, src = mock_db_for_context
        result = get_function_context(db, function_name="process", caller_depth=1)
        fn = result["functions"][0]
        assert "callers" in fn
        assert len(fn["callers"]) >= 1

    def test_no_siblings_when_not_method(self):
        """Functions without class_name should not have sibling_methods."""
        db = MagicMock()
        fn_record = {
            "name": "standalone",
            "path": "/tmp/test.py",
            "line_number": 1,
            "end_line": 3,
            "args": "",
            "decorators": None,
            "class_name": None,
            "complexity": 1,
        }
        session = MagicMock()

        def mock_run(query, **params):
            r = MagicMock()
            if "fn.name = $name" in query:
                r.__iter__ = MagicMock(return_value=iter([fn_record]))
            else:
                r.__iter__ = MagicMock(return_value=iter([]))
            return r

        session.run = mock_run
        session.__enter__ = MagicMock(return_value=session)
        session.__exit__ = MagicMock(return_value=False)
        driver = MagicMock()
        driver.session = MagicMock(return_value=session)
        db.get_driver = MagicMock(return_value=driver)

        result = get_function_context(
            db, function_name="standalone", include_source=False
        )
        fn = result["functions"][0]
        assert "sibling_methods" not in fn


# ---------------------------------------------------------------------------
# _parse_endpoint_decorator tests
# ---------------------------------------------------------------------------

class TestParseEndpointDecorator:
    def test_get_route(self):
        method, route = _parse_endpoint_decorator(["@router.get('/users')"])
        assert method == "GET"
        assert route == "/users"

    def test_post_route(self):
        method, route = _parse_endpoint_decorator(["@app.post(\"/items\")"])
        assert method == "POST"
        assert route == "/items"

    def test_no_decorators(self):
        method, route = _parse_endpoint_decorator([])
        assert method == "UNKNOWN"

    def test_none_decorators(self):
        method, route = _parse_endpoint_decorator(None)
        assert method == "UNKNOWN"


# ---------------------------------------------------------------------------
# get_module_overview tests
# ---------------------------------------------------------------------------

class TestGetModuleOverview:
    def test_module_path_required(self):
        db = MagicMock()
        result = get_module_overview(db)
        assert "error" in result

    def test_empty_module(self):
        db = MagicMock()
        session = MagicMock()

        def mock_run(query, **params):
            r = MagicMock()
            if "count" in query.lower():
                single = MagicMock()
                single.__getitem__ = lambda self, k: 0
                r.single = MagicMock(return_value=single)
            r.__iter__ = MagicMock(return_value=iter([]))
            return r

        session.run = mock_run
        session.__enter__ = MagicMock(return_value=session)
        session.__exit__ = MagicMock(return_value=False)
        driver = MagicMock()
        driver.session = MagicMock(return_value=session)
        db.get_driver = MagicMock(return_value=driver)

        result = get_module_overview(db, module_path="/tmp/empty_module")
        assert result["success"] is True
        assert result["endpoints"] == []

    def test_endpoints_detected(self):
        db = MagicMock()
        session = MagicMock()

        endpoint_rec = {
            "name": "list_users",
            "decorators": ["@router.get('/users')"],
            "line": 10,
            "file": "/app/api/users.py",
            "args": "request",
        }

        def mock_run(query, **params):
            r = MagicMock()
            if "decorators" in query:
                r.__iter__ = MagicMock(return_value=iter([endpoint_rec]))
            elif "count" in query.lower():
                single = MagicMock()
                single.__getitem__ = lambda self, k: {"files": 5, "functions": 10, "classes": 2}.get(k, 0)
                r.single = MagicMock(return_value=single)
            else:
                r.__iter__ = MagicMock(return_value=iter([]))
            return r

        session.run = mock_run
        session.__enter__ = MagicMock(return_value=session)
        session.__exit__ = MagicMock(return_value=False)
        driver = MagicMock()
        driver.session = MagicMock(return_value=session)
        db.get_driver = MagicMock(return_value=driver)

        result = get_module_overview(db, module_path="/app/api")
        assert result["success"] is True
        assert len(result["endpoints"]) == 1
        assert result["endpoints"][0]["method"] == "GET"
        assert result["endpoints"][0]["route"] == "/users"


# ---------------------------------------------------------------------------
# explain_path tests
# ---------------------------------------------------------------------------

class TestExplainPath:
    def test_from_function_required(self):
        db = MagicMock()
        result = explain_path(db, to_function="bar")
        assert "error" in result

    def test_to_function_required(self):
        db = MagicMock()
        result = explain_path(db, from_function="foo")
        assert "error" in result

    def test_direct_path_found(self):
        db = MagicMock()
        session = MagicMock()

        chain_data = [
            {"name": "foo", "path": "/app/a.py", "line": 1, "idx": 0},
            {"name": "bar", "path": "/app/b.py", "line": 5, "idx": 1},
        ]
        path_record = {"chain": chain_data, "hops": 1}

        def mock_run(query, **params):
            r = MagicMock()
            if "shortestPath" in query and params.get("from_name") == "foo":
                r.__iter__ = MagicMock(return_value=iter([path_record]))
                return r
            r.__iter__ = MagicMock(return_value=iter([]))
            return r

        session.run = mock_run
        session.__enter__ = MagicMock(return_value=session)
        session.__exit__ = MagicMock(return_value=False)
        driver = MagicMock()
        driver.session = MagicMock(return_value=session)
        db.get_driver = MagicMock(return_value=driver)

        result = explain_path(db, from_function="foo", to_function="bar")
        assert result["success"] is True
        assert result["paths_found"] == 1
        assert result["direction"] == "forward"
        assert result["paths"][0]["hops"] == 1

    def test_no_path_found(self):
        db = MagicMock()
        session = MagicMock()

        def mock_run(query, **params):
            r = MagicMock()
            r.__iter__ = MagicMock(return_value=iter([]))
            return r

        session.run = mock_run
        session.__enter__ = MagicMock(return_value=session)
        session.__exit__ = MagicMock(return_value=False)
        driver = MagicMock()
        driver.session = MagicMock(return_value=session)
        db.get_driver = MagicMock(return_value=driver)

        result = explain_path(db, from_function="foo", to_function="bar")
        assert result["success"] is True
        assert result["paths_found"] == 0

    def test_reverse_path_found(self):
        db = MagicMock()
        session = MagicMock()

        chain_data = [
            {"name": "bar", "path": "/app/b.py", "line": 5, "idx": 0},
            {"name": "foo", "path": "/app/a.py", "line": 1, "idx": 1},
        ]
        path_record = {"chain": chain_data, "hops": 1}

        call_count = [0]

        def mock_run(query, **params):
            r = MagicMock()
            call_count[0] += 1
            # First call (forward) returns nothing, second call (reverse) returns path
            if call_count[0] <= 1:
                r.__iter__ = MagicMock(return_value=iter([]))
            else:
                r.__iter__ = MagicMock(return_value=iter([path_record]))
            return r

        session.run = mock_run
        session.__enter__ = MagicMock(return_value=session)
        session.__exit__ = MagicMock(return_value=False)
        driver = MagicMock()
        driver.session = MagicMock(return_value=session)
        db.get_driver = MagicMock(return_value=driver)

        result = explain_path(db, from_function="foo", to_function="bar")
        assert result["success"] is True
        assert "reverse" in result["direction"]

    def test_multiple_paths(self):
        db = MagicMock()
        session = MagicMock()

        path1 = {"chain": [{"name": "a", "path": "/x.py", "line": 1, "idx": 0},
                           {"name": "c", "path": "/z.py", "line": 3, "idx": 1}], "hops": 1}
        path2 = {"chain": [{"name": "a", "path": "/x.py", "line": 1, "idx": 0},
                           {"name": "b", "path": "/y.py", "line": 2, "idx": 1},
                           {"name": "c", "path": "/z.py", "line": 3, "idx": 2}], "hops": 2}

        def mock_run(query, **params):
            r = MagicMock()
            if params.get("from_name") == "a":
                r.__iter__ = MagicMock(return_value=iter([path1, path2]))
            else:
                r.__iter__ = MagicMock(return_value=iter([]))
            return r

        session.run = mock_run
        session.__enter__ = MagicMock(return_value=session)
        session.__exit__ = MagicMock(return_value=False)
        driver = MagicMock()
        driver.session = MagicMock(return_value=session)
        db.get_driver = MagicMock(return_value=driver)

        result = explain_path(db, from_function="a", to_function="c")
        assert result["success"] is True
        assert result["paths_found"] == 2
