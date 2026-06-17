"""Configuração runtime: lê de app_config (banco) com fallback para .env.

Chaves expostas (editáveis pela UI):
  Identidade da empresa: EMPRESA_NOME, EMPRESA_CNPJ, EMPRESA_LOGO_*
  Alertas:               ALERT_THRESHOLDS
  SMTP:                  SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD,
                         SMTP_FROM, SMTP_TLS
  SEFAZ:                 SEFAZ_AMBIENTE, SEFAZ_UF

Campos sensíveis (senhas) são cifrados com Fernet (ver app.crypto) antes
de gravar e decifrados ao ler. Quem chama `get(...)` recebe sempre o
valor já em texto claro.

As alterações têm efeito imediato no próximo handler — não há cache.

Settings que continuam no .env por motivo técnico:
  DATABASE_URL, SECRET_KEY — precisam estar disponíveis antes do banco
    abrir e antes da decifragem rodar.
  SYNC_INTERVALO_HORAS, TZ — usados pelo APScheduler no worker, que já
    foi inicializado quando o usuário edita a config. Mover exigiria
    reagendamento dinâmico (ver TODO).
  AUTH_EMAIL, AUTH_PASSWORD — semente do primeiro admin; depois disso
    o cadastro é feito pela UI de Usuários.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from .config import settings
from .crypto import decrypt, encrypt
from .models import AppConfig, CertificadoDigital

# Chaves editáveis. Mapeamento: chave_no_banco -> nome_attr_no_Settings (.env).
# A primeira leitura sem registro no banco devolve o default do .env, sem
# popular a tabela. Salvamento explícito cria a linha.
EDITAVEIS: dict[str, str] = {
    # identidade
    "EMPRESA_NOME":       "EMPRESA_NOME",
    "EMPRESA_CNPJ":       "EMPRESA_CNPJ",
    "EMPRESA_LOGO_LIGHT": "EMPRESA_LOGO_LIGHT",
    "EMPRESA_LOGO_DARK":  "EMPRESA_LOGO_DARK",
    # alertas
    "ALERT_THRESHOLDS":   "ALERT_THRESHOLDS",
    # SMTP
    "SMTP_HOST":          "SMTP_HOST",
    "SMTP_PORT":          "SMTP_PORT",
    "SMTP_USER":          "SMTP_USER",
    "SMTP_PASSWORD":      "SMTP_PASSWORD",
    "SMTP_FROM":          "SMTP_FROM",
    "SMTP_TLS":           "SMTP_TLS",
    # SEFAZ
    "SEFAZ_AMBIENTE":     "SEFAZ_AMBIENTE",
    "SEFAZ_UF":           "SEFAZ_UF",
}

# Conjunto das chaves cujo valor deve ser cifrado no banco.
SENSIVEIS: set[str] = {"SMTP_PASSWORD"}


def _default(chave: str) -> str:
    attr = EDITAVEIS.get(chave)
    if attr is None:
        return ""
    return str(getattr(settings, attr, "") or "")


def get(db: Session, chave: str) -> str:
    """Retorna o valor da chave em texto claro: banco se existir
    (decifrando se sensível), senão default do .env."""
    row = db.get(AppConfig, chave)
    if row is None or row.valor is None:
        return _default(chave)
    if chave in SENSIVEIS:
        # No banco está cifrado; decifrar pra devolver em claro.
        return decrypt(row.valor) or _default(chave)
    return row.valor


def set_(db: Session, chave: str, valor: str) -> None:
    if chave not in EDITAVEIS:
        raise ValueError(f"Chave não permitida: {chave}")
    valor_gravar = encrypt(valor) if chave in SENSIVEIS else valor
    row = db.get(AppConfig, chave)
    if row is None:
        db.add(AppConfig(chave=chave, valor=valor_gravar))
    else:
        row.valor = valor_gravar
    db.commit()


def snapshot(db: Session) -> dict[str, str]:
    """Lê todas as chaves editáveis de uma vez (com fallback).
    Senhas vêm em texto claro — não usar pra renderizar em form."""
    return {k: get(db, k) for k in EDITAVEIS}


def snapshot_safe(db: Session) -> dict[str, str]:
    """Snapshot mascarando senhas (pra usar no template)."""
    out = snapshot(db)
    for k in SENSIVEIS:
        if out.get(k):
            out[k] = "•" * 8  # indicador visual de "tem senha gravada"
    return out


# ---------- helpers tipados ----------

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


def get_smtp(db: Session) -> dict:
    """Configuração SMTP efetiva (banco com fallback .env)."""
    def _int(v, default):
        try:
            return int(v)
        except (TypeError, ValueError):
            return default

    def _bool(v, default):
        if isinstance(v, bool):
            return v
        s = str(v or "").strip().lower()
        if s in ("1", "true", "yes", "sim", "on"):
            return True
        if s in ("0", "false", "no", "nao", "off"):
            return False
        return default

    return {
        "host":     get(db, "SMTP_HOST"),
        "port":     _int(get(db, "SMTP_PORT"), 587),
        "user":     get(db, "SMTP_USER"),
        "password": get(db, "SMTP_PASSWORD"),
        "from":     get(db, "SMTP_FROM") or settings.SMTP_FROM,
        "tls":      _bool(get(db, "SMTP_TLS"), True),
    }


def get_sefaz(db: Session) -> dict:
    """Configuração SEFAZ efetiva (banco com fallback .env)."""
    raw_amb = get(db, "SEFAZ_AMBIENTE") or str(settings.SEFAZ_AMBIENTE)
    try:
        ambiente = int(raw_amb)
        if ambiente not in (1, 2):
            ambiente = settings.SEFAZ_AMBIENTE
    except ValueError:
        ambiente = settings.SEFAZ_AMBIENTE
    uf = (get(db, "SEFAZ_UF") or settings.SEFAZ_UF or "SP").upper()
    return {"ambiente": ambiente, "uf": uf}


# ---------- certificado digital ----------

def get_cert(db: Session) -> tuple[bytes, str] | None:
    """Retorna (pfx_bytes, senha) do certificado salvo no banco.
    Se não houver, retorna None — quem chama deve tentar o fallback do .env."""
    row = db.get(CertificadoDigital, 1)
    if row is None or not row.pfx:
        return None
    senha = decrypt(row.senha_cifrada or "")
    return row.pfx, senha


def get_cert_info(db: Session) -> dict | None:
    """Metadados do cert pra exibir na UI. Não devolve a senha."""
    row = db.get(CertificadoDigital, 1)
    if row is None:
        return None
    return {
        "nome_arquivo": row.nome_arquivo or "",
        "tamanho": len(row.pfx) if row.pfx else 0,
        "atualizado_em": row.atualizado_em,
    }


def set_cert(db: Session, *, pfx_bytes: bytes, senha: str, nome_arquivo: str) -> None:
    """Persiste o certificado. Sobrescreve qualquer cert anterior."""
    row = db.get(CertificadoDigital, 1)
    if row is None:
        row = CertificadoDigital(id=1)
        db.add(row)
    row.pfx = pfx_bytes
    row.senha_cifrada = encrypt(senha)
    row.nome_arquivo = nome_arquivo[:255] if nome_arquivo else None
    db.commit()
