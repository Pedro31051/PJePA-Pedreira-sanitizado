#!/usr/bin/env python3
"""Bloqueia material operacional e sensível na árvore versionada."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Áreas sintéticas permitidas: suítes de teste, onde CNJs são fixtures.
SYNTHETIC_AREAS = ("mcp-server/tests/", "pje-process-agents/tests/")
TEXT_SUFFIXES = {".py", ".js", ".json", ".md", ".toml", ".yml", ".yaml", ".sh", ".txt", ".service", ".conf"}
CNJ = re.compile(r"\b\d{7}-\d{2}\.\d{4}\.\d\.\d{2}\.\d{4}\b")
PERSONAL_PATH = re.compile(r"/(?:home|Users)/[A-Za-z0-9._-]+/")
PRIVATE_KEY = re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")
HARDCODED_AUDIT_KEY = re.compile(r"PJE_AUDIT_MASTER_KEY[^\n]{0,80}[A-Za-z0-9+/]{32,}={0,2}")


def tracked_files() -> list[Path]:
    output = subprocess.check_output(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT,
    )
    return [ROOT / item.decode() for item in output.split(b"\0") if item]


def synthetic_cnj(value: str) -> bool:
    digits = re.sub(r"\D", "", value)
    return digits.startswith("0000000000000")


def main() -> int:
    violations: list[str] = []
    for path in tracked_files():
        if path.suffix.lower() not in TEXT_SUFFIXES or not path.is_file():
            continue
        relative = path.relative_to(ROOT).as_posix()
        text = path.read_text(encoding="utf-8", errors="replace")
        for line_number, line in enumerate(text.splitlines(), start=1):
            if PRIVATE_KEY.search(line) or HARDCODED_AUDIT_KEY.search(line):
                violations.append(f"{relative}:{line_number}: segredo embutido")
            if PERSONAL_PATH.search(line):
                violations.append(f"{relative}:{line_number}: caminho pessoal absoluto")
            if not relative.startswith(SYNTHETIC_AREAS):
                for match in CNJ.finditer(line):
                    if not synthetic_cnj(match.group(0)):
                        violations.append(
                            f"{relative}:{line_number}: identificador processual não sintético"
                        )
    for item in sorted(set(violations)):
        print(item)
    if violations:
        print(f"bloqueado: {len(set(violations))} ocorrência(s)")
        return 1
    print("Higiene do repositório validada.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
