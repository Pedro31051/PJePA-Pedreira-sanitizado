"""Avaliação faturável de modelos sobre um inventário autorizado.

Compara candidato vs baseline com o MESMO dossiê textual extraído localmente
(texto nativo + OCR Tesseract em memória; nenhum PDF, imagem ou screenshot sai
da máquina). Toda resposta passa pelo aterramento literal e pela verificação
fail-closed reais. A saída é apenas métrica opaca: contagens, tokens e
latência — nunca conteúdo processual.

Uso:
    python3 avaliar_modelos_inventario.py --fonte <dir com documentos/*.pdf> \
        --modelos gemini-3.5-flash,gemini-3.6-flash --saida metricas.json

Requer autorização explícita de custo: PJE_RUN_LIVE_AGENT_TESTS=1.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src"
sys.path.insert(0, str(SRC))

import fitz  # noqa: E402

import vertex_process_agent as agent  # noqa: E402

FILENAME_RE = re.compile(r"^(\d{5,})-(.*)\.pdf$")


def _normalise(text: str) -> str:
    return re.sub(r"[ \t]+", " ", text).strip()


def _ocr_page(page: "fitz.Page") -> str:
    """Cascata local: OCR padrão, DPI maior e Tesseract --psm 6.

    O rasterizado temporário fica no diretório da execução e é apagado
    imediatamente; nada além de texto sai desta função.
    """
    for dpi in (150, 300):
        text_page = page.get_textpage_ocr(language="por", dpi=dpi, full=True)
        text = _normalise(page.get_text("text", textpage=text_page, sort=True))
        if text:
            return text
    import subprocess
    import tempfile

    def _tesseract(image_bytes: bytes, suffix: str, psm: str) -> str:
        handle = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
        try:
            handle.write(image_bytes)
            handle.flush()
            result = subprocess.run(
                ["tesseract", handle.name, "stdout", "--psm", psm, "-l", "por"],
                capture_output=True,
                text=True,
                timeout=180,
            )
            return _normalise(result.stdout)
        finally:
            handle.close()
            os.unlink(handle.name)
            assert not os.path.exists(handle.name)

    for psm in ("6", "11"):
        text = _tesseract(page.get_pixmap(dpi=300).tobytes("png"), ".png", psm)
        if text:
            return text
    # Página com imagem mascarada: o render sai branco, mas a imagem embutida
    # original ainda contém o documento (caso RG do processo de referência).
    document = page.parent
    for info in page.get_images(full=True):
        extracted = document.extract_image(info[0])
        for psm in ("6", "3"):
            text = _tesseract(
                extracted["image"], "." + extracted["ext"], psm
            )
            if text:
                return text
    return ""


def _extract_documents(source: Path) -> tuple[list[dict], dict]:
    pdf_dir = source / "documentos"
    files = sorted(pdf_dir.glob("*.pdf"))
    if not files:
        raise SystemExit(f"nenhum PDF em {pdf_dir}")
    documents = []
    stats = {"documents": 0, "pages": 0, "ocr_pages": 0, "characters": 0}
    for path in files:
        match = FILENAME_RE.match(path.name)
        if not match:
            raise SystemExit(f"nome sem ID documental: {path.name}")
        document_id, raw_title = match.groups()
        pages = []
        with fitz.open(path) as pdf:
            for index in range(pdf.page_count):
                page = pdf[index]
                text = _normalise(page.get_text("text", sort=True))
                if len(text) < 20:
                    text = _ocr_page(page)
                    stats["ocr_pages"] += 1
                if not text:
                    raise SystemExit(
                        f"página sem texto após OCR: {path.name} p.{index + 1}"
                    )
                pages.append({"page": index + 1, "text": text})
                stats["pages"] += 1
                stats["characters"] += len(text)
        digest = hashlib.sha256(
            "\n\f\n".join(item["text"] for item in pages).encode("utf-8")
        ).hexdigest()
        documents.append(
            {
                "document_id": document_id,
                "title": raw_title.replace("_", " "),
                "type": raw_title.split("_")[0],
                "date": "",
                "sha256": digest,
                "pages": pages,
            }
        )
        stats["documents"] += 1
    documents.sort(key=lambda item: int(item["document_id"]))
    return documents, stats


def _evaluate(model: str, model_input: str, documents: list[dict]) -> dict:
    start = time.monotonic()
    try:
        draft, metadata = agent._call_vertex(model, model_input)
    except Exception as exc:  # métrica honesta: falha também é resultado
        return {
            "model": model,
            "status": "failed",
            "safe_error": str(exc)[:300],
            "latency_s": round(time.monotonic() - start, 2),
        }
    latency = round(time.monotonic() - start, 2)
    verified = agent.verify_draft(draft, documents)
    verification = verified["verification"]
    cited = {
        (evidence["document_id"])
        for finding in verified["findings"]
        for evidence in finding["evidence"]
    }
    categories: dict[str, int] = {}
    for finding in verified["findings"]:
        categories[finding["category"]] = categories.get(finding["category"], 0) + 1
    return {
        "model": model,
        "status": "completed",
        "model_version": metadata.get("model_version"),
        "latency_s": latency,
        "prompt_tokens": metadata.get("prompt_tokens"),
        "output_tokens": metadata.get("output_tokens"),
        "total_tokens": metadata.get("total_tokens"),
        "findings_draft": len(draft.findings),
        "findings_verified": verification["verified_findings"],
        "findings_rejected": verification["rejected_findings"],
        "rejection_reasons": [
            reason
            for item in verification["rejections"]
            for reason in item["reasons"]
        ][:20],
        "literal_grounding": verification["literal_grounding"],
        "unknowns": len(verified["unknowns"]),
        "recommended_human_reviews": len(verified["recommended_human_reviews"]),
        "documents_cited": len(cited),
        "documents_total": len(documents),
        "categories": categories,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fonte", required=True)
    parser.add_argument(
        "--modelos", default="gemini-3.5-flash,gemini-3.6-flash"
    )
    parser.add_argument("--saida", default="metricas_avaliacao.json")
    args = parser.parse_args()
    if os.environ.get("PJE_RUN_LIVE_AGENT_TESTS") != "1":
        raise SystemExit(
            "recusado: avaliação faturável exige PJE_RUN_LIVE_AGENT_TESTS=1"
        )
    documents, stats = _extract_documents(Path(args.fonte))
    payload = {
        "documents": documents,
        "input_coverage": {
            "documents_available": len(documents),
            "documents_sent": len(documents),
            "characters_sent": stats["characters"],
            "truncated": False,
        },
    }
    model_input = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    print(f"extração local: {stats}", flush=True)
    results = []
    for model in [m.strip() for m in args.modelos.split(",") if m.strip()]:
        print(f"avaliando {model}…", flush=True)
        results.append(_evaluate(model, model_input, documents))
        print(json.dumps(results[-1], ensure_ascii=False), flush=True)
    output = {
        "schema_version": "pje.model-eval/v1",
        "extraction": stats,
        "input_sha256": hashlib.sha256(model_input.encode()).hexdigest(),
        "results": results,
    }
    Path(args.saida).write_text(
        json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"métricas gravadas em {args.saida} (sem conteúdo processual)")


if __name__ == "__main__":
    main()
