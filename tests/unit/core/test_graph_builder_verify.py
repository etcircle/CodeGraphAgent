from pathlib import Path
from unittest.mock import patch

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
