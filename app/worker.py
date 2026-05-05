"""Worker APScheduler que roda o polling SEFAZ uma vez ao dia em horário fixo."""
import logging
import time

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from .config import settings
from .database import SessionLocal, run_migrations
from .services.ingest import TooSoonError, processar

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("worker")


def tick():
    db = SessionLocal()
    try:
        log.info("Tick: consultando SEFAZ p/ %s (%s) UF=%s",
                 settings.EMPRESA_NOME, settings.cnpj_limpo, settings.SEFAZ_UF)
        log.info("Resultado: %s", processar(db))
    except TooSoonError as e:
        log.info("Tick ignorado (throttle/bloqueio): %s", e)
    except Exception:  # noqa: BLE001
        log.exception("Erro no tick")
    finally:
        db.close()


def main():
    run_migrations()
    sched = BlockingScheduler(timezone=settings.TZ)
    trigger = CronTrigger(hour=settings.SYNC_HORA, minute=0, timezone=settings.TZ)
    sched.add_job(tick, trigger)
    log.info("Worker iniciado — sincronização diária às %02d:00 (%s)",
             settings.SYNC_HORA, settings.TZ)
    time.sleep(5)
    tick()   # roda uma vez imediatamente ao subir o container
    sched.start()


if __name__ == "__main__":
    main()
