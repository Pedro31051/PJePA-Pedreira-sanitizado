#!/usr/bin/env python3
"""Audita nomenclatura, duplicidades e organização de arquivos do PJePA-Pedreira.

O comando é estritamente somente leitura. Por padrão, imprime um relatório
Markdown na saída padrão; ``--format json`` oferece uma saída estruturada para
CI e outras automações.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import unicodedata
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]

EXCLUDED_DIRS = {
    ".git",
    ".venv",
    ".agents",
    ".loop",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    "node_modules",
}

REQUIRED_PATHS = {
    "README.md": "documentação inicial do projeto",
    "SECURITY.md": "política de segurança",
    "docs/architecture/current.md": "arquitetura canônica",
    "docs/architecture/naming.md": "convenção de nomes",
    "docs/operations/deployment.md": "procedimento de produção",
    "mcp-server/README.md": "documentação do servidor MCP",
    "mcp-server/pyproject.toml": "configuração do projeto Python",
    "mcp-server/src/server.py": "ponto de entrada do servidor MCP",
    "mcp-server/tests": "suíte de testes offline",
    "automacoes-pje/package.json": "metadados das automações JavaScript",
    "chrome-extension/manifest.json": "manifesto da extensão Chrome",
}

ROOT_ALLOWLIST = {
    ".github",
    ".gitignore",
    "README.md",
    "SECURITY.md",
    "automacoes-pje",
    "chrome-extension",
    "docs",
    "mcp-server",
    "scripts",
}

TEXT_SUFFIXES = {
    ".conf",
    ".css",
    ".html",
    ".ini",
    ".js",
    ".json",
    ".md",
    ".py",
    ".service",
    ".sh",
    ".svg",
    ".timer",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}

SOURCE_SUFFIXES = {".py", ".js", ".json", ".toml", ".yaml", ".yml"}
SNAKE_CASE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
MARKDOWN_LINK = re.compile(r"!?(?:\[[^]]*\])\(([^)]+)\)")
BACKUP_NAME = re.compile(
    r"(?:\.bak|\.backup|\.copy|\.old|\.orig|\.tmp|~|\.antiga(?:-[\w-]+)?)$",
    re.IGNORECASE,
)
HISTORICAL_DATE = re.compile(r"(?:^|-)\d{4}-\d{2}(?:-\d{2})?(?=-|\.|$)")
CONVENTIONAL_SOURCE_NAMES = {"package-lock.json"}
CONVENTIONAL_DUPLICATE_NAMES = {
    ".gitignore",
    "README.md",
    "__init__.py",
    "package.json",
    "progress.md",
    "handoff.md",
}


@dataclass(frozen=True, order=True)
class Finding:
    severity: str
    code: str
    path: str
    detail: str


def relative(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def iter_files() -> list[Path]:
    files: list[Path] = []
    for current, dirs, names in os.walk(ROOT, followlinks=False):
        dirs[:] = sorted(name for name in dirs if name not in EXCLUDED_DIRS)
        base = Path(current)
        for name in sorted(names):
            path = base / name
            if not any(part in EXCLUDED_DIRS for part in path.relative_to(ROOT).parts):
                files.append(path)
    return files


def git_paths(*args: str) -> set[str]:
    try:
        result = subprocess.run(
            ["git", *args, "-z"],
            cwd=ROOT,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return set()
    return {item.decode(errors="replace") for item in result.stdout.split(b"\0") if item}


def sha256(path: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def git_index_sha256(name: str) -> str | None:
    """Calcula o SHA-256 da versão no índice sem restaurar o arquivo."""
    try:
        result = subprocess.run(
            ["git", "show", f":{name}"],
            cwd=ROOT,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None
    return hashlib.sha256(result.stdout).hexdigest()


def check_required(findings: list[Finding]) -> None:
    for name, purpose in REQUIRED_PATHS.items():
        if not (ROOT / name).exists():
            findings.append(Finding("error", "missing-required", name, purpose))


def check_git_state(files: list[Path], findings: list[Finding]) -> None:
    tracked = git_paths("ls-files")
    deleted = git_paths("ls-files", "--deleted")
    current_by_hash: dict[str, list[str]] = defaultdict(list)
    for path in files:
        if not path.is_file() or path.is_symlink():
            continue
        digest = sha256(path)
        if digest:
            current_by_hash[digest].append(relative(path))

    for name in sorted(deleted):
        old_digest = git_index_sha256(name)
        relocated = current_by_hash.get(old_digest or "", [])
        if relocated:
            findings.append(
                Finding(
                    "info",
                    "probable-relocation",
                    name,
                    "conteúdo idêntico encontrado em: " + ", ".join(sorted(relocated)),
                )
            )
        else:
            findings.append(
                Finding(
                    "warning",
                    "tracked-missing",
                    name,
                    "arquivo versionado ausente na working tree; confirme remoção ou migração",
                )
            )

    for path in files:
        name = relative(path)
        if name not in tracked:
            continue
        if path.suffix == ".pyc" or "__pycache__" in path.parts:
            findings.append(
                Finding("error", "tracked-generated", name, "cache Python não deve ser versionado")
            )
        if path.name in {".DS_Store", "Thumbs.db"}:
            findings.append(
                Finding("error", "tracked-generated", name, "metadado local não deve ser versionado")
            )


def check_paths(files: list[Path], findings: list[Finding]) -> None:
    collisions: dict[str, list[str]] = defaultdict(list)
    basenames: dict[str, list[str]] = defaultdict(list)

    for path in files:
        name = relative(path)
        collisions[unicodedata.normalize("NFC", name).casefold()].append(name)
        basenames[path.name.casefold()].append(name)

        if path.is_symlink() and not path.exists():
            findings.append(Finding("error", "broken-symlink", name, "link simbólico sem destino"))
            continue

        normalized = unicodedata.normalize("NFC", path.name)
        if normalized != path.name:
            findings.append(
                Finding("warning", "unicode-name", name, "nome não está normalizado em Unicode NFC")
            )
        if any(char.isspace() for char in path.name):
            findings.append(Finding("warning", "whitespace-name", name, "nome contém espaço"))
        if BACKUP_NAME.search(path.name):
            findings.append(
                Finding(
                    "warning",
                    "backup-file",
                    name,
                    "cópia temporária/legada deve ir para docs/history ou ficar fora do repositório",
                )
            )

        if path.suffix.lower() in SOURCE_SUFFIXES and path.name not in CONVENTIONAL_SOURCE_NAMES:
            stem = path.name[: -len(path.suffix)] if path.suffix else path.name
            for qualifier in (".local.example", ".example", ".local"):
                if stem.endswith(qualifier):
                    stem = stem[: -len(qualifier)]
                    break
            if stem not in {"__init__"} and not SNAKE_CASE.fullmatch(stem):
                findings.append(
                    Finding(
                        "warning",
                        "source-name",
                        name,
                        "arquivo-fonte fora do snake_case definido em docs/architecture/naming.md",
                    )
                )

        parts = Path(name).parts
        if path.suffix == ".py" and path.name.startswith("test_"):
            if not name.startswith("mcp-server/tests/"):
                findings.append(
                    Finding("warning", "misplaced-test", name, "teste Python deve ficar em mcp-server/tests")
                )
        if path.suffix in {".service", ".timer"} and not name.startswith(
            "mcp-server/deploy/systemd/"
        ):
            findings.append(
                Finding(
                    "warning",
                    "misplaced-systemd",
                    name,
                    "unidade systemd deve ficar em mcp-server/deploy/systemd",
                )
            )
        if "dados" in parts:
            findings.append(
                Finding(
                    "warning",
                    "legacy-data-dir",
                    name,
                    "dados estáticos devem ficar em mcp-server/resources por categoria",
                )
            )
        if name.startswith("docs/history/") and path.suffix.lower() == ".md":
            if not HISTORICAL_DATE.search(path.name):
                findings.append(
                    Finding(
                        "warning",
                        "history-without-date",
                        name,
                        "documento histórico deve incluir data AAAA-MM ou AAAA-MM-DD no nome",
                    )
                )

    for names in collisions.values():
        if len(names) > 1:
            joined = ", ".join(sorted(names))
            findings.append(
                Finding("error", "case-collision", sorted(names)[0], f"colisão de caixa/Unicode: {joined}")
            )

    for names in basenames.values():
        if len(names) < 2 or Path(names[0]).name in CONVENTIONAL_DUPLICATE_NAMES:
            continue
        findings.append(
            Finding(
                "info",
                "repeated-basename",
                sorted(names)[0],
                "mesmo nome em locais distintos: " + ", ".join(sorted(names)),
            )
        )

    for item in sorted(ROOT.iterdir(), key=lambda value: value.name):
        if item.name not in ROOT_ALLOWLIST and item.name not in EXCLUDED_DIRS:
            findings.append(
                Finding(
                    "warning",
                    "unexpected-root-item",
                    item.name,
                    "item fora da estrutura canônica descrita no README",
                )
            )


def check_duplicates(files: list[Path], findings: list[Finding]) -> None:
    by_size: dict[int, list[Path]] = defaultdict(list)
    for path in files:
        try:
            if path.is_file() and not path.is_symlink() and path.stat().st_size >= 64:
                by_size[path.stat().st_size].append(path)
        except OSError:
            continue

    for candidates in by_size.values():
        if len(candidates) < 2:
            continue
        by_hash: dict[str, list[Path]] = defaultdict(list)
        for path in candidates:
            digest = sha256(path)
            if digest:
                by_hash[digest].append(path)
        for digest, matches in by_hash.items():
            if len(matches) < 2:
                continue
            names = sorted(relative(path) for path in matches)
            findings.append(
                Finding(
                    "warning",
                    "exact-duplicate",
                    names[0],
                    f"SHA-256 {digest[:12]}; cópias: " + ", ".join(names),
                )
            )


def check_markdown_links(files: list[Path], findings: list[Finding]) -> None:
    for path in files:
        if path.suffix.lower() != ".md" or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            findings.append(
                Finding("warning", "unreadable-markdown", relative(path), "Markdown não é UTF-8 válido")
            )
            continue
        for raw_target in MARKDOWN_LINK.findall(text):
            target = raw_target.strip().split(maxsplit=1)[0].strip("<>")
            target = unquote(target.split("#", 1)[0])
            if not target or "://" in target or target.startswith(("mailto:", "data:")):
                continue
            resolved = (path.parent / target).resolve()
            try:
                resolved.relative_to(ROOT.resolve())
            except ValueError:
                findings.append(
                    Finding(
                        "warning",
                        "external-local-link",
                        relative(path),
                        f"link aponta para fora do projeto: {target}",
                    )
                )
                continue
            if not resolved.exists():
                findings.append(
                    Finding("error", "broken-doc-link", relative(path), f"destino ausente: {target}")
                )


def audit() -> tuple[list[Path], list[Finding]]:
    files = iter_files()
    findings: list[Finding] = []
    check_required(findings)
    check_git_state(files, findings)
    check_paths(files, findings)
    check_duplicates(files, findings)
    check_markdown_links(files, findings)
    return files, sorted(set(findings))


def markdown(files: list[Path], findings: list[Finding]) -> str:
    counts = {severity: 0 for severity in ("error", "warning", "info")}
    for finding in findings:
        counts[finding.severity] += 1

    lines = [
        "# Auditoria de arquivos do PJePA-Pedreira",
        "",
        f"- Arquivos analisados: **{len(files)}**",
        f"- Erros: **{counts['error']}**",
        f"- Alertas: **{counts['warning']}**",
        f"- Informações: **{counts['info']}**",
        "",
        "Escopo: nomes, estrutura, itens obrigatórios, arquivos versionados ausentes, ",
        "links Markdown locais, colisões e duplicidades exatas. Diretórios de cache, ",
        "ambientes virtuais, metadados de agentes e `node_modules` são ignorados.",
        "",
    ]

    labels = {"error": "Erros", "warning": "Alertas", "info": "Informações"}
    for severity in ("error", "warning", "info"):
        subset = [item for item in findings if item.severity == severity]
        lines.extend([f"## {labels[severity]}", ""])
        if not subset:
            lines.extend(["Nenhuma ocorrência.", ""])
            continue
        for item in subset:
            lines.append(f"- `{item.code}` — `{item.path}`: {item.detail}")
        lines.append("")

    lines.extend(
        [
            "## Política de correção segura",
            "",
            "Este auditor não move, renomeia nem exclui arquivos. Antes de remover uma ",
            "duplicata, confirme referências, histórico Git e uso em produção. Prefira uma ",
            "migração separada, com testes e possibilidade de reversão.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    parser.add_argument(
        "--fail-on",
        choices=("never", "error", "warning"),
        default="never",
        help="controla o código de saída para uso em CI",
    )
    args = parser.parse_args()

    files, findings = audit()
    if args.format == "json":
        payload = {
            "root": str(ROOT),
            "files_scanned": len(files),
            "findings": [asdict(item) for item in findings],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(markdown(files, findings))

    severities = {item.severity for item in findings}
    if args.fail_on == "warning" and severities.intersection({"error", "warning"}):
        return 1
    if args.fail_on == "error" and "error" in severities:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
