from pathlib import Path
from unittest.mock import MagicMock, patch

from codegraphcontext.tools.graph_builder import GraphBuilder


class TestGraphBuilderVerification:
    def _builder(self) -> GraphBuilder:
        builder = GraphBuilder.__new__(GraphBuilder)
        builder.driver = None
        builder.parsers = {}
        return builder

    def test_collect_indexable_file_paths_honours_ignore_dirs_and_cgcignore(self, tmp_path):
        (tmp_path / "app.py").write_text("print('hi')")
        (tmp_path / "README.md").write_text("# hello")
        (tmp_path / "image.png").write_text("fake")
        (tmp_path / "notes.secret").write_text("ignore me")
        (tmp_path / "logs").mkdir()
        (tmp_path / "logs" / "runtime.txt").write_text("ignore me")
        (tmp_path / "node_modules").mkdir()
        (tmp_path / "node_modules" / "lib.js").write_text("console.log('nope')")
        (tmp_path / ".cgcignore").write_text("logs/\n*.secret\n")

        builder = self._builder()
        with patch("codegraphcontext.tools.graph_builder.get_config_value", return_value="node_modules,__pycache__"):
            files = builder.collect_indexable_file_paths(tmp_path)

        rel_paths = {str(path.relative_to(tmp_path)) for path in files}
        assert rel_paths == {".cgcignore", "README.md", "app.py"}

    def test_verify_repository_index_reports_missing_and_extra_files(self, tmp_path):
        builder = self._builder()
        app = (tmp_path / "app.py").resolve()
        missing = (tmp_path / "missing.py").resolve()
        extra = (tmp_path / "extra.md").resolve()

        builder.collect_indexable_file_paths = lambda path: [app, missing]
        builder.get_indexed_file_paths = lambda path: {str(app), str(extra)}

        verification = builder.verify_repository_index(tmp_path, sample_limit=5)

        assert verification["expected_count"] == 2
        assert verification["indexed_count"] == 2
        assert verification["missing_count"] == 1
        assert verification["extra_count"] == 1
        assert verification["missing_paths"] == [str(missing)]
        assert verification["extra_paths"] == [str(extra)]
        assert verification["is_clean"] is False

    def test_add_file_to_graph_batches_large_file_writes(self, tmp_path):
        builder = self._builder()
        repo_path = tmp_path.resolve()
        file_path = repo_path / "backend" / "tests" / "heavy_file.py"

        session = MagicMock()
        session.__enter__.return_value = session
        session.__exit__.return_value = False

        def run_side_effect(query, **params):
            result = MagicMock()
            if "RETURN r.path as path" in query:
                result.single.return_value = {"path": str(repo_path)}
            else:
                result.single.return_value = None
            return result

        session.run.side_effect = run_side_effect
        builder.driver = MagicMock()
        builder.driver.session.return_value = session

        file_data = {
            "path": str(file_path),
            "repo_path": str(repo_path),
            "lang": "python",
            "is_dependency": False,
            "functions": [
                {
                    "name": f"fn_{i}",
                    "line_number": i + 1,
                    "args": ["arg_a", "arg_b"],
                    "cyclomatic_complexity": 1,
                    "decorators": [],
                    "context": None,
                    "context_type": None,
                    "class_context": None,
                    "lang": "python",
                    "is_dependency": False,
                }
                for i in range(10)
            ],
            "classes": [
                {"name": f"Class{i}", "line_number": 100 + i, "bases": [], "lang": "python"}
                for i in range(4)
            ],
            "variables": [
                {"name": f"var_{i}", "line_number": 200 + i, "lang": "python"}
                for i in range(12)
            ],
            "imports": [
                {"name": f"pkg.module_{i}", "full_import_name": f"pkg.module_{i}", "line_number": 300 + i}
                for i in range(6)
            ],
            "modules": [],
            "traits": [],
            "interfaces": [],
            "macros": [],
            "structs": [],
            "enums": [],
            "unions": [],
            "records": [],
            "properties": [],
            "module_inclusions": [],
        }

        builder.add_file_to_graph(file_data, repo_path.name, imports_map={})

        assert session.run.call_count <= 15

    def test_create_schema_adds_parameter_uniqueness_constraint(self):
        builder = self._builder()

        session = MagicMock()
        session.__enter__.return_value = session
        session.__exit__.return_value = False
        session.run.return_value = MagicMock()

        builder.driver = MagicMock()
        builder.driver.session.return_value = session

        GraphBuilder.create_schema(builder)

        queries = [call.args[0] for call in session.run.call_args_list]
        assert any(
            "Parameter" in query and "UNIQUE" in query
            for query in queries
        )
