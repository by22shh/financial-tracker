"""Verify packaged application sources against a release manifest from stdin."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path


def main() -> int:
    manifest = json.load(sys.stdin)
    package_root = Path("/opt/venv/lib/python3.13/site-packages")
    files = {
        name: expected
        for name, expected in manifest["files"].items()
        if name.startswith("src/fintracker/")
        and not any(part.startswith(".") for part in Path(name).parts)
    }
    mismatches: list[str] = []
    for name, expected in files.items():
        installed = package_root / Path(name).relative_to("src")
        if not installed.is_file():
            mismatches.append(name)
            continue
        actual = hashlib.sha256(installed.read_bytes()).hexdigest()
        if actual != expected:
            mismatches.append(name)
    print(json.dumps({"verified": len(files), "mismatches": mismatches}, ensure_ascii=False))
    return 1 if mismatches else 0


if __name__ == "__main__":
    raise SystemExit(main())
