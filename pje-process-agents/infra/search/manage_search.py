#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = [
#   "click>=8.1",
#   "google-api-python-client>=2.170",
#   "google-auth>=2.40",
#   "google-cloud-storage>=3.1",
#   "requests>=2.32",
# ]
# ///
"""Provision the official, public-only Agent Platform Search corpus."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import click
import google.auth
import requests
from google.cloud import storage
from googleapiclient import discovery
from googleapiclient.errors import HttpError

ROOT = Path(__file__).resolve().parent
ALLOWED_HOSTS = {
    "atos.cnj.jus.br",
    "legis.senado.leg.br",
    "www.planalto.gov.br",
    "www.sistemas.pa.gov.br",
    "www.tjpa.jus.br",
}


def _discovery_service(project: str, location: str):
    credentials, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    endpoint = (
        "https://discoveryengine.googleapis.com"
        if location == "global"
        else f"https://{location}-discoveryengine.googleapis.com"
    )
    return discovery.build(
        "discoveryengine",
        "v1alpha",
        credentials=credentials,
        discoveryServiceUrl=f"{endpoint}/$discovery/rest?version=v1alpha",
        cache_discovery=False,
    )


def _wait(operation_getter, operation: dict, *, timeout: int = 900) -> dict:
    if operation.get("done"):
        return operation
    name = operation.get("name")
    if not name:
        raise click.ClickException("operação sem nome")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = operation_getter(name)
        if status.get("done"):
            if status.get("error"):
                raise click.ClickException(str(status["error"]))
            return status
        time.sleep(10)
    raise click.ClickException("tempo limite da operação excedido")


def _load_sources() -> list[dict]:
    payload = json.loads((ROOT / "corpus.json").read_text(encoding="utf-8"))
    return payload["sources"]


def _upload_sources(project: str, bucket_name: str, region: str) -> None:
    client = storage.Client(project=project)
    bucket = client.bucket(bucket_name)
    if not bucket.exists():
        bucket = client.create_bucket(bucket, location=region)
        bucket.iam_configuration.uniform_bucket_level_access_enabled = True
        bucket.iam_configuration.public_access_prevention = "enforced"
        bucket.patch()
        click.echo(f"Bucket criado: gs://{bucket_name}")
    else:
        click.echo(f"Bucket existente: gs://{bucket_name}")

    session = requests.Session()
    session.headers["User-Agent"] = "pje-process-agents-official-corpus/1.0"
    index = []
    for source in _load_sources():
        host = requests.utils.urlparse(source["url"]).hostname
        if host not in ALLOWED_HOSTS:
            raise click.ClickException(f"domínio não autorizado: {host}")
        fallback_path = source.get("fallback_path")
        response = None
        request_error = None
        attempts = 1 if fallback_path else 3
        for attempt in range(attempts):
            try:
                response = session.get(
                    source["url"], timeout=15 if fallback_path else 30
                )
                if response.ok:
                    break
                if response.status_code < 500 or attempt == attempts - 1:
                    response.raise_for_status()
            except requests.RequestException as exc:
                request_error = exc
                response = None
                if attempt == attempts - 1:
                    break
            time.sleep(2**attempt)
        if response is None or not response.ok:
            if not fallback_path:
                raise click.ClickException(
                    f"falha ao baixar {source['id']}: {request_error}"
                )
            body = (ROOT / fallback_path).read_bytes()
            content_type = "text/plain"
            click.echo(f"Usando recorte conferido: {source['id']}")
        else:
            body = response.content
            content_type = source["content_type"]
        if len(body) < 500:
            raise click.ClickException(f"fonte vazia ou incompleta: {source['id']}")
        extension = {
            "application/pdf": "pdf",
            "text/plain": "txt",
        }.get(content_type, "html")
        object_name = f"official/{source['id']}.{extension}"
        blob = bucket.blob(object_name)
        blob.metadata = {
            "authority": source["authority"],
            "official_url": source["url"],
            "sha256": hashlib.sha256(body).hexdigest(),
        }
        blob.upload_from_string(body, content_type=content_type)
        index.append({**source, "object": f"gs://{bucket_name}/{object_name}"})
        click.echo(f"Fonte enviada: {source['id']} ({len(body)} bytes)")

    bucket.blob("metadata/corpus-index.json").upload_from_string(
        json.dumps({"sources": index}, ensure_ascii=False, indent=2),
        content_type="application/json",
    )


def _get_connector(service, connector_name: str) -> dict | None:
    try:
        return (
            service.projects()
            .locations()
            .collections()
            .getDataConnector(name=connector_name)
            .execute()
        )
    except HttpError as exc:
        if exc.resp.status == 404:
            return None
        raise


def _ensure_connector(
    service,
    project: str,
    location: str,
    collection_id: str,
    bucket_name: str,
) -> dict:
    parent = f"projects/{project}/locations/{location}"
    connector_name = f"{parent}/collections/{collection_id}/dataConnector"
    input_uris = [
        f"gs://{bucket_name}/official/*.html",
        f"gs://{bucket_name}/official/*.txt",
    ]
    connector = _get_connector(service, connector_name)
    if connector is not None:
        desired_params = {"instance_uris": input_uris}
        if connector.get("params") != desired_params:
            connector = (
                service.projects()
                .locations()
                .collections()
                .updateDataConnector(
                    name=connector_name,
                    updateMask="params",
                    body={"name": connector_name, "params": desired_params},
                )
                .execute()
            )
            click.echo("Origem do conector atualizada para HTML/TXT")
        click.echo(f"Conector existente: {connector_name}")
        return connector
    operation = (
        service.projects()
        .locations()
        .setUpDataConnectorV2(
            parent=parent,
            collectionId=collection_id,
            collectionDisplayName="PJe Official Legal Knowledge",
            body={
                "dataSource": "gcs",
                "refreshInterval": "86400s",
                "params": {"instance_uris": input_uris},
                "entities": [
                    {
                        "entityName": "documents",
                        "params": {
                            "data_schema": "content",
                            "content_config": "CONTENT_REQUIRED",
                        },
                    }
                ],
                "staticIpEnabled": False,
            },
        )
        .execute()
    )
    operations = (
        service.projects().locations().collections().dataConnector().operations()
    )
    _wait(lambda name: operations.get(name=name).execute(), operation)
    connector = _get_connector(service, connector_name)
    if connector is None:
        raise click.ClickException("conector não encontrado após criação")
    click.echo(f"Conector criado: {connector_name}")
    return connector


def _import_documents(service, connector: dict) -> None:
    entity = (connector.get("entities") or [{}])[0]
    data_store = entity.get("dataStore")
    uris = connector.get("params", {}).get("instance_uris")
    if not data_store or not uris:
        raise click.ClickException("conector ainda não expôs data store ou origem")
    branch = f"{data_store}/branches/default_branch"
    operation = (
        service.projects()
        .locations()
        .collections()
        .dataStores()
        .branches()
        .documents()
        .import_(
            parent=branch,
            body={
                "gcsSource": {"inputUris": uris, "dataSchema": "content"},
                "reconciliationMode": "FULL",
            },
        )
        .execute()
    )
    operations = (
        service.projects()
        .locations()
        .collections()
        .dataStores()
        .branches()
        .operations()
    )
    status = _wait(lambda name: operations.get(name=name).execute(), operation)
    metadata = status.get("metadata") or {}
    click.echo(
        "Importação concluída: "
        f"sucesso={metadata.get('successCount', '?')} "
        f"falhas={metadata.get('failureCount', '?')}"
    )
    response = (
        service.projects()
        .locations()
        .collections()
        .dataStores()
        .branches()
        .documents()
        .list(parent=branch, pageSize=100)
        .execute()
    )
    document_count = len(response.get("documents") or [])
    if document_count != len(_load_sources()):
        raise click.ClickException(
            f"cobertura do corpus incompleta: {document_count}/{len(_load_sources())}"
        )
    click.echo(f"Documentos pesquisáveis: {document_count}")


@click.command()
@click.option("--project", required=True)
@click.option("--bucket", required=True)
@click.option("--bucket-region", default="us-east1", show_default=True)
@click.option("--search-location", default="global", show_default=True)
@click.option("--collection", default="pje-official-knowledge", show_default=True)
def main(
    project: str,
    bucket: str,
    bucket_region: str,
    search_location: str,
    collection: str,
) -> None:
    """Create/update the public legal corpus and print its data store path."""
    _upload_sources(project, bucket, bucket_region)
    service = _discovery_service(project, search_location)
    connector = _ensure_connector(service, project, search_location, collection, bucket)
    _import_documents(service, connector)
    data_store = (connector.get("entities") or [{}])[0].get("dataStore")
    if not data_store:
        raise click.ClickException("data store ausente")
    click.echo(json.dumps({"data_store_path": data_store}, ensure_ascii=False))


if __name__ == "__main__":
    main()
