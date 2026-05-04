"""Worker APScheduler que roda o polling SEFAZ periodicamente."""
import logging
import time

from apscheduler.schedulers.blocking import BlockingScheduler

from .config import settings
from .database import Base, SessionLocal, engine
from .services.ingest import processar_todas

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("worker")


def tick():
    db = SessionLocal()
    try:
        log.info("Tick: consultando SEFAZ p/ todas as empresas ativas")
        resultados = processar_todas(db)
        for r in resultados:
            log.info("Resultado: %s", r)
    finally:
        db.close()


def main():
    Base.metadata.create_all(bind=engine)
    sched = BlockingScheduler(timezone="UTC")
    sched.add_job(tick, "interval", seconds=settings.POLL_INTERVAL, next_run_time=None)
    log.info("Worker iniciado, intervalo=%ss", settings.POLL_INTERVAL)
    # Primeira execução em ~30s pra dar tempo do app subir
    time.sleep(5)
    try:
        tick()
    except Exception:  # noqa: BLE001
        log.exception("Erro no tick inicial")
    sched.start()


if __name__ == "__main__":
    main()
