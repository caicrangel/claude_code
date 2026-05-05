"""Polling SEFAZ → parsing → persistência → cota.

Fluxo por docZip recebido:
  - procNFe / NFe completa       → grava/atualiza NotaFiscal (litros)
  - resNFe (resumo)              → grava como pendente (is_resumo=True)
                                   se MANIFESTAR_AUTO=true: manifesta + consChNFe
  - procEventoNFe (evento)       → aplica:
        110111 (Cancelamento)    → marca cancelada=True
        210210 (Ciência)         → marca manifestada=True
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

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
BLOQUEIO_656_KEY = "bloqueio_656_ate"  # ISO timestamp; bloqueia consultas até essa hora
BLOQUEIO_656_SEGUNDOS = 3600  # 1 hora após cStat=656

_client: SefazClient | None = None


class TooSoonError(RuntimeError):
    def __init__(self, segundos_restantes: int):
        super().__init__(f"Aguarde {segundos_restantes}s antes de consultar a SEFAZ novamente")
        self.segundos_restantes = segundos_restantes


def get_client(uf: str = "SP") -> SefazClient:
    return SefazClient(
        cert_path=settings.CERT_PATH,
        cert_password=settings.CERT_PASSWORD,
        ambiente=settings.SEFAZ_AMBIENTE,
        uf=uf,
    )


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
    # Bloqueio prolongado por cStat=656 (consumo indevido)
    bloqueio_raw = get_state(db, BLOQUEIO_656_KEY, "")
    if bloqueio_raw and not force:
        try:
            bloqueio_ate = datetime.fromisoformat(bloqueio_raw)
            agora = datetime.now(timezone.utc)
            if agora < bloqueio_ate:
                restante = int((bloqueio_ate - agora).total_seconds())
                raise TooSoonError(restante)
        except ValueError:
            pass

    desde = _segundos_desde_ultima(db)
    if not force and desde is not None and desde < settings.MIN_SEFAZ_INTERVAL:
        raise TooSoonError(settings.MIN_SEFAZ_INTERVAL - desde)
    set_state(db, LAST_CALL_KEY, datetime.now(timezone.utc).isoformat())


def _registrar_bloqueio_656(db: Session) -> None:
    from datetime import timedelta
    ate = datetime.now(timezone.utc) + timedelta(seconds=BLOQUEIO_656_SEGUNDOS)
    set_state(db, BLOQUEIO_656_KEY, ate.isoformat())
    log.warning("Bloqueio cStat=656 ativado até %s (1h)", ate.isoformat())


# ---------- aplicação de cada tipo ----------

def _aplicar_nfe(db: Session, doc: DocDFe) -> str:
    parsed = parse_nfe(doc.xml)
    if parsed is None:
        return "nfe-invalida"
    nf = db.query(NotaFiscal).filter(NotaFiscal.chave == parsed.chave).first()
    if nf is None:
        if not parsed.itens_diesel:
            return "sem-itens-diesel"
        nf = NotaFiscal(chave=parsed.chave)
        db.add(nf)
    elif not nf.is_resumo and not parsed.itens_diesel:
        return "ja-completa-sem-diesel"

    nf.nsu = doc.nsu
    nf.numero = parsed.numero
    nf.serie = parsed.serie
    nf.emitente_cnpj = parsed.emit_cnpj
    nf.emitente_nome = parsed.emit_nome
    nf.data_emissao = parsed.data_emissao
    nf.valor_total = parsed.valor_total
    nf.litros_diesel = parsed.litros_diesel
    if parsed.itens_diesel:
        nf.ncm = parsed.itens_diesel[0].ncm
        nf.cfop = parsed.itens_diesel[0].cfop
    nf.xml = doc.xml.decode("utf-8", errors="replace")
    nf.is_resumo = False
    return "nfe-completa"


def _aplicar_resumo(db: Session, doc: DocDFe) -> str:
    parsed = parse_resumo(doc.xml)
    if parsed is None:
        return "resumo-invalido"
    nf = db.query(NotaFiscal).filter(NotaFiscal.chave == parsed.chave).first()
    if nf is None:
        nf = NotaFiscal(chave=parsed.chave, is_resumo=True)
        db.add(nf)
    elif not nf.is_resumo:
        return "ja-completa"
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
        # evento de NFe que ainda não temos — ignora; se relevante virá depois
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
    # tenta deduzir pelo conteúdo
    if b"<resNFe" in doc.xml:
        return _aplicar_resumo(db, doc)
    if b"infNFe" in doc.xml:
        return _aplicar_nfe(db, doc)
    if b"infEvento" in doc.xml:
        return _aplicar_evento(db, doc)
    return f"schema-desconhecido:{schema}"


# ---------- manifestação automática ----------

def _manifestar_pendentes(db: Session, client: SefazClient) -> dict:
    """Para cada nota só com resumo (e não manifestada), envia 210210
    e em seguida consChNFe pra puxar a NFe completa."""
    pendentes = (
        db.query(NotaFiscal)
        .filter(NotaFiscal.is_resumo.is_(True),
                NotaFiscal.manifestada.is_(False),
                NotaFiscal.cancelada.is_(False))
        .limit(50)
        .all()
    )
    out = {"tentadas": 0, "ok": 0, "completas": 0, "falhas": 0}
    for nf in pendentes:
        out["tentadas"] += 1
        try:
            ret = manifestar_ciencia(client, nf.chave)
        except Exception:  # noqa: BLE001
            log.exception("Falha manifestando %s", nf.chave)
            out["falhas"] += 1
            continue
        if not ret["ok"]:
            out["falhas"] += 1
            continue
        nf.manifestada = True
        out["ok"] += 1
        # tenta puxar NFe completa imediatamente
        try:
            _, _, docs = client.consultar_chave(settings.cnpj_limpo, nf.chave)
            for d in docs:
                if _aplicar(db, d) == "nfe-completa":
                    out["completas"] += 1
        except Exception:  # noqa: BLE001
            log.exception("Falha puxando NFe completa após manifestação %s", nf.chave)
        db.commit()
    return out


# ---------- entrada principal ----------

def processar(db: Session, *, uf: str = "SP", force: bool = False,
              _skip_throttle: bool = False) -> dict:
    if not settings.cnpj_limpo:
        raise RuntimeError("EMPRESA_CNPJ não configurado no .env")
    if not _skip_throttle:
        _check_throttle(db, force)
    client = get_client(uf)
    cnpj = settings.cnpj_limpo
    nsu_key = f"ultimo_nsu_{uf}"
    ult_nsu = get_state(db, nsu_key, "0")

    contadores: dict[str, int] = {}
    for _ in range(20):
        try:
            cstat, ult_recebido, docs = client.consultar_nsu(cnpj, ult_nsu)
        except Exception as e:  # noqa: BLE001
            log.exception("Erro consultando SEFAZ %s: %s", uf, e)
            break
        log.info("SEFAZ %s cStat=%s ultNSU=%s docs=%d", uf, cstat, ult_recebido, len(docs))
        for doc in docs:
            r = _aplicar(db, doc)
            contadores[r] = contadores.get(r, 0) + 1
        if ult_recebido and ult_recebido != ult_nsu:
            ult_nsu = ult_recebido
            set_state(db, nsu_key, ult_nsu)
        if cstat == "656":
            log.warning("SEFAZ %s cStat=656 (consumo indevido) - bloqueando 1h", uf)
            contadores["consumo-indevido-656"] = contadores.get("consumo-indevido-656", 0) + 1
            _registrar_bloqueio_656(db)
            db.commit()
            break
        if cstat == "137" or not docs:
            break
    db.commit()

    manifestacao = None
    if settings.MANIFESTAR_AUTO:
        manifestacao = _manifestar_pendentes(db, client)

    resumo = avaliar_e_alertar(db)
    resumo["fonte"] = "sefaz"
    resumo["uf"] = uf
    resumo["contadores"] = contadores
    if manifestacao is not None:
        resumo["manifestacao"] = manifestacao
    return resumo


def processar_multiplas_ufs(db: Session, *, force: bool = False) -> dict:
    """Consulta múltiplas UFs configuradas e retorna resumo combinado.

    O throttle interno é aplicado UMA vez por ciclo (na primeira UF);
    entre UFs damos um pequeno gap para não disparar cStat=656 da SEFAZ.
    """
    resultado_final = {
        "fonte": "sefaz",
        "ufs_consultadas": [],
        "contadores": {},
        "total_alertas": 0,
    }

    _check_throttle(db, force)
    ufs = settings.ufs_consulta

    for i, uf in enumerate(ufs):
        if i > 0:
            time.sleep(2)
        try:
            resultado = processar(db, uf=uf, force=True, _skip_throttle=True)
            resultado_final["ufs_consultadas"].append(uf)
            for k, v in resultado.get("contadores", {}).items():
                resultado_final["contadores"][f"{uf}_{k}"] = v
        except Exception:  # noqa: BLE001
            log.exception("Erro processando UF %s", uf)

    resumo = avaliar_e_alertar(db)
    resultado_final["total_alertas"] = resumo.get("alertas_disparados", 0)
    return resultado_final


def manifestar_chave(db: Session, chave: str) -> dict:
    """Disparo manual de manifestação para uma chave específica."""
    uf_principal = settings.ufs_consulta[0] if settings.ufs_consulta else "SP"
    client = get_client(uf_principal)
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
