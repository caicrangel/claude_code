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
from .models import CotaPeriodo, NotaFiscal
from .services import periodo as periodo_svc
from .services.ingest import (
    status_bloqueio_656,
    ultima_sincronizacao,
)
from .services.quota import litros_consumidos, percentual

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = FastAPI(title=f"Cota Diesel - {settings.EMPRESA_NOME}")
app.add_middleware(SessionMiddleware, secret_key=settings.SECRET_KEY, https_only=False)

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
fmt.register(templates.env)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


def _logo_url() -> str | None:
    v = (settings.EMPRESA_LOGO or "").strip()
    if not v:
        return None
    if v.startswith(("http://", "https://", "/")):
        return v
    return "/static/" + v


# Contexto disponível em todas as páginas
@app.middleware("http")
async def globals_middleware(request: Request, call_next):
    request.state.logo_url = _logo_url()
    request.state.empresa_nome = settings.EMPRESA_NOME
    return await call_next(request)


def render(template: str, request: Request, **ctx):
    ctx.setdefault("settings", settings)
    ctx.setdefault("logo_url", _logo_url())
    ctx.setdefault("empresa_nome", settings.EMPRESA_NOME)
    return templates.TemplateResponse(template, {"request": request, **ctx})


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
    return render("login.html", request, erro=None)


@app.post("/login", response_class=HTMLResponse)
def login_post(request: Request, email: str = Form(...), senha: str = Form(...)):
    if not check_credentials(email, senha):
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "erro": "Credenciais inválidas",
             "logo_url": _logo_url(), "empresa_nome": settings.EMPRESA_NOME,
             "settings": settings},
            status_code=401,
        )
    request.session["auth"] = True
    return RedirectResponse("/dashboard", status_code=303)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


def _kpis(db: Session, p: CotaPeriodo, inicio: date | None = None,
          fim: date | None = None) -> dict:
    if inicio is None:
        inicio = p.inicio
    if fim is None:
        fim = p.fim
    consumo = litros_consumidos(db, inicio=inicio, fim=fim)
    cota = Decimal(p.cota_litros or 0)
    pct = percentual(consumo, cota)
    restante = cota - consumo
    status = "ok" if pct < 70 else ("warn" if pct < 95 else "crit")
    canceladas = (
        db.query(NotaFiscal)
        .filter(
            NotaFiscal.data_emissao >= inicio,
            NotaFiscal.data_emissao <= fim,
            NotaFiscal.cancelada.is_(True),
        ).count()
    )
    excluidas = (
        db.query(NotaFiscal)
        .filter(
            NotaFiscal.data_emissao >= inicio,
            NotaFiscal.data_emissao <= fim,
            NotaFiscal.cancelada.is_(False),
            NotaFiscal.excluida_cota.is_(True),
        ).count()
    )
    return {
        "consumo": consumo, "cota": cota, "pct": pct, "restante": restante,
        "status": status, "canceladas": canceladas, "excluidas": excluidas,
        "inicio": inicio, "fim": fim, "periodo": p,
    }


@app.get("/dashboard", response_class=HTMLResponse, dependencies=[Depends(require_login)])
def dashboard(request: Request, db: Session = Depends(get_db)):
    p = periodo_svc.get_periodo_ativo(db)
    if p is None:
        return RedirectResponse("/configuracao", status_code=303)
    k = _kpis(db, p)
    total_notas = (
        db.query(NotaFiscal)
        .filter(NotaFiscal.data_emissao >= p.inicio,
                NotaFiscal.data_emissao <= p.fim)
        .count()
    )
    notas = (
        db.query(NotaFiscal)
        .filter(NotaFiscal.data_emissao >= p.inicio,
                NotaFiscal.data_emissao <= p.fim)
        .order_by(NotaFiscal.data_emissao.desc().nullslast())
        .limit(200).all()
    )
    bloqueio = status_bloqueio_656(db)
    ultima_sync = ultima_sincronizacao(db)
    h = settings.SYNC_INTERVALO_HORAS
    poll_label = "a cada hora" if h == 1 else f"a cada {h} horas"
    return render("dashboard.html", request, **k,
                  notas=notas, total_notas=total_notas, now=fmt.now_local(),
                  bloqueio_sefaz=bloqueio, ultima_sync=ultima_sync,
                  poll_label=poll_label)


def _parse_date(v: str | None, default: date) -> date:
    if not v:
        return default
    try:
        return date.fromisoformat(v)
    except ValueError:
        return default


@app.get("/relatorios", response_class=HTMLResponse, dependencies=[Depends(require_login)])
def relatorios(
    request: Request,
    inicio: str | None = None,
    fim: str | None = None,
    periodo_id: int | None = None,
    db: Session = Depends(get_db),
):
    periodos = periodo_svc.listar(db)
    p_ativo = periodo_svc.get_periodo_ativo(db)
    if p_ativo is None:
        return RedirectResponse("/configuracao", status_code=303)

    if periodo_id:
        sel = periodo_svc.get_periodo(db, periodo_id) or p_ativo
        ini, fim_d = sel.inicio, sel.fim
    else:
        ini = _parse_date(inicio, p_ativo.inicio)
        fim_d = _parse_date(fim, p_ativo.fim)
        sel = p_ativo

    k = _kpis(db, sel, inicio=ini, fim=fim_d)

    base = db.query(NotaFiscal).filter(
        NotaFiscal.data_emissao >= ini, NotaFiscal.data_emissao <= fim_d,
        NotaFiscal.is_resumo.is_(False), NotaFiscal.cancelada.is_(False),
        NotaFiscal.excluida_cota.is_(False),
    )

    fornecedores = (
        base.with_entities(
            NotaFiscal.emitente_cnpj, NotaFiscal.emitente_nome,
            func.sum(NotaFiscal.litros_diesel).label("litros"),
            func.sum(NotaFiscal.valor_total).label("valor"),
            func.count(NotaFiscal.id).label("qtd"),
        )
        .group_by(NotaFiscal.emitente_cnpj, NotaFiscal.emitente_nome)
        .order_by(func.sum(NotaFiscal.litros_diesel).desc()).all()
    )
    mensal = (
        base.with_entities(
            func.date_trunc("month", NotaFiscal.data_emissao).label("mes"),
            func.sum(NotaFiscal.litros_diesel).label("litros"),
            func.sum(NotaFiscal.valor_total).label("valor"),
            func.count(NotaFiscal.id).label("qtd"),
        ).group_by("mes").order_by("mes").all()
    )
    por_ncm = (
        base.with_entities(
            NotaFiscal.ncm,
            func.sum(NotaFiscal.litros_diesel).label("litros"),
            func.count(NotaFiscal.id).label("qtd"),
        ).group_by(NotaFiscal.ncm)
        .order_by(func.sum(NotaFiscal.litros_diesel).desc()).all()
    )

    valor_total = sum((Decimal(f.valor or 0) for f in fornecedores), Decimal(0))
    qtd_total = sum((f.qtd for f in fornecedores), 0)
    ticket_medio = (valor_total / qtd_total) if qtd_total else Decimal(0)
    preco_medio_litro = (valor_total / k["consumo"]) if k["consumo"] else Decimal(0)

    hoje = date.today()
    dias_decorridos = max(1, (min(hoje, fim_d) - ini).days + 1)
    dias_periodo = (fim_d - ini).days + 1
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

    return render(
        "relatorios.html", request, **k,
        periodos=periodos, periodo_selecionado=sel,
        filtro_inicio=ini, filtro_fim=fim_d,
        fornecedores=fornecedores, mensal=mensal, por_ncm=por_ncm,
        valor_total=valor_total, qtd_total=qtd_total,
        ticket_medio=ticket_medio, preco_medio_litro=preco_medio_litro,
        taxa_diaria=Decimal(str(taxa_diaria)),
        projecao_total=Decimal(str(projecao_total)),
        projecao_pct=projecao_pct, projecao_status=projecao_status,
        dias_decorridos=dias_decorridos, dias_periodo=dias_periodo,
        dias_ate_estourar=dias_ate_estourar, data_estouro=data_estouro,
        fornecedores_chart=fornecedores_chart,
        mensal_chart=mensal_chart, ncm_chart=ncm_chart,
        now=fmt.now_local(),
    )


# ---------- configuração: período de cota ----------

@app.get("/configuracao", response_class=HTMLResponse, dependencies=[Depends(require_login)])
def config_get(request: Request, db: Session = Depends(get_db)):
    return render(
        "configuracao.html", request,
        periodo_ativo=periodo_svc.get_periodo_ativo(db),
        periodos=periodo_svc.listar(db),
        msg=request.query_params.get("msg"),
    )


@app.post("/configuracao/novo", dependencies=[Depends(require_login)])
def config_novo(
    nome: str = Form(""),
    inicio: str = Form(...),
    fim: str = Form(...),
    cota_litros: float = Form(...),
    db: Session = Depends(get_db),
):
    periodo_svc.novo_periodo(
        db,
        nome=nome or "Novo período",
        inicio=date.fromisoformat(inicio),
        fim=date.fromisoformat(fim),
        cota_litros=Decimal(str(cota_litros)),
    )
    return RedirectResponse("/configuracao?msg=Novo+periodo+criado", status_code=303)


@app.post("/configuracao/ativar/{periodo_id}", dependencies=[Depends(require_login)])
def config_ativar(periodo_id: int, db: Session = Depends(get_db)):
    periodo_svc.ativar(db, periodo_id)
    return RedirectResponse("/configuracao?msg=Periodo+ativado", status_code=303)


@app.post("/notas/{nota_id}/toggle-cota", dependencies=[Depends(require_login)])
def toggle_cota(nota_id: int, db: Session = Depends(get_db)):
    """Alterna o flag excluida_cota de uma NF (override manual)."""
    nf = db.get(NotaFiscal, nota_id)
    if nf is not None:
        nf.excluida_cota = not nf.excluida_cota
        db.commit()
    return RedirectResponse("/dashboard", status_code=303)

