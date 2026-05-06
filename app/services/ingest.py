"""Polling SEFAZ → parsing → persistência → cota.

Arquitetura:
  - NFeDistribuicaoDFe é NACIONAL: uma única consulta retorna todas as NFe
    emitidas contra o CNPJ do consultante, de qualquer UF emitente.
  - SEFAZ_UF é apenas a UF do consultante (cUFAutor) para roteamento interno.
  - A MANIFESTAÇÃO de notas é feita pela contabilidade, externamente. Após
    manifestada, a NFe completa entra na faixa de NSU e é capturada aqui.

Fluxo por docZip recebido:
  - procNFe / NFe completa  → _aplicar_nfe()
        com itens diesel    → grava/atualiza NotaFiscal
        sem itens diesel    → ignora (não polui banco)
  - resNFe (resumo)         → IGNORADO (sem manifestação no app)
  - procEventoNFe           → _aplicar_evento()
        110111 (Cancelamento) → marca cancelada=True em nota existente
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from ..config import settings
from ..models import NotaFiscal, get_state, set_state
from .parser import parse_evento, parse_nfe
from .quota import avaliar_e_alertar
from .sefaz import DocDFe, SefazClient

log = logging.getLogger(__name__)

NSU_KEY = "ultimo_nsu"
LAST_CALL_KEY = "ultima_consulta_em"
LAST_OK_KEY = "ultima_consulta_ok_em"
BLOQUEIO_656_KEY = "bloqueio_656_ate"
BLOQUEIO_656_COUNT_KEY = "bloqueio_656_count"
BLOQUEIO_656_BASE_SEGUNDOS = 3600     # 1h base
BLOQUEIO_656_MAX_SEGUNDOS = 24 * 3600  # 24h teto (backoff exponencial)


class TooSoonError(RuntimeError):
    def __init__(self, segundos_restantes: int):
        super().__init__(f"Aguarde {segundos_restantes}s antes de consultar a SEFAZ novamente")
        self.segundos_restantes = segundos_restantes


def get_client() -> SefazClient:
    return SefazClient(
        cert_path=settings.CERT_PATH,
        cert_password=settings.CERT_PASSWORD,
        ambiente=settings.SEFAZ_AMBIENTE,
        uf=settings.SEFAZ_UF,
    )


# ---------- throttle e bloqueio ----------

def _segundos_desde(db: Session, key: str) -> int | None:
    raw = get_state(db, key, "")
    if not raw:
        return None
    try:
        last = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return int((datetime.now(timezone.utc) - last).total_seconds())


def _check_throttle(db: Session, force: bool) -> None:
    bloqueio = status_bloqueio_656(db)
    if bloqueio and not force:
        raise TooSoonError(bloqueio["segundos_restantes"])

    desde = _segundos_desde(db, LAST_CALL_KEY)
    if not force and desde is not None and desde < settings.MIN_SEFAZ_INTERVAL:
        raise TooSoonError(settings.MIN_SEFAZ_INTERVAL - desde)
    set_state(db, LAST_CALL_KEY, datetime.now(timezone.utc).isoformat())


def _registrar_bloqueio_656(db: Session) -> None:
    """Registra bloqueio com backoff exponencial.
    1ª ocorrência = 1h, 2ª = 2h, 3ª = 4h, ... até teto de 24h.
    O contador zera quando uma consulta termina sem 656.
    """
    raw = get_state(db, BLOQUEIO_656_COUNT_KEY, "0")
    try:
        count = int(raw)
    except ValueError:
        count = 0
    count += 1
    set_state(db, BLOQUEIO_656_COUNT_KEY, str(count))
    segundos = min(BLOQUEIO_656_MAX_SEGUNDOS,
                   BLOQUEIO_656_BASE_SEGUNDOS * (2 ** (count - 1)))
    ate = datetime.now(timezone.utc) + timedelta(seconds=segundos)
    set_state(db, BLOQUEIO_656_KEY, ate.isoformat())
    log.warning("Bloqueio cStat=656 #%d ativado por %ds (até %s)",
                count, segundos, ate.isoformat())


def _resetar_contador_656(db: Session) -> None:
    set_state(db, BLOQUEIO_656_COUNT_KEY, "0")


def status_bloqueio_656(db: Session) -> dict | None:
    raw = get_state(db, BLOQUEIO_656_KEY, "")
    if not raw:
        return None
    try:
        ate = datetime.fromisoformat(raw)
    except ValueError:
        return None
    agora = datetime.now(timezone.utc)
    if agora >= ate:
        return None
    restante = int((ate - agora).total_seconds())
    return {
        "bloqueado": True,
        "ate_utc": ate,
        "segundos_restantes": restante,
        "minutos_restantes": restante // 60,
    }


def ultima_sincronizacao(db: Session) -> datetime | None:
    raw = get_state(db, LAST_OK_KEY, "")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


# ---------- aplicação de cada tipo de docZip ----------

def _aplicar_nfe(db: Session, doc: DocDFe) -> str:
    """Processa NFe completa. Só armazena se contém itens de diesel."""
    parsed = parse_nfe(doc.xml)
    if parsed is None:
        return "nfe-invalida"

    if not parsed.itens_diesel:
        return "ignorado-sem-diesel"

    nf = db.query(NotaFiscal).filter(NotaFiscal.chave == parsed.chave).first()
    if nf is None:
        nf = NotaFiscal(chave=parsed.chave)
        db.add(nf)

    nf.nsu = doc.nsu
    nf.numero = parsed.numero
    nf.serie = parsed.serie
    nf.emitente_cnpj = parsed.emit_cnpj
    nf.emitente_nome = parsed.emit_nome
    nf.data_emissao = parsed.data_emissao
    nf.valor_total = parsed.valor_total
    nf.litros_diesel = parsed.litros_diesel
    nf.ncm = parsed.itens_diesel[0].ncm
    nf.cfop = parsed.itens_diesel[0].cfop
    nf.xml = doc.xml.decode("utf-8", errors="replace")
    nf.is_resumo = False
    return "nfe-diesel"


def _aplicar_evento(db: Session, doc: DocDFe) -> str:
    """Aplica eventos apenas em notas que já temos no banco (diesel)."""
    parsed = parse_evento(doc.xml)
    if parsed is None or not parsed.chave:
        return "evento-invalido"
    nf = db.query(NotaFiscal).filter(NotaFiscal.chave == parsed.chave).first()
    if nf is None:
        return "evento-fora-do-escopo"  # evento de NFe que não é diesel
    if parsed.is_cancelamento:
        nf.cancelada = True
        return "cancelamento"
    return f"evento-{parsed.tp_evento}"


def _aplicar(db: Session, doc: DocDFe) -> str:
    schema = (doc.schema or "").lower()
    if schema.startswith("procnfe") or schema.startswith("nfe"):
        return _aplicar_nfe(db, doc)
    if schema.startswith("resnfe"):
        # Resumos são ignorados: a manifestação é feita pela contabilidade,
        # e quando isso acontece a NFe completa entra na faixa de NSU.
        return "resumo-ignorado"
    if schema.startswith("proceventonfe") or schema.startswith("evento"):
        return _aplicar_evento(db, doc)
    if schema.startswith("resevento"):
        # retEvento / resEventoNFe — recibo da SEFAZ para evento enviado pela contabilidade.
        # Não há nada para processar; ignorar silenciosamente.
        return "resevento-ignorado"
    # fallback por inspeção do conteúdo
    if b"<resNFe" in doc.xml:
        return "resumo-ignorado"
    if b"infNFe" in doc.xml:
        return _aplicar_nfe(db, doc)
    if b"infEvento" in doc.xml:
        return _aplicar_evento(db, doc)
    return f"schema-desconhecido:{schema}"


# ---------- entrada principal ----------

def processar(db: Session, *, force: bool = False) -> dict:
    """Consulta NFeDistribuicaoDFe nacional para o CNPJ configurado.

    Retorna dicionário com contadores e resumo de cota. Levanta TooSoonError
    se estiver dentro do throttle interno ou bloqueio cStat=656.
    """
    if not settings.cnpj_limpo:
        raise RuntimeError("EMPRESA_CNPJ não configurado no .env")
    _check_throttle(db, force)

    client = get_client()
    cnpj = settings.cnpj_limpo
    ult_nsu = get_state(db, NSU_KEY, "0")

    contadores: dict[str, int] = {}
    bloqueado = False

    for _ in range(20):  # máx 20 páginas por ciclo
        try:
            cstat, ult_recebido, docs = client.consultar_nsu(cnpj, ult_nsu)
        except Exception as e:  # noqa: BLE001
            log.exception("Erro consultando SEFAZ: %s", e)
            break

        log.info("SEFAZ cStat=%s ultNSU=%s docs=%d", cstat, ult_recebido, len(docs))

        for doc in docs:
            r = _aplicar(db, doc)
            contadores[r] = contadores.get(r, 0) + 1

        if ult_recebido and ult_recebido != ult_nsu:
            ult_nsu = ult_recebido
            set_state(db, NSU_KEY, ult_nsu)

        if cstat == "656":
            log.warning("SEFAZ cStat=656 (consumo indevido) - bloqueando 1h")
            contadores["bloqueio-656"] = contadores.get("bloqueio-656", 0) + 1
            _registrar_bloqueio_656(db)
            bloqueado = True
            break
        if cstat == "137" or not docs:
            break

    db.commit()

    if not bloqueado:
        set_state(db, LAST_OK_KEY, datetime.now(timezone.utc).isoformat())
        _resetar_contador_656(db)
        db.commit()

    resumo = avaliar_e_alertar(db)
    resumo["fonte"] = "sefaz"
    resumo["contadores"] = contadores
    return resumo
