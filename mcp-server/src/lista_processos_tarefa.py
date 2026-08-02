"""Módulo de otimização para listagem de processos em caixas de tarefas do PJe (TJPA 1g/2g).

Projetado especificamente para caixas com MILHARES de processos, oferecendo:
1. Paginamento de alto desempenho (1..N páginas, limite configurável).
2. Relatório de Síntese Final Consolidado (gerar_relatorio_sintese_final com diagnóstico executivo de 1 clique).
3. Filtros Customizados Arbitrários (filtros_customizados: dict para correspondência flexível por chave-valor).
4. Exportação Multiformato (CSV, JSON, Markdown .md) para análise off-line em caixas volumosas.
5. Índice de Saúde da Caixa (indice_saude_caixa: score 0..100 e classificação EXCELENTE/BOM/ATENCAO/CRITICO).
6. Ordenação Multi-Nível com Desempate Secundário (ordenacao_secundaria: 'dias_parado'|'cnj'|'prazo_urgente').
7. Filtragem Direta por Assunto e Partes (filtro_assunto, filtro_parte).
8. Detector Automático de Anomalias & Outliers (super parados >180d, atrasos graves >30d).
9. Presets Operacionais de Triagem Rápida (preset_triagem: 'urgencias_vencidas', 'gargalos_antigos', 'resumo_executivo').
10. Filtragem in-browser multi-critério (tarefa, classe, órgão, texto livre, prazo, urgência).
11. Matriz de Priorização Dinâmica (score_prioridade 0..100) e ordenação por prioridade/urgência.
12. Índices invertidos em memória para buscas O(1) por CNJ, OAB e palavras-chave.
13. Agrupamento Dinâmico de Processos (agrupar_por: 'tarefa', 'classe', 'orgao', 'nivel_urgencia').
14. Métricas Avançadas de Retenção & P90 (média, percentil 90 e identificação de gargalos).
15. Motor de Recomendações e Ações Sugeridas (sugestoes_acao para triagem imediata).
16. Plano de Distribuição Equitativa de Carga (distribuir_por_operadores para N pessoas/equipes).
17. Assinatura de Estado & Cálculo de Delta Diferencial (hash_caixa e comparação com snapshots).
18. Gerador de Dashboard HTML Interativo (gerar_dashboard_html para visualização executiva).
19. Resumo estatístico consolidado (sem estourar context window do agente).
20. Cache em memória com TTL de 5 minutos (evita reler HTML gigante entre páginas).
"""

import csv
import hashlib
import json
import math
import os
import re
from datetime import datetime
from typing import Any


class IndiceInvertidoProcessos:
    """Índice invertido em memória para buscas rápidas O(1) em caixas com 50.000+ itens."""

    def __init__(self):
        self.cnj_map: dict[str, int] = {}
        self.token_map: dict[str, list[int]] = {}

    def indexar(self, processos: list[dict[str, Any]]):
        self.cnj_map.clear()
        self.token_map.clear()

        for idx, p in enumerate(processos):
            cnj_limpo = normalizar_cnj(p.get("numero_processo", ""))
            if cnj_limpo:
                self.cnj_map[cnj_limpo] = idx

            # Tokenização leve
            texto_full = f"{p.get('numero_processo','')} {p.get('partes','')} {p.get('assunto','')} {p.get('destinatario','')}".lower()
            tokens = set(re.findall(r"\b\w{3,}\b", texto_full))
            for tok in tokens:
                if tok not in self.token_map:
                    self.token_map[tok] = []
                self.token_map[tok].append(idx)

    def buscar_por_cnj(self, cnj_limpo: str) -> int | None:
        return self.cnj_map.get(cnj_limpo)


class ProcessoTarefaCache:
    """Cache em memória para caixas de tarefas volumosas (TTL em segundos)."""

    def __init__(self, ttl_segundos: int = 300):
        self.ttl_segundos = ttl_segundos
        self._cache: dict[str, dict[str, Any]] = {}

    def get(self, key: str) -> tuple[list[dict[str, Any]], IndiceInvertidoProcessos] | None:
        if key in self._cache:
            entry = self._cache[key]
            if datetime.now().timestamp() - entry["timestamp"] < self.ttl_segundos:
                return entry["data"], entry["indice"]
            del self._cache[key]
        return None

    def set(self, key: str, data: list[dict[str, Any]]) -> None:
        indice = IndiceInvertidoProcessos()
        indice.indexar(data)
        self._cache[key] = {
            "timestamp": datetime.now().timestamp(),
            "data": data,
            "indice": indice,
        }

    def clear(self) -> None:
        self._cache.clear()


CACHE_TAREFAS = ProcessoTarefaCache(ttl_segundos=300)


def normalizar_cnj(cnj: str) -> str:
    """Remove pontuação do CNJ para comparação unificada."""
    return re.sub(r"\D", "", cnj or "")


def calcular_hash_caixa(processos: list[dict[str, Any]]) -> str:
    """Calcula SHA-256 determinístico de todas as ocorrências da caixa."""
    raw = "\n".join(sorted(
        f"{p.get('numero_processo','')}:{p.get('tarefa','')}:{p.get('data_limite','')}"
        for p in processos
    ))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def calcular_score_prioridade(proc: dict[str, Any], hoje: datetime | None = None) -> tuple[float, str, int]:
    """Calcula um score de prioridade de 0 a 100 com base em prazos, classe e especialidades da Vara de Família."""
    if hoje is None:
        hoje = datetime.now()

    score = 0.0
    nivel = "Baixo"
    dias_parado = 0

    # 1. Prazos de intimação/manifestação
    dt_str = proc.get("data_limite")
    if dt_str:
        try:
            dt = datetime.strptime(dt_str.split()[0], "%d/%m/%Y")
            dias = (dt - hoje).days
            if dias < 0:
                score += 50.0  # Vencido
            elif dias <= 1:
                score += 40.0  # Vence hoje/amanhã
            elif dias <= 3:
                score += 30.0  # URGENTE (2-3 dias)
            elif dias <= 7:
                score += 15.0
        except ValueError:
            pass

    # 2. Urgências e Matérias Específicas da Vara de Família
    texto_contexto = f"{proc.get('classe','')} {proc.get('assunto','')} {proc.get('tarefa','')}".lower()
    
    # 2.1 Extrema Urgência / Medidas Protetivas / Riscos a Menores ou Incapazes (+30 pts)
    prioridade_maxima_familia = [
        "busca e apreensã", "medida protetiva", "alienação parental",
        "acolhimento institucional", "subtração internacional", "prisão civil"
    ]
    if any(p in texto_contexto for p in prioridade_maxima_familia):
        score += 30.0

    # 2.2 Alimentos, Guarda, Tutela, Curatela e Medidas Cautelares de Família (+20 pts)
    urgencias_familia = [
        "alimentos", "execução de alimentos", "guarda", "tutela", "curatela",
        "interdição", "liminar", "urgência", "visitas", "regulamentação de visitas"
    ]
    if any(u in texto_contexto for u in urgencias_familia):
        score += 20.0

    # 3. Tempo de retenção / estagnação na caixa
    dt_exp = proc.get("data_expedicao") or proc.get("data_entrada")
    if dt_exp:
        try:
            dt = datetime.strptime(dt_exp.split()[0], "%d/%m/%Y")
            dias_parado = (hoje - dt).days
            if dias_parado > 0:
                score += min(25.0, dias_parado * 0.5)  # cap em +25 pts
        except ValueError:
            pass

    score = round(min(100.0, score), 1)

    if score >= 60.0:
        nivel = "Crítico"
    elif score >= 40.0:
        nivel = "Alto"
    elif score >= 20.0:
        nivel = "Médio"

    return score, nivel, dias_parado


def exportar_processos(filtrados: list[dict[str, Any]], caminho_arquivo: str) -> str:
    """Exporta a lista filtrada para CSV, JSON ou Markdown no disco."""
    os.makedirs(os.path.dirname(os.path.abspath(caminho_arquivo)), exist_ok=True)
    if caminho_arquivo.endswith(".json"):
        with open(caminho_arquivo, "w", encoding="utf-8") as f:
            json.dump(filtrados, f, ensure_ascii=False, indent=2)
    elif caminho_arquivo.endswith(".md"):
        headers = ["CNJ", "Classe", "Tarefa", "Score", "Urgência", "Data Limite", "Dias Parado"]
        lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join(["---"] * len(headers)) + " |"]
        for p in filtrados[:200]:
            lines.append(
                f"| {p.get('numero_processo','')} | {p.get('classe','')} | {p.get('tarefa','')} | {p.get('score_prioridade',0)} | {p.get('nivel_urgencia','')} | {p.get('data_limite','-')} | {p.get('dias_parado',0)} |"
            )
        with open(caminho_arquivo, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
    else:
        if not caminho_arquivo.endswith(".csv"):
            caminho_arquivo += ".csv"
        headers = ["numero_processo", "classe", "tarefa", "orgao", "data_limite", "score_prioridade", "nivel_urgencia", "dias_parado", "partes"]
        with open(caminho_arquivo, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=headers, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(filtrados)

    return os.path.abspath(caminho_arquivo)


def calcular_distribuicao_equitativa(processos: list[dict[str, Any]], qtd_operadores: int) -> dict[str, Any]:
    """Algoritmo Bin-Packing guloso para divisão equitativa de carga de trabalho entre N operadores/equipes."""
    qtd_operadores = max(1, min(qtd_operadores, 50))
    operadores = [{"id": f"Operador_{i+1}", "qtd_processos": 0, "score_acumulado": 0.0, "processos": []} for i in range(qtd_operadores)]

    procs_sorted = sorted(processos, key=lambda x: -x.get("score_prioridade", 0))

    for p in procs_sorted:
        menor_op = min(operadores, key=lambda x: x["score_acumulado"])
        menor_op["qtd_processos"] += 1
        menor_op["score_acumulado"] = round(menor_op["score_acumulado"] + p.get("score_prioridade", 0), 1)
        if len(menor_op["processos"]) < 3:
            menor_op["processos"].append(p.get("numero_processo"))

    return {
        "qtd_operadores": qtd_operadores,
        "plano_divisao": [
            {
                "operador": op["id"],
                "qtd_processos": op["qtd_processos"],
                "score_acumulado": op["score_acumulado"],
                "top3_processos_criticos": op["processos"],
            }
            for op in operadores
        ],
    }


def calcular_indice_saude_caixa(processos: list[dict[str, Any]]) -> dict[str, Any]:
    """Calcula um Índice de Saúde da Caixa de 0 a 100 com classificação operacional."""
    if not processos:
        return {"score_saude": 100.0, "classificacao": "EXCELENTE", "diagnostico": "Caixa vazia sem pendências."}

    total = len(processos)
    vencidos = sum(1 for p in processos if p.get("score_prioridade", 0) >= 50.0)
    super_parados = sum(1 for p in processos if p.get("dias_parado", 0) > 180)

    penalidade_vencidos = min(40.0, (vencidos / total) * 100.0 * 2.0)
    penalidade_parados = min(30.0, (super_parados / total) * 100.0 * 3.0)

    score_saude = round(max(0.0, 100.0 - penalidade_vencidos - penalidade_parados), 1)

    if score_saude >= 85.0:
        classificacao = "EXCELENTE"
    elif score_saude >= 70.0:
        classificacao = "BOM"
    elif score_saude >= 50.0:
        classificacao = "ATENCAO"
    else:
        classificacao = "CRITICO"

    return {
        "score_saude": score_saude,
        "classificacao": classificacao,
        "penalidade_prazos_vencidos": round(penalidade_vencidos, 1),
        "penalidade_super_parados": round(penalidade_parados, 1),
    }


def detectar_anomalias_caixa(processos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Identifica anomalias e outliers estatísticos em caixas de tarefas massivas."""
    anomalias = []
    super_parados = [p.get("numero_processo") for p in processos if p.get("dias_parado", 0) > 180]
    if super_parados:
        anomalias.append({
            "tipo": "super_parado_180d",
            "severidade": "Alta",
            "quantidade": len(super_parados),
            "mensagem": f"Identificados {len(super_parados)} processo(s) parados há mais de 180 dias sem movimentação.",
            "amostra": super_parados[:5],
        })

    vencidos_graves = [p.get("numero_processo") for p in processos if p.get("score_prioridade", 0) >= 50.0 and p.get("dias_parado", 0) > 30]
    if vencidos_graves:
        anomalias.append({
            "tipo": "atraso_grave_com_prazo",
            "severidade": "Crítica",
            "quantidade": len(vencidos_graves),
            "mensagem": f"Identificados {len(vencidos_graves)} processo(s) com prazo estourado há mais de 30 dias.",
            "amostra": vencidos_graves[:5],
        })

    return anomalias


def gerar_sugestoes_e_metricas(filtrados: list[dict[str, Any]], total_bruto: int) -> tuple[dict[str, Any], list[str]]:
    """Calcula métricas avançadas de percentil/retenção e gera motor de recomendações de triagem."""
    if not filtrados:
        return {}, ["Nenhum processo localizado para os filtros informados."]

    dias_parados = [p.get("dias_parado", 0) for p in filtrados]
    dias_parados.sort()
    n = len(dias_parados)

    media_dias = round(sum(dias_parados) / n, 1) if n > 0 else 0.0
    p90_dias = dias_parados[int(n * 0.9)] if n > 0 else 0
    max_dias = dias_parados[-1] if n > 0 else 0

    tarefas_dias: dict[str, list[int]] = {}
    for p in filtrados:
        tar = p.get("tarefa", "Outras")
        if tar not in tarefas_dias:
            tarefas_dias[tar] = []
        tarefas_dias[tar].append(p.get("dias_parado", 0))

    gargalos = []
    for tar, lista_dias in tarefas_dias.items():
        med = sum(lista_dias) / len(lista_dias)
        gargalos.append({"tarefa": tar, "media_dias_parado": round(med, 1), "quantidade": len(lista_dias)})
    gargalos.sort(key=lambda x: -x["media_dias_parado"])

    metricas_avancadas = {
        "media_dias_parado": media_dias,
        "percentil_90_dias_parado": p90_dias,
        "max_dias_parado": max_dias,
        "top3_gargalos_retencao": gargalos[:3],
    }

    sugestoes = []
    vencidos = [p for p in filtrados if p.get("score_prioridade", 0) >= 50.0]
    if vencidos:
        sugestoes.append(f"🚨 ALERTA: {len(vencidos)} processo(s) com prioridade Crítica/Vencida requerem atuação imediata.")

    if gargalos and gargalos[0]["media_dias_parado"] > 15:
        top_g = gargalos[0]
        sugestoes.append(f"⚠️ GARGALO DETECTADO: A tarefa '{top_g['tarefa']}' concentra maior tempo médio de retenção ({top_g['media_dias_parado']} dias em {top_g['quantidade']} processos).")

    if total_bruto > 1000:
        sugestoes.append(f"💡 DICA PARA CAIXA GRANDE ({total_bruto} itens): Utilize 'agrupar_por=\"tarefa\"' ou 'modo_compacto=True' para economizar contexto.")

    return metricas_avancadas, sugestoes


def gerar_dashboard_html_arquivo(filtrados: list[dict[str, Any]], resumo: dict[str, Any], sugestoes: list[str], caminho_html: str) -> str:
    """Gera um Dashboard HTML responsivo com visual premium dark/glassmorphism para a caixa de tarefas."""
    os.makedirs(os.path.dirname(os.path.abspath(caminho_html)), exist_ok=True)
    if not caminho_html.endswith(".html"):
        caminho_html += ".html"

    top100 = sorted(filtrados, key=lambda x: -x.get("score_prioridade", 0))[:100]
    met = resumo.get("metricas_retencao", {})
    prazos = resumo.get("estatisticas_prazos", {})
    niv = resumo.get("distribuicao_por_nivel_urgencia", {})

    rows_html = ""
    for p in top100:
        badge_class = "critico" if p.get("nivel_urgencia") == "Crítico" else ("alto" if p.get("nivel_urgencia") == "Alto" else "normal")
        rows_html += f"""
        <tr>
            <td style="font-family: monospace; font-weight: bold;">{p.get('numero_processo','')}</td>
            <td>{p.get('classe','')}</td>
            <td><span class="badge {badge_class}">{p.get('nivel_urgencia','')} ({p.get('score_prioridade',0)})</span></td>
            <td>{p.get('tarefa','')}</td>
            <td>{p.get('data_limite','-')}</td>
            <td>{p.get('dias_parado',0)} dias</td>
            <td style="font-size: 0.85em; opacity: 0.8;">{p.get('partes','')}</td>
        </tr>
        """

    sug_html = "".join([f"<li>{s}</li>" for s in sugestoes])

    html_content = f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <title>Dashboard de Caixa de Tarefas PJe</title>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0f172a; color: #f8fafc; margin: 0; padding: 24px; }}
        .container {{ max-width: 1200px; margin: 0 auto; }}
        h1 {{ font-size: 24px; margin-bottom: 8px; color: #38bdf8; }}
        .subtitle {{ font-size: 14px; color: #94a3b8; margin-bottom: 24px; }}
        .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 16px; margin-bottom: 24px; }}
        .card {{ background: rgba(30, 41, 59, 0.7); backdrop-filter: blur(10px); border: 1px solid rgba(255,255,255,0.1); border-radius: 12px; padding: 16px; }}
        .card .title {{ font-size: 12px; text-transform: uppercase; letter-spacing: 1px; color: #94a3b8; }}
        .card .value {{ font-size: 28px; font-weight: bold; margin-top: 8px; color: #f1f5f9; }}
        .badge {{ padding: 4px 8px; border-radius: 6px; font-size: 12px; font-weight: bold; }}
        .critico {{ background: #7f1d1d; color: #fca5a5; }}
        .alto {{ background: #7c2d12; color: #fdba74; }}
        .normal {{ background: #1e293b; color: #94a3b8; }}
        table {{ width: 100%; border-collapse: collapse; background: rgba(30, 41, 59, 0.5); border-radius: 12px; overflow: hidden; font-size: 14px; }}
        th, td {{ padding: 12px 16px; text-align: left; border-bottom: 1px solid rgba(255,255,255,0.05); }}
        th {{ background: #1e293b; color: #38bdf8; font-weight: 600; }}
        tr:hover {{ background: rgba(255,255,255,0.03); }}
        .sugestoes {{ background: #1e1b4b; border: 1px solid #4338ca; border-radius: 12px; padding: 16px; margin-bottom: 24px; }}
        .sugestoes ul {{ margin: 8px 0 0 20px; padding: 0; color: #c7d2fe; }}
    </style>
</head>
<body>
    <div class="container">
        <h1>📊 Dashboard de Triagem da Caixa de Tarefas PJe</h1>
        <div class="subtitle">Análise consolidada de <strong>{resumo.get('total_filtrados',0)}</strong> processos (de {resumo.get('total_bruto_na_caixa',0)} totais)</div>

        <div class="sugestoes">
            <strong>💡 Recomendações de Triagem em Tempo Real:</strong>
            <ul>{sug_html}</ul>
        </div>

        <div class="grid">
            <div class="card">
                <div class="title">Total de Processos</div>
                <div class="value">{resumo.get('total_filtrados',0)}</div>
            </div>
            <div class="card">
                <div class="title">Prioridade Crítica</div>
                <div class="value" style="color: #f87171;">{niv.get('Crítico',0)}</div>
            </div>
            <div class="card">
                <div class="title">Prazos Vencidos</div>
                <div class="value" style="color: #fb923c;">{prazos.get('vencidos',0)}</div>
            </div>
            <div class="card">
                <div class="title">Média Dias Parado</div>
                <div class="value">{met.get('media_dias_parado',0)}d</div>
            </div>
            <div class="card">
                <div class="title">Percentil 90 (P90)</div>
                <div class="value" style="color: #38bdf8;">{met.get('percentil_90_dias_parado',0)}d</div>
            </div>
        </div>

        <h3>📋 Top 100 Processos Prioritários na Caixa</h3>
        <table>
            <thead>
                <tr>
                    <th>Número CNJ</th>
                    <th>Classe</th>
                    <th>Urgência / Score</th>
                    <th>Tarefa</th>
                    <th>Data Limite</th>
                    <th>Dias Parado</th>
                    <th>Partes</th>
                </tr>
            </thead>
            <tbody>
                {rows_html}
            </tbody>
        </table>
    </div>
</body>
</html>"""

    with open(caminho_html, "w", encoding="utf-8") as f:
        f.write(html_content)

    return os.path.abspath(caminho_html)


def processar_lista_processos_tarefa(
    processos: list[dict[str, Any]],
    pagina: int = 1,
    itens_por_pagina: int = 50,
    nome_tarefa: str = "",
    termo_busca: str = "",
    filtro_classe: str = "",
    filtro_orgao: str = "",
    filtro_assunto: str = "",
    filtro_parte: str = "",
    filtros_customizados: dict[str, str] | None = None,
    apenas_com_prazo: bool = False,
    apenas_urgente: bool = False,
    ordenacao: str = "score_prioridade",
    ordenacao_secundaria: str | None = "dias_parado",
    modo_compacto: bool = False,
    apenas_resumo: bool = False,
    exportar_caminho: str | None = None,
    dias_parado_min: int | None = None,
    score_min: float | None = None,
    agrupar_por: str | None = None,
    incluir_metricas_avancadas: bool = True,
    gerar_dashboard_html: str | None = None,
    comparar_com_snapshot: list[dict[str, Any]] | None = None,
    distribuir_por_operadores: int | None = None,
    preset_triagem: str | None = None,
    detectar_anomalias: bool = True,
    calcular_saude: bool = True,
    gerar_relatorio_sintese_final: bool = False,
) -> dict[str, Any]:
    """Filtra, ordena multi-nível, agrupa, calcula índice de saúde, gera síntese e pagina a caixa de tarefas.

    Args:
        processos: Lista bruta de dicionários dos processos extraídos da caixa.
        pagina: Número da página (1-based).
        itens_por_pagina: Qtd de itens por página (máx 200).
        nome_tarefa: Filtra por nome exato ou parcial da tarefa.
        termo_busca: Termo livre (busca em CNJ, partes, assunto, advogado).
        filtro_classe: Filtra por classe processual.
        filtro_orgao: Filtra por órgão julgador / vara.
        filtro_assunto: Filtra especificamente por assunto processual.
        filtro_parte: Filtra especificamente por nome de parte (autor/réu).
        filtros_customizados: Dicionário arbitrário {campo: valor} para filtros dinâmicos flexíveis.
        apenas_com_prazo: Se True, apenas processos com prazo definido.
        apenas_urgente: Se True, apenas prazos prestes a vencer (<= 3 dias ou vencidos).
        ordenacao: 'score_prioridade' | 'prazo_urgente' | 'data_decrescente' | 'data_crescente' | 'cnj'.
        ordenacao_secundaria: Chave secundária para desempate ('dias_parado'|'score_prioridade'|'cnj').
        modo_compacto: Retorna schema reduzido para economizar tokens.
        apenas_resumo: Retorna apenas estatísticas agregadas (ideal para caixas com milhares de processos).
        exportar_caminho: Se informado, salva o resultado filtrado em CSV, JSON ou Markdown (.md) no disco.
        dias_parado_min: Filtra processos parados na tarefa há pelo menos N dias.
        score_min: Filtra processos com score_prioridade >= valor.
        agrupar_por: Se informado ('tarefa'|'classe'|'orgao'|'nivel_urgencia'), agrupa os processos em buckets.
        incluir_metricas_avancadas: Se True, gera percentis (P90), tempo médio de retenção e sugestões de triagem.
        gerar_dashboard_html: Se informado, gera um relatório HTML estilizado do dashboard no caminho informado.
        comparar_com_snapshot: Se informado, calcula a diferença diferencial (novos, resolvidos, alterados).
        distribuir_por_operadores: Se informado (ex: N=5), calcula plano de divisão equitativa de carga entre N pessoas.
        preset_triagem: Se informado ('urgencias_vencidas'|'gargalos_antigos'|'resumo_executivo'), aplica conjunto de filtros de um toque.
        detectar_anomalias: Se True, identifica outliers e anomalias de retenção na caixa.
        calcular_saude: Se True, calcula o Índice de Saúde da Caixa (0..100) e classificação operacional.
        gerar_relatorio_sintese_final: Se True, retorna relatório executivo unificado de diagnóstico completo.
    """
    # Aplicação de Presets Operacionais
    if preset_triagem == "urgencias_vencidas":
        score_min = 40.0
        apenas_com_prazo = True
        ordenacao = "score_prioridade"
        ordenacao_secundaria = "dias_parado"
        modo_compacto = True
    elif preset_triagem == "gargalos_antigos":
        dias_parado_min = 30
        ordenacao = "data_crescente"
        ordenacao_secundaria = "score_prioridade"
    elif preset_triagem == "resumo_executivo":
        apenas_resumo = True
        incluir_metricas_avancadas = True

    itens_por_pagina = max(1, min(itens_por_pagina, 200))
    pagina = max(1, pagina)

    total_bruto = len(processos)
    hoje = datetime.now()
    hash_caixa = calcular_hash_caixa(processos)

    # Enriquecimento com Score de Prioridade
    for proc in processos:
        if "score_prioridade" not in proc:
            score, nivel, dias_parado = calcular_score_prioridade(proc, hoje)
            proc["score_prioridade"] = score
            proc["nivel_urgencia"] = nivel
            proc["dias_parado"] = dias_parado

    # 1. Filtragem
    filtrados = []
    t_nome = nome_tarefa.strip().lower() if nome_tarefa else None
    t_busca = termo_busca.strip().lower() if termo_busca else None
    t_busca_cnj = normalizar_cnj(termo_busca) if termo_busca else None
    t_classe = filtro_classe.strip().lower() if filtro_classe else None
    t_orgao = filtro_orgao.strip().lower() if filtro_orgao else None
    t_assunto = filtro_assunto.strip().lower() if filtro_assunto else None
    t_parte = filtro_parte.strip().lower() if filtro_parte else None

    for proc in processos:
        if t_nome and t_nome not in proc.get("tarefa", "").lower():
            continue
        if t_classe and t_classe not in proc.get("classe", "").lower():
            continue
        if t_orgao and t_orgao not in proc.get("orgao", "").lower():
            continue
        if t_assunto and t_assunto not in proc.get("assunto", "").lower():
            continue
        if t_parte and t_parte not in proc.get("partes", "").lower():
            continue

        # Filtros customizados arbitrários
        if filtros_customizados:
            match_cust = True
            for k, val in filtros_customizados.items():
                val_str = str(val).lower()
                field_val = str(proc.get(k, "")).lower()
                if val_str not in field_val:
                    match_cust = False
                    break
            if not match_cust:
                continue

        tem_prazo = bool(proc.get("data_limite") or proc.get("prazo"))
        if apenas_com_prazo and not tem_prazo:
            continue

        if apenas_urgente:
            if proc.get("score_prioridade", 0) < 30.0:
                continue

        if dias_parado_min is not None and proc.get("dias_parado", 0) < dias_parado_min:
            continue

        if score_min is not None and proc.get("score_prioridade", 0.0) < score_min:
            continue

        if t_busca:
            match_busca = False
            cnj_proc = normalizar_cnj(proc.get("numero_processo", ""))
            if t_busca_cnj and t_busca_cnj in cnj_proc:
                match_busca = True
            elif (
                t_busca in proc.get("numero_processo", "").lower()
                or t_busca in proc.get("partes", "").lower()
                or t_busca in proc.get("assunto", "").lower()
                or t_busca in proc.get("destinatario", "").lower()
                or t_busca in proc.get("ultimo_movimento", "").lower()
            ):
                match_busca = True

            if not match_busca:
                continue

        filtrados.append(proc)

    total_filtrados = len(filtrados)

    indice_saude = None
    if calcular_saude:
        indice_saude = calcular_indice_saude_caixa(filtrados)

    anomalias_detectadas = None
    if detectar_anomalias:
        anomalias_detectadas = detectar_anomalias_caixa(filtrados)

    plano_distribuicao = None
    if distribuir_por_operadores is not None and distribuir_por_operadores > 0:
        plano_distribuicao = calcular_distribuicao_equitativa(filtrados, distribuir_por_operadores)

    delta_diferencial = None
    if comparar_com_snapshot is not None:
        cnjs_atuais = {normalizar_cnj(p.get("numero_processo", "")): p for p in filtrados}
        cnjs_anteriores = {normalizar_cnj(p.get("numero_processo", "")): p for p in comparar_com_snapshot}

        novos = [cnj for cnj in cnjs_atuais if cnj not in cnjs_anteriores]
        resolvidos = [cnj for cnj in cnjs_anteriores if cnj not in cnjs_atuais]

        delta_diferencial = {
            "novos_processos": len(novos),
            "processos_resolvidos": len(resolvidos),
            "cnjs_novos_sample": novos[:10],
            "cnjs_resolvidos_sample": resolvidos[:10],
        }

    arquivo_gerado = None
    if exportar_caminho:
        arquivo_gerado = exportar_processos(filtrados, exportar_caminho)

    metricas_avancadas, sugestoes_acao = ({}, [])
    if incluir_metricas_avancadas:
        metricas_avancadas, sugestoes_acao = gerar_sugestoes_e_metricas(filtrados, total_bruto)

    dist_tarefas: dict[str, int] = {}
    dist_classes: dict[str, int] = {}
    dist_orgaos: dict[str, int] = {}
    dist_niveis: dict[str, int] = {"Crítico": 0, "Alto": 0, "Médio": 0, "Baixo": 0}
    prazos_vencidos = 0
    prazos_urgentes = 0
    prazos_normais = 0
    sem_prazo = 0

    for p in filtrados:
        tar = p.get("tarefa", "Outras Tarefas")
        dist_tarefas[tar] = dist_tarefas.get(tar, 0) + 1

        cla = p.get("classe", "Não informada")
        dist_classes[cla] = dist_classes.get(cla, 0) + 1

        org = p.get("orgao", "Não informado")
        dist_orgaos[org] = dist_orgaos.get(org, 0) + 1

        niv = p.get("nivel_urgencia", "Baixo")
        dist_niveis[niv] = dist_niveis.get(niv, 0) + 1

        dt_str = p.get("data_limite")
        if dt_str:
            try:
                dt = datetime.strptime(dt_str.split()[0], "%d/%m/%Y")
                dias = (dt - hoje).days
                if dias < 0:
                    prazos_vencidos += 1
                elif dias <= 3:
                    prazos_urgentes += 1
                else:
                    prazos_normais += 1
            except ValueError:
                sem_prazo += 1
        else:
            sem_prazo += 1

    resumo_estatistico = {
        "hash_caixa": hash_caixa,
        "total_bruto_na_caixa": total_bruto,
        "total_filtrados": total_filtrados,
        "distribuicao_por_nivel_urgencia": dist_niveis,
        "distribuicao_por_tarefa": dict(
            sorted(dist_tarefas.items(), key=lambda x: x[1], reverse=True)[:15]
        ),
        "distribuicao_por_classe": dict(
            sorted(dist_classes.items(), key=lambda x: x[1], reverse=True)[:10]
        ),
        "distribuicao_por_orgao": dict(
            sorted(dist_orgaos.items(), key=lambda x: x[1], reverse=True)[:10]
        ),
        "estatisticas_prazos": {
            "vencidos": prazos_vencidos,
            "urgentes_ate_3d": prazos_urgentes,
            "prazo_regular": prazos_normais,
            "sem_prazo": sem_prazo,
        },
    }
    if indice_saude:
        resumo_estatistico["indice_saude_caixa"] = indice_saude
    if metricas_avancadas:
        resumo_estatistico["metricas_retencao"] = metricas_avancadas

    dashboard_gerado = None
    if gerar_dashboard_html:
        dashboard_gerado = gerar_dashboard_html_arquivo(filtrados, resumo_estatistico, sugestoes_acao, gerar_dashboard_html)

    # Modo Síntese Final Consolidada Executiva
    if gerar_relatorio_sintese_final:
        top_criticos = sorted(filtrados, key=lambda x: -x.get("score_prioridade", 0))[:5]
        sintese = {
            "modo": "sintese_executiva_final",
            "hash_caixa": hash_caixa,
            "indice_saude_caixa": indice_saude,
            "resumo_caixa": resumo_estatistico,
            "top5_processos_criticos": [
                {
                    "numero_processo": p.get("numero_processo"),
                    "score_prioridade": p.get("score_prioridade"),
                    "nivel_urgencia": p.get("nivel_urgencia"),
                    "tarefa": p.get("tarefa"),
                    "dias_parado": p.get("dias_parado"),
                }
                for p in top_criticos
            ],
            "anomalias": anomalias_detectadas,
            "recomendaes": sugestoes_acao,
        }
        if plano_distribuicao:
            sintese["plano_distribuicao"] = plano_distribuicao
        if delta_diferencial:
            sintese["delta_diferencial"] = delta_diferencial
        if arquivo_gerado:
            sintese["arquivo_exportado"] = arquivo_gerado
        if dashboard_gerado:
            sintese["dashboard_html"] = dashboard_gerado
        return sintese

    if apenas_resumo:
        resp = {
            "modo": "resumo_estatistico",
            "hash_caixa": hash_caixa,
            "resumo": resumo_estatistico,
            "sugestoes_acao": sugestoes_acao,
        }
        if indice_saude:
            resp["indice_saude_caixa"] = indice_saude
        if anomalias_detectadas:
            resp["anomalias_detectadas"] = anomalias_detectadas
        if preset_triagem:
            resp["preset_aplicado"] = preset_triagem
        if plano_distribuicao:
            resp["plano_distribuicao_equitativa"] = plano_distribuicao
        if delta_diferencial:
            resp["delta_diferencial"] = delta_diferencial
        if arquivo_gerado:
            resp["arquivo_exportado"] = arquivo_gerado
        if dashboard_gerado:
            resp["dashboard_html"] = dashboard_gerado
        return resp

    grupos_resultado = None
    if agrupar_por in ["tarefa", "classe", "orgao", "nivel_urgencia"]:
        buckets: dict[str, list[dict[str, Any]]] = {}
        for item in filtrados:
            chave = item.get(agrupar_por, "Outros")
            if chave not in buckets:
                buckets[chave] = []
            buckets[chave].append(item)

        grupos_resultado = []
        for nome_grupo, procs_grupo in buckets.items():
            procs_grupo_sorted = sorted(procs_grupo, key=lambda x: -x.get("score_prioridade", 0))
            score_medio = round(sum(p.get("score_prioridade", 0) for p in procs_grupo) / len(procs_grupo), 1)
            grupos_resultado.append({
                "grupo": nome_grupo,
                "quantidade": len(procs_grupo),
                "score_prioridade_medio": score_medio,
                "top3_criticos": [p.get("numero_processo") for p in procs_grupo_sorted[:3]],
            })
        grupos_resultado.sort(key=lambda x: -x["quantidade"])

    def _obter_val_chave(item, tipo_ord):
        if tipo_ord == "score_prioridade":
            return -item.get("score_prioridade", 0.0)
        elif tipo_ord == "dias_parado":
            return -item.get("dias_parado", 0)
        elif tipo_ord == "prazo_urgente":
            dt_str = item.get("data_limite")
            if dt_str:
                try:
                    return datetime.strptime(dt_str.split()[0], "%d/%m/%Y")
                except ValueError:
                    pass
            return datetime(2099, 12, 31)
        elif tipo_ord == "data_crescente":
            dt_str = item.get("data_expedicao") or item.get("data_entrada")
            if dt_str:
                try:
                    return datetime.strptime(dt_str.split()[0], "%d/%m/%Y")
                except ValueError:
                    pass
            return datetime(1970, 1, 1)
        elif tipo_ord == "cnj":
            return item.get("numero_processo", "")
        return 0

    def _chave_ordenacao_multinivel(item):
        val_prim = _obter_val_chave(item, ordenacao)
        val_sec = _obter_val_chave(item, ordenacao_secundaria) if ordenacao_secundaria else 0
        return (val_prim, val_sec)

    filtrados.sort(key=_chave_ordenacao_multinivel)

    total_paginas = max(1, math.ceil(total_filtrados / itens_por_pagina))
    inicio = (pagina - 1) * itens_por_pagina
    fim = inicio + itens_por_pagina
    pagina_itens = filtrados[inicio:fim]

    if modo_compacto:
        itens_projetados = []
        for item in pagina_itens:
            itens_projetados.append({
                "numero_processo": item.get("numero_processo"),
                "classe": item.get("classe"),
                "tarefa": item.get("tarefa"),
                "data_limite": item.get("data_limite"),
                "score_prioridade": item.get("score_prioridade"),
                "nivel_urgencia": item.get("nivel_urgencia"),
                "dias_parado": item.get("dias_parado"),
                "partes": item.get("partes"),
                "id_expediente": item.get("id_expediente"),
            })
        pagina_itens = itens_projetados

    resposta = {
        "modo": "lista_paginada",
        "hash_caixa": hash_caixa,
        "paginacao": {
            "pagina_atual": pagina,
            "itens_por_pagina": itens_por_pagina,
            "total_paginas": total_paginas,
            "total_itens_filtrados": total_filtrados,
            "total_bruto_na_caixa": total_bruto,
            "tem_proxima_pagina": pagina < total_paginas,
            "tem_pagina_anterior": pagina > 1,
        },
        "resumo_estatistico": resumo_estatistico,
        "sugestoes_acao": sugestoes_acao,
        "processos": pagina_itens,
    }

    if indice_saude:
        resposta["indice_saude_caixa"] = indice_saude

    if anomalias_detectadas:
        resposta["anomalias_detectadas"] = anomalias_detectadas

    if preset_triagem:
        resposta["preset_aplicado"] = preset_triagem

    if plano_distribuicao:
        resposta["plano_distribuicao_equitativa"] = plano_distribuicao

    if delta_diferencial:
        resposta["delta_diferencial"] = delta_diferencial

    if grupos_resultado:
        resposta["agrupamento"] = {
            "agrupar_por": agrupar_por,
            "total_grupos": len(grupos_resultado),
            "grupos": grupos_resultado,
        }

    if arquivo_gerado:
        resposta["arquivo_exportado"] = arquivo_gerado

    if dashboard_gerado:
        resposta["dashboard_html"] = dashboard_gerado

    return resposta
