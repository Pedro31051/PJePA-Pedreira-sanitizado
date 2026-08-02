"""Captura passiva do fluxo de download nativo do PJe-TJPI 1g.

Como usar:
1. Roda este script: python3 captura_passiva.py
2. Aguarda o login + abertura dos autos do processo
3. Quando o navegador parar, VOCE clica:
   - No icone de download (seta pra baixo no header)
   - Preenche o dialog (cronologia, etc)
   - Clica em DOWNLOAD
   - Aguarda baixar
4. Volta no terminal e aperta Enter pra finalizar
5. O script imprime o relatorio em ~/.cache/pje-captura/captura_pje.json
"""
import asyncio
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
import keyring

from pje_client import PJeClient

NUMERO_CNJ = "0000000-00.0000.0.00.0000"
DIR_CAPTURA = Path.home() / ".cache" / "pje-captura"
DIR_CAPTURA.mkdir(parents=True, exist_ok=True, mode=0o700)
os.chmod(DIR_CAPTURA, 0o700)
SAIDA = DIR_CAPTURA / "captura_pje.json"


async def main():
    if os.environ.get("PJE_ENABLE_LIVE_TESTS") != "1":
        raise RuntimeError("recusado: defina PJE_ENABLE_LIVE_TESTS=1")
    cpf = keyring.get_password("mcp-pje-tjpi", "cpf")
    senha = keyring.get_password("mcp-pje-tjpi", "senha")
    seed = keyring.get_password("mcp-pje-tjpi", "totp_seed")

    pje = PJeClient(cpf, senha, seed, persona="advogado", headless=False)
    await pje._iniciar()

    # Eventos capturados
    eventos = []
    inicio = time.time()

    def t():
        return round(time.time() - inicio, 2)

    # Captura cliques (via JS injetado)
    js_capturar_cliques = """
    () => {
        if (window.__captura_instalada) return;
        window.__captura_instalada = true;
        window.__cliques = [];
        document.addEventListener('click', (e) => {
            const t = e.target;
            const info = {
                tag: t.tagName,
                id: t.id || null,
                class: t.className || null,
                text: (t.innerText || '').slice(0, 100),
                title: t.getAttribute('title') || null,
                onclick: t.getAttribute('onclick') || null,
                href: t.getAttribute('href') || null,
                name: t.getAttribute('name') || null,
                outerHTML: t.outerHTML.slice(0, 500),
            };
            window.__cliques.push({tempo: Date.now(), elemento: info});
        }, true);
    }
    """

    # Hook de network requests
    def on_request(request):
        # So requests pro PJe (filtra trackers, fonts, etc)
        if "pje.tjpi.jus.br" in request.url:
            eventos.append({
                "t": t(),
                "tipo": "request",
                "method": request.method,
                "url": request.url,
                "resource_type": request.resource_type,
            })

    def on_response(response):
        if "pje.tjpi.jus.br" in response.url:
            eventos.append({
                "t": t(),
                "tipo": "response",
                "status": response.status,
                "url": response.url,
                "content_type": response.headers.get("content-type", ""),
                "content_length": response.headers.get("content-length", ""),
            })

    def on_download(download):
        eventos.append({
            "t": t(),
            "tipo": "download",
            "url": download.url,
            "suggested_filename": download.suggested_filename,
        })
        print(f"\n>>> DOWNLOAD detectado: {download.suggested_filename} ({download.url[:100]})")
        # Salva o download em diretório seguro (~/.cache/pje-captura)
        destino = DIR_CAPTURA / f"download_{download.suggested_filename}"
        asyncio.create_task(download.save_as(str(destino)))
        print(f">>> Salvo em: {destino}")

    pje._page.on("request", on_request)
    pje._page.on("response", on_response)
    pje._page.on("download", on_download)

    print(">>> Login...")
    await pje._login()

    print(">>> Trocar perfil...")
    await pje._trocar_perfil()

    print(">>> Abrindo autos do processo...")
    aba = await pje._abrir_autos_processo(NUMERO_CNJ)

    # Re-instala captura na nova aba
    aba.on("request", on_request)
    aba.on("response", on_response)
    aba.on("download", on_download)
    await aba.evaluate(js_capturar_cliques)

    print(f"\n{'='*70}")
    print(f">>> NAVEGADOR PRONTO - PROCESSO {NUMERO_CNJ} ABERTO")
    print(f"{'='*70}")
    print(">>> AGORA, no navegador VISIVEL:")
    print("    1. Clique no icone de download (seta pra baixo no header)")
    print("    2. Preencha o dialog (cronologia=Decrescente, sem filtros)")
    print("    3. Clique no botao DOWNLOAD")
    print("    4. Aguarde o download completar")
    print(">>> Quando terminar, VOLTE AQUI e aperte ENTER")
    print(f"{'='*70}\n")

    await asyncio.get_event_loop().run_in_executor(None, input, ">>> Aperte ENTER quando terminar: ")

    # Captura cliques registrados pelo JS
    try:
        cliques = await aba.evaluate("window.__cliques || []")
        for c in cliques:
            eventos.append({
                "t": (c["tempo"] - int(inicio * 1000)) / 1000,
                "tipo": "click",
                "elemento": c["elemento"],
            })
    except Exception as e:
        print(f"Falha ao capturar cliques: {e}")

    # Salva relatorio sem informacoes sensiveis de sessao (como ca)
    relatorio = {
        "numero_cnj": NUMERO_CNJ,
        "url_autos": aba.url,
        "processo_id": pje._ultimo_processo_id,
        "total_eventos": len(eventos),
        "eventos": sorted(eventos, key=lambda e: e["t"]),
    }
    SAIDA.write_text(json.dumps(relatorio, indent=2, ensure_ascii=False))

    print(f"\n>>> Relatorio salvo em: {SAIDA}")
    print(f">>> Total de eventos: {len(eventos)}")
    print(f">>> Cliques: {sum(1 for e in eventos if e['tipo']=='click')}")
    print(f">>> Requests: {sum(1 for e in eventos if e['tipo']=='request')}")
    print(f">>> Downloads: {sum(1 for e in eventos if e['tipo']=='download')}")

    await pje._fechar()


if __name__ == "__main__":
    asyncio.run(main())
