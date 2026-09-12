#!/usr/bin/env python3
"""Извлекает обязательные требования из docs/ в машиночитаемый реестр."""
from __future__ import annotations
import json, re, pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs"

def read(name: str) -> str:
    return (DOCS / name).read_text(encoding="utf-8")

items: list[dict] = []

# FR / AI / TECH / QA из ТЗ
tz = read("TZ.md")
for m in re.finditer(r"\*\*((?:FR|AI|TECH|QA)-\d{2})\s+([^*]+?)\.?\*\*", tz):
    items.append({"id": m.group(1), "title": m.group(2).strip(), "source": "docs/TZ.md"})

# A-сценарии приёмки
acc = read("ACCEPTANCE.md")
for m in re.finditer(r"^\|\s*(A\d{2,3})\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|", acc, re.M):
    items.append({"id": m.group(1), "title": m.group(2).strip(),
                  "expected": m.group(3).strip(), "source": "docs/ACCEPTANCE.md"})

# B-примеры расчётов
for m in re.finditer(r"^### (B\d{1,2}) (.+)$", acc, re.M):
    items.append({"id": m.group(1), "title": m.group(2).strip(), "source": "docs/ACCEPTANCE.md"})

# AR-проверки
ar = read("ARCHITECTURE_REVIEW.md")
for m in re.finditer(r"^\|\s*(AR-\d{2})\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|", ar, re.M):
    items.append({"id": m.group(1), "title": m.group(2).strip(),
                  "expected": m.group(3).strip(), "source": "docs/ARCHITECTURE_REVIEW.md"})

# ADR
arch = read("ARCHITECTURE.md")
for m in re.finditer(r"^\|\s*(ADR-\d{2})\s*\|\s*([^|]+?)\s*\|", arch, re.M):
    items.append({"id": m.group(1), "title": m.group(2).strip(), "source": "docs/ARCHITECTURE.md"})

# CMD
dc = read("DATA_CONTRACT.md")
for m in re.finditer(r"^\|\s*(CMD-\d{2})\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|", dc, re.M):
    items.append({"id": m.group(1), "title": m.group(2).strip(),
                  "right": m.group(3).strip(), "source": "docs/DATA_CONTRACT.md"})

seen = {}
for it in items:
    seen.setdefault(it["id"], it)
out = sorted(seen.values(), key=lambda i: (re.sub(r"\d+$", "", i["id"]),
                                           int(re.search(r"\d+$", i["id"]).group())))
(ROOT / ".planning" / "extracted_requirements.json").write_text(
    json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
from collections import Counter
c = Counter(re.sub(r"[-]?\d+$", "", i["id"]) for i in out)
print(dict(c), "total:", len(out))
