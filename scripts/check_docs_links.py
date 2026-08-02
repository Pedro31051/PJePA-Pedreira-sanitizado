#!/usr/bin/env python3
"""Valida links Markdown locais sem acessar a rede."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LINK = re.compile(r"\[[^]]+\]\(([^)]+)\)")


def main() -> int:
    missing: list[str] = []
    for document in ROOT.rglob("*.md"):
        if ".git" in document.parts or ".venv" in document.parts:
            continue
        for target in LINK.findall(document.read_text(encoding="utf-8")):
            target = target.split("#", 1)[0].strip()
            if not target or "://" in target or target.startswith("mailto:"):
                continue
            if not (document.parent / target).resolve().exists():
                missing.append(f"{document.relative_to(ROOT)} -> {target}")
    for item in missing:
        print(item)
    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
