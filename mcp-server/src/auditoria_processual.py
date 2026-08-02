"""Auditoria processual consultiva, local e fail-closed.

O módulo não navega no PJe e não conhece Playwright. Ele recebe ocorrências e
dossiês de um adaptador, aplica um playbook aprovado e persiste apenas
resultados criptografados. Nenhuma função executa movimentação processual.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import hmac
import json
import os
import re
import sqlite3
import tempfile
import uuid
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

import retention_policy

POLICY_SCHEMA = "pje.task-policy/v1"
DOSSIER_SCHEMA = "pje.process-dossier/v1"
FINDING_SCHEMA = "pje.process-audit-finding/v1"
AUDIT_SCHEMA = "pje.process-audit/v1"
RESULT_STATUSES = {
    "consistent",
    "confirmed_mismatch",
    "probable_mismatch",
    "insufficient_evidence",
    "policy_gap",
}
FINDING_TYPES = {
    "wrong_box",
    "wrong_task",
    "missing_task",
    "invalid_parallel_task",
    "next_act",
}
REVIEW_DECISIONS = {"accepted", "rejected", "corrected"}
_EXPORT_MAGIC = b"PJEEXP1\x00"
_EXPORT_NONCE_BYTES = 12
_EXPORT_TAG_BYTES = 16


class AuditError(ValueError):
    """Erro público e seguro da auditoria."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


def _normalise(value: Any) -> str:
    import unicodedata

    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(char for char in text if not unicodedata.combining(char))
    return re.sub(r"\s+", " ", text).strip().casefold()


def _storage_root() -> Path:
    root = Path(os.environ.get("PJE_STORAGE_DIR", "/var/lib/pjepa-mcp/storage"))
    path = root / "auditoria_processual"
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.chmod(0o700)
    except OSError:
        pass
    return path


def database_path() -> Path:
    return _storage_root() / "auditoria.sqlite3"


def _exports_dir() -> Path:
    path = _storage_root() / "exports"
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


class CryptoBox:
    """AES-256-GCM com chave entregue explicitamente ao processo.

    Produção usa ``systemd LoadCredential``. ``PJE_AUDIT_MASTER_KEY`` existe
    apenas para ambientes efêmeros e testes; o código nunca cria, persiste ou
    adivinha uma chave quando ambas as fontes estão ausentes.
    """

    def __init__(self) -> None:
        encoded = (
            self._systemd_credential()
            or os.environ.get("PJE_AUDIT_MASTER_KEY", "").strip()
        )
        if encoded:
            try:
                key = base64.urlsafe_b64decode(encoded.encode("ascii"))
            except Exception as exc:
                raise AuditError("PJE_AUDIT_MASTER_KEY não é base64 válido") from exc
        else:
            raise AuditError(
                "chave da auditoria indisponível; forneça a credencial systemd "
                "audit_master_key ou PJE_AUDIT_MASTER_KEY em ambiente efêmero"
            )
        if len(key) != 32:
            raise AuditError("a chave da auditoria deve ter exatamente 32 bytes")
        self._key = key
        self._aes = AESGCM(key)

    @staticmethod
    def _systemd_credential() -> str:
        credential_dir = os.environ.get("CREDENTIALS_DIRECTORY", "").strip()
        if not credential_dir:
            return ""
        try:
            credential_path = Path(credential_dir) / "audit_master_key"
            return credential_path.read_text(encoding="ascii").strip()
        except OSError:
            return ""

    def reference(self, value: str) -> str:
        return hmac.new(
            self._key,
            value.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def encrypt(self, value: dict[str, Any], *, aad: str) -> bytes:
        nonce = os.urandom(12)
        plaintext = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return nonce + self._aes.encrypt(nonce, plaintext, aad.encode("utf-8"))

    def decrypt(self, value: bytes, *, aad: str) -> dict[str, Any]:
        nonce, ciphertext = value[:12], value[12:]
        plaintext = self._aes.decrypt(
            nonce,
            ciphertext,
            aad.encode("utf-8"),
        )
        decoded = json.loads(plaintext.decode("utf-8"))
        if not isinstance(decoded, dict):
            raise AuditError("payload criptografado não contém um objeto")
        return decoded

    def encrypt_file(self, source: Path, destination: Path, *, aad: str) -> None:
        """Cifra arquivo por blocos e instala o resultado atomicamente."""
        nonce = os.urandom(_EXPORT_NONCE_BYTES)
        encryptor = Cipher(
            algorithms.AES(self._key),
            modes.GCM(nonce),
        ).encryptor()
        encryptor.authenticate_additional_data(aad.encode("utf-8"))
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{destination.name}.",
            dir=destination.parent,
        )
        try:
            with os.fdopen(descriptor, "wb") as output, source.open("rb") as input_:
                output.write(_EXPORT_MAGIC)
                output.write(nonce)
                while block := input_.read(1024 * 1024):
                    output.write(encryptor.update(block))
                output.write(encryptor.finalize())
                output.write(encryptor.tag)
                output.flush()
                os.fsync(output.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, destination)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def decrypt_file(self, source: Path, *, aad: str) -> bytes:
        """Autentica e decifra um export; nenhum plaintext é gravado em disco."""
        size = source.stat().st_size
        header_size = len(_EXPORT_MAGIC) + _EXPORT_NONCE_BYTES
        if size <= header_size + _EXPORT_TAG_BYTES:
            raise AuditError("export criptografado truncado")
        with source.open("rb") as input_:
            if input_.read(len(_EXPORT_MAGIC)) != _EXPORT_MAGIC:
                raise AuditError("formato legado ou export não criptografado")
            nonce = input_.read(_EXPORT_NONCE_BYTES)
            input_.seek(-_EXPORT_TAG_BYTES, os.SEEK_END)
            tag = input_.read(_EXPORT_TAG_BYTES)
            remaining = size - header_size - _EXPORT_TAG_BYTES
            input_.seek(header_size)
            decryptor = Cipher(
                algorithms.AES(self._key),
                modes.GCM(nonce, tag),
            ).decryptor()
            decryptor.authenticate_additional_data(aad.encode("utf-8"))
            plaintext = bytearray()
            while remaining:
                block = input_.read(min(1024 * 1024, remaining))
                if not block:
                    raise AuditError("export criptografado truncado")
                remaining -= len(block)
                plaintext.extend(decryptor.update(block))
            try:
                plaintext.extend(decryptor.finalize())
            except Exception as exc:
                raise AuditError("integridade do export inválida") from exc
        return bytes(plaintext)

    @staticmethod
    def is_encrypted_export(path: Path) -> bool:
        try:
            with path.open("rb") as stream:
                return stream.read(len(_EXPORT_MAGIC)) == _EXPORT_MAGIC
        except OSError:
            return False


def _connect() -> sqlite3.Connection:
    path = database_path()
    connection = sqlite3.connect(path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.execute("PRAGMA busy_timeout=30000")
    connection.execute("PRAGMA foreign_keys=ON")
    _create_schema(connection)
    _purge_expired_rows(connection)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return connection


@contextmanager
def _database() -> Iterator[sqlite3.Connection]:
    connection = _connect()
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def _create_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS audit_jobs (
            job_id TEXT PRIMARY KEY,
            snapshot_id TEXT NOT NULL,
            task_name TEXT NOT NULL,
            policy_version TEXT NOT NULL,
            policy_sha256 TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            total_items INTEGER NOT NULL DEFAULT 0,
            processed_items INTEGER NOT NULL DEFAULT 0,
            finding_items INTEGER NOT NULL DEFAULT 0,
            error_items INTEGER NOT NULL DEFAULT 0,
            authorisation_ref TEXT NOT NULL,
            unit_name TEXT,
            error TEXT
        );

        CREATE TABLE IF NOT EXISTS audit_results (
            finding_id TEXT PRIMARY KEY,
            job_id TEXT NOT NULL,
            process_ref TEXT NOT NULL,
            occurrence_ref TEXT NOT NULL,
            status TEXT NOT NULL,
            finding_type TEXT NOT NULL,
            current_task TEXT NOT NULL,
            destination TEXT,
            case_class TEXT,
            urgency INTEGER NOT NULL DEFAULT 0,
            confidence REAL NOT NULL,
            encrypted_payload BLOB NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(job_id, occurrence_ref),
            FOREIGN KEY(job_id) REFERENCES audit_jobs(job_id)
                ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_audit_results_filters
            ON audit_results(job_id, status, destination, case_class);
        CREATE INDEX IF NOT EXISTS idx_audit_results_process
            ON audit_results(job_id, process_ref);

        CREATE TABLE IF NOT EXISTS audit_reviews (
            review_id TEXT PRIMARY KEY,
            finding_id TEXT NOT NULL,
            decision TEXT NOT NULL,
            reviewer_ref TEXT NOT NULL,
            encrypted_payload BLOB NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(finding_id) REFERENCES audit_results(finding_id)
                ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS document_cache (
            process_ref TEXT NOT NULL,
            document_id TEXT NOT NULL,
            source_fingerprint TEXT NOT NULL,
            content_sha256 TEXT NOT NULL,
            encrypted_payload BLOB NOT NULL,
            extracted_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            PRIMARY KEY(process_ref, document_id, source_fingerprint)
        );

        CREATE TABLE IF NOT EXISTS process_dossiers (
            dossier_id TEXT PRIMARY KEY,
            snapshot_id TEXT NOT NULL,
            process_ref TEXT NOT NULL,
            occurrence_ref TEXT NOT NULL,
            dossier_sha256 TEXT NOT NULL,
            encrypted_payload BLOB NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            UNIQUE(snapshot_id, occurrence_ref)
        );

        CREATE TABLE IF NOT EXISTS audit_exports (
            export_id TEXT PRIMARY KEY,
            file_name TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            authorisation_ref_hmac TEXT NOT NULL DEFAULT ''
        );
        """
    )
    export_columns = {
        str(row[1]) for row in connection.execute("PRAGMA table_info(audit_exports)")
    }
    if "authorisation_ref_hmac" not in export_columns:
        connection.execute(
            "ALTER TABLE audit_exports ADD COLUMN "
            "authorisation_ref_hmac TEXT NOT NULL DEFAULT ''"
        )


def _purge_expired_rows(connection: sqlite3.Connection) -> None:
    now = _now_iso()
    connection.execute(
        "DELETE FROM document_cache WHERE expires_at<=?",
        (now,),
    )
    connection.execute(
        "DELETE FROM process_dossiers WHERE expires_at<=?",
        (now,),
    )
    expired_exports = connection.execute(
        "SELECT file_name FROM audit_exports WHERE expires_at<=?",
        (now,),
    ).fetchall()
    connection.execute(
        "DELETE FROM audit_exports WHERE expires_at<=?",
        (now,),
    )
    for row in expired_exports:
        try:
            (_exports_dir() / str(row["file_name"])).unlink(missing_ok=True)
        except OSError:
            continue


def load_policy(path: str | None = None) -> dict[str, Any]:
    selected = path or os.environ.get("PJE_TASK_POLICY_PATH", "")
    if not selected:
        raise AuditError("playbook não configurado; defina PJE_TASK_POLICY_PATH")
    policy_path = Path(selected).expanduser()
    try:
        raw = policy_path.read_bytes()
        policy = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AuditError(f"não foi possível carregar o playbook: {exc}") from exc
    if not isinstance(policy, dict):
        raise AuditError("o playbook deve ser um objeto JSON")
    _validate_policy(policy)
    policy["_file_sha256"] = hashlib.sha256(raw).hexdigest()
    policy["_path"] = str(policy_path)
    return policy


def _validate_policy(policy: dict[str, Any]) -> None:
    if policy.get("schema_version") != POLICY_SCHEMA:
        raise AuditError(f"schema do playbook deve ser {POLICY_SCHEMA}")
    for field in ("bundle_id", "version", "generated_at"):
        if not str(policy.get(field) or "").strip():
            raise AuditError(f"playbook sem {field}")
    tasks = policy.get("tasks")
    sources = policy.get("sources")
    rules = policy.get("rules")
    if not isinstance(tasks, list) or not tasks:
        raise AuditError("playbook deve conter tarefas")
    if not isinstance(sources, list) or not sources:
        raise AuditError("playbook deve conter fontes")
    if not isinstance(rules, list) or not rules:
        raise AuditError("playbook deve conter regras")
    task_ids = [str(item.get("id") or "") for item in tasks]
    source_ids = [str(item.get("id") or "") for item in sources]
    rule_ids = [str(item.get("id") or "") for item in rules]
    for label, values in (
        ("tarefa", task_ids),
        ("fonte", source_ids),
        ("regra", rule_ids),
    ):
        if any(not value for value in values) or len(values) != len(set(values)):
            raise AuditError(f"IDs de {label} vazios ou duplicados")
    known_tasks = set(task_ids)
    known_sources = set(source_ids)
    by_task = {str(item["id"]): item for item in tasks}
    task_name_owners: dict[str, str] = {}
    for task in tasks:
        aliases = task.get("aliases") or []
        if not isinstance(aliases, list):
            raise AuditError("aliases de tarefa devem ser uma lista")
        names = [task.get("canonical_name"), *aliases]
        for name in names:
            normalized_name = _normalise(name)
            if not normalized_name:
                raise AuditError("tarefa sem nome canônico ou alias válido")
            previous_owner = task_name_owners.get(normalized_name)
            task_id = str(task["id"])
            if previous_owner and previous_owner != task_id:
                raise AuditError("nome ou alias de tarefa ambíguo")
            task_name_owners[normalized_name] = task_id

    for task in tasks:
        references = list(task.get("allowed_destination_ids") or [])
        references += list(task.get("mutually_exclusive_with") or [])
        unknown = set(map(str, references)) - known_tasks
        if unknown:
            raise AuditError(
                "tarefa referencia destinos desconhecidos: "
                + ", ".join(sorted(unknown))
            )
    for rule in rules:
        current = str(rule.get("current_task_id") or "")
        if current not in known_tasks:
            raise AuditError(f"regra referencia tarefa desconhecida: {current}")
        if rule.get("status") == "approved" and (
            not rule.get("approved_by") or not rule.get("approved_at")
        ):
            raise AuditError("regra aprovada sem aprovador e data")
        scope = rule.get("scope") or {}
        if not isinstance(scope, dict):
            raise AuditError("escopo da regra deve ser um objeto")
        unit_aliases = scope.get("unit_aliases") or []
        if not isinstance(unit_aliases, list) or any(
            not _normalise(alias) for alias in unit_aliases
        ):
            raise AuditError("unit_aliases deve conter somente nomes válidos")
        if (
            rule.get("status") == "approved"
            and _normalise(scope.get("tribunal")) == "tjpa"
        ):
            missing_identity = [
                field
                for field in ("orgao_julgador_id", "unit_canonical_segment")
                if not str(scope.get(field) or "").strip()
            ]
            if missing_identity:
                raise AuditError(
                    "regra TJPA aprovada sem identidade da unidade: "
                    + ", ".join(missing_identity)
                )

        destinations = list(map(str, rule.get("allowed_destination_ids") or []))
        if set(destinations) - set(
            map(str, by_task[current].get("allowed_destination_ids") or [])
        ):
            raise AuditError("regra usa destino não permitido pela tarefa")
        outcome = str(rule.get("outcome") or "")
        if outcome not in {"stay", "move", "review"}:
            raise AuditError("regra sem outcome válido")
        if outcome == "move" and len(destinations) != 1:
            raise AuditError("regra move exige exatamente um destino")
        if outcome == "stay" and destinations:
            raise AuditError("regra stay não pode recomendar destino")
        if set(map(str, rule.get("source_ids") or [])) - known_sources:
            raise AuditError("regra referencia fonte desconhecida")


def policy_task_for_name(
    policy: dict[str, Any],
    task_name: str,
) -> dict[str, Any] | None:
    wanted = _normalise(task_name)
    for task in policy["tasks"]:
        names = [task.get("canonical_name"), *(task.get("aliases") or [])]
        if wanted in {_normalise(name) for name in names}:
            return task
    return None


def task_matches_allowlist(
    policy: dict[str, Any],
    task_name: str,
    allowed_task_names: Iterable[str],
) -> bool:
    """Confere allowlist pela identidade da tarefa, não pelo rótulo bruto.

    O PJe pode apresentar nomes diferentes para a mesma tarefa conforme a
    lotação ou a versão do fluxo. O playbook é a fonte de verdade para esses
    aliases; comparar as strings recebidas diretamente faria o piloto recusar
    uma tarefa válida antes de chegar à validação do playbook.
    """
    requested = policy_task_for_name(policy, task_name)
    if requested is None:
        return False
    allowed_ids = set()
    for allowed_name in allowed_task_names:
        allowed_task = policy_task_for_name(policy, str(allowed_name))
        if allowed_task is not None:
            allowed_ids.add(str(allowed_task.get("id") or ""))
    return str(requested.get("id") or "") in allowed_ids


def snapshot_unit_identity(snapshot: dict[str, Any]) -> dict[str, str] | None:
    """Obtém a unidade canônica apenas quando o snapshot a prova unívoca."""
    organs = snapshot.get("orgaos_julgadores") or []
    if not isinstance(organs, list) or len(organs) != 1:
        return None
    organ = organs[0] if isinstance(organs[0], dict) else {}
    organ_id = str(organ.get("id_orgao_julgador") or "").strip()
    organ_name = str(organ.get("orgao_julgador") or "").strip()
    try:
        collected = int(snapshot.get("ocorrencias_coletadas") or 0)
        organ_count = int(organ.get("ocorrencias") or 0)
    except (TypeError, ValueError):
        return None
    if not organ_id or not organ_name or collected < 1 or organ_count != collected:
        return None
    return {"id": organ_id, "name": organ_name}


def requested_unit_matches_snapshot(
    requested_unit: str,
    selected_unit: str,
    *,
    unit_identity: dict[str, str] | None = None,
    allowed_aliases: Iterable[str] = (),
) -> bool:
    """Confere lotação integral, unidade canônica ou alias declarado.

    Nome abreviado só é aceito quando o snapshot prova um único órgão e uma
    regra aprovada declara esse alias. Secretaria e papel nunca servem de
    identidade de unidade.
    """
    requested = _normalise(requested_unit)
    selected = _normalise(selected_unit)
    if not requested or not selected:
        return False
    if requested == selected:
        return True
    if not unit_identity:
        return False
    if requested == _normalise(unit_identity.get("name")):
        return True
    aliases = {_normalise(alias) for alias in allowed_aliases if _normalise(alias)}
    return requested in aliases


def validate_snapshot_and_policy(
    snapshot: dict[str, Any],
    policy: dict[str, Any],
    *,
    task_name: str,
    policy_version: str,
    requested_unit: str = "",
) -> dict[str, Any]:
    blockers: list[str] = []
    if snapshot.get("status") != "completo":
        blockers.append("snapshot não está completo")
    if float(snapshot.get("cobertura_percentual") or 0) != 100.0:
        blockers.append("snapshot não possui cobertura de 100%")
    if policy.get("version") != policy_version:
        blockers.append(
            f"versão solicitada {policy_version!r} difere do playbook carregado"
        )
    task = policy_task_for_name(policy, task_name)
    if task is None:
        blockers.append("tarefa não existe no playbook")

    metadata = snapshot.get("metadados") or {}
    actual_unit = str(metadata.get("lotacao_selecionada") or "")
    unit_identity = snapshot_unit_identity(snapshot)
    if not actual_unit:
        blockers.append("snapshot não registra lotação/perfil efetivamente selecionado")

    approved_rules = [
        rule
        for rule in policy["rules"]
        if rule.get("status") == "approved"
        and (task is None or rule.get("current_task_id") == task.get("id"))
        and _rule_is_current(rule)
    ]
    if not approved_rules:
        blockers.append("não há regra aprovada e vigente para a tarefa")
    matching_rules = []
    if actual_unit and approved_rules:
        matching_rules = [
            rule
            for rule in approved_rules
            if _scope_matches(
                rule.get("scope") or {},
                snapshot,
                actual_unit,
            )
        ]
        if not matching_rules:
            blockers.append("lotação/perfil do snapshot não corresponde ao playbook")
    if requested_unit:
        unit_aliases = [
            alias
            for rule in matching_rules
            for alias in (rule.get("scope") or {}).get("unit_aliases", [])
        ]
        if not requested_unit_matches_snapshot(
            requested_unit,
            actual_unit,
            unit_identity=unit_identity,
            allowed_aliases=unit_aliases,
        ):
            blockers.append("lotação solicitada difere da lotação do snapshot")
    return {
        "ok": not blockers,
        "blockers": blockers,
        "task": task,
        "approved_rules": len(approved_rules),
        "snapshot_id": snapshot.get("snapshot_id"),
        "snapshot_status": snapshot.get("status"),
        "coverage_percent": snapshot.get("cobertura_percentual"),
        "unit": actual_unit,
        "policy_version": policy.get("version"),
        "policy_sha256": policy.get("_file_sha256"),
        "read_only": True,
    }


def _rule_is_current(rule: dict[str, Any], today: date | None = None) -> bool:
    today = today or _now().date()
    try:
        start = date.fromisoformat(str(rule["valid_from"]))
        end = (
            date.fromisoformat(str(rule["valid_until"]))
            if rule.get("valid_until")
            else None
        )
    except (KeyError, ValueError):
        return False
    return start <= today and (end is None or today <= end)


def _scope_matches(
    scope: dict[str, Any],
    snapshot: dict[str, Any],
    unit: str,
    occurrence: dict[str, Any] | None = None,
) -> bool:
    if _normalise(scope.get("tribunal")) not in {"tjpa", "tjpa-synthetic"}:
        return False
    environment = scope.get("environment")
    expected_degree = "1g" if environment == "first_degree" else "2g"
    if str(snapshot.get("grau") or "") != expected_degree:
        return False
    unit_identity = snapshot_unit_identity(snapshot)
    expected_organ_id = str(scope.get("orgao_julgador_id") or "").strip()
    if expected_organ_id and (
        not unit_identity or unit_identity["id"] != expected_organ_id
    ):
        return False
    expected_canonical_unit = str(scope.get("unit_canonical_segment") or "").strip()
    if expected_canonical_unit and (
        not unit_identity
        or _normalise(unit_identity["name"]) != _normalise(expected_canonical_unit)
    ):
        return False
    scoped_unit = unit_identity["name"] if unit_identity else unit
    unit_pattern = str(scope.get("unit_pattern") or ".*")
    role_pattern = str(scope.get("role_pattern") or ".*")
    effective_role = str(
        (snapshot.get("metadados") or {}).get("papel_ativo")
        or str(unit).rsplit("/", 1)[-1].strip()
        or snapshot.get("persona")
        or ""
    )
    try:
        if not re.search(unit_pattern, scoped_unit, re.IGNORECASE):
            return False
        if not re.search(
            role_pattern,
            effective_role,
            re.IGNORECASE,
        ):
            return False
    except re.error:
        return False
    if occurrence is not None and scope.get("case_class_codes"):
        case_class = _normalise(occurrence.get("classe_judicial"))
        accepted = {_normalise(item) for item in scope["case_class_codes"]}
        if case_class not in accepted:
            return False
    return True


def create_job(
    *,
    snapshot_id: str,
    task_name: str,
    policy_version: str,
    policy_sha256: str,
    authorisation_ref: str,
    unit_name: str,
    total_items: int,
) -> dict[str, Any]:
    job_id = uuid.uuid4().hex
    now = _now_iso()
    with _database() as connection:
        connection.execute(
            """
            INSERT INTO audit_jobs (
                job_id, snapshot_id, task_name, policy_version,
                policy_sha256, status, created_at, updated_at, total_items,
                authorisation_ref, unit_name
            ) VALUES (?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                snapshot_id,
                task_name,
                policy_version,
                policy_sha256,
                now,
                now,
                total_items,
                authorisation_ref,
                unit_name,
            ),
        )
    return get_job(job_id)


def find_resumable_job(
    snapshot_id: str,
    task_name: str,
    policy_version: str,
) -> dict[str, Any] | None:
    with _database() as connection:
        row = connection.execute(
            """
            SELECT * FROM audit_jobs
             WHERE snapshot_id=? AND task_name=? AND policy_version=?
               AND status IN ('queued', 'running', 'failed', 'cancelled')
             ORDER BY created_at DESC LIMIT 1
            """,
            (snapshot_id, task_name, policy_version),
        ).fetchone()
    return _public_job(dict(row)) if row else None


def get_job(job_id: str) -> dict[str, Any]:
    with _database() as connection:
        row = connection.execute(
            "SELECT * FROM audit_jobs WHERE job_id=?",
            (job_id,),
        ).fetchone()
    if row is None:
        raise AuditError("job de auditoria não encontrado")
    return _public_job(dict(row))


def _public_job(job: dict[str, Any]) -> dict[str, Any]:
    total = int(job.get("total_items") or 0)
    processed = int(job.get("processed_items") or 0)
    job["progress_percent"] = round(processed * 100.0 / total, 2) if total else 100.0
    job["read_only"] = True
    job["schema_version"] = AUDIT_SCHEMA
    return job


def update_job(job_id: str, **changes: Any) -> dict[str, Any]:
    allowed = {
        "status",
        "total_items",
        "processed_items",
        "finding_items",
        "error_items",
        "error",
    }
    payload = {key: value for key, value in changes.items() if key in allowed}
    if not payload:
        return get_job(job_id)
    payload["updated_at"] = _now_iso()
    assignments = ", ".join(f"{key}=?" for key in payload)
    with _database() as connection:
        connection.execute(
            f"UPDATE audit_jobs SET {assignments} WHERE job_id=?",
            [*payload.values(), job_id],
        )
    return get_job(job_id)


def completed_occurrence_refs(job_id: str) -> set[str]:
    with _database() as connection:
        rows = connection.execute(
            "SELECT occurrence_ref FROM audit_results WHERE job_id=?",
            (job_id,),
        ).fetchall()
    return {str(row["occurrence_ref"]) for row in rows}


def occurrence_reference(occurrence_key: str) -> str:
    return CryptoBox().reference(occurrence_key)


def save_finding(job_id: str, finding: dict[str, Any]) -> str:
    _validate_finding(finding)
    crypto = CryptoBox()
    finding_id = str(finding.get("finding_id") or uuid.uuid4().hex)
    finding["finding_id"] = finding_id
    process_number = str(finding.get("process_number") or "")
    occurrence_key = str(finding.get("occurrence_key") or "")
    process_ref = crypto.reference(process_number)
    occurrence_ref = crypto.reference(occurrence_key)
    encrypted = crypto.encrypt(finding, aad=f"finding:{finding_id}")
    with _database() as connection:
        connection.execute(
            """
            INSERT INTO audit_results (
                finding_id, job_id, process_ref, occurrence_ref, status,
                finding_type, current_task, destination, case_class, urgency,
                confidence, encrypted_payload, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(job_id, occurrence_ref) DO UPDATE SET
                finding_id=excluded.finding_id,
                process_ref=excluded.process_ref,
                status=excluded.status,
                finding_type=excluded.finding_type,
                current_task=excluded.current_task,
                destination=excluded.destination,
                case_class=excluded.case_class,
                urgency=excluded.urgency,
                confidence=excluded.confidence,
                encrypted_payload=excluded.encrypted_payload,
                created_at=excluded.created_at
            """,
            (
                finding_id,
                job_id,
                process_ref,
                occurrence_ref,
                finding["status"],
                finding["finding_type"],
                finding["current_task"],
                finding.get("recommended_destination"),
                finding.get("case_class"),
                int(finding.get("urgency") or 0),
                float(finding["confidence"]),
                encrypted,
                _now_iso(),
            ),
        )
    return finding_id


def _validate_finding(finding: dict[str, Any]) -> None:
    if finding.get("schema_version") != FINDING_SCHEMA:
        raise AuditError("finding com schema inválido")
    if finding.get("status") not in RESULT_STATUSES:
        raise AuditError("finding com status inválido")
    if finding.get("finding_type") not in FINDING_TYPES:
        raise AuditError("finding com tipo inválido")
    confidence = float(finding.get("confidence") or 0)
    if not 0 <= confidence <= 1:
        raise AuditError("confiança deve ficar entre zero e um")
    if finding["status"] in {"confirmed_mismatch", "probable_mismatch"}:
        if not finding.get("supporting_evidence"):
            raise AuditError("divergência não pode ser salva sem evidência")
        if finding.get("finding_type") in {
            "wrong_box",
            "wrong_task",
        } and not finding.get("recommended_destination"):
            raise AuditError("divergência não pode ser salva sem destino")
    if finding["status"] == "insufficient_evidence" and not finding.get(
        "abstention_reason"
    ):
        raise AuditError("abstenção exige motivo")


def list_findings(
    *,
    job_id: str,
    status: str = "",
    destination: str = "",
    case_class: str = "",
    minimum_confidence: float = 0,
    urgent_only: bool = False,
    page: int = 1,
    items_per_page: int = 50,
) -> dict[str, Any]:
    get_job(job_id)
    page = max(1, int(page))
    items_per_page = max(1, min(200, int(items_per_page)))
    filters = ["job_id=?", "confidence>=?"]
    values: list[Any] = [job_id, max(0.0, min(1.0, minimum_confidence))]
    if status:
        if status not in RESULT_STATUSES:
            raise AuditError("filtro de status inválido")
        filters.append("status=?")
        values.append(status)
    if destination:
        filters.append("LOWER(COALESCE(destination,'')) LIKE ?")
        values.append(f"%{destination.casefold()}%")
    if case_class:
        filters.append("LOWER(COALESCE(case_class,'')) LIKE ?")
        values.append(f"%{case_class.casefold()}%")
    if urgent_only:
        filters.append("urgency=1")
    where = " AND ".join(filters)
    with _database() as connection:
        total = int(
            connection.execute(
                f"SELECT COUNT(*) FROM audit_results WHERE {where}",
                values,
            ).fetchone()[0]
        )
        rows = connection.execute(
            f"""
            SELECT finding_id, status, finding_type, current_task,
                   destination, case_class, urgency, confidence, created_at
              FROM audit_results WHERE {where}
             ORDER BY urgency DESC, confidence DESC, created_at
             LIMIT ? OFFSET ?
            """,
            [*values, items_per_page, (page - 1) * items_per_page],
        ).fetchall()
    return {
        "schema_version": AUDIT_SCHEMA,
        "job_id": job_id,
        "items": [dict(row) for row in rows],
        "pagination": {
            "page": page,
            "items_per_page": items_per_page,
            "total_items": total,
            "total_pages": max(1, (total + items_per_page - 1) // items_per_page),
        },
        "note": (
            "A listagem não expõe número processual. Use explicar_resultado "
            "com finding_id ou com CNJ autorizado."
        ),
    }


def explain_finding(
    *,
    finding_id: str = "",
    job_id: str = "",
    process_number: str = "",
) -> dict[str, Any]:
    crypto = CryptoBox()
    with _database() as connection:
        if finding_id:
            row = connection.execute(
                "SELECT * FROM audit_results WHERE finding_id=?",
                (finding_id,),
            ).fetchone()
        elif job_id and process_number:
            row = connection.execute(
                """
                SELECT * FROM audit_results
                 WHERE job_id=? AND process_ref=?
                 ORDER BY created_at DESC LIMIT 1
                """,
                (job_id, crypto.reference(process_number)),
            ).fetchone()
        else:
            raise AuditError("informe finding_id ou job_id + número do processo")
    if row is None:
        raise AuditError("resultado de auditoria não encontrado")
    payload = crypto.decrypt(
        row["encrypted_payload"],
        aad=f"finding:{row['finding_id']}",
    )
    payload["reviews"] = list_reviews(str(row["finding_id"]))
    return payload


def register_review(
    *,
    finding_id: str,
    decision: str,
    reviewer: str,
    justification: str,
    corrected_destination: str = "",
    corrected_next_steps: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if decision not in REVIEW_DECISIONS:
        raise AuditError("decisão de revisão inválida")
    if not reviewer.strip() or not justification.strip():
        raise AuditError("revisor e justificativa são obrigatórios")
    if decision == "corrected" and not (
        corrected_destination.strip() or corrected_next_steps
    ):
        raise AuditError("revisão corrected exige correção explícita")
    explain_finding(finding_id=finding_id)
    review_id = uuid.uuid4().hex
    payload = {
        "review_id": review_id,
        "finding_id": finding_id,
        "decision": decision,
        "reviewer": reviewer,
        "justification": justification,
        "corrected_destination": corrected_destination or None,
        "corrected_next_steps": corrected_next_steps or [],
        "created_at": _now_iso(),
        "updates_policy_automatically": False,
        "writes_to_pje": False,
    }
    crypto = CryptoBox()
    encrypted = crypto.encrypt(payload, aad=f"review:{review_id}")
    with _database() as connection:
        connection.execute(
            """
            INSERT INTO audit_reviews (
                review_id, finding_id, decision, reviewer_ref,
                encrypted_payload, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                review_id,
                finding_id,
                decision,
                crypto.reference(reviewer),
                encrypted,
                payload["created_at"],
            ),
        )
    return payload


def list_reviews(finding_id: str) -> list[dict[str, Any]]:
    crypto = CryptoBox()
    with _database() as connection:
        rows = connection.execute(
            """
            SELECT review_id, encrypted_payload FROM audit_reviews
             WHERE finding_id=? ORDER BY created_at
            """,
            (finding_id,),
        ).fetchall()
    return [
        crypto.decrypt(
            row["encrypted_payload"],
            aad=f"review:{row['review_id']}",
        )
        for row in rows
    ]


def export_report(
    job_id: str,
    ttl_hours: int = 24,
    authorisation_ref: str = "",
) -> dict[str, Any]:
    job = get_job(job_id)
    if not authorisation_ref or not hmac.compare_digest(
        str(job.get("authorisation_ref") or ""),
        str(authorisation_ref),
    ):
        raise AuditError("autorização não corresponde ao job de auditoria")
    ttl_hours = max(1, min(168, int(ttl_hours)))
    _cleanup_exports()
    with _database() as connection:
        rows = connection.execute(
            """
            SELECT finding_id, encrypted_payload FROM audit_results
             WHERE job_id=? ORDER BY created_at
            """,
            (job_id,),
        ).fetchall()
    crypto = CryptoBox()
    findings = [
        crypto.decrypt(
            row["encrypted_payload"],
            aad=f"finding:{row['finding_id']}",
        )
        for row in rows
    ]
    export_id = uuid.uuid4().hex
    destination = _exports_dir() / f"{export_id}.json.gz.enc"
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{export_id}.",
        dir=_exports_dir(),
    )
    os.close(descriptor)
    try:
        with gzip.open(temporary, "wt", encoding="utf-8") as stream:
            json.dump(
                {
                    "schema_version": AUDIT_SCHEMA,
                    "job": job,
                    "findings": findings,
                    "generated_at": _now_iso(),
                    "read_only": True,
                },
                stream,
                ensure_ascii=False,
            )
        os.chmod(temporary, 0o600)
        crypto.encrypt_file(
            Path(temporary),
            destination,
            aad=f"audit-export:{export_id}",
        )
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    digest = hashlib.sha256()
    with destination.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    digest_hex = digest.hexdigest()
    expires = _now() + timedelta(hours=ttl_hours)
    try:
        with _database() as connection:
            connection.execute(
                """
                INSERT INTO audit_exports (
                    export_id, file_name, sha256, created_at, expires_at,
                    authorisation_ref_hmac
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    export_id,
                    destination.name,
                    digest_hex,
                    _now_iso(),
                    expires.isoformat(),
                    crypto.reference(authorisation_ref),
                ),
            )
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    return {
        "export_id": export_id,
        "read_action": "ler_export_relatorio",
        "sha256": digest_hex,
        "records": len(findings),
        "expires_at": expires.isoformat(),
        "confidential": True,
        "permissions": "0600",
        "encrypted_at_rest": True,
    }


def audit_export_path(export_id: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{32}", str(export_id or "")):
        raise AuditError("ID de export inválido")
    with _database() as connection:
        row = connection.execute(
            """
            SELECT file_name, expires_at FROM audit_exports
             WHERE export_id=?
            """,
            (export_id,),
        ).fetchone()
    if row is None:
        raise AuditError("export de auditoria não encontrado ou expirado")
    path = _exports_dir() / str(row["file_name"])
    if (
        datetime.fromisoformat(str(row["expires_at"])) <= _now()
        or not path.is_file()
        or path.parent.resolve() != _exports_dir().resolve()
    ):
        path.unlink(missing_ok=True)
        with _database() as connection:
            connection.execute(
                "DELETE FROM audit_exports WHERE export_id=?",
                (export_id,),
            )
        raise AuditError("export de auditoria não encontrado ou expirado")
    return path


def read_export(export_id: str, authorisation_ref: str) -> bytes:
    if not authorisation_ref:
        raise AuditError("autorização explícita é obrigatória")
    path = audit_export_path(export_id)
    with _database() as connection:
        row = connection.execute(
            "SELECT authorisation_ref_hmac FROM audit_exports WHERE export_id=?",
            (export_id,),
        ).fetchone()
    crypto = CryptoBox()
    if row is None or not hmac.compare_digest(
        str(row["authorisation_ref_hmac"] or ""),
        crypto.reference(authorisation_ref),
    ):
        raise AuditError("autorização não corresponde ao export")
    return crypto.decrypt_file(path, aad=f"audit-export:{export_id}")


def _cleanup_exports() -> None:
    with _database() as connection:
        known = {
            str(row["file_name"])
            for row in connection.execute(
                "SELECT file_name FROM audit_exports"
            ).fetchall()
        }
    for path in _exports_dir().glob("*.json.gz*"):
        try:
            if path.name not in known:
                path.unlink()
        except OSError:
            continue


def get_cached_document(
    process_number: str,
    document_id: str,
    source_fingerprint: str,
) -> dict[str, Any] | None:
    crypto = CryptoBox()
    process_ref = crypto.reference(process_number)
    with _database() as connection:
        row = connection.execute(
            """
            SELECT encrypted_payload, expires_at FROM document_cache
             WHERE process_ref=? AND document_id=? AND source_fingerprint=?
            """,
            (process_ref, document_id, source_fingerprint),
        ).fetchone()
    if row is None or datetime.fromisoformat(row["expires_at"]) <= _now():
        return None
    return crypto.decrypt(
        row["encrypted_payload"],
        aad=f"document:{process_ref}:{document_id}:{source_fingerprint}",
    )


def save_cached_document(
    process_number: str,
    document_id: str,
    source_fingerprint: str,
    content_sha256: str,
    payload: dict[str, Any],
    ttl_days: int = 7,
) -> None:
    retention_policy.require_legacy_persistence("o cache cifrado de peças")
    crypto = CryptoBox()
    process_ref = crypto.reference(process_number)
    aad = f"document:{process_ref}:{document_id}:{source_fingerprint}"
    encrypted = crypto.encrypt(payload, aad=aad)
    expires = _now() + timedelta(days=max(1, min(30, ttl_days)))
    with _database() as connection:
        connection.execute(
            """
            INSERT INTO document_cache (
                process_ref, document_id, source_fingerprint, content_sha256,
                encrypted_payload, extracted_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(process_ref, document_id, source_fingerprint)
            DO UPDATE SET
                content_sha256=excluded.content_sha256,
                encrypted_payload=excluded.encrypted_payload,
                extracted_at=excluded.extracted_at,
                expires_at=excluded.expires_at
            """,
            (
                process_ref,
                document_id,
                source_fingerprint,
                content_sha256,
                encrypted,
                _now_iso(),
                expires.isoformat(),
            ),
        )


def save_dossier(dossier: dict[str, Any]) -> dict[str, Any]:
    retention_policy.require_legacy_persistence("o dossiê persistente de auditoria")
    if dossier.get("schema_version") != DOSSIER_SCHEMA:
        raise AuditError("dossiê com schema inválido")
    process_number = str(dossier.get("process_number") or "")
    occurrence_key = str(dossier.get("occurrence_key") or "")
    snapshot_id = str(dossier.get("snapshot_id") or "")
    if not process_number or not occurrence_key or not snapshot_id:
        raise AuditError("dossiê sem identidade completa")
    crypto = CryptoBox()
    process_ref = crypto.reference(process_number)
    occurrence_ref = crypto.reference(occurrence_key)
    digest = hashlib.sha256(
        json.dumps(
            dossier,
            sort_keys=True,
            ensure_ascii=False,
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    dossier_id = uuid.uuid4().hex
    encrypted = crypto.encrypt(dossier, aad=f"dossier:{dossier_id}")
    expires = _now() + timedelta(days=7)
    with _database() as connection:
        previous = connection.execute(
            """
            SELECT dossier_id FROM process_dossiers
             WHERE snapshot_id=? AND occurrence_ref=?
            """,
            (snapshot_id, occurrence_ref),
        ).fetchone()
        if previous:
            connection.execute(
                "DELETE FROM process_dossiers WHERE dossier_id=?",
                (previous["dossier_id"],),
            )
        connection.execute(
            """
            INSERT INTO process_dossiers (
                dossier_id, snapshot_id, process_ref, occurrence_ref,
                dossier_sha256, encrypted_payload, created_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                dossier_id,
                snapshot_id,
                process_ref,
                occurrence_ref,
                digest,
                encrypted,
                _now_iso(),
                expires.isoformat(),
            ),
        )
    return {
        "dossier_id": dossier_id,
        "dossier_sha256": digest,
        "expires_at": expires.isoformat(),
    }


def evaluate_dossier(
    occurrence: dict[str, Any],
    dossier: dict[str, Any],
    policy: dict[str, Any],
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    """Aplica regras aprovadas e se abstém diante de qualquer ambiguidade."""
    if dossier.get("schema_version") != DOSSIER_SCHEMA:
        raise AuditError("dossiê com schema inválido")
    current_name = str(occurrence.get("tarefa") or "")
    task = policy_task_for_name(policy, current_name)
    base = {
        "schema_version": FINDING_SCHEMA,
        "finding_id": uuid.uuid4().hex,
        "process_number": str(occurrence.get("numero_processo") or ""),
        "occurrence_key": str(occurrence.get("chave_ocorrencia") or ""),
        "case_class": occurrence.get("classe_judicial"),
        "current_task": current_name,
        "recommended_destination": None,
        "alternative_destinations": [],
        "next_steps": [],
        "applied_rules": [],
        "conflicting_rules": [],
        "supporting_evidence": [],
        "contrary_evidence": [],
        "coverage": dossier.get("coverage") or {},
        "confidence": 0.0,
        "abstention_reason": None,
        "snapshot_id": snapshot.get("snapshot_id"),
        "snapshot_finalized_at": snapshot.get("finalizado_em"),
        "policy_version": policy.get("version"),
        "policy_sha256": policy.get("_file_sha256"),
        "extractor_versions": dossier.get("extractor_versions") or {},
        "model": {
            "enabled": False,
            "reason": ("adaptador semântico desabilitado até aprovação institucional"),
        },
        "read_only": True,
        "human_review_required": True,
        "created_at": _now_iso(),
        "urgency": int(bool(occurrence.get("prioridade"))),
    }
    if task is None:
        return {
            **base,
            "status": "policy_gap",
            "finding_type": "wrong_task",
            "abstention_reason": "tarefa atual não existe no playbook",
        }

    parallel = {
        _normalise(item)
        for item in dossier.get("parallel_tasks") or []
        if _normalise(item) != _normalise(current_name)
    }
    exclusive_ids = set(map(str, task.get("mutually_exclusive_with") or []))
    tasks_by_id = {str(item["id"]): item for item in policy["tasks"]}
    exclusive_names = {
        _normalise(tasks_by_id[item]["canonical_name"])
        for item in exclusive_ids
        if item in tasks_by_id
    }
    invalid_parallel = sorted(parallel & exclusive_names)
    if invalid_parallel:
        evidence = [
            {
                "source_type": "snapshot",
                "snapshot_id": snapshot.get("snapshot_id"),
                "occurrence_key": occurrence.get("chave_ocorrencia"),
                "claim": "tarefas mutuamente exclusivas coexistem no snapshot",
            }
        ]
        return {
            **base,
            "status": "confirmed_mismatch",
            "finding_type": "invalid_parallel_task",
            "supporting_evidence": evidence,
            "confidence": 1.0,
            "abstention_reason": None,
        }

    unit = str((snapshot.get("metadados") or {}).get("lotacao_selecionada") or "")
    rules = [
        rule
        for rule in policy["rules"]
        if rule.get("status") == "approved"
        and _rule_is_current(rule)
        and rule.get("current_task_id") == task.get("id")
        and _scope_matches(rule.get("scope") or {}, snapshot, unit, occurrence)
    ]
    if not rules:
        return {
            **base,
            "status": "policy_gap",
            "finding_type": "next_act",
            "abstention_reason": "nenhuma regra aprovada cobre este processo",
        }

    facts = dossier.get("facts") or {}
    matches: list[dict[str, Any]] = []
    unknown_requirements: list[str] = []
    for rule in rules:
        required = [
            _evaluate_condition(condition, facts)
            for condition in rule.get("required_conditions") or []
        ]
        blocked = [
            _evaluate_condition(condition, facts)
            for condition in rule.get("blocking_conditions") or []
        ]
        if any(result is None for result in required):
            unknown_requirements.extend(
                condition["fact"]
                for condition, result in zip(
                    rule.get("required_conditions") or [],
                    required,
                )
                if result is None
            )
            continue
        if any(result is None for result in blocked):
            unknown_requirements.extend(
                condition["fact"]
                for condition, result in zip(
                    rule.get("blocking_conditions") or [],
                    blocked,
                )
                if result is None
            )
            continue
        if not all(required) or any(result is True for result in blocked):
            continue
        missing_evidence = _missing_evidence(rule, facts)
        if missing_evidence:
            unknown_requirements.extend(missing_evidence)
            continue
        matches.append(rule)

    if not matches:
        reason = (
            "faltam fatos/evidências exigidos: "
            + ", ".join(sorted(set(unknown_requirements)))
            if unknown_requirements
            else "nenhuma regra vigente satisfeita; não é seguro inferir destino"
        )
        return {
            **base,
            "status": "insufficient_evidence",
            "finding_type": "next_act",
            "abstention_reason": reason,
        }

    max_priority = max(int(rule.get("priority") or 0) for rule in matches)
    winners = [
        rule for rule in matches if int(rule.get("priority") or 0) == max_priority
    ]
    outcomes = {
        (
            rule.get("outcome"),
            tuple(rule.get("allowed_destination_ids") or []),
        )
        for rule in winners
    }
    if len(outcomes) != 1:
        return {
            **base,
            "status": "insufficient_evidence",
            "finding_type": "next_act",
            "conflicting_rules": [rule["id"] for rule in winners],
            "abstention_reason": "regras de mesma prioridade produzem conclusões diferentes",
        }

    winner = sorted(winners, key=lambda rule: str(rule["id"]))[0]
    evidence = _evidence_for_rule(winner, facts)
    destination_ids = list(winner.get("allowed_destination_ids") or [])
    destination = (
        tasks_by_id[destination_ids[0]]["canonical_name"] if destination_ids else None
    )
    outcome = winner["outcome"]
    if outcome == "review":
        return {
            **base,
            "status": "insufficient_evidence",
            "finding_type": "next_act",
            "applied_rules": [winner["id"]],
            "supporting_evidence": evidence,
            "abstention_reason": "a regra aprovada exige revisão humana",
        }

    coverage = dossier.get("coverage") or {}
    complete = bool(coverage.get("complete"))
    if outcome == "move":
        status = "confirmed_mismatch" if complete else "probable_mismatch"
        finding_type = "wrong_box"
        confidence = 1.0 if complete else min(0.89, _evidence_confidence(evidence))
    else:
        status = "consistent"
        finding_type = "next_act"
        confidence = _evidence_confidence(evidence)
    return {
        **base,
        "status": status,
        "finding_type": finding_type,
        "recommended_destination": destination,
        "next_steps": list(winner.get("next_acts") or []),
        "applied_rules": [winner["id"]],
        "supporting_evidence": evidence,
        "confidence": confidence,
        "abstention_reason": None,
    }


def _fact_values(
    facts: dict[str, Any],
    name: str,
) -> list[dict[str, Any]]:
    value = facts.get(name)
    if value is None:
        return []
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return [value] if isinstance(value, dict) else [{"value": value}]


def _evaluate_condition(
    condition: dict[str, Any],
    facts: dict[str, Any],
) -> bool | None:
    values = _fact_values(facts, str(condition.get("fact") or ""))
    operator = condition.get("operator")
    expected = condition.get("value")
    if operator == "exists":
        return bool(values)
    if operator == "not_exists":
        return not values
    if not values:
        return None

    def compare(actual: Any) -> bool:
        if operator == "equals":
            return actual == expected
        if operator == "not_equals":
            return actual != expected
        if operator == "contains":
            return _normalise(expected) in _normalise(actual)
        if operator == "in":
            return actual in (expected or [])
        try:
            if operator == "greater_than":
                return float(actual) > float(expected)
            if operator == "less_than":
                return float(actual) < float(expected)
        except (TypeError, ValueError):
            return False
        return False

    return any(compare(item.get("value")) for item in values)


def _missing_evidence(
    rule: dict[str, Any],
    facts: dict[str, Any],
) -> list[str]:
    missing = []
    for requirement in rule.get("evidence_requirements") or []:
        if not requirement.get("required", True):
            continue
        values = _fact_values(facts, str(requirement.get("fact") or ""))
        cited = [
            item
            for item in values
            if item.get("citations") or item.get("source_type") == "snapshot"
        ]
        if len(cited) < int(requirement.get("minimum_count") or 1):
            missing.append(str(requirement.get("fact") or ""))
    return missing


def _evidence_for_rule(
    rule: dict[str, Any],
    facts: dict[str, Any],
) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    names = {
        str(item.get("fact") or "")
        for item in (
            list(rule.get("required_conditions") or [])
            + list(rule.get("evidence_requirements") or [])
        )
    }
    for name in sorted(names):
        for item in _fact_values(facts, name):
            for citation in item.get("citations") or []:
                evidence.append({"fact": name, **citation})
            if item.get("source_type") == "snapshot":
                evidence.append({"fact": name, **item})
    unique: dict[str, dict[str, Any]] = {}
    for item in evidence:
        digest = hashlib.sha256(
            json.dumps(item, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        unique[digest] = item
    return list(unique.values())


def _evidence_confidence(evidence: Iterable[dict[str, Any]]) -> float:
    values = [
        float(item.get("confidence", 1.0))
        for item in evidence
        if item.get("confidence") is not None
    ]
    return round(min(values), 4) if values else 0.0


def build_document_fingerprint(document: dict[str, Any]) -> str:
    stable = {
        "id": str(document.get("id") or document.get("documento_id") or ""),
        "title": document.get("titulo") or "",
        "type": document.get("tipo") or "",
        "date": document.get("data") or "",
    }
    return hashlib.sha256(
        json.dumps(stable, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def split_document_pages(text: str) -> list[dict[str, Any]]:
    marker = re.compile(r"--- Página (\d+) ---\n")
    matches = list(marker.finditer(text or ""))
    if not matches:
        return [{"page": 1, "text": text or ""}]
    pages = []
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        pages.append(
            {
                "page": int(match.group(1)),
                "text": text[start:end].strip(),
            }
        )
    return pages


def extract_structured_facts(
    occurrence: dict[str, Any],
    movements: Iterable[dict[str, Any]],
    documents: Iterable[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Extrator conservador: só afirma padrões textuais com citação."""
    facts: dict[str, list[dict[str, Any]]] = {}

    movement_list = [item for item in (movements or []) if isinstance(item, dict)]

    def add(name: str, value: Any, citation: dict[str, Any]) -> None:
        facts.setdefault(name, []).append(
            {
                "value": value,
                "citations": [citation],
            }
        )

    for movement in movement_list:
        text = " ".join(
            str(movement.get(key) or "")
            for key in ("data", "titulo", "tipo", "detalhes", "descricao")
        ).strip()
        normal = _normalise(text)
        citation = {
            "source_type": "movement",
            "date": movement.get("data"),
            "excerpt": text[:500],
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "confidence": 1.0,
        }
        if "suspens" in normal:
            add("suspension_reference", True, citation)
        if any(term in normal for term in ("recurso", "apelacao", "agravo")):
            add("has_open_appeal_reference", True, citation)
        if "audiencia" in normal and not any(
            term in normal for term in ("cancelad", "realizad")
        ):
            add("has_hearing_reference", True, citation)

    # A timeline do cliente é retornada em ordem decrescente: o primeiro
    # movimento é o mais recente. Os fatos abaixo só registram menções
    # textuais; não afirmam vigência processual nem autorizam destino.
    if movement_list:
        latest = movement_list[0]
        latest_text = " ".join(
            str(latest.get(key) or "")
            for key in ("data", "titulo", "tipo", "detalhes", "descricao")
        ).strip()
        latest_normal = _normalise(latest_text)
        latest_citation = {
            "source_type": "movement",
            "movement_id": latest.get("id"),
            "date": latest.get("data"),
            "excerpt": latest_text[:500],
            "sha256": hashlib.sha256(latest_text.encode("utf-8")).hexdigest(),
            "confidence": 1.0,
            "timeline_position": 0,
            "timeline_order": "latest_first",
        }
        if "redistribu" in latest_normal:
            add(
                "latest_movement_mentions_redistribution",
                True,
                latest_citation,
            )
        if "suspens" in latest_normal:
            add(
                "latest_movement_mentions_suspension",
                True,
                latest_citation,
            )
        if "audiencia" in latest_normal:
            add(
                "latest_movement_mentions_hearing",
                True,
                latest_citation,
            )
        if any(term in latest_normal for term in ("recurso", "apelacao", "agravo")):
            add(
                "latest_movement_mentions_appeal",
                True,
                latest_citation,
            )

    operative = re.compile(
        r"\b(determino|intime-se|expeça-se|expeca-se|oficie-se|cumpra-se)\b",
        re.IGNORECASE,
    )
    for document in documents or []:
        document_id = str(document.get("document_id") or "")
        title = str(document.get("title") or document.get("tipo") or "")
        for page in document.get("pages") or []:
            text = str(page.get("text") or "")
            match = operative.search(_normalise(text))
            if not match:
                continue
            start = max(0, match.start() - 180)
            end = min(len(text), match.end() + 320)
            excerpt = re.sub(r"\s+", " ", text[start:end]).strip()
            add(
                "operative_order_reference",
                True,
                {
                    "source_type": "document",
                    "document_id": document_id,
                    "title": title,
                    "page": page.get("page"),
                    "excerpt": excerpt,
                    "sha256": document.get("content_sha256"),
                    "confidence": 0.75,
                    "warning": (
                        "presença textual de comando; cumprimento ainda deve "
                        "ser confirmado por regra/evidência posterior"
                    ),
                },
            )
    if occurrence.get("prioridade") is not None:
        facts["priority_flag"] = [
            {
                "value": bool(occurrence["prioridade"]),
                "source_type": "snapshot",
                "snapshot_field": "prioridade",
            }
        ]
    return facts
