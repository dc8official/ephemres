import os
import sys
from logging.config import fileConfig

# Ensure backend directory is in sys.path so 'app' module can be imported
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from alembic import context
from app.database import Base, sync_engine

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    from app.config import settings
    url = sync_engine.url if sync_engine is not None else settings.sync_db_url
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    if sync_engine is not None:
        with sync_engine.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
            )
            with context.begin_transaction():
                context.run_migrations()
    else:
        # In Stage 5A, migrations are executed via app.core.migrator
        pass


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
