"""Compatibilidade isolada entre Scrapling e versões recentes do lxml."""

from __future__ import annotations

from typing import Any

from scrapling import parser as scrapling_parser

_LXML_HTML_PARSER = scrapling_parser.HTMLParser


def html_parser(*args: Any, **kwargs: Any) -> Any:
    kwargs.pop("strip_cdata", None)
    return _LXML_HTML_PARSER(*args, **kwargs)


def install() -> None:
    scrapling_parser.HTMLParser = html_parser
