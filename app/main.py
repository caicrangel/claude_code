import logging
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from .auth import check_credentials, require_login
from .config import settings
from .database import Base, engine, get_db
from .models import NotaFiscal
from .services.ingest import TooSoonError, processar
from .services.quota import litros_consumidos, percentual

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = FastAPI(title=f"Cota Diesel - {settings.EMPRESA_NOME}")
app.add_middleware(SessionMiddleware, secret_key=settings.SECRET_KEY, https_only=False)

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.on_event("startup")
def on_startup():
    Base.metadata.create_all(bind=engine)


@app.get("/", response_class=HTMLResponse)
def root(request: Request):
    if not request.session.get("auth"):
        return RedirectResponse("/login", status_code=303)
    return RedirectResponse("/dashboard", status_code=303)


@app.get("/login", response_class=HTMLResponse)
def login_get(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "erro": None})


@app.post("/login", response_class=HTMLResponse)
def login_post(request: Request, email: str = Form(...), senha: str = Form(...)):
    if not check_credentials(email, senha):
        return templates.TemplateResponse(
            "login.html", {"request": request, "erro": "Credenciais inválidas"}, status_code=401
        )
    request.session["auth"] = True
    return RedirectResponse("/dashboard", status_code=303)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/dashboard", response_class=HTMLResponse, dependencies=[Depends(require_login)])
def dashboard(request: Request, db: Session = Depends(get_db)):
    consumo = litros_consumidos(db)
    pct = percentual(consumo)
    cota = Decimal(str(settings.COTA_LITROS))
    total_notas = db.query(NotaFiscal).count()
    notas = (
        db.query(NotaFiscal)
        .order_by(NotaFiscal.data_emissao.desc().nullslast())
        .limit(200)
        .all()
    )
    status = "ok" if pct < 70 else ("warn" if pct < 95 else "crit")
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "settings": settings,
            "consumo": consumo,
            "cota": cota,
            "pct": pct,
            "restante": cota - consumo,
            "status": status,
            "notas": notas,
            "total_notas": total_notas,
            "now": datetime.utcnow(),
        },
    )


@app.post("/sync", dependencies=[Depends(require_login)])
def sync_now(db: Session = Depends(get_db)):
    try:
        return {"ok": True, **processar(db)}
    except TooSoonError as ex:
        return {"ok": False, "throttle": True, "segundos_restantes": ex.segundos_restantes,
                "erro": str(ex)}
    except Exception as ex:  # noqa: BLE001
        return {"ok": False, "erro": str(ex)}
