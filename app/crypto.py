"""Criptografia simétrica para segredos guardados no banco.

Usa Fernet com chave derivada do SECRET_KEY do .env via SHA-256.
Trocar SECRET_KEY invalida todos os segredos cifrados — o admin precisará
reconfigurar (cert/SMTP). Esse é o comportamento desejado: o SECRET_KEY
é a raiz da confiança.
"""
from __future__ import annotations

import base64
import hashlib
import logging

from cryptography.fernet import Fernet, InvalidToken

from .config import settings

log = logging.getLogger(__name__)


def _fernet() -> Fernet:
    raw = (settings.SECRET_KEY or "change-me").encode() + b"::cota-diesel-secrets-v1"
    digest = hashlib.sha256(raw).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt(text: str) -> str:
    """Cifra um texto. Vazio devolve vazio (sem cifrar)."""
    if not text:
        return ""
    return _fernet().encrypt(text.encode()).decode()


def decrypt(token: str) -> str:
    """Decifra um token. Vazio ou inválido devolve string vazia."""
    if not token:
        return ""
    try:
        return _fernet().decrypt(token.encode()).decode()
    except (InvalidToken, ValueError):
        log.warning("Token cifrado inválido — SECRET_KEY pode ter mudado")
        return ""
