# db/alembic/env.py
from logging.config import fileConfig
import os
import psycopg
import sys
from sqlalchemy import create_engine, pool
from sqlalchemy.engine.url import make_url
from alembic import context

config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name)

def _to_psycopg_conninfo(url: str) -> str:
    u = make_url(url)
    # Normalize to plain psycopg/libpq driver for the DSN we hand to psycopg
    if u.drivername.startswith("postgresql+"):
        u = u.set(drivername="postgresql")
    # IMPORTANT: do not mask the password. SQLAlchemy masks by default when str(u) is used.
    # render_as_string(hide_password=False) keeps the real password in the DSN.
    # IMPORTANT: SQLAlchemy masks passwords when str(u) is used. Keep the real password in the DSN:
    return u.render_as_string(hide_password=False)

def _resolve_url() -> str:
    # 1) alembic -x url=...
    xargs = context.get_x_argument(as_dictionary=True)
    url = xargs.get("url")
    # 2) env var
    if not url:
        url = os.getenv("DATABASE_URL")
    # 3) alembic.ini
    if not url:
        url = config.get_main_option("sqlalchemy.url")
    # Debug: show where URL came from (masked)
    src = (
        "-x url" if xargs.get("url") else (
            "env:DATABASE_URL" if os.getenv("DATABASE_URL") else "alembic.ini"
        )
    )
    _mask = (lambda s: s.replace(u.password, "***") if s and (u := make_url(s)).password else s)
    print(f"[alembic/env] url-source={src} url={_mask(url)}", file=sys.stderr, flush=True)
    return url

effective_url = _resolve_url()

target_metadata = None

def run_migrations_offline() -> None:
    context.configure(
        url=effective_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        version_table_schema="public",
    )
    with context.begin_transaction():
        context.run_migrations()

def run_migrations_online() -> None:
    if not effective_url:
        raise RuntimeError("No DB URL for Alembic")

    # Build connection kwargs explicitly to avoid masked-password surprises
    u = make_url(effective_url)
    conn_kwargs = {
        "user": u.username,
        "password": u.password,
        "host": u.host,
        "port": int(u.port) if u.port else None,
        "dbname": u.database,
        "connect_timeout": 5,
    }
    # Debug: print parsed parts with masked password
    masked = conn_kwargs.copy()
    if masked.get("password"):
        masked["password"] = "***"
    print(f"[alembic/env] psycopg kwargs: {masked}", file=sys.stderr, flush=True)
    if not conn_kwargs.get("password"):
        raise RuntimeError("DATABASE_URL has no password; cannot run Alembic migrations.")

    engine = create_engine(
        "postgresql+psycopg://",
        poolclass=pool.NullPool,
        creator=lambda: psycopg.connect(**{k: v for k, v in conn_kwargs.items() if v is not None}),
    )

    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            version_table_schema="public",
        )
        with context.begin_transaction():
            context.run_migrations()

if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()


    