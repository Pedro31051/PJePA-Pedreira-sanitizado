from __future__ import annotations

import importlib
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

pje_client = importlib.import_module("pje_client")


def test_recognises_signed_pdf_without_rigid_storage_hostname():
    assert pje_client._is_probable_consolidated_pdf_url(
        "https://object.example/autos/processo.pdf?X-Amz-Signature=secret"
    )
    assert pje_client._is_probable_consolidated_pdf_url(
        "https://pje.example/download/autos/42", "application/pdf"
    )
    assert not pje_client._is_probable_consolidated_pdf_url(
        "https://pje.example/assets/icon.png", "image/png"
    )


def test_download_timeout_is_long_but_bounded(monkeypatch):
    monkeypatch.setenv("PJE_NATIVE_DOWNLOAD_TIMEOUT_MS", "450000")
    assert pje_client._native_download_timeout_ms() == 450000
    assert pje_client._native_download_timeout_ms(1) == 120000
    assert pje_client._native_download_timeout_ms(9999999) == 900000
