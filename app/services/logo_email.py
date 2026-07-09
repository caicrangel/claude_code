"""Resolve a logo da empresa para uso em e-mails.

E-mails não conseguem carregar caminhos relativos (/static/...) nem
data: URI (bloqueado por Gmail/Outlook). Solução confiável: embutir a
imagem como anexo inline (CID). Este módulo devolve o `src` a usar no
HTML e, quando for arquivo local, os bytes para embutir.
"""
from __future__ import annotations

import logging
import mimetypes
from pathlib import Path

from sqlalchemy.orm import Session

from .. import runtime_config
from ..config import settings

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
LOGO_CID = "logo_empresa"


def logo_para_email(db: Session) -> tuple[str | None, tuple[str, bytes, str] | None]:
    """Retorna (html_src, inline_image).

    - Logo é arquivo em /static existente → ("cid:logo_empresa", (cid, bytes, mime)).
    - Logo é URL http(s) → (url, None) — referência direta no HTML.
    - Sem logo / arquivo inexistente → (None, None).

    Usa a logo do tema claro (fundo do e-mail é claro), com fallback para
    a do tema escuro e depois para EMPRESA_LOGO do .env.
    """
    raw = (runtime_config.logo_value(db, "light")
           or runtime_config.logo_value(db, "dark")
           or (settings.EMPRESA_LOGO or "")).strip()
    if not raw:
        return None, None
    if raw.startswith(("http://", "https://")):
        return raw, None

    fname = raw.lstrip("/")
    if fname.startswith("static/"):
        fname = fname[len("static/"):]
    path = (STATIC_DIR / fname).resolve()
    # Impede path traversal: só arquivos dentro de STATIC_DIR.
    try:
        path.relative_to(STATIC_DIR.resolve())
    except ValueError:
        log.warning("Logo fora de STATIC_DIR ignorada: %s", raw)
        return None, None
    if not path.is_file():
        return None, None
    data = path.read_bytes()
    mimetype = mimetypes.guess_type(str(path))[0] or "image/png"
    return f"cid:{LOGO_CID}", (LOGO_CID, data, mimetype)


def bloco_html(logo_src: str | None) -> str:
    """HTML do cabeçalho com a logo centralizada (vazio se não houver logo)."""
    if not logo_src:
        return ""
    return (
        '<div style="text-align:center;margin:0 0 18px;">'
        f'<img src="{logo_src}" alt="" '
        'style="max-height:52px;max-width:220px;object-fit:contain;"></div>'
    )
