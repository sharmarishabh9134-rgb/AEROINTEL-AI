from pathlib import Path
from tempfile import TemporaryDirectory

from backend.app.config import Settings
from backend.app.database import make_engine


def test_local_default_remains_sqlite(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    settings = Settings(_env_file=None)
    engine = make_engine(settings.database_url)
    try:
        assert engine.dialect.name == "sqlite"
        assert engine.dialect.driver == "pysqlite"
    finally:
        engine.dispose()


def test_dotenv_postgresql_url_uses_psycopg_without_fallback(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("DEMO_MODE", raising=False)
    with TemporaryDirectory(dir=Path.cwd()) as temp_dir:
        dotenv = Path(temp_dir) / ".env"
        dotenv.write_text(
            "DATABASE_URL=postgresql://db.example.invalid/aerointel_preview\n"
            "DEMO_MODE=false\n",
            encoding="utf-8",
        )
        settings = Settings(_env_file=dotenv)
        engine = make_engine(settings.database_url)
        try:
            assert settings.demo_mode is False
            assert engine.dialect.name == "postgresql"
            assert engine.dialect.driver == "psycopg"
            assert engine.pool.size() == 1
            assert engine.pool._max_overflow == 0
        finally:
            engine.dispose()


def test_postgres_alias_uses_psycopg3():
    engine = make_engine("postgres://db.example.invalid/aerointel_preview")
    try:
        assert engine.dialect.name == "postgresql"
        assert engine.dialect.driver == "psycopg"
    finally:
        engine.dispose()


def test_unsupported_database_url_fails_instead_of_falling_back():
    import pytest

    with pytest.raises(ValueError, match="SQLite or PostgreSQL with Psycopg 3"):
        make_engine("mysql://db.example.invalid/aerointel")


def test_empty_database_url_fails_instead_of_falling_back():
    import pytest

    with pytest.raises(ValueError, match="DATABASE_URL must not be empty"):
        make_engine("   ")
