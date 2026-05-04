"""Polling fonte (SIEG ou SEFAZ) → parsing → persistência → cota."""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone

from sqlalchemy.orm import Session

from ..config import settings
from ..models import NotaFiscal, get_state, set_state
from .parser import parse_nfe
from .quota import avaliar_e_alertar
from .sefaz import SefazClient
from .sieg import SiegClient

log = logging.getLogger(__name__)
NSU_KEY = "ultimo_nsu"
LAST_CALL_KEY = "ultima_consulta_em"

_sefaz: SefazClient | None = None
_sieg: SiegClient | None = None


class TooSoonError(RuntimeError):
    def __init__(self, segundos_restantes: int):
        super().__init__(f"Aguarde {segundos_restantes}s antes de consultar novamente")
        self.segundos_restantes = segundos_restantes


def _segundos_desde_ultima(db: Session) -> int | None:
    raw = get_state(db, LAST_CALL_KEY, "")
    if not raw:
        return None
    try:
        last = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return int((datetime.now(timezone.utc) - last).total_seconds())


def _check_throttle(db: Session, force: bool) -> None:
    desde = _segundos_desde_ultima(db)
    if not force and desde is not None and desde < settings.MIN_SEFAZ_INTERVAL:
        raise TooSoonError(settings.MIN_SEFAZ_INTERVAL - desde)
    set_state(db, LAST_CALL_KEY, datetime.now(timezone.utc).isoformat())


def _registrar_xml(db: Session, xml: bytes) -> tuple[bool, str]:
    """Tenta gravar uma NFe a partir de um XML. Retorna (gravada, motivo)."""
    parsed = parse_nfe(xml)
    if parsed is None:
        return False, "nao-eh-nfe-completa"
    if not parsed.itens_diesel:
        return False, "sem-itens-diesel"
    if db.query(NotaFiscal).filter(NotaFiscal.chave == parsed.chave).first():
        return False, "ja-existe"
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
        xml=xml.decode("utf-8", errors="replace"),
    ))
    return True, "ok"


# ---------- SIEG ----------

def _get_sieg() -> SiegClient:
    global _sieg
    if _sieg is None:
        _sieg = SiegClient(settings.SIEG_API_KEY)
    return _sieg


def _processar_sieg(db: Session) -> dict:
    cnpj = settings.cnpj_limpo
    if not cnpj:
        raise RuntimeError("EMPRESA_CNPJ não configurado")
    client = _get_sieg()
    # Busca o período inteiro até hoje (ou fim do período se já passou)
    hoje = date.today()
    data_fim = min(settings.PERIODO_FIM, hoje)
    log.info("SIEG: baixando NFe %s..%s p/ %s", settings.PERIODO_INICIO, data_fim, cnpj)
    xmls = client.baixar_nfe_destinatario(cnpj, settings.PERIODO_INICIO, data_fim)
    novas = 0
    motivos: dict[str, int] = {}
    for xml in xmls:
        gravada, motivo = _registrar_xml(db, xml)
        if gravada:
            novas += 1
        else:
            motivos[motivo] = motivos.get(motivo, 0) + 1
    if novas:
        db.commit()
    log.info("SIEG: %d xmls baixados, %d gravados, motivos=%s", len(xmls), novas, motivos)
    return {"fonte": "sieg", "baixados": len(xmls), "novas": novas, "motivos": motivos}


# ---------- SEFAZ direto ----------

def _get_sefaz() -> SefazClient:
    global _sefaz
    if _sefaz is None:
        _sefaz = SefazClient(
            cert_path=settings.CERT_PATH,
            cert_password=settings.CERT_PASSWORD,
            ambiente=settings.SEFAZ_AMBIENTE,
            uf=settings.SEFAZ_UF,
        )
    return _sefaz


def _processar_sefaz(db: Session) -> dict:
    cnpj = settings.cnpj_limpo
    if not cnpj:
        raise RuntimeError("EMPRESA_CNPJ não configurado")
    client = _get_sefaz()
    novas = 0
    ult_nsu = get_state(db, NSU_KEY, "0")

    for _ in range(20):
        try:
            cstat, ult_recebido, docs = client.consultar(cnpj, ult_nsu)
        except Exception as e:  # noqa: BLE001
            log.exception("Erro consultando SEFAZ: %s", e)
            break
        log.info("SEFAZ cStat=%s ultNSU=%s docs=%d", cstat, ult_recebido, len(docs))
        for doc in docs:
            if not doc.chave:
                continue
            gravada, _ = _registrar_xml(db, doc.xml)
            if gravada:
                novas += 1
        if ult_recebido and ult_recebido != ult_nsu:
            ult_nsu = ult_recebido
            set_state(db, NSU_KEY, ult_nsu)
        if cstat == "137" or not docs:
            break
    if novas:
        db.commit()
    return {"fonte": "sefaz", "novas": novas}


# ---------- Entrada pública ----------

def processar(db: Session, *, force: bool = False) -> dict:
    _check_throttle(db, force)
    if settings.SIEG_API_KEY:
        resumo = _processar_sieg(db)
    else:
        resumo = _processar_sefaz(db)
    resumo.update(avaliar_e_alertar(db))
    return resumo
