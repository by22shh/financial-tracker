"""Bootstrap restricted runtime roles and apply the packaged Alembic migrations."""

import os

import psycopg
from alembic import command
from alembic.config import Config
from psycopg import sql

from fintracker.runtime.health import migrations_path


def main() -> None:
    owner_dsn = os.environ["FINTRACKER_DB__OWNER_DSN"]
    with psycopg.connect(owner_dsn.replace("postgresql+psycopg://", "postgresql://")) as conn:
        for role, variable in (
            ("fintracker_api", "FINTRACKER_BOOTSTRAP_API_PASSWORD"),
            ("fintracker_worker", "FINTRACKER_BOOTSTRAP_WORKER_PASSWORD"),
        ):
            exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone()
            if not exists:
                conn.execute(sql.SQL("CREATE ROLE {} LOGIN").format(sql.Identifier(role)))
            conn.execute(
                sql.SQL(
                    "ALTER ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                    "NOREPLICATION NOBYPASSRLS PASSWORD {}"
                ).format(sql.Identifier(role), sql.Literal(os.environ[variable]))
            )
        conn.execute("GRANT CONNECT ON DATABASE fintracker TO fintracker_api, fintracker_worker")
        conn.execute("GRANT USAGE ON SCHEMA public TO fintracker_api, fintracker_worker")

    config = Config()
    config.set_main_option("script_location", migrations_path())
    command.upgrade(config, "head")
    print("Restricted roles initialized; migrations applied.")


if __name__ == "__main__":
    main()
