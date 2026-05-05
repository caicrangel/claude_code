"""Polling SEFAZ → parsing → persistência → cota.

Arquitetura:
  - NFeDistribuicaoDFe é NACIONAL: uma única consulta retorna todas as NFe
    emitidas contra o CNPJ do consultante, de qualquer UF emitente.
  - SEFAZ_UF é apenas a UF do consultante (cUFAutor) para roteamento interno.

Fluxo por docZip recebido:
  - procNFe / NFe completa  → _aplicar_nfe()
        com itens diesel    → grava/atualiza NotaFiscal
        sem itens diesel    → ignora (ou deleta resumo prévio)
  - resNFe (resumo)         → _aplicar_resumo()
        grava como pendente; só sabemos se é diesel após manifestar.
        Se MANIFESTAR_AUTO=true: manifesta + consChNFe na sequência.
        Se a NFe completa retornar sem diesel, o resumo é DELETADO.
  - procEventoNFe           → _aplicar_evento()
        110111 (Cancelamento) → marca cancelada=True
        210210 (Ciência)      → marca manifestada=True
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from ..config import settings
from ..models import NotaFiscal, get_state, set_state
from .eventos import manifestar_ciencia
from .parser import parse_evento, parse_nfe, parse_resumo
from .quota import avaliar_e_alertar
from .sefaz import DocDFe, SefazClient

log = logging.getLogger(__name__)

NSU_KEY = "ultimo_nsu"
LAST_CALL_KEY = "ultima_consulta_em"
LAST_OK_KEY = "ultima_consulta_ok_em"  # última consulta bem-sucedida (para UI)
BLOQUEIO_656_KEY = "bloqueio_656_ate"
BLOQUEIO_656_SEGUNDOS = 3600  # 1h após cStat=656


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
    ate = datetime.now(timezone.utc) + timedelta(seconds=BLOQUEIO_656_SEGUNDOS)
    set_state(db, BLOQUEIO_656_KEY, ate.isoformat())
    log.warning("Bloqueio cStat=656 ativado até %s (1h)", ate.isoformat())


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
    """Processa NFe completa. Se a nota não tem diesel:
    - se já existe resumo no banco para essa chave → deleta (era falso-positivo)
    - se não existe → ignora (não polui banco)
    """
    parsed = parse_nfe(doc.xml)
    if parsed is None:
        return "nfe-invalida"

    nf = db.query(NotaFiscal).filter(NotaFiscal.chave == parsed.chave).first()
    tem_diesel = bool(parsed.itens_diesel)

    if not tem_diesel:
        if nf is not None and nf.is_resumo:
            db.delete(nf)
            log.info("Removido resumo %s (NFe completa sem diesel)", parsed.chave)
            return "resumo-descartado-sem-diesel"
        return "ignorado-sem-diesel"

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
    return "nfe-completa"


def _aplicar_resumo(db: Session, doc: DocDFe) -> str:
    """Resumos não têm itens — não dá para saber se é diesel.
    Armazenamos para manifestação posterior; será deletado se não for diesel.
    """
    parsed = parse_resumo(doc.xml)
    if parsed is None:
        return "resumo-invalido"

    nf = db.query(NotaFiscal).filter(NotaFiscal.chave == parsed.chave).first()
    if nf is None:
        nf = NotaFiscal(chave=parsed.chave, is_resumo=True)
        db.add(nf)
    elif not nf.is_resumo:
        return "resumo-ja-completo"

    nf.nsu = doc.nsu
    nf.numero = parsed.numero
    nf.serie = parsed.serie
    nf.emitente_cnpj = parsed.emit_cnpj
    nf.emitente_nome = parsed.emit_nome
    nf.data_emissao = parsed.data_emissao
    nf.valor_total = parsed.valor_total
    return "resumo"


def _aplicar_evento(db: Session, doc: DocDFe) -> str:
    parsed = parse_evento(doc.xml)
    if parsed is None or not parsed.chave:
        return "evento-invalido"
    nf = db.query(NotaFiscal).filter(NotaFiscal.chave == parsed.chave).first()
    if nf is None:
        return "evento-sem-nf"
    if parsed.is_cancelamento:
        nf.cancelada = True
        return "cancelamento"
    if parsed.tp_evento == "210210":
        nf.manifestada = True
        return "manifestacao-confirmada"
    return f"evento-{parsed.tp_evento}"


def _aplicar(db: Session, doc: DocDFe) -> str:
    schema = (doc.schema or "").lower()
    if schema.startswith("procnfe") or schema.startswith("nfe"):
        return _aplicar_nfe(db, doc)
    if schema.startswith("resnfe"):
        return _aplicar_resumo(db, doc)
    if schema.startswith("proceventonfe") or schema.startswith("evento"):
        return _aplicar_evento(db, doc)
    # fallback por inspeção do conteúdo
    if b"<resNFe" in doc.xml:
        return _aplicar_resumo(db, doc)
    if b"infNFe" in doc.xml:
        return _aplicar_nfe(db, doc)
    if b"infEvento" in doc.xml:
        return _aplicar_evento(db, doc)
    return f"schema-desconhecido:{schema}"


# ---------- manifestação automática ----------

def _manifestar_pendentes(db: Session, client: SefazClient) -> dict:
    """Para cada resumo pendente: envia evento 210210 + consChNFe.
    Se a NFe completa não tem diesel, _aplicar_nfe deleta o resumo automaticamente.
    """
    pendentes = (
        db.query(NotaFiscal)
        .filter(NotaFiscal.is_resumo.is_(True),
                NotaFiscal.manifestada.is_(False),
                NotaFiscal.cancelada.is_(False))
        .limit(50)
        .all()
    )
    out = {"tentadas": 0, "ok": 0, "completas": 0, "descartadas": 0, "falhas": 0}
    for nf in pendentes:
        out["tentadas"] += 1
        chave = nf.chave
        try:
            ret = manifestar_ciencia(client, chave)
        except Exception:  # noqa: BLE001
            log.exception("Falha manifestando %s", chave)
            out["falhas"] += 1
            continue
        if not ret["ok"]:
            out["falhas"] += 1
            continue
        nf.manifestada = True
        out["ok"] += 1
        try:
            _, _, docs = client.consultar_chave(settings.cnpj_limpo, chave)
            for d in docs:
                resultado = _aplicar(db, d)
                if resultado == "nfe-completa":
                    out["completas"] += 1
                elif resultado == "resumo-descartado-sem-diesel":
                    out["descartadas"] += 1
        except Exception:  # noqa: BLE001
            log.exception("Falha puxando NFe completa após manifestação %s", chave)
        db.commit()
    return out


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

    manifestacao = None
    if settings.MANIFESTAR_AUTO and not bloqueado:
        try:
            manifestacao = _manifestar_pendentes(db, client)
        except Exception:  # noqa: BLE001
            log.exception("Erro na manifestação automática")

    if not bloqueado:
        set_state(db, LAST_OK_KEY, datetime.now(timezone.utc).isoformat())
        db.commit()

    resumo = avaliar_e_alertar(db)
    resumo["fonte"] = "sefaz"
    resumo["contadores"] = contadores
    if manifestacao is not None:
        resumo["manifestacao"] = manifestacao
    return resumo


def manifestar_chave(db: Session, chave: str) -> dict:
    """Disparo manual de manifestação para uma chave específica."""
    client = get_client()
    nf = db.query(NotaFiscal).filter(NotaFiscal.chave == chave).first()
    ret = manifestar_ciencia(client, chave)
    if ret["ok"] and nf is not None:
        nf.manifestada = True
        try:
            _, _, docs = client.consultar_chave(settings.cnpj_limpo, chave)
            for d in docs:
                _aplicar(db, d)
        except Exception:  # noqa: BLE001
            log.exception("Falha em consChNFe após manifestação")
        db.commit()
    return ret
