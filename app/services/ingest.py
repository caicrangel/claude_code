"""Orquestra polling SEFAZ → parsing → persistência → cota."""
from __future__ import annotations

import logging
from datetime import date

from sqlalchemy.orm import Session

from ..config import settings
from ..models import Empresa, NotaFiscal
from .parser import parse_nfe
from .quota import avaliar_e_alertar
from .sefaz import SefazClient

log = logging.getLogger(__name__)

_client: SefazClient | None = None


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


def processar_empresa(db: Session, empresa: Empresa) -> dict:
    """Consulta SEFAZ até esgotar lotes, persiste NFe novas e avalia cota."""
    client = get_client()
    novas = 0
    iter_count = 0
    ult_nsu = empresa.ultimo_nsu or "0"

    while iter_count < 20:  # SEFAZ devolve em lotes; iteramos até cStat=137 (nada mais)
        iter_count += 1
        try:
            cstat, ult_recebido, docs = client.consultar(empresa.cnpj, ult_nsu)
        except Exception as e:  # noqa: BLE001
            log.exception("Erro consultando SEFAZ p/ %s: %s", empresa.cnpj, e)
            break
        log.info("SEFAZ %s cStat=%s ultNSU=%s docs=%d", empresa.cnpj, cstat, ult_recebido, len(docs))

        for doc in docs:
            if not doc.chave:
                continue
            existe = db.query(NotaFiscal).filter(NotaFiscal.chave == doc.chave).first()
            if existe:
                continue
            parsed = parse_nfe(doc.xml)
            if parsed is None:
                # docZip pode ser resumo (resNFe/resEvento) - ignoramos ou poderíamos
                # acionar consulta completa por chave (consNFe). Para MVP, só persiste
                # quando temos NFe completa.
                continue
            if not parsed.itens_diesel:
                continue
            nf = NotaFiscal(
                empresa_id=empresa.id,
                chave=parsed.chave,
                numero=parsed.numero,
                serie=parsed.serie,
                emitente_cnpj=parsed.emit_cnpj,
                emitente_nome=parsed.emit_nome,
                data_emissao=parsed.data_emissao,
                valor_total=parsed.valor_total,
                litros_diesel=parsed.litros_diesel,
                ncm=parsed.itens_diesel[0].ncm if parsed.itens_diesel else "",
                cfop=parsed.itens_diesel[0].cfop if parsed.itens_diesel else "",
                xml=doc.xml.decode("utf-8", errors="replace"),
            )
            db.add(nf)
            novas += 1

        if ult_recebido and ult_recebido != ult_nsu:
            ult_nsu = ult_recebido
            empresa.ultimo_nsu = ult_nsu
            db.commit()

        # cStat 137 = nada mais a retornar; 138 = lote ok mas pode ter mais
        if cstat == "137" or not docs:
            break

    if novas:
        db.commit()
    resumo = avaliar_e_alertar(db, empresa)
    resumo["novas"] = novas
    return resumo


def processar_todas(db: Session) -> list[dict]:
    out = []
    for emp in db.query(Empresa).filter(Empresa.ativo.is_(True)).all():
        try:
            r = processar_empresa(db, emp)
            r["empresa"] = emp.nome
            out.append(r)
        except Exception as e:  # noqa: BLE001
            log.exception("Falha processando empresa %s: %s", emp.cnpj, e)
    return out
