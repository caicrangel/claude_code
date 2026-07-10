"""Envio de alertas via Telegram Bot API.

Usa os endpoints HTTP oficiais:
  sendMessage  → texto (alertas de NF nova e de limiar)
  sendDocument → arquivo (PDF do panorama mensal)

Configuração (banco, com fallback .env) via runtime_config.get_telegram:
  TELEGRAM_ENABLED, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID.

Falhas são logadas e nunca propagam — Telegram é canal complementar ao
e-mail, não pode derrubar o fluxo principal.
"""
from __future__ import annotations

import logging

import requests
from sqlalchemy.orm import Session

from ..config import settings
from ..database import SessionLocal

log = logging.getLogger(__name__)

API = "https://api.telegram.org"


def _cfg(db: Session | None) -> dict:
    if db is None:
        return {
            "enabled": settings.TELEGRAM_ENABLED,
            "token": (settings.TELEGRAM_BOT_TOKEN or "").strip(),
            "chat_id": (settings.TELEGRAM_CHAT_ID or "").strip(),
        }
    from .. import runtime_config
    return runtime_config.get_telegram(db)


def _resolver_cfg(db: Session | None) -> dict | None:
    """Lê a config (abrindo sessão própria se preciso). Retorna None se o
    canal está desabilitado ou incompleto."""
    owns = db is None
    if owns:
        db = SessionLocal()
    try:
        cfg = _cfg(db)
    finally:
        if owns and db is not None:
            db.close()
    if not cfg["enabled"] or not cfg["token"] or not cfg["chat_id"]:
        return None
    return cfg


def send_message(text: str, *, db: Session | None = None,
                 parse_mode: str | None = "HTML") -> bool:
    """Envia uma mensagem de texto. Retorna True se o Telegram aceitou."""
    cfg = _resolver_cfg(db)
    if cfg is None:
        return False
    payload = {
        "chat_id": cfg["chat_id"],
        "text": text,
        "disable_web_page_preview": True,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode
    try:
        r = requests.post(f"{API}/bot{cfg['token']}/sendMessage",
                          json=payload, timeout=20)
        if not r.ok:
            log.warning("Telegram sendMessage falhou: %s %s", r.status_code, r.text[:300])
        return r.ok
    except Exception:  # noqa: BLE001
        log.exception("Erro enviando mensagem Telegram")
        return False


def send_document(filename: str, content: bytes, *, caption: str = "",
                  db: Session | None = None) -> bool:
    """Envia um arquivo (ex.: PDF do panorama). Retorna True se aceitou."""
    cfg = _resolver_cfg(db)
    if cfg is None:
        return False
    try:
        r = requests.post(
            f"{API}/bot{cfg['token']}/sendDocument",
            data={"chat_id": cfg["chat_id"], "caption": caption[:1024],
                  "parse_mode": "HTML"},
            files={"document": (filename, content, "application/pdf")},
            timeout=30,
        )
        if not r.ok:
            log.warning("Telegram sendDocument falhou: %s %s", r.status_code, r.text[:300])
        return r.ok
    except Exception:  # noqa: BLE001
        log.exception("Erro enviando documento Telegram")
        return False


def enviar_teste(db: Session | None = None) -> tuple[bool, str]:
    """Envia uma mensagem de teste. Retorna (ok, detalhe) para a UI."""
    cfg = _resolver_cfg(db)
    if cfg is None:
        return False, "Telegram desabilitado ou token/chat_id ausentes"
    ok = send_message("✅ <b>Teste de alerta</b>\nO Telegram está configurado corretamente.",
                      db=db)
    return ok, ("Mensagem de teste enviada" if ok else "Falha ao enviar (ver logs)")
