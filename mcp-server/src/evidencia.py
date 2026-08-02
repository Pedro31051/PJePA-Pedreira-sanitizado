import base64
import io
import logging
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("pjepa.evidencia")

def carimbar_screenshot(
    png_bytes: bytes,
    url: str,
    processo: Optional[str] = None,
    descricao: Optional[str] = None,
) -> bytes:
    try:
        from PIL import Image, ImageDraw, ImageFont
        img = Image.open(io.BytesIO(png_bytes))
        draw = ImageDraw.Draw(img)
        
        linhas = [
            f"URL: {url}",
            f"Data/Hora: {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}",
        ]
        if processo:
            linhas.append(f"Processo: {processo}")
        if descricao:
            linhas.append(f"Acao: {descricao}")
            
        texto = "\n".join(linhas)
        
        try:
            font = ImageFont.load_default()
        except Exception:
            font = None
            
        x, y = 10, 10
        if font:
            bbox = draw.textbbox((x, y), texto, font=font)
            box_w = bbox[2] - bbox[0] + 20
            box_h = bbox[3] - bbox[1] + 20
        else:
            box_w = 400
            box_h = len(linhas) * 15 + 20
            
        if img.mode != 'RGBA':
            img = img.convert('RGBA')
            
        overlay = Image.new('RGBA', img.size, (0, 0, 0, 0))
        draw_ov = ImageDraw.Draw(overlay)
        draw_ov.rectangle([x, y, x + box_w, y + box_h], fill=(0, 0, 0, 180))
        
        img = Image.alpha_composite(img, overlay)
        draw = ImageDraw.Draw(img)
        
        draw.text((x + 10, y + 10), texto, fill=(255, 255, 255, 255), font=font)
        
        out = io.BytesIO()
        img.save(out, format="PNG")
        return out.getvalue()
    except Exception as exc:
        logger.warning(f"Falha ao carimbar screenshot: {exc}")
        return png_bytes

async def tirar_evidencia_efemera(
    page,
    *,
    full_page: bool = False,
    clip: Optional[Dict[str, Any]] = None,
    processo: Optional[str] = None,
    descricao: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    Tira um screenshot da página ativa, converte para base64 e apaga o
    arquivo temporário imediatamente. Nunca persiste dados em disco após a chamada.

    Retorna:
        {"mime": "image/png", "b64": "<base64>", "bytes": 12345} ou None em caso de erro
    """
    if not page or page.is_closed():
        logger.warning("Tentativa de screenshot em pagina nula ou fechada.")
        return None

    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    tmp_path = tmp.name
    tmp.close()
    try:
        opts = {"path": tmp_path, "full_page": full_page}
        if clip:
            opts["clip"] = clip
        
        await page.screenshot(**opts)
        
        p = Path(tmp_path)
        if p.exists():
            data = p.read_bytes()
            url = page.url or "URL desconhecida"
            data = carimbar_screenshot(data, url, processo, descricao)
            
            return {
                "mime": "image/png",
                "b64": base64.b64encode(data).decode("ascii"),
                "bytes": len(data),
            }
        return None
    except Exception as e:
        logger.exception(f"Erro ao tirar screenshot: {e}")
        return None
    finally:
        try:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
        except OSError as e:
            logger.warning(f"Nao foi possivel remover arquivo temporario de print {tmp_path}: {e}")

