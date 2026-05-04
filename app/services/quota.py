"""Lógica de cota e disparo de alertas (single-tenant)."""
from __future__ import annotations

import logging
from decimal import Decimal

from sqlalchemy import func
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Alerta, NotaFiscal
from .email import send_email

log = logging.getLogger(__name__)


def litros_consumidos(db: Session) -> Decimal:
    total = (
        db.query(func.coalesce(func.sum(NotaFiscal.litros_diesel), 0))
        .filter(
            NotaFiscal.data_emissao >= settings.PERIODO_INICIO,
            NotaFiscal.data_emissao <= settings.PERIODO_FIM,
            NotaFiscal.is_resumo.is_(False),
            NotaFiscal.cancelada.is_(False),
        )
        .scalar()
    )
    return Decimal(total or 0)


def percentual(consumo: Decimal) -> float:
    cota = Decimal(str(settings.COTA_LITROS))
    if cota <= 0:
        return 0.0
    return float((consumo / cota) * 100)


def avaliar_e_alertar(db: Session) -> dict:
    consumo = litros_consumidos(db)
    cota = Decimal(str(settings.COTA_LITROS))
    pct = percentual(consumo)
    restante = cota - consumo
    periodo_iso = settings.PERIODO_INICIO.isoformat()

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
            f"Período: {settings.PERIODO_INICIO} a {settings.PERIODO_FIM}\n"
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
