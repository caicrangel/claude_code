"""Cota e disparo de alertas."""
from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal

from sqlalchemy import func
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Alerta, CotaPeriodo, NotaFiscal
from .email import send_email
from .periodo import get_periodo_ativo

log = logging.getLogger(__name__)


def litros_consumidos(db: Session, *, inicio: date | None = None,
                      fim: date | None = None) -> Decimal:
    if inicio is None or fim is None:
        p = get_periodo_ativo(db)
        if p is None:
            return Decimal(0)
        inicio, fim = p.inicio, p.fim
    total = (
        db.query(func.coalesce(func.sum(NotaFiscal.litros_diesel), 0))
        .filter(
            NotaFiscal.data_emissao >= inicio,
            NotaFiscal.data_emissao <= fim,
            NotaFiscal.is_resumo.is_(False),
            NotaFiscal.cancelada.is_(False),
            NotaFiscal.excluida_cota.is_(False),
        )
        .scalar()
    )
    return Decimal(total or 0)


def percentual(consumo: Decimal, cota: Decimal) -> float:
    if cota <= 0:
        return 0.0
    return float((consumo / cota) * 100)


def avaliar_e_alertar(db: Session) -> dict:
    p = get_periodo_ativo(db)
    if p is None:
        return {"consumo": 0.0, "cota": 0.0, "pct": 0.0, "restante": 0.0,
                "alertas_disparados": []}
    consumo = litros_consumidos(db, inicio=p.inicio, fim=p.fim)
    cota = Decimal(p.cota_litros or 0)
    pct = percentual(consumo, cota)
    restante = cota - consumo
    periodo_iso = p.inicio.isoformat()

    disparados: list[int] = []
    for thr in sorted(settings.thresholds):
        if pct < thr:
            continue
        existe = db.query(Alerta).filter(
            Alerta.threshold_pct == thr,
            Alerta.periodo_inicio_iso == periodo_iso,
        ).first()
        if existe:
            continue
        msg = (
            f"Empresa: {settings.EMPRESA_NOME} (CNPJ {settings.cnpj_limpo})\n"
            f"Período: {p.inicio} a {p.fim}\n"
            f"Cota: {cota:.3f} L\n"
            f"Consumido: {consumo:.3f} L ({pct:.2f}%)\n"
            f"Restante: {restante:.3f} L\n\n"
            f"Limite atingido: {thr}% da cota."
        )
        ok = send_email(
            to=settings.EMAIL_ALERTAS,
            subject=f"[Cota Diesel] {settings.EMPRESA_NOME} atingiu {thr}%",
            body=msg,
        )
        db.add(Alerta(
            threshold_pct=thr,
            litros_consumidos=consumo,
            canal="email" if ok else "log",
            mensagem=msg,
            periodo_inicio_iso=periodo_iso,
        ))
        disparados.append(thr)
    if disparados:
        db.commit()
    return {
        "consumo": float(consumo),
        "cota": float(cota),
        "pct": pct,
        "restante": float(restante),
        "alertas_disparados": disparados,
    }
