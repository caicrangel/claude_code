"""Gestão de períodos de cota."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from ..models import CotaPeriodo


def get_periodo_ativo(db: Session) -> CotaPeriodo | None:
    return db.query(CotaPeriodo).filter(CotaPeriodo.ativo.is_(True)).first()


def get_periodo(db: Session, periodo_id: int) -> CotaPeriodo | None:
    return db.get(CotaPeriodo, periodo_id)


def listar(db: Session) -> list[CotaPeriodo]:
    return db.query(CotaPeriodo).order_by(CotaPeriodo.inicio.desc()).all()


def novo_periodo(db: Session, *, nome: str, inicio: date, fim: date,
                 cota_litros: Decimal) -> CotaPeriodo:
    """Encerra o período ativo (se houver) e cria um novo já ativo.
    As NFs já gravadas continuam no banco — só mudam de "período corrente"."""
    db.query(CotaPeriodo).filter(CotaPeriodo.ativo.is_(True)).update({"ativo": False})
    novo = CotaPeriodo(
        nome=nome, inicio=inicio, fim=fim,
        cota_litros=cota_litros, ativo=True,
    )
    db.add(novo)
    db.commit()
    db.refresh(novo)
    return novo


def ativar(db: Session, periodo_id: int) -> CotaPeriodo | None:
    p = get_periodo(db, periodo_id)
    if p is None:
        return None
    db.query(CotaPeriodo).filter(CotaPeriodo.ativo.is_(True)).update({"ativo": False})
    p.ativo = True
    db.commit()
    db.refresh(p)
    return p


class PeriodoAtivoError(RuntimeError):
    """Levantada ao tentar excluir o período ativo."""


def excluir(db: Session, periodo_id: int) -> bool:
    """Exclui um período arquivado (criado por engano, por exemplo).

    NÃO apaga NFs — elas não têm vínculo direto com períodos (são
    filtradas por data). Recusa excluir o período ativo. Retorna True se
    excluiu, False se o período não existe."""
    p = get_periodo(db, periodo_id)
    if p is None:
        return False
    if p.ativo:
        raise PeriodoAtivoError(
            "Não é possível excluir o período ativo. Ative outro antes.")
    db.delete(p)
    db.commit()
    return True
