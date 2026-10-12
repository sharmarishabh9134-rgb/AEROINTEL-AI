from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from backend.app.config import get_settings

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Base(DeclarativeBase):
    pass


def make_engine(url: str):
    url = url.strip()
    if not url:
        raise ValueError("DATABASE_URL must not be empty")

    parsed_url = make_url(url)
    if parsed_url.get_backend_name() == "sqlite" and parsed_url.database and parsed_url.database != ":memory:":
        database_path = Path(parsed_url.database)
        if not database_path.is_absolute():
            parsed_url = parsed_url.set(database=str((PROJECT_ROOT / database_path).resolve()))
            url = parsed_url.render_as_string(hide_password=False)

    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+psycopg://", 1)
    elif url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    elif url.startswith("postgresql+") and not url.startswith("postgresql+psycopg://"):
        raise ValueError("PostgreSQL DATABASE_URL must use the installed Psycopg 3 driver")
    elif not url.startswith("sqlite") and not url.startswith("postgresql+psycopg://"):
        raise ValueError("DATABASE_URL must use SQLite or PostgreSQL with Psycopg 3")

    kwargs = {"pool_pre_ping": True, "pool_recycle": 1800}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    elif url.startswith("postgresql"):
        # Vercel/serverless instances should hold as few client connections as possible.
        # Psycopg's prepare_threshold=None disables prepared statements for transaction poolers.
        kwargs.update({
            "pool_size": 1,
            "max_overflow": 0,
            "pool_timeout": 10,
            "connect_args": {"prepare_threshold": None},
        })
    return create_engine(url, **kwargs)


engine = make_engine(get_settings().database_url)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
