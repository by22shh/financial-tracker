"""Configure the fixed OpenAI profile without exposing the API key."""

from __future__ import annotations

import datetime as dt
import getpass
import os
import shutil
from pathlib import Path

RUNTIME_ENV = Path("/opt/fintracker/env/runtime.env")


def main() -> None:
    api_key = getpass.getpass("OpenAI API key: ").strip().replace("\\_", "_")
    if not api_key.startswith("sk-") or len(api_key) < 40:
        raise SystemExit("Invalid OpenAI API key format")

    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    backup = RUNTIME_ENV.with_name(f"runtime.env.before-ai-{stamp}")
    shutil.copy2(RUNTIME_ENV, backup)

    updates = {
        "FINTRACKER_AI__ENABLED": "true",
        "FINTRACKER_AI__BASE_URL": "https://api.openai.com/v1",
        "FINTRACKER_AI__API_KEY": api_key,
        "FINTRACKER_AI__MODEL": "gpt-5.6-luna",
        "FINTRACKER_AI__REASONING_EFFORT": "medium",
        "FINTRACKER_AI__SERVICE_TIER": "default",
        "FINTRACKER_AI__PRICE_INPUT_PER_MTOK": "0.20",
        "FINTRACKER_AI__PRICE_CACHED_INPUT_PER_MTOK": "0.02",
        "FINTRACKER_AI__PRICE_OUTPUT_PER_MTOK": "1.20",
    }
    output: list[str] = []
    seen: set[str] = set()
    for line in RUNTIME_ENV.read_text().splitlines():
        name = line.split("=", 1)[0] if "=" in line else ""
        if name in updates:
            output.append(f"{name}={updates[name]}")
            seen.add(name)
        else:
            output.append(line)
    output.extend(f"{name}={value}" for name, value in updates.items() if name not in seen)

    temporary = RUNTIME_ENV.with_name("runtime.env.ai.tmp")
    temporary.write_text("\n".join(output) + "\n")
    os.chmod(temporary, 0o600)
    temporary.replace(RUNTIME_ENV)
    print(f"AI configured; backup={backup}; key_length={len(api_key)}")


if __name__ == "__main__":
    main()
