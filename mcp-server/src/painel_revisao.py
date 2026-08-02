"""CLI Review Panel Interface (`painel_revisao.py`).

Provides PainelRevisao class for terminal inspection and human review of process analysis reports.
Renders summary overview, 6D completeness breakdown, domain profile findings, timeline, and material conflicts.
"""

from __future__ import annotations

import json
import math
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


class PainelRevisao:
    def __init__(self, analysis_report: Dict[str, Any]):
        self.report = analysis_report or {}
        self.process_number = str(self.report.get("process_number") or self.report.get("numero_processo") or "Desconhecido")
        self.status = str(self.report.get("status") or "concluído")

    def render_summary(self) -> str:
        """Renders executive summary table for CLI."""
        completeness = self.report.get("completeness") or self.report.get("completude") or 0.0
        if isinstance(completeness, dict):
            score = completeness.get("overall_score", completeness.get("score", 0.0))
        else:
            score = completeness
        score_pct = _safe_format_score(score)

        domain = self.report.get("domain_profile") or self.report.get("perfil_dominio") or {}
        profile_name = domain.get("profile_name", "Geral") if isinstance(domain, dict) else str(domain)

        lines = [
            "============================================================",
            f"   PAINEL DE REVISÃO PROCESSUAL CLI — PROCESSO {self.process_number}",
            "============================================================",
            f"  • Número do Processo: {self.process_number}",
            f"  • Status da Análise : {self.status.upper()}",
            f"  • Score Completenesse: {score_pct}%",
            f"  • Perfil de Domínio : {profile_name}",
            "------------------------------------------------------------",
        ]
        return "\n".join(lines)

    def render_completeness(self) -> str:
        """Renders 6D completeness gauge for terminal."""
        completeness = self.report.get("completeness") or self.report.get("completude") or {}
        if isinstance(completeness, dict):
            score = completeness.get("overall_score", 0.0)
            dims = completeness.get("dimensions", completeness)
        else:
            score = completeness
            dims = {}

        score_pct = _safe_format_score(score)
        bar_len = 20
        filled = int(bar_len * (score_pct / 100))
        gauge_bar = "[" + "█" * filled + "░" * (bar_len - filled) + "]"

        lines = [
            "\n[📊 COMPLETUDE MULTIDIMENSIONAR 6D]",
            f"Score Geral: {gauge_bar} {score_pct}%",
        ]

        dim_labels = [
            ("origin", "Origem dos Dados"),
            ("documents", "Cobertura Documental"),
            ("attachments", "Integridade Anexos"),
            ("pages", "Legibilidade Páginas"),
            ("metadata", "Metadados"),
            ("temporal", "Coerência Temporal"),
        ]

        for k, label in dim_labels:
            val = dims.get(k, 0.0)
            if isinstance(val, dict):
                val = val.get("score", 0.0)
            pct = _safe_format_score(val)
            f_len = int(bar_len * (pct / 100))
            sub_bar = "[" + "█" * f_len + "░" * (bar_len - f_len) + "]"
            lines.append(f"  • {label:<22}: {sub_bar} {pct}%")

        return "\n".join(lines)

    def render_domain_profile(self) -> str:
        """Renders domain profile entities and validation results."""
        domain = self.report.get("domain_profile") or self.report.get("perfil_dominio") or {}
        if not domain:
            return "\n[⚖️ PERFIL DE DOMÍNIO]\n  Nenhum perfil específico aplicado."

        profile_name = domain.get("profile_name", "Geral") if isinstance(domain, dict) else str(domain)
        details = domain.get("details") or domain.get("entities") if isinstance(domain, dict) else domain

        lines = [
            "\n[⚖️ PERFIL DE DOMÍNIO]",
            f"  • Perfil Ativo: {profile_name}",
            "  • Entidades Extraídas:",
        ]

        if isinstance(details, dict):
            for k, v in details.items():
                v_str = json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v)
                lines.append(f"    - {k}: {v_str}")
        else:
            lines.append(f"    - Details: {details}")

        return "\n".join(lines)

    def render_conflicts(self) -> str:
        """Renders conflict engine findings with explicit abstention statuses."""
        conflicts = self.report.get("conflicts") or self.report.get("conflitos") or []
        lines = ["\n[🛑 MOTOR DE CONFLITOS & ABSTENÇÕES EXPLÍCITAS]"]

        if not conflicts:
            lines.append("  ✔ Nenhum conflito material ou abstenção registrada.")
            return "\n".join(lines)

        for idx, conf in enumerate(conflicts, 1):
            if isinstance(conf, dict):
                cat = conf.get("category", "Conflito")
                topic = conf.get("topic", "Divergência")
                status = conf.get("status", "unresolved")
                reason = conf.get("abstention_reason") or conf.get("reason") or "Sem justificativa"
                lines.append(f"  {idx}. [{status.upper()}] {cat} - {topic}")
                if status == "abstained":
                    lines.append(f"     -> Motivo da Abstenção Explícita: {reason}")
            else:
                lines.append(f"  {idx}. Conflito: {conf}")

        return "\n".join(lines)

    def render_timeline(self) -> str:
        """Renders timeline of procedural events."""
        events = self.report.get("timeline") or self.report.get("linha_do_tempo") or []
        lines = ["\n[📅 LINHA DO TEMPO PROCESSUAL]"]

        if not events:
            lines.append("  Nenhum evento registrado.")
            return "\n".join(lines)

        for ev in events:
            if isinstance(ev, dict):
                dt = ev.get("date") or ev.get("data") or "Data N/A"
                title = ev.get("title") or ev.get("event") or ev.get("descricao") or "Evento"
                lines.append(f"  • {dt} | {title}")
            else:
                lines.append(f"  • {ev}")

        return "\n".join(lines)

    def render_full_report(self) -> str:
        """Combines all CLI views into a full terminal report string."""
        sections = [
            self.render_summary(),
            self.render_completeness(),
            self.render_domain_profile(),
            self.render_timeline(),
            self.render_conflicts(),
            "\n============================================================",
        ]
        return "\n".join(sections)

    def display_in_terminal(self) -> None:
        """Prints the full CLI report to terminal stdout."""
        print(self.render_full_report())
