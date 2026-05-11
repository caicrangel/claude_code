"""Configuração runtime: lê de app_config (banco) com fallback para .env.

Chaves expostas:
  EMPRESA_NOME, EMPRESA_CNPJ, EMPRESA_LOGO_LIGHT, EMPRESA_LOGO_DARK,
  ALERT_THRESHOLDS.

Demais settings (SMTP, SEFAZ, DB, SECRET_KEY, certificado) continuam
vindo apenas do .env por questão de segurança/sensibilidade.

As alterações têm efeito imediato no próximo handler — não há cache.
Para chamadas que precisam de muitos valores juntos, use `snapshot()`.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from .config import settings
from .models import AppConfig

# Chaves editáveis e seu default vindo do Settings (.env).
# A primeira vez que o sistema lê uma chave ausente em app_config,
# ele NÃO popula o banco; só responde o default. O salvamento explícito
# (POST /configuracao/app) cria a linha.
EDITAVEIS: dict[str, str] = {
    "EMPRESA_NOME":       "EMPRESA_NOME",
    "EMPRESA_CNPJ":       "EMPRESA_CNPJ",
    "EMPRESA_LOGO_LIGHT": "EMPRESA_LOGO_LIGHT",
    "EMPRESA_LOGO_DARK":  "EMPRESA_LOGO_DARK",
    "ALERT_THRESHOLDS":   "ALERT_THRESHOLDS",
}


def _default(chave: str) -> str:
    attr = EDITAVEIS.get(chave)
    if attr is None:
        return ""
    return str(getattr(settings, attr, "") or "")


def get(db: Session, chave: str) -> str:
    """Retorna o valor da chave: banco se existir, senão default do .env."""
    row = db.get(AppConfig, chave)
    if row is not None and row.valor is not None:
        return row.valor
    return _default(chave)


def set_(db: Session, chave: str, valor: str) -> None:
    if chave not in EDITAVEIS:
        raise ValueError(f"Chave não permitida: {chave}")
    row = db.get(AppConfig, chave)
    if row is None:
        db.add(AppConfig(chave=chave, valor=valor))
    else:
        row.valor = valor
    db.commit()


def snapshot(db: Session) -> dict[str, str]:
    """Lê todas as chaves editáveis de uma vez (com fallback)."""
    return {k: get(db, k) for k in EDITAVEIS}


# Helpers tipados (alvos comuns)

def empresa_nome(db: Session) -> str:
    return get(db, "EMPRESA_NOME") or "Cota Diesel"


def cnpj_limpo(db: Session) -> str:
    raw = get(db, "EMPRESA_CNPJ")
    return "".join(c for c in raw if c.isdigit())


def thresholds(db: Session) -> list[int]:
    raw = get(db, "ALERT_THRESHOLDS") or "70,85,95,100"
    out: list[int] = []
    for piece in raw.split(","):
        piece = piece.strip()
        if not piece:
            continue
        try:
            out.append(int(piece))
        except ValueError:
            continue
    return sorted(set(out))


def logo_value(db: Session, tema: str) -> str:
    """tema ∈ {'light','dark'}. Retorna o valor bruto (URL ou caminho)."""
    chave = "EMPRESA_LOGO_LIGHT" if tema == "light" else "EMPRESA_LOGO_DARK"
    return get(db, chave)


def resolve_logo(value: str) -> str | None:
    v = (value or "").strip()
    if not v:
        return None
    if v.startswith(("http://", "https://", "/")):
        return v
    return "/static/" + v


def logos_url(db: Session) -> dict[str, str | None]:
    fallback_attr = (settings.EMPRESA_LOGO or "").strip()
    fallback = resolve_logo(fallback_attr)
    return {
        "light": resolve_logo(logo_value(db, "light")) or fallback,
        "dark":  resolve_logo(logo_value(db, "dark"))  or fallback,
    }
