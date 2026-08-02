"""Page objects e locators semânticos do PJe-TJPA.

Este módulo não autentica e não contém regras de negócio. Ele encapsula apenas
a navegação de leitura na interface, mantendo seletores instáveis fora do
cliente e do servidor MCP.
"""

from __future__ import annotations

import asyncio
import re
import time
import unicodedata
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import urljoin, urlsplit

CNJ_PATTERN = re.compile(r"\d{7}-\d{2}\.\d{4}\.\d\.\d{2}\.\d{4}")
GENERAL_SEARCH_ROUTE = re.compile(
    r"#/(?:[^?#]*/)?consulta-processual(?:[/?#]|$)", re.IGNORECASE
)
MAX_PAGINAS_CONSULTA_GERAL = 80


class ProcessRegistrationPage:
    """Cadastro do PJe com inventário sanitizado e fail-closed.

    Este page object nunca clica em controles de protocolo ou assinatura.
    Ele somente abre a tela e descreve os campos visíveis, sem devolver
    valores preenchidos, tokens JSF ou texto processual.
    """

    ROUTE = "/Processo/cadastrar.seam?newInstance=true"
    FINAL_ACTION = re.compile(
        r"\b(?:protocolar|assinar|enviar\s+processo)\b", re.IGNORECASE
    )
    INITIAL_SELECTORS = {
        "Matéria": "select[id$='areaDireitoCombo']",
        "Jurisdição": "select[id$='jurisdicaoCombo']",
        "Classe judicial": "select[id$='classeJudicialCombo']",
    }

    def __init__(self, page: Any, url_base: str):
        self.page = page
        self.url_base = url_base.rstrip("/")

    async def open(self) -> None:
        await self.page.goto(
            f"{self.url_base}{self.ROUTE}",
            wait_until="domcontentloaded",
        )
        await self.page.locator("body").wait_for(
            state="attached", timeout=20_000
        )
        heading = self.page.get_by_text(
            re.compile(r"Cadastro de processo", re.IGNORECASE)
        ).first
        try:
            await heading.wait_for(state="visible", timeout=20_000)
        except Exception as exc:
            raise RuntimeError(
                "a tela autenticada de cadastro de processo não foi confirmada"
            ) from exc

    @staticmethod
    def _safe_path(url: str) -> str:
        parts = urlsplit(str(url or ""))
        return parts.path or "/"

    @staticmethod
    def _normalized(value: str) -> str:
        base = unicodedata.normalize("NFKD", str(value or ""))
        return "".join(
            char for char in base if not unicodedata.combining(char)
        ).strip().casefold()

    async def _select_semantic(
        self,
        label: str,
        requested: str,
        dependent_label: str = "",
    ) -> str:
        selector = self.INITIAL_SELECTORS[label]
        field = self.page.locator(selector).first
        await field.wait_for(state="visible", timeout=20_000)
        options = await field.evaluate(
            """
            (el) => Array.from(el.options).map((option) => ({
                label: (option.textContent || '').replace(/\\s+/g, ' ').trim(),
                value: option.value,
                disabled: option.disabled,
            }))
            """
        )
        wanted = self._normalized(requested)
        exact = [
            option for option in options
            if not option["disabled"]
            and self._normalized(option["label"]) == wanted
        ]
        partial = [
            option for option in options
            if not option["disabled"]
            and wanted in self._normalized(option["label"])
        ]
        matches = exact or partial
        if not matches:
            available = [
                option["label"] for option in options
                if option["label"]
                and "noselectionvalue" not in option["value"].casefold()
            ]
            raise ValueError(
                f"{label} não encontrada; opções disponíveis: {available[:100]}"
            )
        if len(matches) > 1 and not exact:
            raise ValueError(
                f"{label} ambígua; correspondências: "
                f"{[option['label'] for option in matches[:20]]}"
            )
        selected = matches[0]
        await field.select_option(value=selected["value"])
        await field.dispatch_event("change")
        await field.dispatch_event("blur")
        field_id = await field.get_attribute("id")
        if field_id:
            await self.page.wait_for_function(
                "(data) => document.getElementById(data.id)?.value === data.value",
                arg={"id": field_id, "value": selected["value"]},
                timeout=10_000,
            )
        if dependent_label:
            dependent = self.page.locator(
                self.INITIAL_SELECTORS[dependent_label]
            ).first
            try:
                await dependent.locator("option").nth(1).wait_for(
                    state="attached", timeout=8_000
                )
            except Exception:
                # O snapshot devolve o onchange e as opções atuais para
                # diagnóstico seguro quando o Ajax não popular o dependente.
                pass
        return selected["label"]

    async def fill_initial(
        self,
        materia: str = "",
        jurisdicao: str = "",
        classe_judicial: str = "",
        advance: bool = False,
    ) -> dict[str, Any]:
        """Preenche opções dependentes e, se autorizado, avança uma etapa."""
        selected: dict[str, str] = {}
        if materia:
            selected["materia"] = await self._select_semantic(
                "Matéria", materia, "Jurisdição"
            )
        if jurisdicao:
            if not materia:
                raise ValueError("informe matéria antes da jurisdição")
            selected["jurisdicao"] = await self._select_semantic(
                "Jurisdição", jurisdicao, "Classe judicial"
            )
        if classe_judicial:
            if not materia or not jurisdicao:
                raise ValueError(
                    "informe matéria e jurisdição antes da classe judicial"
                )
            selected["classe_judicial"] = await self._select_semantic(
                "Classe judicial", classe_judicial
            )

        if advance:
            if len(selected) != 3:
                raise ValueError(
                    "matéria, jurisdição e classe judicial são obrigatórias "
                    "para avançar"
                )
            include = self.page.get_by_role(
                "button", name=re.compile(r"^Incluir$", re.IGNORECASE)
            ).first
            if not await include.count():
                include = self.page.locator(
                    "input[type='button'][value='Incluir']"
                ).first
            await include.wait_for(state="visible", timeout=10_000)
            text = (
                await include.get_attribute("value")
                or await include.text_content()
                or ""
            )
            if self.FINAL_ACTION.search(text):
                raise RuntimeError("controle final bloqueado pela política")
            await include.click()
            next_step = self.page.get_by_text(
                re.compile(
                    r"^(?:Assuntos?|Partes?|Características|"
                    r"Incluir petições e documentos)$",
                    re.IGNORECASE,
                )
            ).first
            await next_step.wait_for(state="visible", timeout=30_000)

        result = await self.snapshot()
        result["selecoes_iniciais"] = selected
        result["etapa_avancada"] = bool(advance)
        return result

    async def search_subject(self, term: str) -> list[dict[str, Any]]:
        """Pesquisa o catálogo TPU da etapa Assuntos sem selecionar resultado."""
        field = self.page.locator(
            "input[id$='assuntoCompleto']"
        ).first
        await field.wait_for(state="visible", timeout=20_000)
        await field.fill(str(term or "").strip())
        await field.dispatch_event("change")
        await field.dispatch_event("blur")
        search = self.page.locator(
            "input[type='button'][value='Pesquisar'][id*='Assunto']"
        ).first
        if not await search.count():
            search = self.page.locator(
                "input[type='button'][value='Pesquisar']"
            ).first
        async with self.page.expect_response(
            lambda response: "/Processo/update.seam" in response.url,
            timeout=20_000,
        ):
            await search.click()
        await self.page.wait_for_function(
            r"""
            (term) => {
                const fold = (value) => String(value || '')
                    .normalize('NFD').replace(/[\u0300-\u036f]/g, '')
                    .toLocaleLowerCase('pt-BR');
                const table = document.querySelector(
                    'table[id^="r_processoAssuntoListList"]'
                );
                return table && fold(table.innerText).includes(fold(term));
            }
            """,
            arg=str(term or "").strip(),
            timeout=20_000,
        )
        rows = await self.page.evaluate(
            r"""
            () => {
                const clean = (value, limit = 600) => String(value || '')
                    .replace(/\s+/g, ' ').trim().slice(0, limit);
                return Array.from(document.querySelectorAll(
                    'table[id^="r_processoAssuntoListList"] tbody tr'
                ))
                    .map((row) => ({
                        text: clean(row.innerText || row.textContent),
                        controls: Array.from(row.querySelectorAll(
                            'a, button, input[type="button"], '
                            + 'input[type="submit"], input[type="image"]'
                        )).map((el) => ({
                            id: clean(el.id, 240),
                            name: clean(el.getAttribute('name'), 240),
                            text: clean(
                                el.textContent || el.value
                                || el.getAttribute('title')
                                || el.getAttribute('alt')
                            ),
                            type: clean(el.getAttribute('type'), 80),
                        })).filter((item) =>
                            item.id || item.name || item.text
                        ).slice(0, 10),
                    }))
                    .filter((row) => row.text && row.controls.length)
                    .slice(0, 100);
            }
            """
        )
        return rows

    async def select_subject(self, code: str) -> dict[str, Any]:
        """Associa um assunto TPU exato, sem navegar para etapa posterior."""
        normalized = re.sub(r"\D", "", str(code or ""))
        if not normalized:
            raise ValueError("informe o código numérico do assunto")
        rows = self.page.locator(
            "table[id^='r_processoAssuntoListList'] tbody tr"
        )
        target = None
        for index in range(await rows.count()):
            row = rows.nth(index)
            text = re.sub(
                r"\s+", " ", await row.inner_text()
            ).strip()
            if re.match(rf"^{re.escape(normalized)}\b", text):
                target = row
                break
        if target is None:
            raise ValueError(
                "código do assunto não encontrado nos resultados pesquisados"
            )
        control = target.locator(
            "a, button, input[type='button'], input[type='submit'], "
            "input[type='image']"
        ).first
        await control.click()
        await self.page.wait_for_function(
            """
            (code) => Array.from(document.querySelectorAll(
                'table[id*="Assunto"] tr, [id*="Assunto"]'
            )).some((el) => {
                const text = (el.innerText || el.textContent || '')
                    .replace(/\\s+/g, ' ').trim();
                return new RegExp('(^|\\\\s)' + code + '(\\\\s|$)').test(text);
            })
            """,
            arg=normalized,
            timeout=20_000,
        )
        result = await self.snapshot()
        result["assunto_adicionado"] = normalized
        return result

    async def open_step(self, step: str) -> dict[str, Any]:
        """Abre somente uma etapa allowlisted do cadastro."""
        aliases = {
            "assuntos": re.compile(r"^Assuntos?", re.IGNORECASE),
            "partes": re.compile(r"^Partes?", re.IGNORECASE),
            "caracteristicas": re.compile(r"^Características", re.IGNORECASE),
            "documentos": re.compile(
                r"^(?:Incluir\s+)?petições e documentos", re.IGNORECASE
            ),
            "resumo": re.compile(r"^Resumo", re.IGNORECASE),
        }
        key = self._normalized(step).replace(" ", "_")
        if key not in aliases:
            raise ValueError(
                f"etapa inválida; use uma de {sorted(aliases)}"
            )
        candidates = self.page.get_by_text(aliases[key])
        ranked = []
        for index in range(await candidates.count()):
            candidate = candidates.nth(index)
            if await candidate.is_visible():
                meta = await candidate.evaluate(
                    """
                    (el) => {
                        const clean = (value) => String(value || '')
                            .replace(/\\s+/g, ' ').trim().slice(0, 240);
                        const ancestry = [];
                        let node = el;
                        for (let i = 0; node && i < 6; i += 1) {
                            ancestry.push({
                                tag: node.tagName.toLowerCase(),
                                id: clean(node.id),
                                classes: clean(node.className),
                            });
                            node = node.parentElement;
                        }
                        const signature = ancestry.map((item) =>
                            item.id + ' ' + item.classes
                        ).join(' ');
                        const inMenu = Boolean(el.closest(
                            'nav, header, aside, [role="menu"], '
                            + '.menu-usuario, #menu'
                        ));
                        let score = 0;
                        if (/tab|wizard|step|processo/i.test(signature)) score += 5;
                        if (el.closest('form')) score += 2;
                        if (el.matches('a, button, input')) score += 2;
                        if (inMenu) score -= 20;
                        return {
                            score,
                            tag: el.tagName.toLowerCase(),
                            id: clean(el.id),
                            text: clean(el.textContent || el.value),
                            href: clean(el.getAttribute('href')),
                            title: clean(el.getAttribute('title')),
                            ancestry,
                        };
                    }
                    """
                )
                ranked.append((int(meta["score"]), index, candidate, meta))
        if not ranked:
            raise RuntimeError(f"etapa {step} não encontrada no cadastro")
        ranked.sort(key=lambda item: (item[0], -item[1]), reverse=True)
        _, _, target, clicked_meta = ranked[0]
        await target.click()
        if key == "partes":
            marker = self.page.get_by_text(
                re.compile(
                    r"Polo ativo|Polo passivo|Adicionar parte|CPF/CNPJ",
                    re.IGNORECASE,
                )
            ).first
            await marker.wait_for(state="visible", timeout=20_000)
        elif key == "documentos":
            await self.page.wait_for_timeout(3_000)
        else:
            await self.page.wait_for_load_state("domcontentloaded")
        result = await self.snapshot()
        result["etapa_aberta"] = key
        result["controle_etapa"] = clicked_meta
        return result

    async def open_party_form(self, pole: str) -> dict[str, Any]:
        """Abre o modal de parte no polo ativo ou passivo."""
        key = self._normalized(pole)
        selectors = {
            "ativo": "#addParteA",
            "autor": "#addParteA",
            "passivo": "#addParteP",
            "reu": "#addParteP",
        }
        if key not in selectors:
            raise ValueError("polo inválido; use ativo ou passivo")
        control = self.page.locator(selectors[key]).first
        await control.wait_for(state="visible", timeout=10_000)
        before = await self.page.evaluate(
            """
            () => Array.from(document.querySelectorAll(
                'input:not([type="hidden"]), select, textarea'
            )).filter((el) => {
                const style = getComputedStyle(el);
                return style.display !== 'none'
                    && style.visibility !== 'hidden'
                    && el.getClientRects().length > 0;
            }).length
            """
        )
        await control.click()
        await self.page.wait_for_function(
            """
            (before) => Array.from(document.querySelectorAll(
                'input:not([type="hidden"]), select, textarea'
            )).filter((el) => {
                const style = getComputedStyle(el);
                return style.display !== 'none'
                    && style.visibility !== 'hidden'
                    && el.getClientRects().length > 0;
            }).length > before
            """,
            arg=before,
            timeout=20_000,
        )
        desired_role = "AUTOR" if key in {"ativo", "autor"} else "RÉU"
        selects = self.page.locator("select:visible")
        role_select = None
        role_value = None
        for index in range(await selects.count()):
            candidate = selects.nth(index)
            options = await candidate.evaluate(
                """
                (el) => Array.from(el.options).map((option) => ({
                    label: (option.textContent || '').trim(),
                    value: option.value,
                }))
                """
            )
            for option in options:
                if self._normalized(option["label"]) == self._normalized(
                    desired_role
                ):
                    role_select = candidate
                    role_value = option["value"]
                    break
            if role_select is not None:
                break
        if role_select is None:
            raise RuntimeError(
                f"tipo de parte {desired_role} não encontrado no modal"
            )
        before_role = await self.page.evaluate(
            """
            () => Array.from(document.querySelectorAll(
                'input:not([type="hidden"]), select, textarea'
            )).filter((el) => {
                const style = getComputedStyle(el);
                return style.display !== 'none'
                    && style.visibility !== 'hidden'
                    && el.getClientRects().length > 0;
            }).length
            """
        )
        await role_select.select_option(value=role_value)
        await role_select.dispatch_event("change")
        await self.page.wait_for_function(
            """
            (before) => Array.from(document.querySelectorAll(
                'input:not([type="hidden"]), select, textarea'
            )).filter((el) => {
                const style = getComputedStyle(el);
                return style.display !== 'none'
                    && style.visibility !== 'hidden'
                    && el.getClientRects().length > 0;
            }).length > before
            """,
            arg=before_role,
            timeout=20_000,
        )
        result = await self.snapshot()
        result["form_parte_aberto"] = (
            "ativo" if key in {"ativo", "autor"} else "passivo"
        )
        result["tipo_parte"] = desired_role
        return result

    async def search_party_by_cpf(self, cpf: str) -> dict[str, Any]:
        """Consulta CPF no modal sem devolver o documento ou dados pessoais."""
        digits = re.sub(r"\D", "", str(cpf or ""))
        if len(digits) != 11:
            raise ValueError("CPF deve conter 11 dígitos")
        field = self.page.locator(
            "input[id$='preCadastroPessoaFisica_nrCPF']"
        ).first
        await field.wait_for(state="visible", timeout=10_000)
        await field.fill(digits)
        search = self.page.locator(
            "input[id$='pesquisarDocumentoPrincipal']"
        ).first
        before = await self.page.evaluate(
            """
            () => Array.from(document.querySelectorAll(
                'input:not([type="hidden"]), select, textarea'
            )).filter((el) => {
                const style = getComputedStyle(el);
                return style.display !== 'none'
                    && style.visibility !== 'hidden'
                    && el.getClientRects().length > 0;
            }).length
            """
        )
        await search.click()
        await self.page.wait_for_function(
            """
            (before) => Array.from(document.querySelectorAll(
                'input:not([type="hidden"]), select, textarea'
            )).filter((el) => {
                const style = getComputedStyle(el);
                return style.display !== 'none'
                    && style.visibility !== 'hidden'
                    && el.getClientRects().length > 0;
            }).length > before
            """,
            arg=before,
            timeout=20_000,
        )
        result = await self.snapshot()
        result["cpf_consultado"] = "***.***.***-**"
        result["dados_pessoais_omitidos"] = True
        return result

    async def confirm_party(self) -> dict[str, Any]:
        """Confirma e vincula a parte consultada sem devolver identificação."""
        button = self.page.locator(
            "input[id$='btnConfirmarCadastro'][value='Confirmar']"
        ).first
        await button.wait_for(state="visible", timeout=10_000)
        await button.click()
        await button.wait_for(state="hidden", timeout=20_000)
        link_button = self.page.locator(
            "input[id$='btnInserirParteProcesso']"
        ).first
        await link_button.wait_for(state="visible", timeout=20_000)
        await link_button.click()
        await link_button.wait_for(state="hidden", timeout=20_000)
        return {
            "parte_adicionada": True,
            "parte_vinculada_ao_processo": True,
            "dados_pessoais_omitidos": True,
        }

    async def upload_primary_pdf(
        self,
        pdf_path: str,
        description: str,
        document_type: str,
    ) -> dict[str, Any]:
        """Carrega o PDF principal, sem acionar protocolo ou assinatura."""
        path = Path(pdf_path)
        type_field = self.page.locator("select[id$='cbTD']").first
        await type_field.wait_for(state="visible", timeout=20_000)
        options = await type_field.evaluate(
            """
            (el) => Array.from(el.options).map((option) => ({
                label: (option.textContent || '').replace(/\\s+/g, ' ').trim(),
                value: option.value,
                disabled: option.disabled,
            }))
            """
        )
        requested = self._normalized(document_type)
        matches = [
            item for item in options
            if not item["disabled"]
            and self._normalized(item["label"]) == requested
        ]
        if len(matches) != 1:
            raise ValueError(
                "tipo de documento não encontrado de forma inequívoca"
            )
        await type_field.select_option(value=matches[0]["value"])
        description_field = self.page.locator(
            "input[id$='ipDesc']"
        ).first
        await description_field.fill(str(description).strip()[:120])
        pdf_radio = self.page.locator(
            "input[name='raTipoDocPrincipal'][type='radio']"
        ).first
        if not await pdf_radio.is_checked():
            await pdf_radio.check()
        upload = self.page.locator(
            "input[id$='uploadDocumentoPrincipal:file'][type='file']"
        ).first
        await upload.set_input_files(str(path))
        loaded_name = await upload.evaluate(
            "(el) => el.files && el.files[0] ? el.files[0].name : ''"
        )
        if loaded_name != path.name:
            raise RuntimeError("o navegador não confirmou o PDF selecionado")
        return {
            "documento_carregado": True,
            "nome_arquivo": path.name,
            "tipo_documento": matches[0]["label"],
            "descricao_documento": str(description).strip()[:120],
            "protocolo_executado": False,
        }

    async def snapshot(self) -> dict[str, Any]:
        """Retorna somente metadados necessários ao mapeamento."""
        result = await self.page.evaluate(
            r"""
            () => {
                const clean = (value, limit = 240) => String(value || '')
                    .replace(/\s+/g, ' ').trim().slice(0, limit);
                const visible = (el) => {
                    const style = getComputedStyle(el);
                    return style.display !== 'none'
                        && style.visibility !== 'hidden'
                        && el.getClientRects().length > 0;
                };
                const labelFor = (el) => {
                    const id = el.id || '';
                    const explicit = id
                        ? document.querySelector(
                            'label[for="' + CSS.escape(id) + '"]'
                        )
                        : null;
                    if (explicit) return clean(explicit.textContent);
                    const parent = el.closest('label');
                    if (parent) return clean(parent.textContent);
                    const group = el.closest(
                        '.form-group, .ui-field-contain, td, div'
                    );
                    if (!group) return '';
                    const candidate = group.querySelector('label');
                    return candidate ? clean(candidate.textContent) : '';
                };
                const selectorMeta = (el) => ({
                    tag: el.tagName.toLowerCase(),
                    id: clean(el.id),
                    name: clean(el.getAttribute('name')),
                    type: clean(el.getAttribute('type')),
                    label: labelFor(el),
                    placeholder: clean(el.getAttribute('placeholder')),
                    required: el.required
                        || el.getAttribute('aria-required') === 'true',
                    accept: clean(el.getAttribute('accept')),
                    onchange: clean(el.getAttribute('onchange'), 600),
                    visible: visible(el),
                });

                const selects = Array.from(
                    document.querySelectorAll('select')
                ).filter(visible).map((el) => ({
                    ...selectorMeta(el),
                    options: Array.from(el.options).map((option) => ({
                        label: clean(option.textContent),
                        value: clean(option.value),
                        disabled: option.disabled,
                    })).filter((option) =>
                        option.label || option.value
                    ).slice(0, 100),
                }));

                const inputs = Array.from(document.querySelectorAll(
                    'input:not([type="hidden"]), textarea'
                )).filter(visible).map(selectorMeta);

                const controls = Array.from(document.querySelectorAll(
                    'button, input[type="submit"], input[type="button"], '
                    + 'a[role="button"], .btn'
                )).filter((el) =>
                    visible(el)
                    && el.id !== 'dropMenuPerfil'
                    && !el.closest('li.menu-usuario, .menu-usuario')
                ).map((el) => ({
                    tag: el.tagName.toLowerCase(),
                    id: clean(el.id),
                    name: clean(el.getAttribute('name')),
                    text: clean(
                        el.textContent || el.value
                        || el.getAttribute('aria-label')
                        || el.getAttribute('title')
                    ),
                    type: clean(el.getAttribute('type')),
                })).filter((item) => item.text).slice(0, 100);

                const steps = Array.from(document.querySelectorAll(
                    '[role="tab"], .nav-tabs a, .ui-tabs-nav a, '
                    + '.step, .steps li, .wizard li'
                )).filter(visible).map((el) => clean(el.textContent))
                    .filter(Boolean).slice(0, 50);

                const headings = Array.from(document.querySelectorAll(
                    'h1, h2, h3, h4, legend'
                )).filter(visible).map((el) => clean(el.textContent))
                    .filter(Boolean).slice(0, 50);

                const navigation = Array.from(document.querySelectorAll(
                    'a, button, input[type="button"]'
                )).filter(visible).map((el) => clean(
                    el.textContent || el.value || el.getAttribute('title')
                )).filter((text) => new RegExp(
                    'Dados iniciais|Assuntos?|Partes?|Características|'
                    + 'petições|documentos|Resumo',
                    'i'
                ).test(text)).slice(0, 30);

                return {
                    selects, inputs, controls, steps, headings, navigation
                };
            }
            """
        )
        controls = list(result.get("controls") or [])
        sensitive_select = re.compile(
            r"(?:formInserirParteProcesso|preCadastroPessoaFisica)",
            re.IGNORECASE,
        )
        for item in result.get("selects") or []:
            signature = " ".join(
                str(item.get(key) or "") for key in ("id", "name")
            )
            if sensitive_select.search(signature):
                item["options"] = []
                item["opcoes_omitidas_por_privacidade"] = True
        final_controls = [
            item for item in controls
            if self.FINAL_ACTION.search(str(item.get("text") or ""))
        ]
        return {
            "status": "mapeado",
            "rota": self._safe_path(self.page.url),
            "etapa_final_detectada": bool(final_controls),
            "controles_finais_bloqueados": [
                str(item.get("text") or "") for item in final_controls
            ],
            **result,
        }


def validate_general_search_value(value: str) -> str:
    """Preserva o identificador bruto sem inferir CPF, CNPJ, OAB ou CNJ."""
    normalized = re.sub(r"\s+", " ", str(value or "")).strip()
    if not normalized:
        raise ValueError("informe um identificador para a busca processual geral")
    if len(normalized) > 200:
        raise ValueError("identificador excede o limite de 200 caracteres")
    if any(ord(char) < 32 for char in normalized):
        raise ValueError("identificador contém caractere de controle")
    return normalized


def normalize_general_search_results(
    raw_items: Iterable[dict[str, Any]],
    limit: int,
) -> list[dict[str, Any]]:
    """Normaliza linhas/cards da tela sem depender da posição das colunas."""
    items: list[dict[str, Any]] = []
    seen = set()
    for raw in raw_items:
        text = re.sub(r"\s+", " ", str(raw.get("text") or "")).strip()
        if not text:
            continue
        match = CNJ_PATTERN.search(text)
        cnj = match.group(0) if match else None
        stable_key = cnj or text.casefold()
        if stable_key in seen:
            continue
        seen.add(stable_key)
        item: dict[str, Any] = {
            "numero_cnj": cnj,
            "texto_resumido": text[:1200],
        }
        href_path = str(raw.get("href_path") or "")
        if href_path.startswith("/"):
            item["rota_resultado"] = href_path[:500]
        items.append(item)
        if len(items) >= max(1, min(100, int(limit))):
            break
    return items


def _normalized_header(value: str) -> str:
    base = unicodedata.normalize("NFKD", str(value or ""))
    return re.sub(
        r"\s+",
        " ",
        "".join(char for char in base if not unicodedata.combining(char)),
    ).strip().casefold()


def mask_process_search_value(criterion: str, value: Any) -> str:
    """Redige identificadores pessoais em logs e metadados da resposta."""
    criterion = str(criterion or "").strip().casefold()
    if criterion == "oab":
        if isinstance(value, Mapping):
            number = re.sub(r"\D", "", str(value.get("numero") or ""))
            letter = str(value.get("letra") or "").strip().upper()
            uf = str(value.get("uf") or "").strip().upper()
        elif isinstance(value, (tuple, list)):
            number = re.sub(r"\D", "", str(value[0] if value else ""))
            if len(value) > 2:
                letter = str(value[1] or "").strip().upper()
                uf = str(value[2] or "").strip().upper()
            else:
                letter = ""
                uf = str(value[1] if len(value) > 1 else "").strip().upper()
        else:
            raw = str(value or "")
            number = re.sub(r"\D", "", raw)
            compact = re.sub(r"[^0-9A-Z]", "", raw.upper())
            letter_match = re.match(r"^\d{1,10}([A-Z])(?:[A-Z]{2})?$", compact)
            letter = letter_match.group(1) if letter_match else ""
            uf_match = re.search(r"\b([A-Z]{2})\b", raw.upper())
            uf = uf_match.group(1) if uf_match else ""
        suffix = number[-2:] if number else ""
        return f"***{suffix}{letter}{f'/{uf}' if uf else ''}"

    digits = re.sub(r"\D", "", str(value or ""))
    if criterion == "cpf":
        return f"***.***.***-{digits[-2:]}" if digits else "[redigido]"
    if criterion == "cnpj":
        return f"**.***.***/****-{digits[-2:]}" if digits else "[redigido]"
    if criterion == "numero_cnj":
        match = CNJ_PATTERN.search(str(value or ""))
        return match.group(0) if match else (
            f"{digits[:7]}-**.****.*.**.{digits[-4:]}"
            if len(digits) == 20 else "[redigido]"
        )

    words = re.findall(r"[^\W\d_]+", str(value or ""), re.UNICODE)
    if not words:
        return "[redigido]"
    return " ".join(f"{word[0].upper()}***" for word in words[:8])


def normalize_native_search_rows(
    raw_rows: Iterable[dict[str, Any]],
    limit: int,
) -> list[dict[str, Any]]:
    """Mapeia a grade legada pelos cabeçalhos, com fallback posicional."""
    aliases = {
        "caracteristicas": "caracteristicas",
        "orgao julgador": "orgao_julgador",
        "autuado em": "autuado_em",
        "classe judicial": "classe_judicial",
        "polo ativo": "polo_ativo",
        "polo passivo": "polo_passivo",
        "no(s) atual(is)": "nos_atuais",
        "nos atuais": "nos_atuais",
        "ultima moviment.": "ultima_movimentacao",
        "ultima movimentacao": "ultima_movimentacao",
    }
    fallback = (
        "processo",
        "caracteristicas",
        "orgao_julgador",
        "autuado_em",
        "classe_judicial",
        "polo_ativo",
        "polo_passivo",
        "nos_atuais",
        "ultima_movimentacao",
    )
    results: list[dict[str, Any]] = []
    seen = set()
    safe_limit = max(1, min(100, int(limit or 20)))

    for raw in raw_rows:
        columns = [
            re.sub(r"\s+", " ", str(value or "")).strip()
            for value in raw.get("columns", [])
        ]
        headers = [
            _normalized_header(value) for value in raw.get("headers", [])
        ]
        match = CNJ_PATTERN.search(" ".join(columns))
        if not match or match.group(0) in seen:
            continue
        cnj = match.group(0)
        seen.add(cnj)
        item: dict[str, Any] = {"numero_cnj": cnj}
        for index, value in enumerate(columns):
            header = headers[index] if index < len(headers) else ""
            key = aliases.get(header)
            if not key and index < len(fallback):
                key = fallback[index]
            if key and key != "processo":
                item[key] = value
        if item.get("classe_judicial"):
            item["classe"] = item["classe_judicial"]
        route = str(raw.get("href_path") or "")
        if route.startswith("/"):
            item["rota_resultado"] = route[:500]
        results.append(item)
        if len(results) >= safe_limit:
            break
    return results


def filter_native_results_by_party_role(
    results: Iterable[dict[str, Any]], criterion: str, value: Any
) -> tuple[list[dict[str, Any]], str | None]:
    """Restringe busca nominal ao polo pedido, sem alterar a consulta no PJe."""
    role_field = {
        "nome_requerente": "polo_ativo",
        "nome_requerido": "polo_passivo",
    }.get(str(criterion or "").strip().casefold())
    items = list(results)
    if not role_field:
        return items, None
    needle = _normalized_header(str(value or ""))
    return [
        item for item in items
        if needle in _normalized_header(item.get(role_field, ""))
    ], role_field


class NativeProcessSearchPage:
    """Consulta processual legada, usada como fonte canônica de busca."""

    ROUTE = "/Processo/ConsultaProcesso/listView.seam"
    CRITERIA = {
        "numero_cnj",
        "nome_parte",
        "nome_requerente",
        "nome_requerido",
        "nome_advogado",
        "outros_nomes",
        "numero_documento",
        "cpf",
        "cnpj",
        "oab",
        "assunto",
        "classe_judicial",
        "jurisdicao",
        "orgao_julgador",
        "prioridade_processual",
        "data_autuacao",
        "valor_causa",
        "movimento_processual",
        "orgao_origem_criminal",
        "procedimento_criminal",
        "ano_procedimento_criminal",
        "protocolo_policia",
    }

    def __init__(self, page: Any, url_base: str):
        self.page = page
        self.url_base = url_base.rstrip("/")

    async def open(self) -> None:
        await self.page.goto(
            f"{self.url_base}{self.ROUTE}",
            wait_until="domcontentloaded",
        )
        await self.page.locator("body").wait_for(
            state="attached", timeout=20_000
        )
        heading = self.page.get_by_text(
            re.compile(r"Consulta\s+processos", re.IGNORECASE)
        ).first
        try:
            await heading.wait_for(state="visible", timeout=15_000)
        except Exception as exc:
            raise RuntimeError(
                "a tela autenticada 'Consulta processos' não foi confirmada"
            ) from exc

    async def _first_visible(self, selectors: Iterable[str]) -> Any:
        for selector in selectors:
            locator = self.page.locator(selector).first
            if not await locator.count():
                continue
            try:
                if await locator.is_visible():
                    return locator
            except Exception:
                continue
        return None

    async def _fill_number(self, value: str) -> None:
        digits = re.sub(r"\D", "", value)
        if len(digits) != 20:
            raise ValueError("número CNJ deve conter 20 dígitos")
        fields = self.page.locator(
            "input[id*='numeroProcesso'], input[id*='numProcesso'], "
            "input[name*='numeroProcesso'], input[name*='numProcesso']"
        )
        count = await fields.count()
        parts = (
            digits[0:7], digits[7:9], digits[9:13], digits[13:14],
            digits[14:16], digits[16:20],
        )
        if count >= 6:
            for index, part in enumerate(parts):
                field = fields.nth(index)
                if await field.is_editable():
                    await field.clear()
                    await field.fill(part)
            return
        if count >= 5:
            compact = (parts[0], parts[1], parts[2], parts[4], parts[5])
            for index, part in enumerate(compact):
                field = fields.nth(index)
                await field.clear()
                await field.fill(part)
            return
        raise RuntimeError("campos segmentados do número CNJ não encontrados")

    async def _fill_document(self, criterion: str, value: str) -> None:
        digits = re.sub(r"\D", "", value)
        expected = 11 if criterion == "cpf" else 14
        if len(digits) != expected:
            raise ValueError(
                f"{criterion.upper()} deve conter {expected} dígitos"
            )
        radio = self.page.locator(
            f"input#{criterion}, input[type='radio'][id$='{criterion}'], "
            f"input[type='radio'][value='{criterion.upper()}']"
        ).first
        if await radio.count():
            await radio.check(force=True)
        field = await self._first_visible(
            (
                "input[id='fPP:dpDec:documentoParte']",
                "input[id*='documentoParte']",
                "input[name*='documentoParte']",
            )
        )
        if field is None:
            raise RuntimeError("campo compartilhado CPF/CNPJ não encontrado")
        await field.clear()
        await field.fill(digits)

    async def _fill_oab(self, value: Any) -> None:
        if isinstance(value, Mapping):
            number = str(value.get("numero") or "")
            letter = str(value.get("letra") or "")
            uf = str(value.get("uf") or "")
        elif isinstance(value, (tuple, list)) and value:
            number = str(value[0])
            if len(value) > 2:
                letter = str(value[1] or "")
                uf = str(value[2] or "")
            else:
                letter = ""
                uf = str(value[1] if len(value) > 1 else "")
        else:
            raw = str(value or "")
            upper = raw.upper().strip()
            uf_match = re.search(r"\b([A-Z]{2})\b", raw.upper())
            uf = uf_match.group(1) if uf_match else ""
            without_uf = re.sub(r"(?:/|\s)[A-Z]{2}\s*$", "", upper)
            match = re.fullmatch(
                r"(\d{1,10})\s*[-./]?\s*([A-Z]?)", without_uf
            )
            number = match.group(1) if match else ""
            letter = match.group(2) if match else ""
        number = re.sub(r"\D", "", number)
        letter = letter.strip().upper()
        uf = uf.strip().upper()
        if not number or not uf or (letter and not re.fullmatch(r"[A-Z]", letter)):
            raise ValueError("OAB exige número e UF")
        field = await self._first_visible(
            (
                "input[id*='numeroOAB']",
                "input[name*='numeroOAB']",
                "input[id*='oab'][type='text']",
            )
        )
        if field is None:
            raise RuntimeError("campo número da OAB não encontrado")
        await field.clear()
        await field.fill(number)
        letter_field = await self._first_visible(
            (
                "input[id*='letraOAB']",
                "input[name*='letraOAB']",
            )
        )
        if letter_field is not None:
            await letter_field.clear()
            if letter:
                await letter_field.fill(letter)
        uf_field = await self._first_visible(
            (
                "select[id*='ufOAB']",
                "select[name*='ufOAB']",
                "select[id*='estadoOAB']",
            )
        )
        if uf_field is None:
            raise RuntimeError("campo UF da OAB não encontrado")
        try:
            await uf_field.select_option(value=uf)
        except Exception:
            await uf_field.select_option(label=uf)

    async def _select_by_label_or_value(
        self, selectors: Iterable[str], value: str, label: str
    ) -> None:
        field = await self._first_visible(selectors)
        if field is None:
            raise RuntimeError(f"campo {label} não encontrado")
        option = await field.evaluate(
            r"""
            (select, wanted) => {
                const normalize = (value) => String(value || '')
                    .normalize('NFD').replace(/[\u0300-\u036f]/g, '')
                    .replace(/\s+/g, ' ').trim().toLowerCase();
                const needle = normalize(wanted);
                const options = Array.from(select.options).filter((item) =>
                    !/NoSelectionConverter/.test(item.value));
                const exact = options.find((item) =>
                    item.value === wanted
                    || normalize(item.textContent) === needle);
                const partial = options.filter((item) =>
                    normalize(item.textContent).includes(needle));
                const match = exact || (partial.length === 1 ? partial[0] : null);
                return match ? {value: match.value, label: match.textContent.trim()} : null;
            }
            """,
            str(value or "").strip(),
        )
        if not option:
            raise ValueError(
                f"{label} não possui opção única correspondente a '{value}'"
            )
        await field.select_option(value=option["value"])

    async def _fill_range(
        self,
        value: Any,
        start_selectors: Iterable[str],
        end_selectors: Iterable[str],
        label: str,
    ) -> None:
        if isinstance(value, Mapping):
            start = str(value.get("inicio") or value.get("valor") or "").strip()
            end = str(value.get("fim") or value.get("valor_final") or start).strip()
        else:
            parts = re.split(r"\s*(?:\.\.|\sa\s)\s*", str(value or ""), maxsplit=1)
            start = parts[0].strip()
            end = (parts[1] if len(parts) > 1 else start).strip()
        if not start or not end:
            raise ValueError(f"{label} exige valor inicial e final")
        start_field = await self._first_visible(start_selectors)
        end_field = await self._first_visible(end_selectors)
        if start_field is None or end_field is None:
            raise RuntimeError(f"campos de intervalo {label} não encontrados")
        await start_field.clear()
        await start_field.fill(start)
        await end_field.clear()
        await end_field.fill(end)

    async def _fill_movement(self, value: str) -> None:
        field = await self._first_visible((
            "input[id*='movimentacaoProcessualSuggest']",
            "input[name*='movimentacaoProcessualSuggest']",
        ))
        if field is None:
            raise RuntimeError("campo movimento processual não encontrado")
        term = validate_general_search_value(value)
        await field.fill(term)
        await field.press("ArrowDown")
        candidates = self.page.locator(
            "div[id*='j_id418'] tr.rich-sb-int, "
            "table[id*='j_id418'][id$='suggest'] tr.rich-sb-int"
        ).filter(has_not_text=re.compile(r"termo não encontrado", re.I))
        try:
            await candidates.first.wait_for(state="visible", timeout=8_000)
        except Exception as exc:
            raise ValueError(
                f"movimento processual não encontrado para '{term}'"
            ) from exc
        exact = candidates.filter(has_text=re.compile(
            rf"^\s*{re.escape(term)}\s*$", re.I
        ))
        target = exact.first if await exact.count() else candidates.first
        await target.click()

    async def _ensure_criminal_filters_open(self) -> None:
        probe = self.page.locator(
            "input[id$=':numeroProcedCriminal']"
        ).first
        if await probe.count() and await probe.is_visible():
            return
        header = await self._first_visible((
            "div.rich-stglpanel:has-text('Filtros Criminais') "
            ".rich-stglpanel-header",
            "div[id*='j_id424'] .rich-stglpanel-header",
        ))
        if header is None:
            raise RuntimeError("seção Filtros Criminais não encontrada")
        await header.click()
        await probe.wait_for(state="visible", timeout=10_000)

    async def _fill(self, criterion: str, value: Any) -> None:
        if criterion in {
            "orgao_origem_criminal", "procedimento_criminal",
            "ano_procedimento_criminal", "protocolo_policia",
        }:
            await self._ensure_criminal_filters_open()
        if criterion == "numero_cnj":
            await self._fill_number(str(value or ""))
            return
        if criterion in ("cpf", "cnpj"):
            await self._fill_document(criterion, str(value or ""))
            return
        if criterion == "oab":
            await self._fill_oab(value)
            return
        select_fields = {
            "jurisdicao": (
                ("select[id*='jurisdicaoCombo']", "select[name*='jurisdicaoCombo']"),
                "jurisdição",
            ),
            "orgao_julgador": (
                ("select[id*='orgaoJulgadorCombo']", "select[name*='orgaoJulgadorCombo']"),
                "órgão julgador",
            ),
            "prioridade_processual": (
                ("select[id*='prioridadeProcessualCombo']", "select[name*='prioridadeProcessualCombo']"),
                "prioridade processual",
            ),
            "orgao_origem_criminal": (
                ("select[id*='orgaoOrigemCriminal']", "select[name*='orgaoOrigemCriminal']"),
                "órgão de origem criminal",
            ),
        }
        if criterion in select_fields:
            field_selectors, label = select_fields[criterion]
            await self._select_by_label_or_value(
                field_selectors, str(value or ""), label
            )
            return
        if criterion == "data_autuacao":
            await self._fill_range(
                value,
                ("input[id*='dataAutuacaoInicioInputDate']",),
                ("input[id*='dataAutuacaoFimInputDate']",),
                "data de autuação",
            )
            return
        if criterion == "valor_causa":
            await self._fill_range(
                value,
                ("input[id*='valorCausaInicial']",),
                ("input[id*='valorCausaFinal']",),
                "valor da causa",
            )
            return
        if criterion == "movimento_processual":
            await self._fill_movement(str(value or ""))
            return
        if criterion == "procedimento_criminal":
            raw = str(value or "").strip()
            if isinstance(value, Mapping):
                number = str(value.get("numero") or "").strip()
                year = str(value.get("ano") or "").strip()
            else:
                match = re.fullmatch(r"(\d{1,30})(?:\s*[-/]\s*(\d{4}))?", raw)
                if not match:
                    raise ValueError("procedimento criminal exige número e ano opcional")
                number, year = match.group(1), match.group(2) or ""
            number_field = await self._first_visible(
                ("input[id$=':numeroProcedCriminal']",)
            )
            year_field = await self._first_visible(
                ("input[id$=':anoProcedCriminal']",)
            )
            if number_field is None or year_field is None:
                raise RuntimeError("campos do procedimento criminal não encontrados")
            await number_field.fill(number)
            if year:
                await year_field.fill(year)
            return
        if criterion in {"nome_parte", "nome_requerente", "nome_requerido"}:
            selectors = ("input[id*='nomeParte']", "input[name*='nomeParte']")
        elif criterion == "nome_advogado":
            selectors = (
                "input[id*='nomeRepresentante']",
                "input[name*='nomeRepresentante']",
                "input[id*='nomeAdvogado']",
                "input[id*='representante']",
            )
        elif criterion == "outros_nomes":
            selectors = (
                "input[id*='outrosNomesAlcunha']",
                "input[name*='outrosNomesAlcunha']",
            )
        elif criterion == "numero_documento":
            selectors = (
                "input[id*='numeroDocumento']",
                "input[name*='numeroDocumento']",
            )
        elif criterion == "assunto":
            selectors = ("input[id*=':assunto']", "input[name*=':assunto']")
        elif criterion == "classe_judicial":
            selectors = (
                "input[id*='classeJudicial']",
                "input[name*='classeJudicial']",
            )
        elif criterion == "ano_procedimento_criminal":
            raw_year = str(value or "").strip()
            if not re.fullmatch(r"\d{4}", raw_year):
                raise ValueError("ano do procedimento deve ter quatro dígitos")
            selectors = ("input[id$=':anoProcedCriminal']",)
        else:
            selectors = ("input[id*='numeroProtocoloPolicia']",)
        field = await self._first_visible(selectors)
        if field is None:
            raise RuntimeError(f"campo {criterion} não encontrado")
        normalized = validate_general_search_value(str(value or ""))
        await field.clear()
        await field.fill(normalized)

    async def _result_fingerprint(self) -> str:
        return await self.page.evaluate(
            """
            () => {
                const tables = Array.from(document.querySelectorAll('table'));
                const table = tables.find((item) =>
                    /Processo/i.test(item.innerText || '')
                    && /Órgão julgador|Orgao julgador/i.test(item.innerText || '')
                );
                return (table?.innerText || document.body.innerText || '')
                    .replace(/\\s+/g, ' ').trim();
            }
            """
        )

    async def _wait_result_change(self, before: str, timeout: int) -> None:
        try:
            await self.page.wait_for_function(
                """
                (previous) => {
                    const body = document.body.innerText || '';
                    const tables = Array.from(document.querySelectorAll('table'));
                    const table = tables.find((item) =>
                        /Processo/i.test(item.innerText || '')
                        && /Órgão julgador|Orgao julgador/i.test(item.innerText || '')
                    );
                    const current = (table?.innerText || body)
                        .replace(/\\s+/g, ' ').trim();
                    const busy = Array.from(document.querySelectorAll(
                        '[aria-busy="true"], .ui-blockui'
                    )).some((item) => item.getClientRects().length > 0);
                    return !busy && current !== previous;
                }
                """,
                before,
                timeout=timeout,
            )
        except Exception:
            body = await self.page.locator("body").inner_text()
            if not re.search(
                r"nenhum\s+(?:registro|processo)|sem\s+resultado|"
                r"\b\d+\s+resultados?\s+encontrados?|"
                r"\d{7}-\d{2}\.\d{4}\.\d\.\d{2}\.\d{4}",
                body,
                re.IGNORECASE,
            ):
                raise RuntimeError(
                    "a consulta não confirmou atualização da grade"
                )

    async def _extract_page(self) -> dict[str, Any]:
        return await self.page.evaluate(
            """
            () => {
                const clean = (value) => String(value || '')
                    .replace(/\\s+/g, ' ').trim();
                const tables = Array.from(document.querySelectorAll('table'));
                const table = tables.find((item) => {
                    const text = clean(item.innerText);
                    return /Processo/i.test(text)
                        && /Órgão julgador|Orgao julgador/i.test(text);
                });
                if (!table) return {headers: [], rows: []};
                const headers = Array.from(
                    table.querySelectorAll('thead th, thead td')
                ).map((cell) => clean(cell.textContent));
                const rows = Array.from(table.querySelectorAll('tbody tr'))
                    .map((row) => {
                        const columns = Array.from(row.querySelectorAll('td'))
                            .map((cell) => clean(cell.textContent));
                        const link = row.querySelector('a[href]');
                        let hrefPath = '';
                        if (link) {
                            try {
                                const url = new URL(link.href, location.href);
                                hrefPath = url.pathname + url.hash;
                            } catch (_) {}
                        }
                        return {headers, columns, href_path: hrefPath};
                    }).filter((row) =>
                        /\\d{7}-\\d{2}\\.\\d{4}\\.\\d\\.\\d{2}\\.\\d{4}/
                            .test(row.columns.join(' '))
                    );
                return {headers, rows};
            }
            """
        )

    async def _go_next(self) -> bool:
        return bool(await self.page.evaluate(
            """
            () => {
                const visible = (el) => {
                    const rect = el.getBoundingClientRect();
                    const style = getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0
                        && style.visibility !== 'hidden'
                        && style.display !== 'none';
                };
                const candidates = Array.from(
                    document.querySelectorAll('a, button, input[type="button"]')
                ).filter((el) => {
                    if (!visible(el) || el.disabled) return false;
                    const text = String(el.textContent || el.value || '').trim();
                    const hint = String(
                        el.getAttribute('title')
                        || el.getAttribute('aria-label') || el.id || ''
                    ).toLowerCase();
                    const nextText = ['»', '>', '›'].includes(text);
                    const nextHint = /pr[oó]xim|next/.test(hint);
                    const lastPage = ['»»', '>>', '≫'].includes(text)
                        || /[uú]ltim|last/.test(hint);
                    const disabled = /disabled|inactive/.test(
                        String(el.className || '').toLowerCase()
                    ) || el.getAttribute('aria-disabled') === 'true';
                    return !lastPage && !disabled && (nextText || nextHint);
                });
                if (!candidates.length) return false;
                candidates[0].click();
                return true;
            }
            """
        ))

    async def search(
        self,
        criterion: str,
        value: Any,
        limit: int = 20,
        max_pages: int = MAX_PAGINAS_CONSULTA_GERAL,
    ) -> dict[str, Any]:
        criterion = str(criterion or "").strip().casefold()
        if criterion not in self.CRITERIA:
            raise ValueError(f"critério de busca não suportado: {criterion}")
        safe_limit = max(1, min(100, int(limit or 20)))
        safe_pages = max(1, min(
            MAX_PAGINAS_CONSULTA_GERAL, int(max_pages or 1)
        ))
        await self.open()
        await self._fill(criterion, value)

        button = await self._first_visible(
            (
                "input[value='Pesquisar']",
                "button:has-text('Pesquisar')",
                "button[type='submit']",
                "input[type='submit']",
            )
        )
        if button is None:
            raise RuntimeError("botão Pesquisar não encontrado")
        before = await self._result_fingerprint()
        await button.evaluate("(element) => element.click()")
        await self._wait_result_change(before, 20_000)

        unique_rows: dict[str, dict[str, Any]] = {}
        pages_read = 0
        total = 0
        fingerprints = set()
        body_text = ""
        role_field = {
            "nome_requerente": "polo_ativo",
            "nome_requerido": "polo_passivo",
        }.get(criterion)
        while pages_read < safe_pages:
            fingerprint = await self._result_fingerprint()
            if fingerprint in fingerprints:
                break
            fingerprints.add(fingerprint)
            pages_read += 1
            extracted = await self._extract_page()
            for row in extracted.get("rows", []):
                match = CNJ_PATTERN.search(" ".join(row.get("columns", [])))
                if match:
                    unique_rows.setdefault(match.group(0), row)

            body_text = await self.page.locator("body").inner_text()
            total_match = re.search(
                r"(\d+)\s*resultados?\s*encontrados?", body_text, re.IGNORECASE
            )
            if total_match:
                total = int(total_match.group(1))
            if role_field:
                provisional = normalize_native_search_rows(
                    unique_rows.values(), 100
                )
                role_matches, _ = filter_native_results_by_party_role(
                    provisional, criterion, value
                )
                if len(role_matches) >= safe_limit:
                    break
            elif len(unique_rows) >= safe_limit:
                break
            if total and len(unique_rows) >= total:
                break
            if not await self._go_next():
                break
            await self._wait_result_change(fingerprint, 12_000)

        unfiltered_results = normalize_native_search_rows(
            unique_rows.values(), 100 if role_field else safe_limit
        )
        results, role_field = filter_native_results_by_party_role(
            unfiltered_results, criterion, value
        )
        results = results[:safe_limit]
        if not total:
            total = len(unique_rows)
        total_form = total
        if role_field:
            total = len(results)
        masked = mask_process_search_value(criterion, value)
        return {
            "fonte": "PJe > Processo > Consulta processos",
            "rota": self.ROUTE,
            "criterio_solicitado": criterion,
            "criterio_aplicado": criterion,
            "campo_busca": criterion,
            "filtros_aplicados": {criterion: masked},
            "valor_busca_mascarado": masked,
            "total_encontrados": total,
            "total_encontrado": total,
            "total_formulario": total_form,
            "retornados": len(results),
            "limite_aplicado": safe_limit,
            "sem_resultado_confirmado": bool(
                total == 0 and (
                    bool(
                        role_field
                        and total_form <= len(unique_rows)
                        and unfiltered_results
                    )
                    or bool(re.search(
                        r"nenhum\s+(?:registro|processo)|sem\s+resultado",
                        body_text,
                        re.IGNORECASE,
                    ))
                )
            ),
            "resultado_completo": total_form <= len(unique_rows),
            "paginacao": {
                "paginas_visitadas": pages_read,
                "limite_paginas": safe_pages,
                "total_unicos_lidos": len(unique_rows),
            },
            "resultados": results,
            "somente_leitura": True,
            **({
                "filtro_polo_aplicado": role_field,
                "criterio_formulario": "nome_parte",
            } if role_field else {}),
        }


class GeneralProcessSearchPage:
    """Rota Angular ``Consulta processual`` do menu lateral do PJe."""

    def __init__(self, page: Any, url_base: str):
        self.page = page
        self.url_base = url_base.rstrip("/")
        self.frame = None
        self.navigation_source = None
        self.navigation_diagnostic: dict[str, Any] = {}

    async def open(self) -> Any:
        await self.page.goto(
            f"{self.url_base}/home.seam", wait_until="domcontentloaded"
        )
        panel_link = self.page.get_by_role(
            "link", name=re.compile(r"Painel do usu[aá]rio", re.IGNORECASE)
        ).first
        if not await panel_link.count():
            raise RuntimeError("atalho 'Painel do usuário' não encontrado")
        panel_href = await panel_link.get_attribute("href")
        if not panel_href:
            raise RuntimeError("atalho 'Painel do usuário' sem destino")
        await self.page.goto(
            urljoin(f"{self.url_base}/", panel_href),
            wait_until="domcontentloaded",
        )
        iframe = self.page.locator("iframe#ngFrame[name='ngFrame']")
        await iframe.wait_for(state="attached", timeout=20_000)
        handle = await iframe.element_handle()
        if handle is None:
            raise RuntimeError("iframe principal do painel não encontrado")
        frame = await handle.content_frame()
        if frame is None:
            raise RuntimeError("conteúdo do painel ainda não está disponível")
        await frame.wait_for_url(re.compile(r"^https?://", re.IGNORECASE), timeout=30_000)
        await frame.locator("body").wait_for(state="attached", timeout=20_000)
        try:
            await frame.locator("app-root, side-bar").first.wait_for(
                state="attached", timeout=20_000
            )
        except Exception:
            # Algumas versões do painel não expõem o componente raiz, mas já
            # podem estar utilizáveis quando o corpo possui links ou botões.
            await frame.locator("a, button").first.wait_for(
                state="attached", timeout=20_000
            )

        menu = frame.get_by_role(
            "menubar",
            name=re.compile(r"Menu lateral do PJe", re.IGNORECASE),
        )
        link = await self._first_visible(
            (
                menu.get_by_role(
                    "link",
                    name=re.compile(r"^Consulta processual$", re.IGNORECASE),
                ),
                    frame.get_by_text(
                        re.compile(r"^Consulta processual$", re.IGNORECASE),
                        exact=True,
                    ),
                    frame.locator(
                        "a[href*='consulta-processual'], "
                        "[routerlink*='consulta-processual'], "
                        "[title*='Consulta processual' i], "
                        "[aria-label*='Consulta processual' i]"
                    ),
            )
        )
        if link is None or not await link.count():
            hidden_link = frame.locator(
                "a, button, [role='menuitem'], [title], [aria-label]"
            ).filter(
                has_text=re.compile(r"^\s*Consulta processual\s*$", re.IGNORECASE)
            ).first
            if await hidden_link.count():
                self.navigation_diagnostic["item_recolhido"] = (
                    await hidden_link.evaluate(
                        """
                        (el) => ({
                            tag: el.tagName.toLowerCase(),
                            href: (el.getAttribute('href') || '')
                                .replace(/[?].*$/, ''),
                            routerlink: el.getAttribute('routerlink') || '',
                            title: el.getAttribute('title') || '',
                            aria: el.getAttribute('aria-label') || '',
                            classes: String(el.className || '').slice(0, 180),
                        })
                        """
                    )
                )
                await hidden_link.click(force=True)
                self.navigation_source = (
                    "clique no item Consulta processual recolhido "
                    "do menu lateral"
                )
                link = hidden_link

        if link is None or not await link.count():
            candidates = await frame.evaluate(
                """
                () => {
                    const root = document.querySelector(
                        'side-bar, [role="menubar"], nav, [class*="sidebar" i]'
                    );
                    const nodes = root
                        ? root.querySelectorAll(
                            'a, button, [role="menuitem"], [title], [aria-label]'
                        )
                        : [];
                    return {
                        hash: location.hash.replace(/[?].*$/, ''),
                        root: root ? root.tagName.toLowerCase() : null,
                        itens: Array.from(nodes).map((el) => ({
                            texto: (el.textContent || '').trim()
                                .replace(/\\s+/g, ' ').slice(0, 160),
                            titulo: (el.getAttribute('title') || '').slice(0, 160),
                            aria: (el.getAttribute('aria-label') || '').slice(0, 160),
                            href: (el.getAttribute('href') || '')
                                .replace(/[?].*$/, '').slice(0, 240),
                            routerlink: (el.getAttribute('routerlink') || '')
                                .slice(0, 160),
                            tag: el.tagName.toLowerCase(),
                            classes: String(el.className || '').slice(0, 160),
                        })).slice(0, 50),
                    };
                }
                """
            )
            try:
                await frame.evaluate(
                    "() => { location.hash = '#/consulta-processual'; }"
                )
                await frame.wait_for_url(GENERAL_SEARCH_ROUTE, timeout=20_000)
                self.navigation_source = (
                    "rota documentada do item Consulta processual "
                    "(menu não renderizado neste perfil)"
                )
            except Exception as exc:
                raise RuntimeError(
                    "item 'Consulta processual' não está disponível no menu "
                    "lateral e a rota documentada não abriu; candidatos "
                    f"sanitizados: {candidates}"
                ) from exc
        else:
            if self.navigation_source is None:
                self.navigation_diagnostic["item_visivel"] = (
                    await link.evaluate(
                        """
                        (el) => ({
                            tag: el.tagName.toLowerCase(),
                            href: (el.getAttribute('href') || '')
                                .replace(/[?].*$/, ''),
                            routerlink: el.getAttribute('routerlink') || '',
                            target: el.getAttribute('target') || '',
                            title: el.getAttribute('title') || '',
                            aria: el.getAttribute('aria-label') || '',
                            classes: String(el.className || '').slice(0, 180),
                        })
                        """
                    )
                )
                await link.click()
                self.navigation_source = (
                    "clique no menu lateral Consulta processual"
                )
        await frame.wait_for_url(GENERAL_SEARCH_ROUTE, timeout=20_000)
        await frame.locator("body").wait_for(
            state="attached", timeout=20_000
        )
        fresh_handle = await iframe.element_handle()
        fresh_frame = (
            await fresh_handle.content_frame() if fresh_handle is not None else None
        )
        if fresh_frame is not None:
            frame = fresh_frame
        self.navigation_diagnostic["url_apos_navegacao"] = re.sub(
            r"[?].*$", "", frame.url
        )
        self.navigation_diagnostic["paginas_abertas"] = [
            re.sub(r"[?].*$", "", page.url)
            for page in self.page.context.pages
        ]
        try:
            await frame.locator("input").first.wait_for(
                state="visible", timeout=30_000
            )
        except Exception:
            pass
        self.frame = frame
        return frame

    async def _first_visible(self, candidates: Iterable[Any]) -> Any:
        for candidate in candidates:
            if not await candidate.count():
                continue
            locator = candidate.first
            try:
                if await locator.is_visible():
                    return locator
            except Exception:
                continue
        return None

    async def search(self, value: str, limit: int = 20) -> dict[str, Any]:
        identifier = validate_general_search_value(value)
        frame = self.frame or await self.open()
        field = await self._first_visible(
            (
                frame.get_by_label(re.compile(r"Número do processo", re.IGNORECASE)),
                frame.get_by_placeholder(re.compile(r"Número do processo", re.IGNORECASE)),
                frame.get_by_label(re.compile(r"CPF(?: ou CNPJ)?", re.IGNORECASE)),
                frame.get_by_placeholder(
                    re.compile(r"CPF(?: ou CNPJ)?", re.IGNORECASE)
                ),
                frame.locator(
                    "input[id*='numeroProcesso'], "
                    "input[name*='numeroProcesso'], "
                    "input[formcontrolname*='numeroProcesso' i], "
                    "input[id*='cpf'], input[name*='cpf'], "
                    "input[formcontrolname*='cpf' i], "
                    "input[type='search']"
                ),
            )
        )
        if field is None:
            form_state = await frame.evaluate(
                """
                () => ({
                    hash: location.hash.replace(/[?].*$/, ''),
                    texto: (document.body.innerText || '').trim()
                        .replace(/\\s+/g, ' ').slice(0, 1200),
                    campos: Array.from(document.querySelectorAll(
                        'input, textarea, select'
                    )).map((el) => ({
                        tag: el.tagName.toLowerCase(),
                        type: el.getAttribute('type') || '',
                        id: el.id || '',
                        name: el.getAttribute('name') || '',
                        placeholder: el.getAttribute('placeholder') || '',
                        aria: el.getAttribute('aria-label') || '',
                        formcontrol: el.getAttribute('formcontrolname') || '',
                        classes: String(el.className || '').slice(0, 160),
                    })).slice(0, 40),
                    botoes: Array.from(document.querySelectorAll(
                        'button, input[type="submit"], [role="button"]'
                    )).map((el) => ({
                        texto: (el.textContent || el.value || '').trim()
                            .replace(/\\s+/g, ' ').slice(0, 160),
                        id: el.id || '',
                        title: el.getAttribute('title') || '',
                        aria: el.getAttribute('aria-label') || '',
                    })).slice(0, 40),
                })
                """
            )
            self.navigation_diagnostic["angular_indisponivel"] = {
                "hash": form_state.get("hash", ""),
                "campos": len(form_state.get("campos", [])),
                "botoes": len(form_state.get("botoes", [])),
            }
            return await self._search_legacy(identifier, limit)
        await field.clear()
        await field.fill(identifier)

        search_button = await self._first_visible(
            (
                frame.get_by_role(
                    "button",
                    name=re.compile(r"^Pesquisar$", re.IGNORECASE),
                ),
                frame.locator(
                    "button:has-text('Pesquisar'), "
                    "input[type='submit'][value*='Pesquisar' i]"
                ),
            )
        )
        if search_button is None:
            raise RuntimeError("botão 'Pesquisar' não encontrado")
        await search_button.click()

        try:
            await frame.wait_for_function(
                """
                () => {
                    const text = document.body.innerText || '';
                    const done = /nenhum\\s+(registro|processo)|sem\\s+resultado/i;
                    const cnj = /\\d{7}-\\d{2}\\.\\d{4}\\.\\d\\.\\d{2}\\.\\d{4}/;
                    const busy = document.querySelector(
                        '[aria-busy="true"], .p-progressbar-indeterminate'
                    );
                    return !busy && (done.test(text) || cnj.test(text));
                }
                """,
                timeout=30_000,
            )
        except Exception:
            # A tela pode responder com um identificador interno sem exibir
            # CNJ. A extração abaixo decide pelo DOM atual sem fabricar zero.
            pass

        raw_items = await frame.evaluate(
            """
            () => {
                const selectors = [
                    'table tbody tr',
                    '[role="row"]',
                    'article',
                    '[class*="resultado" i]',
                    '[class*="processo" i]',
                    '.card'
                ];
                const nodes = Array.from(document.querySelectorAll(
                    selectors.join(',')
                ));
                return nodes.map((node) => {
                    const text = (node.innerText || node.textContent || '')
                        .trim().replace(/\\s+/g, ' ');
                    const anchor = node.querySelector('a[href]');
                    let hrefPath = '';
                    if (anchor) {
                        try {
                            const parsed = new URL(anchor.href, location.href);
                            hrefPath = parsed.pathname + parsed.hash;
                        } catch (_) {}
                    }
                    return { text, href_path: hrefPath };
                }).filter((item) => item.text);
            }
            """
        )
        results = normalize_general_search_results(raw_items, limit)
        body_state = await frame.evaluate(
            """
            () => {
                const text = document.body.innerText || '';
                return {
                    no_results: /nenhum\\s+(registro|processo)|sem\\s+resultado/i.test(text),
                    route: location.hash.replace(/[?].*$/, ''),
                };
            }
            """
        )
        return {
            "fonte": "Menu lateral do PJe > Consulta processual",
            "entrada": self.navigation_source,
            "rota": body_state.get("route") or "#/consulta-processual",
            "valor_busca": identifier,
            "retornados": len(results),
            "sem_resultado_confirmado": bool(body_state.get("no_results")),
            "resultados": results,
            "somente_leitura": True,
            "criterio_inferido": False,
        }

    async def _search_legacy(
        self, identifier: str, limit: int
    ) -> dict[str, Any]:
        """Pesquisa pelo item Processo → Processo do menu geral legado."""
        await self.page.goto(
            f"{self.url_base}/home.seam", wait_until="domcontentloaded"
        )
        menu_item = self.page.locator(
            "a[href*='/Processo/ConsultaProcesso/listView.seam']"
        ).first
        if not await menu_item.count():
            raise RuntimeError(
                "item Processo → Processo não encontrado no menu geral"
            )
        await menu_item.evaluate("(el) => el.click()")
        await self.page.wait_for_url(
            re.compile(r"/Processo/ConsultaProcesso/listView\.seam", re.IGNORECASE),
            timeout=20_000,
        )

        digits = re.sub(r"\D", "", identifier)
        if len(digits) in (11, 14):
            criterion = "cpf" if len(digits) == 11 else "cnpj"
            if criterion == "cnpj":
                cnpj_radio = self.page.locator(
                    "input#cnpj, input[type='radio'][id='cnpj']"
                ).first
                if await cnpj_radio.count():
                    await cnpj_radio.check(force=True)
            document_field = self.page.locator(
                "input[id='fPP:dpDec:documentoParte'], "
                "input[id*='documentoParte'], "
                "input[name*='documentoParte']"
            ).first
            await document_field.wait_for(state="visible", timeout=15_000)
            await document_field.fill(digits)
        elif len(digits) == 20:
            criterion = "numero_cnj"
            fields = self.page.locator("input[id*='numeroProcesso']")
            values = (
                digits[0:7],
                digits[7:9],
                digits[9:13],
                digits[13:14],
                digits[14:16],
                digits[16:20],
            )
            count = await fields.count()
            if count < 5:
                raise RuntimeError(
                    "campos do número CNJ não encontrados na consulta geral"
                )
            if count >= 6:
                for index in (0, 1, 2, 5):
                    await fields.nth(index).fill(values[index])
            else:
                compact = (
                    values[0],
                    values[1],
                    values[2],
                    values[4],
                    values[5],
                )
                for index, part in enumerate(compact):
                    await fields.nth(index).fill(part)
        else:
            criterion = "nome_parte"
            name_field = self.page.locator(
                "input[id*='nomeParte'], input[name*='nomeParte']"
            ).first
            await name_field.wait_for(state="visible", timeout=15_000)
            await name_field.fill(identifier)

        buttons = self.page.locator(
            "input[value='Pesquisar'], button:has-text('Pesquisar'), "
            "button[type='submit']"
        )
        search_button = await self._first_visible(
            buttons.nth(index) for index in range(await buttons.count())
        )
        if search_button is None:
            raise RuntimeError(
                "botão Pesquisar não encontrado na consulta processual geral"
            )

        before = await self.page.locator("body").inner_text()
        await search_button.evaluate("(el) => el.click()")
        try:
            await self.page.wait_for_load_state(
                "networkidle", timeout=15_000
            )
        except Exception:
            pass
        try:
            await self.page.wait_for_function(
                """
                (anterior) => {
                    const atual = document.body.innerText || '';
                    return atual !== anterior
                        || /Nenhum resultado|resultado|\\d{7}-\\d{2}\\.\\d{4}/i
                            .test(atual);
                }
                """,
                before,
                timeout=15_000,
            )
        except Exception:
            pass

        pagina_atual = 0
        limite_paginas = MAX_PAGINAS_CONSULTA_GERAL
        resultados_unicos: dict[str, dict[str, Any]] = {}
        fingerprints: set[str] = set()
        total_encontrado = 0

        while pagina_atual < limite_paginas:
            pagina_atual += 1
            marca = re.sub(r"\s+", "", str(await self.page.locator("body").inner_text()))

            if marca in fingerprints:
                break
            fingerprints.add(marca)

            raw_items = await self.page.evaluate(
                r"""
                () => Array.from(document.querySelectorAll('table tr'))
                    .map((row) => {
                        const link = row.querySelector('a[href]');
                        let hrefPath = '';
                        if (link) {
                            try {
                                hrefPath = new URL(
                                    link.getAttribute('href'), location.href
                                ).pathname;
                            } catch (_) {}
                        }
                        return {
                            text: (row.innerText || row.textContent || '')
                                .trim().replace(/\s+/g, ' '),
                            href_path: hrefPath,
                        };
                    })
                """
            )

            for item in raw_items:
                match = CNJ_PATTERN.search(str(item.get("text") or ""))
                if not match:
                    continue
                cnj = match.group(0)
                if cnj not in resultados_unicos:
                    resultados_unicos[cnj] = item

            body_text = await self.page.locator("body").inner_text()
            total_match = re.search(
                r"(\d+)\s+resultados?\s+encontrados?", body_text, re.IGNORECASE
            )
            if total_match:
                total_encontrado = int(total_match.group(1))
            if total_match and len(resultados_unicos) >= total_encontrado:
                break

            avancou = await self.page.evaluate(
                r"""
                () => {
                    const candidatos = Array.from(
                        document.querySelectorAll('a, button, input')
                    ).filter((el) => {
                        const texto = (
                            (el.textContent || el.value || '').trim().toLowerCase()
                        );
                        if (!texto) {
                            return false;
                        }
                        const hint = (
                            (el.getAttribute('id') || '') +
                            (el.getAttribute('name') || '') +
                            (el.getAttribute('class') || '') +
                            (el.getAttribute('onclick') || '') +
                            (el.getAttribute('href') || '') +
                            (el.getAttribute('title') || '') +
                            (el.getAttribute('aria-label') || '') +
                            (el.getAttribute('role') || '')
                        ).toLowerCase();

                        const aparenta_proximo = /(^|\s)(pr[óo]ximo|seguinte|next)\b|^>+$|^>>+$/.test(
                            texto
                        ) || /page|pager|pagin|next|pr[óo]ximo/.test(hint);
                        if (!aparenta_proximo) {
                            return false;
                        }

                        if (
                            (el.getAttribute('disabled') || '').toLowerCase() === 'true' ||
                            (el.getAttribute('aria-disabled') || '').toLowerCase() === 'true'
                        ) {
                            return false;
                        }

                        const classes = (el.getAttribute('class') || '').toLowerCase();
                        if (classes.includes('disabled') || classes.includes('inactive')) {
                            return false;
                        }

                        const rect = el.getBoundingClientRect();
                        return rect.width > 0 && rect.height > 0;
                    });

                    if (!candidatos.length) {
                        return {clicou: false};
                    }

                    candidatos
                        .sort((a, b) => {
                            const pa = (a.textContent || a.value || '').trim().length;
                            const pb = (b.textContent || b.value || '').trim().length;
                            return pa - pb;
                        })[0]
                        .click();
                    return {clicou: true};
                }
                """
            )
            if not avancou.get("clicou"):
                break

            try:
                await self.page.wait_for_function(
                    r"""
                    (referencia) => {
                        const atual = (document.body.innerText || '').replace(/\s+/g, '');
                        return atual !== referencia;
                    }
                    """,
                    re.sub(r"\s+", "", marca),
                    timeout=8_000,
                )
            except Exception:
                break

        results = normalize_general_search_results(
            resultados_unicos.values(),
            limit,
        )
        if not total_encontrado:
            body_text = await self.page.locator("body").inner_text()
            total_match = re.search(
                r"(\d+)\s+resultados?\s+encontrados?", body_text, re.IGNORECASE
            )
            total_encontrado = int(total_match.group(1)) if total_match else len(
                resultados_unicos
            )

        return {
            "fonte": "Menu geral do PJe > Processo > Processo",
            "entrada": (
                "clique no item real do menu geral; fallback oficial porque "
                "a rota Angular Consulta processual não ficou disponível"
            ),
            "rota": "/Processo/ConsultaProcesso/listView.seam",
            "valor_busca": identifier,
            "criterio_aplicado": criterion,
            "total_encontrado": total_encontrado,
            "retornados": len(results),
            "sem_resultado_confirmado": bool(
                re.search(
                    r"nenhum\s+(?:registro|processo)|sem\s+resultado",
                    await self.page.locator("body").inner_text(),
                    re.IGNORECASE,
                )
            ),
            "resultados": results,
            "somente_leitura": True,
            "paginacao_legacy": {
                "paginas_visitadas": pagina_atual,
                "limite_paginas": limite_paginas,
                "total_unicos": len(resultados_unicos),
            },
            "diagnostico_rota_angular": (
                self.navigation_diagnostic.get("angular_indisponivel")
            ),
        }


class TaskFlowPanelPage:
    """Page Object para navegação e leitura do painel de tarefas (Angular) do PJe."""

    def __init__(self, page: Any, url_base: str):
        self.page = page
        self.url_base = url_base.rstrip("/")
        self.frame = None

    async def open(self) -> Any:
        """Abre o painel de tarefas e localiza o frame Angular correspondente."""
        await self.page.goto(
            f"{self.url_base}/home.seam", wait_until="domcontentloaded"
        )
        link = self.page.get_by_role(
            "link", name=re.compile(r"Painel do usu[aá]rio", re.IGNORECASE)
        ).first
        if not await link.count():
            raise RuntimeError("Link 'Painel do usuário' não encontrado no PJe")
        href = await link.get_attribute("href")
        if not href:
            raise RuntimeError("Link do painel interno veio sem destino")
        if href.startswith("/"):
            origem = re.match(r"^https?://[^/]+", self.url_base).group(0)
            href = origem + href
        await self.page.goto(href, wait_until="domcontentloaded")

        limite = time.monotonic() + 30
        while time.monotonic() < limite:
            for frame in self.page.frames:
                if frame == self.page.main_frame:
                    continue
                try:
                    if await frame.locator('a[href*="lista-processos-tarefa"]').count():
                        self.frame = frame
                        return frame
                except Exception:
                    continue
            await asyncio.sleep(0.25)
        raise RuntimeError(
            "Painel interno carregou, mas nenhuma caixa/tarefa ficou visível"
        )

    async def list_boxes(self) -> List[Dict[str, Any]]:
        """Lê todas as caixas/tarefas listadas no painel sem abri-las."""
        if not self.frame:
            await self.open()
        
        tarefas = await self.frame.evaluate("""
        () => {
            const mapa = new Map();
            document.querySelectorAll(
                'a[href*="lista-processos-tarefa"]'
            ).forEach((link) => {
                const nome = (
                    link.getAttribute('title')
                    || link.querySelector('.nome')?.textContent
                    || ''
                ).trim();
                const quantidade = Number(
                    link.querySelector('.quantidadeTarefa')?.textContent || 0
                );
                if (!nome) return;
                const atual = mapa.get(nome);
                if (!atual || quantidade > atual.quantidade) {
                    mapa.set(nome, {
                        grupo: 'tarefas',
                        nome,
                        quantidade,
                        rota: (link.getAttribute('href') || '').split('?')[0],
                    });
                }
            });
            return Array.from(mapa.values()).sort(
                (a, b) => a.nome.localeCompare(b.nome, 'pt-BR')
            );
        }
        """)
        return tarefas

    async def open_box(self, nome_tarefa: str) -> None:
        """Abre uma caixa de tarefas específica pelo nome."""
        if not self.frame:
            await self.open()
            
        link = self.frame.locator(f'a[title="{nome_tarefa}"]').first
        if not await link.count():
            link = self.frame.get_by_role("link", name=re.compile(re.escape(nome_tarefa), re.IGNORECASE)).first
            
        if not await link.count():
            raise RuntimeError(f"Caixa/tarefa '{nome_tarefa}' não encontrada no painel")
            
        await link.evaluate("(el) => el.click()")
        try:
            await self.frame.wait_for_function(
                "() => /lista-processos-tarefa/.test(location.hash)",
                timeout=15_000,
            )
            await self.frame.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            pass

    async def mapear_transicoes_primeiro_processo(self) -> List[Dict[str, Any]]:
        """Seleciona o primeiro processo da caixa aberta e mapeia as transições disponíveis."""
        if not self.frame:
            raise RuntimeError("Nenhuma caixa aberta no painel. Chame open_box primeiro.")
            
        sucesso_clique = await self.frame.evaluate("""
        () => {
            const cnjRegex = /\\d{7}-\\d{2}\\.\\d{4}\\.\\d\\.\\d{2}\\.\\d{4}/;
            const elementos = Array.from(document.querySelectorAll(
                'article, tr, li, .card, [class*="processo" i], div.processo-detalhe, div.processo-linha, a.processo-link'
            ));
            const alvo = elementos.find(el => cnjRegex.test(el.innerText || ''));
            if (alvo) {
                alvo.click();
                return true;
            }
            return false;
        }
        """)
        if not sucesso_clique:
            return []
            
        await asyncio.sleep(3.0)
        
        transicoes = await self.frame.evaluate("""
        () => {
            const lista = [];
            
            document.querySelectorAll('select').forEach((sel) => {
                const label = (sel.getAttribute('aria-label') || sel.id || sel.name || '').toLowerCase();
                if (label.includes('pagin') || label.includes('item') || label.includes('qtd')) return;
                
                Array.from(sel.options).forEach((opt) => {
                    const val = opt.value;
                    const text = (opt.textContent || '').trim();
                    if (!val || val === '0' || val.toLowerCase().includes('selecione')) return;
                    
                    lista.push({
                        tipo: 'select_option',
                        id: val,
                        nome: text,
                        seletor: `select option[value="${val}"]`,
                        // Um select PODE ter botão de confirmação separado,
                        // mas isso é hipótese, não prova: em parte dos fluxos
                        // do PJe o change do select já dispara a transição.
                        // Fica INDETERMINADA até promoção explícita via
                        // confirmar_reversibilidade_transicao.
                        reversibilidade: 'INDETERMINADA',
                        classificacao_fonte: 'heuristica_tipo_elemento'
                    });
                });
            });
            
            document.querySelectorAll('button, a, [role="button"]').forEach((btn) => {
                const text = (btn.innerText || btn.textContent || '').trim();
                const title = btn.getAttribute('title') || '';
                const ariaLabel = btn.getAttribute('aria-label') || '';
                const id = btn.id || '';
                
                const textLower = text.toLowerCase();
                const titleLower = title.toLowerCase();
                const ariaLower = ariaLabel.toLowerCase();
                
                const isTransitionBtn = (
                    textLower.includes('encaminhar') || titleLower.includes('encaminhar') || ariaLower.includes('encaminhar') ||
                    textLower.includes('mover') || titleLower.includes('mover') || ariaLower.includes('mover') ||
                    textLower.includes('enviar para') || titleLower.includes('enviar para') ||
                    textLower.includes('transição') || titleLower.includes('transição') ||
                    btn.classList.contains('btn-transicao') || id.includes('transicao') || id.includes('encaminhar')
                );
                
                if (isTransitionBtn) {
                    lista.push({
                        tipo: 'button',
                        id: id || text,
                        nome: text || title || ariaLabel,
                        seletor: id ? `#${id}` : `button:has-text("${text}")`,
                        reversibilidade: 'COMMIT_NA_SELECAO',
                        classificacao_fonte: 'heuristica_tipo_elemento'
                    });
                }
            });
            
            return lista;
        }
        """)
        
        return transicoes
