"""Lógica de cota e disparo de alertas."""
from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal

from sqlalchemy import func
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Empresa, NotaFiscal, Alerta
from .email import send_email

log = logging.getLogger(__name__)


def litros_consumidos(db: Session, empresa: Empresa) -> Decimal:
    total = (
        db.query(func.coalesce(func.sum(NotaFiscal.litros_diesel), 0))
        .filter(
            NotaFiscal.empresa_id == empresa.id,
            NotaFiscal.data_emissao >= empresa.periodo_inicio,
            NotaFiscal.data_emissao <= empresa.periodo_fim,
        )
        .scalar()
    )
    return Decimal(total or 0)


def percentual(empresa: Empresa, consumo: Decimal) -> float:
    cota = Decimal(empresa.cota_litros or 0)
    if cota <= 0:
        return 0.0
    return float((consumo / cota) * 100)


def avaliar_e_alertar(db: Session, empresa: Empresa) -> dict:
    consumo = litros_consumidos(db, empresa)
    pct = percentual(empresa, consumo)
    cota = Decimal(empresa.cota_litros or 0)
    restante = cota - consumo

    periodo_iso = empresa.periodo_inicio.isoformat()
    disparados: list[int] = []
    for thr in sorted(settings.thresholds):
        if pct >= thr:
            existe = db.query(Alerta).filter(
                Alerta.empresa_id == empresa.id,
                Alerta.threshold_pct == thr,
                Alerta.periodo_inicio_iso == periodo_iso,
            ).first()
            if existe:
                continue
            msg = (
                f"Empresa: {empresa.nome} (CNPJ {empresa.cnpj})\n"
                f"Período: {empresa.periodo_inicio} a {empresa.periodo_fim}\n"
                f"Cota: {cota:.3f} L\n"
                f"Consumido: {consumo:.3f} L ({pct:.2f}%)\n"
                f"Restante: {restante:.3f} L\n\n"
                f"Limite atingido: {thr}% da cota."
            )
            ok = send_email(
                to=empresa.email_alertas or "",
                subject=f"[Cota Diesel] {empresa.nome} atingiu {thr}%",
                body=msg,
            )
            db.add(Alerta(
                empresa_id=empresa.id,
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
