from datetime import datetime

from sqlalchemy import (
    Boolean, Column, DateTime, Integer, Numeric, String, Text, UniqueConstraint,
)

from .database import Base


class NotaFiscal(Base):
    __tablename__ = "notas_fiscais"

    id = Column(Integer, primary_key=True)
    chave = Column(String(44), nullable=False, unique=True, index=True)
    nsu = Column(String(20), index=True)
    numero = Column(String(20))
    serie = Column(String(5))
    emitente_cnpj = Column(String(14))
    emitente_nome = Column(String(200))
    data_emissao = Column(DateTime)
    valor_total = Column(Numeric(14, 2), default=0)
    litros_diesel = Column(Numeric(14, 3), default=0)
    ncm = Column(String(8))
    cfop = Column(String(4))
    xml = Column(Text)
    # Status:
    is_resumo = Column(Boolean, default=False, nullable=False)     # só recebemos resNFe
    manifestada = Column(Boolean, default=False, nullable=False)   # evento 210210 OK
    cancelada = Column(Boolean, default=False, nullable=False)     # evento 110111 OK
    criado_em = Column(DateTime, default=datetime.utcnow)
    atualizado_em = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Alerta(Base):
    __tablename__ = "alertas"

    id = Column(Integer, primary_key=True)
    threshold_pct = Column(Integer, nullable=False)
    litros_consumidos = Column(Numeric(14, 3), nullable=False)
    enviado_em = Column(DateTime, default=datetime.utcnow)
    canal = Column(String(20), default="email")
    mensagem = Column(Text)
    periodo_inicio_iso = Column(String(10), nullable=False)

    __table_args__ = (
        UniqueConstraint("threshold_pct", "periodo_inicio_iso", name="uq_alerta_periodo"),
    )


class State(Base):
    """Tabela chave/valor para estado interno (ex.: ultimo_nsu)."""
    __tablename__ = "state"

    key = Column(String(50), primary_key=True)
    value = Column(String(255), nullable=False, default="")


def get_state(db, key: str, default: str = "") -> str:
    row = db.get(State, key)
    return row.value if row else default


def set_state(db, key: str, value: str) -> None:
    row = db.get(State, key)
    if row is None:
        db.add(State(key=key, value=value))
    else:
        row.value = value
    db.commit()
