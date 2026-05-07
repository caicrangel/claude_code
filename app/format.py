"""Filtros Jinja2 para formatação pt-BR."""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from .config import settings

TZ = ZoneInfo(settings.TZ)


def _ptbr(num: str) -> str:
    return num.replace(",", "X").replace(".", ",").replace("X", ".")


def fmt_litros(v) -> str:
    if v is None:
        return "—"
    return _ptbr(f"{Decimal(str(v)):,.2f}") + " L"


def fmt_litros_curto(v) -> str:
    if v is None:
        return "—"
    return _ptbr(f"{Decimal(str(v)):,.0f}") + " L"


def fmt_moeda(v) -> str:
    if v is None:
        return "—"
    return "R$ " + _ptbr(f"{Decimal(str(v)):,.2f}")


def fmt_inteiro(v) -> str:
    if v is None:
        return "—"
    return _ptbr(f"{Decimal(str(v)):,.0f}")


def fmt_pct(v, casas: int = 2) -> str:
    if v is None:
        return "—"
    return _ptbr(f"{float(v):,.{casas}f}") + "%"


def to_local(dt):
    if dt is None:
        return None
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(TZ)
    return dt


def fmt_data(v, com_hora: bool = True) -> str:
    if v is None:
        return "—"
    v = to_local(v)
    if isinstance(v, datetime):
        return v.strftime("%d/%m/%Y %H:%M") if com_hora else v.strftime("%d/%m/%Y")
    if isinstance(v, date):
        return v.strftime("%d/%m/%Y")
    return str(v)


def fmt_data_curta(v) -> str:
    return fmt_data(v, com_hora=False)


def now_local() -> datetime:
    return datetime.now(TZ)


def register(env) -> None:
    env.filters["litros"] = fmt_litros
    env.filters["litros_curto"] = fmt_litros_curto
    env.filters["moeda"] = fmt_moeda
    env.filters["inteiro"] = fmt_inteiro
    env.filters["pct"] = fmt_pct
    env.filters["data"] = fmt_data
    env.filters["data_curta"] = fmt_data_curta
