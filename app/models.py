from datetime import datetime, date

from sqlalchemy import (
    Column, Integer, String, Boolean, DateTime, Date, Numeric,
    ForeignKey, UniqueConstraint, Text,
)
from sqlalchemy.orm import relationship

from .database import Base


class Empresa(Base):
    __tablename__ = "empresas"

    id = Column(Integer, primary_key=True)
    nome = Column(String(200), nullable=False)
    cnpj = Column(String(14), nullable=False, unique=True, index=True)
    cota_litros = Column(Numeric(14, 3), nullable=False, default=0)
    periodo_inicio = Column(Date, nullable=False)
    periodo_fim = Column(Date, nullable=False)
    email_alertas = Column(String(200), nullable=True)
    ultimo_nsu = Column(String(20), nullable=False, default="000000000000000")
    ativo = Column(Boolean, default=True)
    criado_em = Column(DateTime, default=datetime.utcnow)

    notas = relationship("NotaFiscal", back_populates="empresa", cascade="all, delete-orphan")
    alertas = relationship("Alerta", back_populates="empresa", cascade="all, delete-orphan")
    usuarios = relationship("Usuario", back_populates="empresa")


class Usuario(Base):
    __tablename__ = "usuarios"

    id = Column(Integer, primary_key=True)
    email = Column(String(200), nullable=False, unique=True, index=True)
    senha_hash = Column(String(255), nullable=False)
    nome = Column(String(200), nullable=True)
    is_admin = Column(Boolean, default=False)
    empresa_id = Column(Integer, ForeignKey("empresas.id"), nullable=True)
    ativo = Column(Boolean, default=True)
    criado_em = Column(DateTime, default=datetime.utcnow)

    empresa = relationship("Empresa", back_populates="usuarios")


class NotaFiscal(Base):
    __tablename__ = "notas_fiscais"

    id = Column(Integer, primary_key=True)
    empresa_id = Column(Integer, ForeignKey("empresas.id"), nullable=False, index=True)
    chave = Column(String(44), nullable=False, unique=True, index=True)
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
    criado_em = Column(DateTime, default=datetime.utcnow)

    empresa = relationship("Empresa", back_populates="notas")


class Alerta(Base):
    __tablename__ = "alertas"

    id = Column(Integer, primary_key=True)
    empresa_id = Column(Integer, ForeignKey("empresas.id"), nullable=False, index=True)
    threshold_pct = Column(Integer, nullable=False)
    litros_consumidos = Column(Numeric(14, 3), nullable=False)
    enviado_em = Column(DateTime, default=datetime.utcnow)
    canal = Column(String(20), default="email")
    mensagem = Column(Text)
    periodo_inicio_iso = Column(String(10), nullable=False)

    __table_args__ = (
        UniqueConstraint("empresa_id", "threshold_pct", "periodo_inicio_iso", name="uq_alerta_periodo"),
    )

    empresa = relationship("Empresa", back_populates="alertas")
