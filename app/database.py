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
    "CREATE INDEX IF NOT EXISTS ix_notas_nsu ON notas_fiscais(nsu)",
]


def run_migrations() -> None:
    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        for sql in _MIGRATIONS:
            conn.execute(text(sql))
