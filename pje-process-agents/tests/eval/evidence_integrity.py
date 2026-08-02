"""Deterministic eval for literal process and retrieved normative evidence."""

import json
from urllib.parse import urlparse

ALLOWED_SOURCE_SUFFIXES = (
    "planalto.gov.br",
    "senado.leg.br",
    "cnj.jus.br",
    "tjpa.jus.br",
    "sefa.pa.gov.br",
    "sistemas.pa.gov.br",
)


def _text(content):
    return "".join(part.get("text", "") for part in (content or {}).get("parts", []))


def _normalize(value):
    return " ".join(value.split())


def _walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _search_response_text(agent_data):
    responses = []
    for item in _walk(agent_data):
        response = item.get("function_response")
        if not isinstance(response, dict):
            continue
        name = response.get("name", "")
        if name not in {"discovery_engine_search", "search_official_knowledge"}:
            continue
        responses.append(json.dumps(response.get("response"), ensure_ascii=False))
    if not responses:
        raise ValueError("nenhuma resposta da busca oficial no trace")
    return _normalize(" ".join(responses))


def _normative_sources(report):
    for act in report.get("acts", []):
        yield from act.get("normative_sources", [])
    for review in report.get("recommended_human_reviews", []):
        yield from review.get("normative_sources", [])


def _official_host(url):
    hostname = (urlparse(url).hostname or "").casefold()
    return any(
        hostname == suffix or hostname.endswith(f".{suffix}")
        for suffix in ALLOWED_SOURCE_SUFFIXES
    )


def evaluate(instance):
    try:
        prompt = _text(instance.get("prompt"))
        dossier = json.loads(prompt.split("DOSSIÊ_JSON=", 1)[1])
        report = json.loads(_text(instance.get("response")))
        retrieved_text = _search_response_text(instance.get("agent_data") or {})
        documents = {item["document_id"]: item for item in dossier["documents"]}
        if report.get("read_only") is not True:
            raise ValueError("read_only ausente")
        if report.get("process_number") != dossier.get("process_number"):
            raise ValueError("processo divergente")
        if set(report.get("documents_reviewed", [])) != set(documents):
            raise ValueError("cobertura documental divergente")
        acts = report.get("acts") or []
        if not acts:
            raise ValueError("nenhum ato")
        for act in acts:
            if not act.get("evidence"):
                raise ValueError("ato sem prova")
            for evidence in act["evidence"]:
                document = documents[evidence["document_id"]]
                if evidence["sha256"].casefold() != document["sha256"].casefold():
                    raise ValueError("hash divergente")
                pages = {int(page["page"]): page["text"] for page in document["pages"]}
                page_text = pages[int(evidence["page"])]
                if _normalize(evidence["excerpt"]) not in _normalize(page_text):
                    raise ValueError("trecho não literal")
        normative_sources = list(_normative_sources(report))
        if not normative_sources:
            raise ValueError("relatório sem fundamento normativo recuperado")
        for source in normative_sources:
            if not _official_host(source.get("url", "")):
                raise ValueError("domínio normativo não oficial")
            excerpt = _normalize(source.get("retrieved_excerpt", ""))
            if not excerpt or excerpt not in retrieved_text:
                raise ValueError("trecho normativo ausente da resposta da busca")
    except Exception as exc:
        return {"score": 0, "explanation": str(exc)}
    return {
        "score": 1,
        "explanation": "Provas processuais e normativas literais verificadas.",
    }
