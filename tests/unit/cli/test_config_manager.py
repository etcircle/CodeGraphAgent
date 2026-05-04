from codegraphcontext.cli import config_manager


def test_default_database_allows_falkordb_remote_backend() -> None:
    is_valid, error = config_manager.validate_config_value(
        "DEFAULT_DATABASE",
        "falkordb-remote",
    )

    assert is_valid is True
    assert error is None


def test_default_config_stays_local_first() -> None:
    assert config_manager.DEFAULT_CONFIG["DEFAULT_DATABASE"] == "falkordb"
