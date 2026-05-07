from datetime import date, datetime

from sqlalchemy import (
    Boolean, Column, Date, DateTime, Integer, Numeric, String, Text, UniqueConstraint,
)

from .database import Base


class CotaPeriodo(Base):
    """Histórico de períodos de cota. Apenas um pode estar ativo (ativo=True).
    Mudar de período não apaga as NFs — todas ficam no banco e podem ser
    consultadas filtrando por data nos relatórios."""
    __tablename__ = "cota_periodos"

    id = Column(Integer, primary_key=True)
    nome = Column(String(120))
    inicio = Column(Date, nullable=False)
    fim = Column(Date, nullable=False)
    cota_litros = Column(Numeric(14, 3), nullable=False)
    ativo = Column(Boolean, default=False, nullable=False, index=True)
    criado_em = Column(DateTime, default=datetime.utcnow)
    atualizado_em = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


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
    natureza_operacao = Column(String(200))
    xml = Column(Text)
    # Status:
    is_resumo = Column(Boolean, default=False, nullable=False)
    cancelada = Column(Boolean, default=False, nullable=False)
    # Se True, a NF não conta no consumo (ex.: diferença de preço, devolução,
    # bonificação). Pode ser definido automaticamente pela natureza_operacao
    # ou alternado manualmente pelo usuário.
    excluida_cota = Column(Boolean, default=False, nullable=False)
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
