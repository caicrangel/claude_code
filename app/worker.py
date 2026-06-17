"""Worker APScheduler que roda o polling SEFAZ em intervalo fixo.

Quando há bloqueio cStat=656 ativo, o tick é silenciosamente ignorado
(ver TooSoonError em ingest._check_throttle). O bloqueio usa backoff
exponencial para evitar renovar a cada hora.

Também roda o panorama mensal: dia 1 de cada mês, 08h (TZ do app), envia
e-mail de fechamento do mês anterior. Dedup via state (não reenvia se já
foi enviado naquele mês), então é seguro mesmo se o worker reiniciar.

NOTA — tick imediato no startup:
  Por padrão consultamos a SEFAZ ao subir, MAS só se a última consulta OK
  foi há mais do que SYNC_INTERVALO_HORAS. Isso evita que reinicios
  frequentes do container (rebuilds, restart de serviço) gerem rajadas de
  consultas que disparam o cStat=656 (consumo indevido).
"""
import logging
import time
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.blocking import BlockingScheduler

from . import runtime_config
from .config import settings
from .database import SessionLocal, run_migrations
from .services.ingest import LAST_OK_KEY, TooSoonError, processar
from .services.notify import enviar_panorama_mensal
from .models import get_state

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("worker")


def tick():
    db = SessionLocal()
    try:
        log.info("Tick: consultando SEFAZ p/ %s (%s) UF=%s",
                 runtime_config.empresa_nome(db), runtime_config.cnpj_limpo(db),
                 settings.SEFAZ_UF)
        log.info("Resultado: %s", processar(db))
    except TooSoonError as e:
        log.info("Tick ignorado (throttle/bloqueio): %s", e)
    except Exception:  # noqa: BLE001
        log.exception("Erro no tick")
    finally:
        db.close()


def tick_panorama_mensal():
    """Dispara o panorama mensal. Idempotente — dedup interna por mês."""
    db = SessionLocal()
    try:
        r = enviar_panorama_mensal(db)
        log.info("Panorama mensal: %s", r)
    except Exception:  # noqa: BLE001
        log.exception("Erro no tick do panorama mensal")
    finally:
        db.close()


def _deve_tickar_no_startup() -> bool:
    """Só consulta no startup se a última consulta OK foi há mais do que o
    intervalo configurado. Evita rajadas após rebuild/restart do container."""
    db = SessionLocal()
    try:
        raw = get_state(db, LAST_OK_KEY, "")
        if not raw:
            return True  # nunca consultou — primeira execução
        try:
            ultima = datetime.fromisoformat(raw)
        except ValueError:
            return True
        decorrido = datetime.now(timezone.utc) - ultima
        limite = timedelta(hours=settings.SYNC_INTERVALO_HORAS)
        if decorrido >= limite:
            return True
        restante = limite - decorrido
        mins = int(restante.total_seconds() // 60)
        log.info(
            "Tick de startup ignorado — última consulta SEFAZ foi há %s "
            "(próximo tick em ~%d min, pelo cron)",
            decorrido, mins,
        )
        return False
    finally:
        db.close()


def main():
    run_migrations()
    sched = BlockingScheduler(timezone=settings.TZ)
    sched.add_job(tick, "interval", hours=settings.SYNC_INTERVALO_HORAS)
    sched.add_job(tick_panorama_mensal, "cron", day=1, hour=8, minute=0)
    log.info("Worker iniciado — sincronização a cada %dh + panorama dia 1 às 08h (%s)",
             settings.SYNC_INTERVALO_HORAS, settings.TZ)
    time.sleep(5)
    if _deve_tickar_no_startup():
        tick()
    sched.start()


if __name__ == "__main__":
    main()
