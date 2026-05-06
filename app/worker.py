"""Worker APScheduler que roda o polling SEFAZ em intervalo fixo.

Quando há bloqueio cStat=656 ativo, o tick é silenciosamente ignorado
(ver TooSoonError em ingest._check_throttle). O bloqueio usa backoff
exponencial para evitar renovar a cada hora.
"""
import logging
import time

from apscheduler.schedulers.blocking import BlockingScheduler

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
    sched.add_job(tick, "interval", hours=settings.SYNC_INTERVALO_HORAS)
    log.info("Worker iniciado — sincronização a cada %dh (%s)",
             settings.SYNC_INTERVALO_HORAS, settings.TZ)
    time.sleep(5)
    tick()
    sched.start()


if __name__ == "__main__":
    main()
