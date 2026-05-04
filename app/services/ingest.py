"""Polling SEFAZ → parsing → persistência → cota (single-tenant)."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from ..config import settings
from ..models import NotaFiscal, get_state, set_state
from .parser import parse_nfe
from .quota import avaliar_e_alertar
from .sefaz import SefazClient

log = logging.getLogger(__name__)
NSU_KEY = "ultimo_nsu"
LAST_CALL_KEY = "ultima_consulta_em"
_client: SefazClient | None = None


class TooSoonError(RuntimeError):
    """Lançada quando uma nova consulta SEFAZ é solicitada antes do intervalo mínimo."""

    def __init__(self, segundos_restantes: int):
        super().__init__(f"Aguarde {segundos_restantes}s antes de consultar a SEFAZ novamente")
        self.segundos_restantes = segundos_restantes


def get_client() -> SefazClient:
    global _client
    if _client is None:
        _client = SefazClient(
            cert_path=settings.CERT_PATH,
            cert_password=settings.CERT_PASSWORD,
            ambiente=settings.SEFAZ_AMBIENTE,
            uf=settings.SEFAZ_UF,
        )
    return _client


def _segundos_desde_ultima(db: Session) -> int | None:
    raw = get_state(db, LAST_CALL_KEY, "")
    if not raw:
        return None
    try:
        last = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return int((datetime.now(timezone.utc) - last).total_seconds())


def processar(db: Session, *, force: bool = False) -> dict:
    if not settings.cnpj_limpo:
        raise RuntimeError("EMPRESA_CNPJ não configurado no .env")

    desde = _segundos_desde_ultima(db)
    if not force and desde is not None and desde < settings.MIN_SEFAZ_INTERVAL:
        raise TooSoonError(settings.MIN_SEFAZ_INTERVAL - desde)

    set_state(db, LAST_CALL_KEY, datetime.now(timezone.utc).isoformat())

    client = get_client()
    novas = 0
    ult_nsu = get_state(db, NSU_KEY, "0")

    for _ in range(20):
        try:
            cstat, ult_recebido, docs = client.consultar(settings.cnpj_limpo, ult_nsu)
        except Exception as e:  # noqa: BLE001
            log.exception("Erro consultando SEFAZ: %s", e)
            break
        log.info("SEFAZ cStat=%s ultNSU=%s docs=%d", cstat, ult_recebido, len(docs))

        for doc in docs:
            if not doc.chave:
                continue
            if db.query(NotaFiscal).filter(NotaFiscal.chave == doc.chave).first():
                continue
            parsed = parse_nfe(doc.xml)
            if parsed is None or not parsed.itens_diesel:
                continue
            db.add(NotaFiscal(
                chave=parsed.chave,
                numero=parsed.numero,
                serie=parsed.serie,
                emitente_cnpj=parsed.emit_cnpj,
                emitente_nome=parsed.emit_nome,
                data_emissao=parsed.data_emissao,
                valor_total=parsed.valor_total,
                litros_diesel=parsed.litros_diesel,
                ncm=parsed.itens_diesel[0].ncm,
                cfop=parsed.itens_diesel[0].cfop,
                xml=doc.xml.decode("utf-8", errors="replace"),
            ))
            novas += 1

        if ult_recebido and ult_recebido != ult_nsu:
            ult_nsu = ult_recebido
            set_state(db, NSU_KEY, ult_nsu)

        if cstat == "137" or not docs:
            break

    if novas:
        db.commit()
    resumo = avaliar_e_alertar(db)
    resumo["novas"] = novas
    return resumo
