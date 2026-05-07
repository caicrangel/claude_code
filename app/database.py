from sqlalchemy import create_engine, text
from sqlalchemy.orm import declarative_base, sessionmaker

from .config import settings

engine = create_engine(settings.DATABASE_URL, pool_pre_ping=True, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


_MIGRATIONS = [
    "ALTER TABLE notas_fiscais ADD COLUMN IF NOT EXISTS nsu VARCHAR(20)",
    "ALTER TABLE notas_fiscais ADD COLUMN IF NOT EXISTS is_resumo BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE notas_fiscais ADD COLUMN IF NOT EXISTS manifestada BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE notas_fiscais ADD COLUMN IF NOT EXISTS cancelada BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE notas_fiscais ADD COLUMN IF NOT EXISTS atualizado_em TIMESTAMP",
    "ALTER TABLE notas_fiscais ADD COLUMN IF NOT EXISTS natureza_operacao VARCHAR(200)",
    "ALTER TABLE notas_fiscais ADD COLUMN IF NOT EXISTS excluida_cota BOOLEAN NOT NULL DEFAULT FALSE",
    "CREATE INDEX IF NOT EXISTS ix_notas_nsu ON notas_fiscais(nsu)",
    # Consolida NSUs antigos (multi-UF) no NSU único (single-UF)
    """
    INSERT INTO state (key, value)
    SELECT 'ultimo_nsu', MAX(value::bigint)::text
    FROM state
    WHERE key LIKE 'ultimo_nsu_%'
    HAVING MAX(value::bigint) > COALESCE(
        (SELECT value::bigint FROM state WHERE key = 'ultimo_nsu'), 0
    )
    ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
    """,
    "DELETE FROM state WHERE key LIKE 'ultimo_nsu_%'",
]


def run_migrations() -> None:
    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        for sql in _MIGRATIONS:
            conn.execute(text(sql))
    _seed_periodo_inicial()


def _seed_periodo_inicial() -> None:
    """Cria o período inicial a partir do .env quando o banco está vazio."""
    from decimal import Decimal
    from .models import CotaPeriodo
    db = SessionLocal()
    try:
        if db.query(CotaPeriodo).count() > 0:
            return
        db.add(CotaPeriodo(
            nome="Período inicial",
            inicio=settings.PERIODO_INICIO,
            fim=settings.PERIODO_FIM,
            cota_litros=Decimal(str(settings.COTA_LITROS)),
            ativo=True,
        ))
        db.commit()
    finally:
        db.close()
