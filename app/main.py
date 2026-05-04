import logging
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from .auth import current_user, hash_password, verify_password
from .config import settings
from .database import Base, SessionLocal, engine, get_db
from .models import Empresa, NotaFiscal, Usuario
from .services.ingest import processar_empresa
from .services.quota import litros_consumidos, percentual

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = FastAPI(title="Cota Diesel")
app.add_middleware(SessionMiddleware, secret_key=settings.SECRET_KEY, https_only=False)

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.on_event("startup")
def on_startup():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        if not db.query(Usuario).first():
            admin = Usuario(
                email=settings.ADMIN_EMAIL,
                senha_hash=hash_password(settings.ADMIN_PASSWORD),
                nome="Administrador",
                is_admin=True,
            )
            db.add(admin)
            db.commit()
            log.info("Usuário admin criado: %s", settings.ADMIN_EMAIL)
    finally:
        db.close()


@app.get("/", response_class=HTMLResponse)
def root(request: Request):
    if not request.session.get("uid"):
        return RedirectResponse("/login", status_code=303)
    return RedirectResponse("/dashboard", status_code=303)


@app.get("/login", response_class=HTMLResponse)
def login_get(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "erro": None})


@app.post("/login", response_class=HTMLResponse)
def login_post(
    request: Request,
    email: str = Form(...),
    senha: str = Form(...),
    db: Session = Depends(get_db),
):
    user = db.query(Usuario).filter(Usuario.email == email, Usuario.ativo.is_(True)).first()
    if not user or not verify_password(senha, user.senha_hash):
        return templates.TemplateResponse(
            "login.html", {"request": request, "erro": "Credenciais inválidas"}, status_code=401
        )
    request.session["uid"] = user.id
    return RedirectResponse("/dashboard", status_code=303)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


def _empresas_visiveis(db: Session, user: Usuario):
    q = db.query(Empresa).filter(Empresa.ativo.is_(True))
    if not user.is_admin and user.empresa_id:
        q = q.filter(Empresa.id == user.empresa_id)
    return q.order_by(Empresa.nome).all()


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(
    request: Request,
    user: Usuario = Depends(current_user),
    db: Session = Depends(get_db),
):
    empresas = _empresas_visiveis(db, user)
    cards = []
    for e in empresas:
        consumo = litros_consumidos(db, e)
        pct = percentual(e, consumo)
        cards.append({
            "empresa": e,
            "consumo": consumo,
            "pct": pct,
            "restante": Decimal(e.cota_litros) - consumo,
        })
    return templates.TemplateResponse(
        "dashboard.html",
        {"request": request, "user": user, "cards": cards, "now": datetime.utcnow()},
    )


@app.get("/empresas/{empresa_id}", response_class=HTMLResponse)
def empresa_detalhe(
    empresa_id: int,
    request: Request,
    user: Usuario = Depends(current_user),
    db: Session = Depends(get_db),
):
    e = db.get(Empresa, empresa_id)
    if not e or (not user.is_admin and user.empresa_id != e.id):
        raise HTTPException(404)
    consumo = litros_consumidos(db, e)
    pct = percentual(e, consumo)
    notas = (
        db.query(NotaFiscal)
        .filter(NotaFiscal.empresa_id == e.id)
        .order_by(NotaFiscal.data_emissao.desc())
        .limit(50)
        .all()
    )
    return templates.TemplateResponse(
        "empresa.html",
        {
            "request": request, "user": user, "e": e, "notas": notas,
            "consumo": consumo, "pct": pct,
            "restante": Decimal(e.cota_litros) - consumo,
        },
    )


@app.post("/empresas/{empresa_id}/sync")
def empresa_sync(
    empresa_id: int,
    user: Usuario = Depends(current_user),
    db: Session = Depends(get_db),
):
    e = db.get(Empresa, empresa_id)
    if not e or (not user.is_admin and user.empresa_id != e.id):
        raise HTTPException(404)
    try:
        resumo = processar_empresa(db, e)
    except Exception as ex:  # noqa: BLE001
        return {"ok": False, "erro": str(ex)}
    return {"ok": True, **resumo}


# --- Admin: cadastro de empresas e usuários (mínimo) ---

@app.get("/admin", response_class=HTMLResponse)
def admin_home(
    request: Request,
    user: Usuario = Depends(current_user),
    db: Session = Depends(get_db),
):
    if not user.is_admin:
        raise HTTPException(403)
    empresas = db.query(Empresa).order_by(Empresa.nome).all()
    usuarios = db.query(Usuario).order_by(Usuario.email).all()
    return templates.TemplateResponse(
        "admin.html",
        {"request": request, "user": user, "empresas": empresas, "usuarios": usuarios},
    )


@app.post("/admin/empresas")
def admin_cria_empresa(
    nome: str = Form(...),
    cnpj: str = Form(...),
    cota_litros: float = Form(...),
    periodo_inicio: str = Form(...),
    periodo_fim: str = Form(...),
    email_alertas: str = Form(""),
    user: Usuario = Depends(current_user),
    db: Session = Depends(get_db),
):
    if not user.is_admin:
        raise HTTPException(403)
    cnpj_limpo = "".join(c for c in cnpj if c.isdigit())
    e = Empresa(
        nome=nome, cnpj=cnpj_limpo,
        cota_litros=Decimal(str(cota_litros)),
        periodo_inicio=date.fromisoformat(periodo_inicio),
        periodo_fim=date.fromisoformat(periodo_fim),
        email_alertas=email_alertas or None,
    )
    db.add(e)
    db.commit()
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/usuarios")
def admin_cria_usuario(
    email: str = Form(...),
    senha: str = Form(...),
    nome: str = Form(""),
    empresa_id: int | None = Form(None),
    is_admin: bool = Form(False),
    user: Usuario = Depends(current_user),
    db: Session = Depends(get_db),
):
    if not user.is_admin:
        raise HTTPException(403)
    u = Usuario(
        email=email, senha_hash=hash_password(senha), nome=nome or None,
        empresa_id=empresa_id or None, is_admin=is_admin,
    )
    db.add(u)
    db.commit()
    return RedirectResponse("/admin", status_code=303)
