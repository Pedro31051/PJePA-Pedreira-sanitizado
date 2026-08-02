#!/usr/bin/env python3
"""Conta ocorrências sensíveis no histórico sem imprimir valores."""

from __future__ import annotations

import re
import subprocess

PATTERNS = {
    "private_key_marker": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    "audit_key_literal": re.compile(rb"PJE_AUDIT_MASTER_KEY[^\n]{0,80}[A-Za-z0-9+/]{32,}={0,2}"),
    "personal_path": re.compile(rb"/(?:home|Users)/[A-Za-z0-9._-]+/"),
    "process_identifier": re.compile(rb"\b\d{7}-\d{2}\.\d{4}\.\d\.\d{2}\.\d{4}\b"),
}


def main() -> int:
    commits = subprocess.check_output(["git", "rev-list", "--all"]).splitlines()
    counts = {name: 0 for name in PATTERNS}
    affected_commits: set[bytes] = set()
    for commit in commits:
        archive = subprocess.check_output(["git", "grep", "-I", "-h", "-E", ".", commit], stderr=subprocess.DEVNULL)
        matched = False
        for name, pattern in PATTERNS.items():
            found = len(pattern.findall(archive))
            counts[name] += found
            matched = matched or bool(found)
        if matched:
            affected_commits.add(commit)
    print(f"commits_examined={len(commits)}")
    print(f"commits_with_sensitive_patterns={len(affected_commits)}")
    for name in sorted(counts):
        print(f"{name}={counts[name]}")
    return 1 if any(counts.values()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
