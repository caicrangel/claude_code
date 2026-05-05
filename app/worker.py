"""Worker APScheduler que roda o polling SEFAZ periodicamente."""
import logging
import time

from apscheduler.schedulers.blocking import BlockingScheduler

from .config import settings
from .database import SessionLocal, run_migrations
from .services.ingest import TooSoonError, processar_multiplas_ufs

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("worker")


def tick():
    db = SessionLocal()
    try:
        log.info("Tick: consultando SEFAZ p/ %s (%s) UFs=%s",
                 settings.EMPRESA_NOME, settings.cnpj_limpo, ",".join(settings.ufs_consulta))
        log.info("Resultado: %s", processar_multiplas_ufs(db))
    except TooSoonError as e:
        log.info("Tick ignorado (throttle): %s", e)
    except Exception:  # noqa: BLE001
        log.exception("Erro no tick")
    finally:
        db.close()


def main():
    run_migrations()
    sched = BlockingScheduler(timezone="UTC")
    sched.add_job(tick, "interval", seconds=settings.POLL_INTERVAL)
    log.info("Worker iniciado, intervalo=%ss", settings.POLL_INTERVAL)
    time.sleep(5)
    tick()
    sched.start()


if __name__ == "__main__":
    main()
