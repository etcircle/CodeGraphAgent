from unittest.mock import patch

from codegraphcontext import core


def test_get_database_manager_accepts_explicit_remote_falkordb(monkeypatch) -> None:
    monkeypatch.setenv("DEFAULT_DATABASE", "falkordb-remote")
    monkeypatch.setenv("FALKORDB_HOST", "graph.local")
    monkeypatch.delenv("CGC_RUNTIME_DB_TYPE", raising=False)
    monkeypatch.delenv("DATABASE_TYPE", raising=False)

    sentinel = object()

    with patch(
        "codegraphcontext.core.database_falkordb_remote.FalkorDBRemoteManager",
        return_value=sentinel,
    ):
        assert core.get_database_manager() is sentinel


def test_get_database_manager_prefers_local_falkordb_when_available(monkeypatch) -> None:
    monkeypatch.delenv("CGC_RUNTIME_DB_TYPE", raising=False)
    monkeypatch.delenv("DEFAULT_DATABASE", raising=False)
    monkeypatch.delenv("DATABASE_TYPE", raising=False)
    monkeypatch.delenv("FALKORDB_HOST", raising=False)
    monkeypatch.delenv("NEO4J_URI", raising=False)
    monkeypatch.delenv("NEO4J_USERNAME", raising=False)
    monkeypatch.delenv("NEO4J_PASSWORD", raising=False)

    sentinel = object()

    with patch.object(core, "_is_falkordb_remote_configured", return_value=False), patch.object(
        core,
        "_is_falkordb_available",
        return_value=True,
    ), patch(
        "codegraphcontext.core.database_falkordb.FalkorDBManager",
        return_value=sentinel,
    ):
        assert core.get_database_manager() is sentinel
