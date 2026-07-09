from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
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
    "ALTER TABLE notas_fiscais ADD COLUMN IF NOT EXISTS periodo_id INTEGER",
    "CREATE INDEX IF NOT EXISTS ix_notas_nsu ON notas_fiscais(nsu)",
    "CREATE INDEX IF NOT EXISTS ix_notas_periodo_id ON notas_fiscais(periodo_id)",
    # Modelo aditivo de inclusão em período (muitos-para-muitos).
    """
    CREATE TABLE IF NOT EXISTS nota_periodo (
        nota_id INTEGER NOT NULL,
        periodo_id INTEGER NOT NULL,
        PRIMARY KEY (nota_id, periodo_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_nota_periodo_periodo ON nota_periodo(periodo_id)",
    # Migra o antigo pin exclusivo (notas_fiscais.periodo_id) para inclusão.
    """
    INSERT INTO nota_periodo (nota_id, periodo_id)
    SELECT id, periodo_id FROM notas_fiscais WHERE periodo_id IS NOT NULL
    ON CONFLICT DO NOTHING
    """,
    # Zera o pin antigo para não recriar a inclusão em toda subida.
    "UPDATE notas_fiscais SET periodo_id = NULL WHERE periodo_id IS NOT NULL",
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
    _seed_admin_inicial()
    _seed_email_alerta_inicial()


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
    except IntegrityError:
        # Outro processo (app/worker) já semeou ao subir junto — ok.
        db.rollback()
    finally:
        db.close()


def _seed_admin_inicial() -> None:
    """Quando a tabela usuarios está vazia, cria um admin a partir do .env
    (AUTH_EMAIL + AUTH_PASSWORD). Permite primeiro acesso sem comando manual.
    Após o primeiro login o admin pode gerenciar usuários pela UI."""
    from .auth import hash_senha
    from .models import Usuario
    db = SessionLocal()
    try:
        if db.query(Usuario).count() > 0:
            return
        email = (settings.AUTH_EMAIL or "").strip().lower()
        senha = settings.AUTH_PASSWORD or ""
        if not email or not senha:
            return
        db.add(Usuario(
            email=email,
            senha_hash=hash_senha(senha),
            nome="Administrador",
            role="admin",
            ativo=True,
        ))
        db.commit()
    except IntegrityError:
        # Outro processo (app/worker) já criou o admin ao subir junto — ok.
        db.rollback()
    finally:
        db.close()


def _seed_email_alerta_inicial() -> None:
    """Migra o EMAIL_ALERTAS do .env para a tabela na primeira execução."""
    from .models import EmailAlerta
    db = SessionLocal()
    try:
        if db.query(EmailAlerta).count() > 0:
            return
        email = (settings.EMAIL_ALERTAS or "").strip()
        if not email:
            return
        db.add(EmailAlerta(email=email, nome="(do .env)", ativo=True))
        db.commit()
    except IntegrityError:
        # Outro processo (app/worker) já migrou o e-mail ao subir junto — ok.
        db.rollback()
    finally:
        db.close()
