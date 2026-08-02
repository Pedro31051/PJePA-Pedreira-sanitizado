"""PJe Complete Process Analysis Dashboard Generator (`generate_pje_dashboard.py`).

Generates a self-contained, responsive HTML dashboard rendering executive overview cards,
6D completeness gauge, domain profile status (Inventory/Usucapião), missing document alerts,
timeline of procedural events, and explicit conflict alerts.
"""

from __future__ import annotations

import html
import json
import math
from datetime import datetime, timezone
from typing import Any, Dict


def _safe_format_score(val: Any) -> int:
    """Safely format score to integer percentage [0, 100], handling NaN, Inf, None, invalid types."""
    if val is None or not isinstance(val, (int, float)):
        return 0
    if isinstance(val, float) and (math.isnan(val) or math.isinf(val)):
        return 0

    fval = float(val)
    if fval < 0.0:
        fval = 0.0

    if fval <= 1.0:
        pct = int(fval * 100)
    else:
        pct = int(fval)

    return max(0, min(100, pct))


def generate_html_dashboard(analysis_report: Dict[str, Any]) -> str:
    """Generate self-contained HTML dashboard from analysis report dictionary."""
    process_number = html.escape(str(analysis_report.get("process_number") or analysis_report.get("numero_processo") or "N/A"))
    status = html.escape(str(analysis_report.get("status") or "completed"))

    # Completeness extraction
    completeness_data = analysis_report.get("completeness") or analysis_report.get("completude") or {}
    if isinstance(completeness_data, (int, float)):
        overall_score = completeness_data
        dimensions = {}
    elif isinstance(completeness_data, dict):
        overall_score = completeness_data.get("overall_score", completeness_data.get("score", 0.0))
        dimensions = completeness_data.get("dimensions", completeness_data)
    else:
        overall_score = 0.0
        dimensions = {}

    overall_percent = _safe_format_score(overall_score)

    # Domain profile extraction
    domain_data = analysis_report.get("domain_profile") or analysis_report.get("perfil_dominio") or {}
    if isinstance(domain_data, str):
        profile_name = domain_data
        domain_details = {}
    elif isinstance(domain_data, dict):
        profile_name = str(domain_data.get("profile_name") or domain_data.get("nome") or "Geral")
        domain_details = domain_data.get("details") or domain_data.get("entities") or domain_data
    else:
        profile_name = "Geral"
        domain_details = {}

    profile_name_escaped = html.escape(profile_name)

    # Missing documents / pendencias
    missing_docs = analysis_report.get("missing_documents") or analysis_report.get("pendencias") or analysis_report.get("missing_requirements") or []
    if isinstance(missing_docs, str):
        missing_docs = [missing_docs]

    # Timeline events
    timeline_events = analysis_report.get("timeline") or analysis_report.get("linha_do_tempo") or []

    # Conflicts
    conflicts_data = analysis_report.get("conflicts") or analysis_report.get("conflitos") or []

    # Format Date
    generated_at = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M:%S UTC")

    # Build 6D Dimensions HTML
    dim_keys = [
        ("origin", "Origem dos Dados"),
        ("documents", "Cobertura Documental"),
        ("attachments", "Integridade dos Anexos"),
        ("pages", "Legibilidade de Páginas"),
        ("metadata", "Metadados & Indexação"),
        ("temporal", "Coerência Temporal"),
    ]

    dim_cards_html = ""
    for key, label in dim_keys:
        val = dimensions.get(key, dimensions.get(label, 0.0))
        if isinstance(val, dict):
            val = val.get("score", 0.0)
        score_pct = _safe_format_score(val)

        color = "#10b981" if score_pct >= 80 else "#f59e0b" if score_pct >= 50 else "#ef4444"
        dim_cards_html += f"""
        <div class="dim-card">
            <div class="dim-label">{html.escape(label)}</div>
            <div class="dim-bar-container">
                <div class="dim-bar" style="width: {score_pct}%; background-color: {color};"></div>
            </div>
            <div class="dim-val">{score_pct}%</div>
        </div>
        """

    # Build Missing Docs HTML
    missing_html = ""
    if missing_docs:
        for doc in missing_docs:
            doc_str = html.escape(str(doc.get("description") or doc.get("requirement") or doc if isinstance(doc, dict) else str(doc)))
            missing_html += f'<li class="alert-item"><span class="badge badge-danger">PENDENTE</span> {doc_str}</li>'
    else:
        missing_html = '<li class="text-success">✔ Nenhuma pendência documental detectada.</li>'

    # Build Timeline HTML
    timeline_html = ""
    if timeline_events:
        for event in timeline_events:
            if isinstance(event, dict):
                date_str = html.escape(str(event.get("date") or event.get("data") or "Data N/A"))
                title_str = html.escape(str(event.get("title") or event.get("descricao") or event.get("event") or "Evento"))
                desc_str = html.escape(str(event.get("description") or event.get("detalhes") or ""))
            else:
                date_str = "Data N/A"
                title_str = html.escape(str(event))
                desc_str = ""

            timeline_html += f"""
            <div class="timeline-step">
                <div class="timeline-badge"></div>
                <div class="timeline-panel">
                    <div class="timeline-date">{date_str}</div>
                    <div class="timeline-title">{title_str}</div>
                    {f'<div class="timeline-desc">{desc_str}</div>' if desc_str else ''}
                </div>
            </div>
            """
    else:
        timeline_html = '<p class="text-muted">Nenhum evento registrado na linha do tempo.</p>'

    # Build Conflicts HTML
    conflicts_html = ""
    if conflicts_data:
        for conflict in conflicts_data:
            if isinstance(conflict, dict):
                cat = html.escape(str(conflict.get("category") or conflict.get("categoria") or "Conflito Material"))
                topic = html.escape(str(conflict.get("topic") or conflict.get("topico") or "Divergência"))
                status_c = html.escape(str(conflict.get("status") or "unresolved"))
                reason = html.escape(str(conflict.get("abstention_reason") or conflict.get("reason") or "Divergência de provas"))
            else:
                cat = "Conflito Material"
                topic = html.escape(str(conflict))
                status_c = "abstained"
                reason = "Divergência documental"

            badge_class = "badge-warning" if status_c == "abstained" else "badge-danger"
            badge_text = f"ABSTENÇÃO EXPLÍCITA ({reason})" if status_c == "abstained" else f"STATUS: {status_c.upper()}"

            conflicts_html += f"""
            <div class="conflict-box">
                <div class="conflict-header">
                    <span class="conflict-category">{cat}</span> — <strong>{topic}</strong>
                    <span class="badge {badge_class}">{badge_text}</span>
                </div>
            </div>
            """
    else:
        conflicts_html = '<p class="text-success">✔ Zero conflitos ou divergências materiais detectados no processo.</p>'

    # Domain details HTML formatting
    domain_details_json = html.escape(json.dumps(domain_details, indent=2, ensure_ascii=False))

    # Overall Gauge Color
    gauge_color = "#10b981" if overall_percent >= 80 else "#f59e0b" if overall_percent >= 50 else "#ef4444"

    return f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Painel de Análise Processual - {process_number}</title>
    <style>
        :root {{
            --bg-primary: #0f172a;
            --bg-card: #1e293b;
            --text-main: #f8fafc;
            --text-muted: #94a3b8;
            --accent-blue: #3b82f6;
            --accent-green: #10b981;
            --accent-amber: #f59e0b;
            --accent-red: #ef4444;
            --border-color: #334155;
        }}
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            background-color: var(--bg-primary);
            color: var(--text-main);
            padding: 24px;
            line-height: 1.5;
        }}
        .header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding-bottom: 20px;
            border-bottom: 1px solid var(--border-color);
            margin-bottom: 24px;
        }}
        .header h1 {{ font-size: 1.75rem; font-weight: 700; color: #ffffff; }}
        .header .subtitle {{ color: var(--text-muted); font-size: 0.875rem; }}
        .cards-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
            gap: 16px;
            margin-bottom: 24px;
        }}
        .card {{
            background: var(--bg-card);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            padding: 20px;
        }}
        .card-title {{ font-size: 0.875rem; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.05em; font-weight: 600; margin-bottom: 8px; }}
        .card-value {{ font-size: 1.75rem; font-weight: 700; }}
        .card-subtext {{ font-size: 0.75rem; color: var(--text-muted); margin-top: 4px; }}
        
        .main-grid {{
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 24px;
            margin-bottom: 24px;
        }}
        @media (max-width: 900px) {{ .main-grid {{ grid-template-columns: 1fr; }} }}

        .section-title {{
            font-size: 1.25rem;
            font-weight: 600;
            margin-bottom: 16px;
            display: flex;
            align-items: center;
            gap: 8px;
        }}

        /* Gauge */
        .gauge-container {{
            display: flex;
            align-items: center;
            gap: 24px;
            margin-bottom: 20px;
        }}
        .gauge-circle {{
            width: 100px;
            height: 100px;
            border-radius: 50%;
            background: conic-gradient({gauge_color} {overall_percent}%, var(--border-color) 0);
            display: flex;
            align-items: center;
            justify-content: center;
            position: relative;
        }}
        .gauge-circle::before {{
            content: "";
            position: absolute;
            width: 76px;
            height: 76px;
            border-radius: 50%;
            background: var(--bg-card);
        }}
        .gauge-text {{
            position: relative;
            z-index: 1;
            font-size: 1.5rem;
            font-weight: 800;
            color: var(--text-main);
        }}

        .dim-card {{ margin-bottom: 12px; }}
        .dim-label {{ font-size: 0.875rem; color: var(--text-muted); margin-bottom: 4px; display: flex; justify-content: space-between; }}
        .dim-bar-container {{ height: 8px; background: var(--border-color); border-radius: 4px; overflow: hidden; }}
        .dim-bar {{ height: 100%; transition: width 0.3s ease; }}
        .dim-val {{ font-size: 0.75rem; color: var(--text-muted); text-align: right; margin-top: 2px; }}

        /* Alerts & Lists */
        ul.alert-list {{ list-style: none; }}
        li.alert-item {{ margin-bottom: 8px; padding: 10px 14px; background: rgba(239, 68, 68, 0.1); border-left: 4px solid var(--accent-red); border-radius: 4px; font-size: 0.9rem; }}
        .badge {{ display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 0.75rem; font-weight: 700; text-transform: uppercase; margin-right: 6px; }}
        .badge-danger {{ background: var(--accent-red); color: white; }}
        .badge-warning {{ background: var(--accent-amber); color: black; }}
        .badge-success {{ background: var(--accent-green); color: white; }}

        /* Timeline */
        .timeline-step {{ display: flex; gap: 16px; margin-bottom: 16px; position: relative; }}
        .timeline-badge {{ width: 12px; height: 12px; border-radius: 50%; background: var(--accent-blue); margin-top: 6px; flex-shrink: 0; }}
        .timeline-panel {{ background: rgba(255,255,255,0.03); padding: 12px 16px; border-radius: 8px; border: 1px solid var(--border-color); flex-grow: 1; }}
        .timeline-date {{ font-size: 0.75rem; color: var(--accent-blue); font-weight: 600; }}
        .timeline-title {{ font-size: 0.95rem; font-weight: 600; margin-top: 2px; }}
        .timeline-desc {{ font-size: 0.85rem; color: var(--text-muted); margin-top: 4px; }}

        /* Conflicts */
        .conflict-box {{ background: rgba(245, 158, 11, 0.08); border: 1px solid var(--accent-amber); border-radius: 8px; padding: 14px; margin-bottom: 12px; }}
        .conflict-header {{ font-size: 0.9rem; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px; }}

        pre.code-block {{ background: #090d16; padding: 14px; border-radius: 8px; color: #a7f3d0; font-family: monospace; font-size: 0.85rem; overflow-x: auto; }}
        .text-success {{ color: var(--accent-green); }}
        .text-muted {{ color: var(--text-muted); }}
    </style>
</head>
<body>
    <div class="header">
        <div>
            <h1>Painel de Análise Processual — PJe</h1>
            <div class="subtitle">Processo nº {process_number} | Perfil: {profile_name_escaped}</div>
        </div>
        <div style="text-align: right;">
            <span class="badge badge-success">{status.upper()}</span>
            <div class="subtitle" style="margin-top:4px;">Gerado em {generated_at}</div>
        </div>
    </div>

    <!-- Cards Overview -->
    <div class="cards-grid">
        <div class="card">
            <div class="card-title">Número do Processo</div>
            <div class="card-value">{process_number}</div>
            <div class="card-subtext">Tribunal de Justiça do Pará</div>
        </div>
        <div class="card">
            <div class="card-title">Completude Multidimensional</div>
            <div class="card-value" style="color: {gauge_color};">{overall_percent}%</div>
            <div class="card-subtext">Avaliação 6D unificada</div>
        </div>
        <div class="card">
            <div class="card-title">Perfil de Rito</div>
            <div class="card-value" style="font-size: 1.3rem;">{profile_name_escaped}</div>
            <div class="card-subtext">Regras de negócio aplicadas</div>
        </div>
        <div class="card">
            <div class="card-title">Status da Análise</div>
            <div class="card-value" style="font-size: 1.3rem; color: var(--accent-blue);">{status.upper()}</div>
            <div class="card-subtext">Auditoria concluída com sucesso</div>
        </div>
    </div>

    <div class="main-grid">
        <!-- Completeness 6D Gauge Section -->
        <div class="card">
            <div class="section-title">📊 Cobertura & Completude 6D</div>
            <div class="gauge-container">
                <div class="gauge-circle">
                    <div class="gauge-text">{overall_percent}%</div>
                </div>
                <div>
                    <div style="font-weight: 600; font-size: 1.1rem;">Score Global</div>
                    <div class="text-muted" style="font-size: 0.85rem;">Média ponderada das 6 dimensões de completude</div>
                </div>
            </div>
            <div>
                {dim_cards_html}
            </div>
        </div>

        <!-- Missing Docs & Domain Profile -->
        <div class="card">
            <div class="section-title">⚠️ Requisitos & Pendências Documentais</div>
            <ul class="alert-list">
                {missing_html}
            </ul>

            <div class="section-title" style="margin-top: 24px;">⚖️ Entidades do Perfil de Domínio ({profile_name_escaped})</div>
            <pre class="code-block">{domain_details_json}</pre>
        </div>
    </div>

    <div class="main-grid">
        <!-- Timeline Section -->
        <div class="card">
            <div class="section-title">📅 Linha do Tempo Processual</div>
            {timeline_html}
        </div>

        <!-- Conflict Alerts Section -->
        <div class="card">
            <div class="section-title">🛑 Motor de Conflitos & Abstenções Explícitas</div>
            {conflicts_html}
        </div>
    </div>
</body>
</html>
"""
