"""Robustez adicional para a consulta processual nativa do PJe.

Este módulo fica separado do inventário de seletores porque esse inventário
é atualizado por outras rotinas. A subclasse mantém o contrato do page object
e trata, de forma determinística, tanto postback JSF quanto atualização AJAX.
"""

from __future__ import annotations

import re
import time
from typing import Any

from mapa_pje import NativeProcessSearchPage as _MappedNativeSearchPage

RESULT_SIGNAL = re.compile(
    r"nenhum\s+(?:registro|processo)|sem\s+resultado|"
    r"\b\d+\s+resultados?\s+encontrados?|"
    r"\d{7}-\d{2}\.\d{4}\.\d\.\d{2}\.\d{4}",
    re.IGNORECASE,
)


class NativeProcessSearchPage(_MappedNativeSearchPage):
    """Confirma o resultado sem confundir navegação com falso vazio."""

    async def _body_has_result_signal(self) -> bool:
        try:
            body = await self.page.locator("body").inner_text()
        except Exception:
            return False
        return bool(RESULT_SIGNAL.search(body))

    async def _wait_result_change(self, before: str, timeout: int) -> bool:
        deadline = time.monotonic() + max(1, timeout) / 1000
        while True:
            remaining_ms = max(
                1,
                int((deadline - time.monotonic()) * 1000),
            )
            try:
                await self.page.wait_for_function(
                    """
                    (previous) => {
                        const body = document.body.innerText || '';
                        const tables = Array.from(
                            document.querySelectorAll('table')
                        );
                        const table = tables.find((item) =>
                            /Processo/i.test(item.innerText || '')
                            && /Órgão julgador|Orgao julgador/i.test(
                                item.innerText || ''
                            )
                        );
                        const current = (table?.innerText || body)
                            .replace(/\\s+/g, ' ').trim();
                        const busy = Array.from(document.querySelectorAll(
                            '[aria-busy="true"], .ui-blockui'
                        )).some(
                            (item) => item.getClientRects().length > 0
                        );
                        return !busy && current !== previous;
                    }
                    """,
                    before,
                    timeout=remaining_ms,
                )
                self._search_response_confirmed = True
                return True
            except Exception:
                try:
                    await self.page.wait_for_load_state(
                        "domcontentloaded",
                        timeout=remaining_ms,
                    )
                    await self.page.locator("body").wait_for(
                        state="attached",
                        timeout=remaining_ms,
                    )
                except Exception:
                    pass
                if await self._body_has_result_signal():
                    self._search_response_confirmed = True
                    return True
                if time.monotonic() >= deadline:
                    if self._search_response_confirmed is None:
                        self._search_response_confirmed = False
                    return False

    async def search(
        self,
        criterion: str,
        value: Any,
        limit: int = 20,
        max_pages: int = 80,
    ) -> dict[str, Any]:
        self._search_response_confirmed = None
        result = await super().search(
            criterion=criterion,
            value=value,
            limit=limit,
            max_pages=max_pages,
        )
        confirmed = bool(self._search_response_confirmed)
        result["resposta_confirmada"] = confirmed
        result["resultado_completo"] = bool(
            confirmed and result.get("resultado_completo")
        )
        result["avisos"] = (
            []
            if confirmed
            else [
                "O PJe não confirmou a atualização da grade; o retorno "
                "vazio é inconclusivo e não deve ser tratado como ausência "
                "de processos."
            ]
        )
        return result
