"""Official-knowledge retrieval with a deterministic local test seam."""

from __future__ import annotations

import json
import os
import re

from google.adk.tools.discovery_engine_search_tool import DiscoveryEngineSearchTool

from .official_sources import OFFICIAL_SOURCES


def search_official_knowledge(query: str) -> str:
    """Search the allow-listed official inventory sources in local test mode.

    Args:
        query: Legal topic or provision to search for.

    Returns:
        JSON metadata for relevant primary sources. This local seam is not a
        substitute for the managed Agent Platform Search corpus in production.
    """
    tokens = set(re.findall(r"[a-zà-ÿ0-9]+", query.casefold()))
    ranked = []
    for source in OFFICIAL_SOURCES:
        searchable = (
            f"{source['title']} {source['topics']} {source.get('provisions', '')}"
        ).casefold()
        score = sum(token in searchable for token in tokens)
        ranked.append((score, source))
    ranked.sort(key=lambda item: item[0], reverse=True)
    selected = [source for score, source in ranked if score > 0][:5]
    if not selected:
        selected = list(OFFICIAL_SOURCES)
    return json.dumps(
        {
            "mode": "official_catalog_test_seam",
            "warning": (
                "Use apenas como índice de fontes. Confira o dispositivo no "
                "texto oficial antes de formular conclusão normativa."
            ),
            "results": selected,
        },
        ensure_ascii=False,
    )


def official_knowledge_tool():
    """Return managed Vertex AI Search or its deterministic test replacement."""
    data_store_id = os.environ.get("PJE_OFFICIAL_SEARCH_DATA_STORE_ID", "").strip()
    integration_test = os.environ.get("INTEGRATION_TEST", "").upper() == "TRUE"
    if integration_test or not data_store_id:
        return search_official_knowledge
    return DiscoveryEngineSearchTool(data_store_id=data_store_id, max_results=5)
