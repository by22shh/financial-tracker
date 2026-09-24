"""Create private deployment configuration; never overwrite existing credentials.

Run on the target: python3 /opt/fintracker/deploy/prepare_env.py
No credentials are printed. Bot and AI credentials are supplied separately.
"""

import os
import secrets
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parent.parent / "env"
    root.mkdir(mode=0o700, exist_ok=True)
    paths = [root / name for name in ("database.env", "runtime.env", "migration.env")]
    if any(path.exists() for path in paths):
        raise SystemExit("Configuration already exists; refusing to overwrite any credentials.")

    owner, api, worker = (secrets.token_hex(32) for _ in range(3))

    def dsn(role: str, password: str) -> str:
        return f"postgresql+psycopg://{role}:{password}@postgres:5432/fintracker"

    configs = (
        {
            "POSTGRES_USER": "fintracker_owner",
            "POSTGRES_DB": "fintracker",
            "POSTGRES_PASSWORD": owner,
            "POSTGRES_INITDB_ARGS": "--data-checksums",
        },
        {
            "FINTRACKER_ENV": "prod",
            "FINTRACKER_DB__API_DSN": dsn("fintracker_api", api),
            "FINTRACKER_DB__WORKER_DSN": dsn("fintracker_worker", worker),
            "FINTRACKER_TELEGRAM__BOT_TOKEN": "",
            "FINTRACKER_TELEGRAM__BOT_ID": "0",
            "FINTRACKER_TELEGRAM__WEBHOOK_SECRET": secrets.token_hex(32),
            "FINTRACKER_TELEGRAM__CREATION_MODE": "allowlist",
            "FINTRACKER_TELEGRAM__CREATION_ALLOWLIST": "",
            "FINTRACKER_AI__ENABLED": "false",
            "FINTRACKER_AI__API_KEY": "",
            "FINTRACKER_ASR__PROVIDER": "none",
            "FINTRACKER_SECRETS__INVITE_HMAC_KEY": secrets.token_hex(32),
            "FINTRACKER_SECRETS__CURSOR_HMAC_KEY": secrets.token_hex(32),
        },
        {
            "FINTRACKER_DB__OWNER_DSN": dsn("fintracker_owner", owner),
            "FINTRACKER_BOOTSTRAP_API_PASSWORD": api,
            "FINTRACKER_BOOTSTRAP_WORKER_PASSWORD": worker,
        },
    )
    for path, values in zip(paths, configs, strict=True):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write("".join(f"{key}={value}\n" for key, value in values.items()))
    print("Private environment files created; Telegram credentials are still required.")


if __name__ == "__main__":
    main()
