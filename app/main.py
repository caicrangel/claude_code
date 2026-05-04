import logging
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from . import format as fmt
from .auth import check_credentials, require_login
from .config import settings
from .database import get_db, run_migrations
from .models import NotaFiscal
from .services.ingest import TooSoonError, manifestar_chave, processar
from .services.quota import litros_consumidos, percentual

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = FastAPI(title=f"Cota Diesel - {settings.EMPRESA_NOME}")
app.add_middleware(SessionMiddleware, secret_key=settings.SECRET_KEY, https_only=False)

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
fmt.register(templates.env)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.on_event("startup")
def on_startup():
    run_migrations()


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


def _kpis(db: Session) -> dict:
    consumo = litros_consumidos(db)
    cota = Decimal(str(settings.COTA_LITROS))
    pct = percentual(consumo)
    restante = cota - consumo
    status = "ok" if pct < 70 else ("warn" if pct < 95 else "crit")
    pendentes = db.query(NotaFiscal).filter(
        NotaFiscal.is_resumo.is_(True),
        NotaFiscal.cancelada.is_(False),
    ).count()
    canceladas = db.query(NotaFiscal).filter(NotaFiscal.cancelada.is_(True)).count()
    return {"consumo": consumo, "cota": cota, "pct": pct, "restante": restante,
            "status": status, "pendentes": pendentes, "canceladas": canceladas}


@app.get("/dashboard", response_class=HTMLResponse, dependencies=[Depends(require_login)])
def dashboard(request: Request, db: Session = Depends(get_db)):
    k = _kpis(db)
    total_notas = db.query(NotaFiscal).count()
    notas = (
        db.query(NotaFiscal)
        .order_by(NotaFiscal.data_emissao.desc().nullslast())
        .limit(200)
        .all()
    )
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request, "settings": settings, **k,
            "notas": notas, "total_notas": total_notas, "now": fmt.now_local(),
        },
    )


@app.get("/relatorios", response_class=HTMLResponse, dependencies=[Depends(require_login)])
def relatorios(request: Request, db: Session = Depends(get_db)):
    k = _kpis(db)
    ini, fim = settings.PERIODO_INICIO, settings.PERIODO_FIM
    base = db.query(NotaFiscal).filter(
        NotaFiscal.data_emissao >= ini, NotaFiscal.data_emissao <= fim,
    )

    fornecedores = (
        base.with_entities(
            NotaFiscal.emitente_cnpj,
            NotaFiscal.emitente_nome,
            func.sum(NotaFiscal.litros_diesel).label("litros"),
            func.sum(NotaFiscal.valor_total).label("valor"),
            func.count(NotaFiscal.id).label("qtd"),
        )
        .group_by(NotaFiscal.emitente_cnpj, NotaFiscal.emitente_nome)
        .order_by(func.sum(NotaFiscal.litros_diesel).desc())
        .all()
    )

    mensal = (
        base.with_entities(
            func.date_trunc("month", NotaFiscal.data_emissao).label("mes"),
            func.sum(NotaFiscal.litros_diesel).label("litros"),
            func.sum(NotaFiscal.valor_total).label("valor"),
            func.count(NotaFiscal.id).label("qtd"),
        )
        .group_by("mes").order_by("mes").all()
    )

    por_ncm = (
        base.with_entities(
            NotaFiscal.ncm,
            func.sum(NotaFiscal.litros_diesel).label("litros"),
            func.count(NotaFiscal.id).label("qtd"),
        )
        .group_by(NotaFiscal.ncm).order_by(func.sum(NotaFiscal.litros_diesel).desc()).all()
    )

    valor_total = sum((Decimal(f.valor or 0) for f in fornecedores), Decimal(0))
    qtd_total = sum((f.qtd for f in fornecedores), 0)
    ticket_medio = (valor_total / qtd_total) if qtd_total else Decimal(0)
    preco_medio_litro = (valor_total / k["consumo"]) if k["consumo"] else Decimal(0)

    # Projeção
    hoje = date.today()
    dias_decorridos = max(1, (min(hoje, fim) - ini).days + 1)
    dias_periodo = (fim - ini).days + 1
    taxa_diaria = float(k["consumo"]) / dias_decorridos
    projecao_total = taxa_diaria * dias_periodo
    projecao_pct = (projecao_total / float(k["cota"]) * 100) if k["cota"] else 0
    if taxa_diaria > 0 and k["restante"] > 0:
        dias_ate_estourar = float(k["restante"]) / taxa_diaria
        data_estouro = hoje.fromordinal(hoje.toordinal() + int(dias_ate_estourar))
    else:
        dias_ate_estourar = None
        data_estouro = None
    projecao_status = "ok" if projecao_total <= float(k["cota"]) else "crit"

    fornecedores_chart = {
        "labels": [f.emitente_nome or f.emitente_cnpj for f in fornecedores[:10]],
        "litros": [float(f.litros or 0) for f in fornecedores[:10]],
        "valor":  [float(f.valor or 0) for f in fornecedores[:10]],
    }
    mensal_chart = {
        "labels": [m.mes.strftime("%m/%Y") if m.mes else "—" for m in mensal],
        "litros": [float(m.litros or 0) for m in mensal],
        "valor":  [float(m.valor or 0) for m in mensal],
    }
    ncm_chart = {
        "labels": [n.ncm or "—" for n in por_ncm],
        "litros": [float(n.litros or 0) for n in por_ncm],
    }

    return templates.TemplateResponse(
        "relatorios.html",
        {
            "request": request, "settings": settings, **k,
            "fornecedores": fornecedores,
            "mensal": mensal,
            "por_ncm": por_ncm,
            "valor_total": valor_total,
            "qtd_total": qtd_total,
            "ticket_medio": ticket_medio,
            "preco_medio_litro": preco_medio_litro,
            "taxa_diaria": Decimal(str(taxa_diaria)),
            "projecao_total": Decimal(str(projecao_total)),
            "projecao_pct": projecao_pct,
            "projecao_status": projecao_status,
            "dias_decorridos": dias_decorridos,
            "dias_periodo": dias_periodo,
            "dias_ate_estourar": dias_ate_estourar,
            "data_estouro": data_estouro,
            "fornecedores_chart": fornecedores_chart,
            "mensal_chart": mensal_chart,
            "ncm_chart": ncm_chart,
            "now": fmt.now_local(),
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


@app.post("/manifestar/{chave}", dependencies=[Depends(require_login)])
def manifestar(chave: str, db: Session = Depends(get_db)):
    if len(chave) != 44 or not chave.isdigit():
        return {"ok": False, "erro": "chave inválida"}
    try:
        return {"ok": True, **manifestar_chave(db, chave)}
    except Exception as ex:  # noqa: BLE001
        log.exception("Falha manifestação manual")
        return {"ok": False, "erro": str(ex)}
