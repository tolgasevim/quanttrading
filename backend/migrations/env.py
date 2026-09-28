from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from quant.config import get_settings
from quant.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)
# The URL is used directly, not via config.set_main_option: an escaped password contains "%",
# which Alembic's ini interpolation would try to expand.
DB_URL = get_settings().db_url()
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(url=DB_URL, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = create_engine(DB_URL, poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
