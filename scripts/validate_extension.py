#!/usr/bin/env python3
"""Validação offline do manifesto e referências da extensão."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "chrome-extension"


def main() -> int:
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["manifest_version"] == 3
    references = {manifest["action"]["default_popup"]}
    references.update(manifest.get("icons", {}).values())
    references.update(manifest["action"].get("default_icon", {}).values())
    for content in manifest.get("content_scripts", []):
        references.update(content.get("js", []))
    missing = sorted(item for item in references if not (ROOT / item).is_file())
    if missing:
        raise SystemExit(f"referências ausentes no manifesto: {', '.join(missing)}")
    print("Manifesto da extensão validado.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
