"""Benchmark exclusivamente sintético de listas processuais em grande volume."""

import os
import time

from lista_processos_tarefa import processar_lista_processos_tarefa


def gerar_caixa_sintetica(qtd_processos: int = 10000):
    """Gera uma caixa fictícia contendo milhares de processos para simulação."""
    tarefas_opcoes = [
        "Minutar Sentença",
        "Minutar Despacho",
        "Análise de Gabinete",
        "Aguardando Decurso de Prazo",
        "Cumprimento de Sentença",
    ]
    classes_opcoes = [
        "Procedimento Comum Cível",
        "Execução Fiscal",
        "Agravo de Instrumento",
        "Apelação Cível",
        "Mandado de Segurança",
    ]
    orgaos_opcoes = [
        "1ª Vara Cível de Marabá",
        "2ª Vara Cível de Marabá",
        "3ª Vara Cível de Belém",
        "1ª Câmara Cível",
    ]

    processos = []
    for i in range(1, qtd_processos + 1):
        num_seq = f"{i:07d}"
        cnj = f"{num_seq}-50.2026.8.14.0028"
        tar = tarefas_opcoes[i % len(tarefas_opcoes)]
        cla = classes_opcoes[i % len(classes_opcoes)]
        org = orgaos_opcoes[i % len(orgaos_opcoes)]
        dia_limite = (i % 15) + 1
        data_limite = f"{dia_limite:02d}/08/2026 23:59" if i % 2 == 0 else ""
        dt_exp = f"{(i % 20) + 1:02d}/07/2026"

        processos.append({
            "id_expediente": str(1000000 + i),
            "numero_processo": cnj,
            "classe": cla,
            "tarefa": tar,
            "orgao": org,
            "data_limite": data_limite,
            "data_expedicao": dt_exp,
            "partes": f"Autor {i} X Réu {i}",
            "assunto": f"Assunto {i}",
            "destinatario": f"Advogado {i % 500}",
            "ultimo_movimento": "Juntada de Petição",
        })
    return processos


def testar_desempenho_e_funcionalidades_v12():
    print("🚀 Gerando caixa de tarefas sintética com 300.000 processos para as Iterações 11 & 12...")
    inicio_geracao = time.time()
    caixa_gigante = gerar_caixa_sintetica(300000)
    print(f"✅ 300.000 processos gerados em {time.time() - inicio_geracao:.3f}s")

    print("\n📝 1. Testando Exportação de Tabela Markdown (.md)...")
    md_out = "/tmp/Relatorio_Tabela_300k.md"
    if os.path.exists(md_out):
        os.remove(md_out)
    t0 = time.time()
    processar_lista_processos_tarefa(caixa_gigante, exportar_caminho=md_out)
    tempo_md = time.time() - t0
    print(f"⏱️ Tempo para gerar tabela Markdown de 300.000 itens: {tempo_md:.4f}s")
    assert os.path.exists(md_out)
    print(f"  Arquivo Markdown gerado com sucesso: {md_out} ({os.path.getsize(md_out)} bytes)")

    print("\n📊 2. Testando Relatório Executivo de Síntese Final Consolidado em 300.000 Processos...")
    t0 = time.time()
    res_sintese = processar_lista_processos_tarefa(
        caixa_gigante, gerar_relatorio_sintese_final=True
    )
    tempo_sintese = time.time() - t0
    print(f"⏱️ Tempo para gerar Síntese Final de 300.000 itens: {tempo_sintese:.4f}s")
    assert res_sintese["modo"] == "sintese_executiva_final"
    assert "top5_processos_criticos" in res_sintese
    print(f"  Hash da Caixa: {res_sintese['hash_caixa']}")
    print(f"  Índice de Saúde: {res_sintese['indice_saude_caixa']['score_saude']}/100")
    print(f"  Top 1 Processo Crítico: {res_sintese['top5_processos_criticos'][0]['numero_processo']}")

    print("\n✅ TODOS OS TESTES DAS ITERAÇÕES 11 & 12 PASSARAM COM SUCESSO!")


if __name__ == "__main__":
    testar_desempenho_e_funcionalidades_v12()
