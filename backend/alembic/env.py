"""
Alembic env.py — configured for async SQLAlchemy + our models.

Why async? Our app uses an async engine (create_async_engine). Alembic needs
to use the same async driver (asyncpg) so the migration runs against the real
DB without a separate sync connection.
"""
import asyncio
import os
from logging.config import fileConfig

from dotenv import load_dotenv
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

# Load .env so DATABASE_URL is available when running alembic from the terminal.
load_dotenv()

# Pull in our models' metadata so alembic autogenerate can diff them against the DB.
# This import also registers all models with Base.metadata.
from backend.database import Base  # noqa: E402
import backend.models  # noqa: F401, E402  — registers all tables on Base.metadata

# Alembic Config object — access values from alembic.ini.
config = context.config

# Override the sqlalchemy.url from alembic.ini with the real value from .env.
config.set_main_option("sqlalchemy.url", os.environ["DATABASE_URL"])

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# This is what alembic autogenerate compares against: our ORM models.
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """
    Offline mode: emit SQL to stdout without connecting to the DB.
    Useful for generating SQL scripts to review before running.
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """
    Online mode: connect to the DB asynchronously and run migrations.
    NullPool is used so there's no persistent connection pool during migrations.
    """
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
