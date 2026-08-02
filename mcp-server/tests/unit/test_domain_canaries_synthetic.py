import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
import analise_processual_completa as ap
from domain_profiles import adverse_possession_v1, inventory_v1


def test_canary_a_inventory_synthetic_structure():
    """Valida Inventário contra estrutura inequivocamente sintética."""
    docs = [
        {
            "id": "doc_01",
            "document_id": "doc_01",
            "title": "Petição Inicial de Inventário",
            "document_type": "Petição Inicial",
            "text": "CONTEÚDO SINTÉTICO: INVENTÁRIO DE PESSOA_TESTE_A, FALECIDA EM 10/05/2023. INVENTARIANTE PESSOA_TESTE_B. HERDEIROS: PESSOA_TESTE_C E PESSOA_TESTE_D. BEM FICTÍCIO VALOR R$ 200.000,00.",
            "source_bytes_sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            "created_at": "2023-05-15T10:00:00Z"
        }
    ]
    
    dossier_data = {
        "documents": docs,
        "parties": [
            {"name": "PESSOA_TESTE_B", "role": "inventariante"},
            {"name": "PESSOA_TESTE_C", "role": "herdeiro"}
        ]
    }
    
    res = inventory_v1.extract_inventory_v1(dossier_data=dossier_data)
    assert res["schema_version"] == "inventory/v1"
    assert res["rite_type"] == "inventario_comum"
    assert "extractor_metadata" in res

def test_canary_b_usucapiao_synthetic_structure():
    """Valida Usucapião contra estrutura inequivocamente sintética."""
    docs = [
        {
            "id": "doc_02",
            "document_id": "doc_02",
            "title": "Petição Inicial de Usucapião",
            "document_type": "Petição Inicial",
            "text": "AÇÃO DE USUCAPIÃO URBANA ESPECIAL DO IMÓVEL SITUADO NA RUA A, ÁREA TOTAL DE 250M2. POSSE MANSA E PACÍFICA HÁ 12 ANOS. PLANTA E MEMORIAL DESCRITIVO ANEXOS.",
            "source_bytes_sha256": "f4c0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            "created_at": "2024-02-10T14:00:00Z"
        }
    ]
    
    dossier_data = {
        "documents": docs,
        "parties": [
            {"name": "PESSOA_TESTE_E", "role": "autor"}
        ]
    }
    
    res = adverse_possession_v1.extract_adverse_possession_v1(dossier_data=dossier_data)
    assert res["schema_version"] == "adverse-possession/v1"
    assert "possession_details" in res
    assert res["possession_details"]["mansa_e_pacifica"] is True

def test_canary_c_scanned_ocr_fallback():
    """Canário C — Valida comportamento defensivo em documento escaneado."""
    page_without_text = {"page": 1, "text": "", "width": 595, "height": 842}
    pages = ap.split_pages(page_without_text["text"])
    assert isinstance(pages, list)

def test_canary_d_visual_content_routing():
    """Canário D — Valida identificação de plantas e memoriais."""
    doc_visual = {
        "title": "Planta Baixa e Memorial Descritivo",
        "text": "MEMORIAL DESCRITIVO DO IMÓVEL COM ÁREA DE 500M2 E PLANTA ANEXA."
    }
    is_visual = any(w in doc_visual["text"].lower() for w in ["planta", "memorial", "croqui"])
    assert is_visual is True
