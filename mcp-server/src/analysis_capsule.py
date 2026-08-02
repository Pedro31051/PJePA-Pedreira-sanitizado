"""Cápsulas locais e efêmeras para uma única análise processual.

Nada neste módulo conhece número de processo, nome de parte ou conteúdo dos
autos. O chamador recebe um identificador opaco e só pode operar dentro da raiz
controlada. O recibo de descarte também não contém dados processuais.
"""

from __future__ import annotations

import json
import os
import shutil
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

Mode = Literal["production", "training"]
MAX_FAILED_RETENTION = timedelta(hours=24)


class CapsuleError(ValueError):
    """Falha pública e sem conteúdo processual."""


def _now() -> datetime:
    return datetime.now(UTC)


def _default_root(mode: Mode) -> Path:
    configured = os.environ.get("PJE_ANALYSIS_CAPSULE_DIR", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    volatile = Path("/run/pjepa-mcp/analysis-capsules")
    if mode == "production" and volatile.parent.exists():
        return volatile.resolve()
    storage = Path(os.environ.get("PJE_STORAGE_DIR", "/var/lib/pjepa-mcp/storage"))
    return (storage / "analysis-capsules").resolve()


def _validate_id(capsule_id: str) -> str:
    value = str(capsule_id or "").strip().lower()
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise CapsuleError("identificador de cápsula inválido") from exc
    if parsed.hex != value.replace("-", ""):
        raise CapsuleError("identificador de cápsula inválido")
    return parsed.hex


def _capsule_path(root: Path, capsule_id: str) -> Path:
    safe_id = _validate_id(capsule_id)
    resolved_root = root.expanduser().resolve()
    candidate = (resolved_root / safe_id).resolve()
    if candidate.parent != resolved_root:
        raise CapsuleError("cápsula fora da raiz controlada")
    return candidate


@dataclass(frozen=True)
class Capsule:
    capsule_id: str
    path: Path
    mode: Mode
    expires_at: datetime

    def file(self, relative_name: str) -> Path:
        relative = Path(str(relative_name or ""))
        if relative.is_absolute() or ".." in relative.parts:
            raise CapsuleError("nome de arquivo inválido")
        candidate = (self.path / relative).resolve()
        if self.path.resolve() not in candidate.parents:
            raise CapsuleError("arquivo fora da cápsula")
        candidate.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        return candidate


def create_capsule(
    *,
    mode: Mode = "production",
    root: str | os.PathLike[str] | None = None,
    failed_retention: timedelta = MAX_FAILED_RETENTION,
) -> Capsule:
    if mode not in {"production", "training"}:
        raise CapsuleError("modo de cápsula inválido")
    if failed_retention <= timedelta(0) or failed_retention > MAX_FAILED_RETENTION:
        raise CapsuleError("retenção de falha deve ser de no máximo 24 horas")
    capsule_root = Path(root).resolve() if root is not None else _default_root(mode)
    capsule_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(capsule_root, 0o700)
    for _attempt in range(3):
        capsule_id = uuid.uuid4().hex
        final_path = capsule_root / capsule_id
        try:
            final_path.mkdir(mode=0o700)
            break
        except FileExistsError:
            continue
    else:  # pragma: no cover - colisão UUID repetida é apenas defesa extrema
        raise CapsuleError("não foi possível criar cápsula opaca")
    os.chmod(final_path, 0o700)
    created = _now()
    expires = created + failed_retention
    metadata = {
        "schema_version": "pje.analysis-capsule/v1",
        "capsule_id": capsule_id,
        "mode": mode,
        "created_at": created.isoformat(),
        "expires_at": expires.isoformat(),
    }
    metadata_path = final_path / ".capsule.json"
    metadata_path.write_text(
        json.dumps(metadata, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    os.chmod(metadata_path, 0o600)
    return Capsule(capsule_id, final_path, mode, expires)


def open_capsule(
    capsule_id: str,
    *,
    root: str | os.PathLike[str] | None = None,
    mode: Mode = "production",
) -> Capsule:
    capsule_root = Path(root).resolve() if root is not None else _default_root(mode)
    path = _capsule_path(capsule_root, capsule_id)
    if not path.is_dir() or path.is_symlink():
        raise CapsuleError("cápsula não encontrada")
    metadata_path = path / ".capsule.json"
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        stored_mode = str(metadata["mode"])
        expires_at = datetime.fromisoformat(str(metadata["expires_at"]))
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        raise CapsuleError("metadados de cápsula inválidos") from exc
    if stored_mode not in {"production", "training"}:
        raise CapsuleError("metadados de cápsula inválidos")
    return Capsule(_validate_id(capsule_id), path, stored_mode, expires_at)


def purge_capsule(
    capsule_id: str,
    *,
    reason: Literal["delivery_confirmed", "test_passed", "expired"],
    root: str | os.PathLike[str] | None = None,
    mode: Mode = "production",
) -> dict[str, object]:
    capsule_root = Path(root).resolve() if root is not None else _default_root(mode)
    path = _capsule_path(capsule_root, capsule_id)
    if not path.exists():
        return {
            "capsule_id": _validate_id(capsule_id),
            "reason": reason,
            "purged": True,
            "verified_absent": True,
            "files_removed": 0,
            "purged_at": _now().isoformat(),
        }
    if not path.is_dir() or path.is_symlink():
        raise CapsuleError("alvo de descarte não é uma cápsula regular")
    if not (path / ".capsule.json").is_file():
        raise CapsuleError("alvo de descarte não possui marcador de cápsula")
    files_removed = sum(1 for item in path.rglob("*") if item.is_file())
    shutil.rmtree(path)
    verified_absent = not path.exists()
    if not verified_absent:
        raise CapsuleError("não foi possível verificar o descarte da cápsula")
    return {
        "capsule_id": _validate_id(capsule_id),
        "reason": reason,
        "purged": True,
        "verified_absent": True,
        "files_removed": files_removed,
        "purged_at": _now().isoformat(),
    }


def record_test_result(
    capsule_id: str,
    *,
    passed: bool,
    root: str | os.PathLike[str] | None = None,
    mode: Mode = "training",
) -> dict[str, object]:
    """Teste aprovado sempre apaga; falha só pode viver até o TTL interno."""
    if passed:
        return purge_capsule(
            capsule_id, reason="test_passed", root=root, mode=mode
        )
    capsule = open_capsule(capsule_id, root=root, mode=mode)
    return {
        "capsule_id": capsule.capsule_id,
        "passed": False,
        "purged": False,
        "expires_at": capsule.expires_at.isoformat(),
        "maximum_retention_hours": 24,
    }


def purge_expired(
    *, root: str | os.PathLike[str] | None = None, mode: Mode = "production"
) -> list[dict[str, object]]:
    capsule_root = Path(root).resolve() if root is not None else _default_root(mode)
    if not capsule_root.exists():
        return []
    receipts = []
    for candidate in capsule_root.iterdir():
        if not candidate.is_dir() or candidate.is_symlink():
            continue
        try:
            capsule = open_capsule(candidate.name, root=capsule_root, mode=mode)
        except CapsuleError:
            continue
        if capsule.expires_at <= _now():
            receipts.append(
                purge_capsule(
                    capsule.capsule_id,
                    reason="expired",
                    root=capsule_root,
                    mode=mode,
                )
            )
    return receipts
