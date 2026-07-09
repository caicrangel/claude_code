import logging
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from . import format as fmt
from . import runtime_config
from .auth import (
    ROLE_ADMIN,
    ROLE_COMUM,
    autenticar,
    current_user,
    hash_senha,
    require_admin,
    require_login,
)
from .config import settings
from .database import get_db, run_migrations
from .models import CotaPeriodo, EmailAlerta, NotaFiscal, Usuario, get_state, set_state
from .services import periodo as periodo_svc
from .services.ingest import (
    TooSoonError,
    UploadInvalido,
    importar_xml_manual,
    processar,
    status_bloqueio_656,
    ultima_sincronizacao,
)
from .services.quota import (
    avaliar_e_alertar,
    cond_pertence_periodo,
    litros_consumidos,
    percentual,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = FastAPI(title=f"Cota Diesel - {settings.EMPRESA_NOME}")
app.add_middleware(SessionMiddleware, secret_key=settings.SECRET_KEY, https_only=False)

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
fmt.register(templates.env)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


class _SettingsView:
    """Proxy para uso nos templates: mistura valores dinâmicos (do banco)
    com os do .env. Permite que os templates continuem usando
    `settings.EMPRESA_NOME`, `settings.cnpj_limpo`, etc."""

    def __init__(self, db: Session) -> None:
        self._db = db

    @property
    def EMPRESA_NOME(self) -> str:
        return runtime_config.empresa_nome(self._db)

    @property
    def cnpj_limpo(self) -> str:
        return runtime_config.cnpj_limpo(self._db)

    def __getattr__(self, item: str):
        # fallback transparente para os campos restantes do .env
        return getattr(settings, item)


def render(template: str, request: Request, db: Session, **ctx):
    view = _SettingsView(db)
    logos = runtime_config.logos_url(db)
    user = current_user(request, db)
    ctx.setdefault("settings", view)
    ctx.setdefault("logo_urls", logos)
    ctx.setdefault("logo_url", logos.get("dark") or logos.get("light"))
    ctx.setdefault("empresa_nome", view.EMPRESA_NOME)
    ctx.setdefault("current_user", user)
    ctx.setdefault("is_admin", bool(user and user.role == ROLE_ADMIN))
    return templates.TemplateResponse(template, {"request": request, **ctx})


@app.on_event("startup")
def on_startup():
    run_migrations()


@app.get("/", response_class=HTMLResponse)
def root(request: Request):
    if not request.session.get("user_id"):
        return RedirectResponse("/login", status_code=303)
    return RedirectResponse("/dashboard", status_code=303)


@app.get("/login", response_class=HTMLResponse)
def login_get(request: Request, db: Session = Depends(get_db)):
    return render("login.html", request, db, erro=None)


@app.post("/login", response_class=HTMLResponse)
def login_post(
    request: Request,
    email: str = Form(...),
    senha: str = Form(...),
    db: Session = Depends(get_db),
):
    user = autenticar(db, email, senha)
    if user is None:
        return render("login.html", request, db, erro="Credenciais inválidas")
    request.session["user_id"] = user.id
    request.session["role"] = user.role
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
    consumo = litros_consumidos(db, periodo=p, inicio=inicio, fim=fim)
    cota = Decimal(p.cota_litros or 0)
    pct = percentual(consumo, cota)
    restante = cota - consumo
    status = "ok" if pct < 70 else ("warn" if pct < 95 else "crit")
    membros = cond_pertence_periodo(p, inicio, fim)
    canceladas = (
        db.query(NotaFiscal)
        .filter(membros, NotaFiscal.cancelada.is_(True)).count()
    )
    excluidas = (
        db.query(NotaFiscal)
        .filter(membros,
                NotaFiscal.cancelada.is_(False),
                NotaFiscal.excluida_cota.is_(True)).count()
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
    membros = cond_pertence_periodo(p)
    total_notas = db.query(NotaFiscal).filter(membros).count()
    notas = (
        db.query(NotaFiscal)
        .filter(membros)
        .order_by(NotaFiscal.data_emissao.desc().nullslast())
        .limit(200).all()
    )
    bloqueio = status_bloqueio_656(db)
    ultima_sync = ultima_sincronizacao(db)
    h = settings.SYNC_INTERVALO_HORAS
    poll_label = "a cada hora" if h == 1 else f"a cada {h} horas"
    upload_ok = request.query_params.get("upload_ok")
    upload_erro = request.query_params.get("upload_erro")
    sync_ok = request.query_params.get("sync_ok")
    sync_erro = request.query_params.get("sync_erro")
    return render("dashboard.html", request, db, **k,
                  notas=notas, total_notas=total_notas, now=fmt.now_local(),
                  bloqueio_sefaz=bloqueio, ultima_sync=ultima_sync,
                  poll_label=poll_label,
                  upload_ok=upload_ok, upload_erro=upload_erro,
                  sync_ok=sync_ok, sync_erro=sync_erro)


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
    else:
        sel = p_ativo
    # inicio/fim do form têm precedência sobre as datas do período selecionado:
    # o período define o "contexto de cota" (% consumido, projeção); as datas
    # definem o intervalo exibido. Permite filtrar um subintervalo livremente.
    ini = _parse_date(inicio, sel.inicio)
    fim_d = _parse_date(fim, sel.fim)

    k = _kpis(db, sel, inicio=ini, fim=fim_d)

    base = db.query(NotaFiscal).filter(
        cond_pertence_periodo(sel, ini, fim_d),
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
        "relatorios.html", request, db, **k,
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


# ---------- configuração (admin) ----------

ABAS_VALIDAS = {"periodos", "parametros", "emails", "usuarios"}


def _config_redirect(request: Request, *, msg: str | None = None,
                     erro: str | None = None) -> RedirectResponse:
    """Redireciona para /configuracao preservando a aba (do query ?aba=...)."""
    from urllib.parse import urlencode
    aba = request.query_params.get("aba", "")
    if aba not in ABAS_VALIDAS:
        aba = ""
    params = {}
    if msg:
        params["msg"] = msg
    if erro:
        params["erro"] = erro
    if aba:
        params["aba"] = aba
    qs = ("?" + urlencode(params)) if params else ""
    return RedirectResponse(f"/configuracao{qs}", status_code=303)


@app.get("/configuracao", response_class=HTMLResponse,
         dependencies=[Depends(require_admin)])
def config_get(request: Request, db: Session = Depends(get_db)):
    return render(
        "configuracao.html", request, db,
        periodo_ativo=periodo_svc.get_periodo_ativo(db),
        periodos=periodo_svc.listar(db),
        usuarios=db.query(Usuario).order_by(Usuario.email).all(),
        emails_alerta=db.query(EmailAlerta).order_by(EmailAlerta.email).all(),
        params=runtime_config.snapshot_safe(db),
        cert_info=runtime_config.get_cert_info(db),
        msg=request.query_params.get("msg"),
        erro=request.query_params.get("erro"),
    )


@app.post("/configuracao/novo", dependencies=[Depends(require_admin)])
def config_novo(
    request: Request,
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
    return _config_redirect(request, msg="Novo periodo criado")


@app.post("/configuracao/ativar/{periodo_id}", dependencies=[Depends(require_admin)])
def config_ativar(periodo_id: int, request: Request, db: Session = Depends(get_db)):
    periodo_svc.ativar(db, periodo_id)
    return _config_redirect(request, msg="Periodo ativado")


@app.post("/configuracao/periodos/{periodo_id}/excluir",
          dependencies=[Depends(require_admin)])
def config_periodo_excluir(periodo_id: int, request: Request,
                           db: Session = Depends(get_db)):
    try:
        ok = periodo_svc.excluir(db, periodo_id)
    except periodo_svc.PeriodoAtivoError as e:
        return _config_redirect(request, erro=str(e))
    if not ok:
        return _config_redirect(request, erro="Periodo nao encontrado")
    return _config_redirect(request, msg="Periodo excluido")


# ---- usuários ----

@app.post("/configuracao/usuarios/novo", dependencies=[Depends(require_admin)])
def usuario_novo(
    request: Request,
    email: str = Form(...),
    senha: str = Form(...),
    nome: str = Form(""),
    role: str = Form(ROLE_COMUM),
    db: Session = Depends(get_db),
):
    email = (email or "").strip().lower()
    if not email or not senha:
        return _config_redirect(request, erro="Email e senha obrigatorios")
    if role not in (ROLE_ADMIN, ROLE_COMUM):
        role = ROLE_COMUM
    if db.query(Usuario).filter(Usuario.email == email).first():
        return _config_redirect(request, erro="Email ja cadastrado")
    db.add(Usuario(
        email=email, senha_hash=hash_senha(senha),
        nome=nome or None, role=role, ativo=True,
    ))
    db.commit()
    return _config_redirect(request, msg="Usuario criado")


@app.post("/configuracao/usuarios/{user_id}/excluir",
          dependencies=[Depends(require_admin)])
def usuario_excluir(user_id: int, request: Request, db: Session = Depends(get_db)):
    if request.session.get("user_id") == user_id:
        return _config_redirect(request, erro="Nao e possivel excluir a si mesmo")
    u = db.get(Usuario, user_id)
    if u is None:
        return _config_redirect(request)
    if u.role == ROLE_ADMIN:
        outros_admins = db.query(Usuario).filter(
            Usuario.role == ROLE_ADMIN, Usuario.id != user_id,
            Usuario.ativo.is_(True),
        ).count()
        if outros_admins == 0:
            return _config_redirect(request, erro="Mantenha ao menos um admin")
    db.delete(u)
    db.commit()
    return _config_redirect(request, msg="Usuario excluido")


@app.post("/configuracao/usuarios/{user_id}/senha",
          dependencies=[Depends(require_admin)])
def usuario_senha(user_id: int, request: Request, senha: str = Form(...),
                  db: Session = Depends(get_db)):
    if not senha:
        return _config_redirect(request, erro="Senha vazia")
    u = db.get(Usuario, user_id)
    if u is None:
        return _config_redirect(request)
    u.senha_hash = hash_senha(senha)
    db.commit()
    return _config_redirect(request, msg="Senha atualizada")


# ---- e-mails de alerta ----

@app.post("/configuracao/emails/novo", dependencies=[Depends(require_admin)])
def email_novo(
    request: Request,
    email: str = Form(...),
    nome: str = Form(""),
    db: Session = Depends(get_db),
):
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        return _config_redirect(request, erro="Email invalido")
    if db.query(EmailAlerta).filter(EmailAlerta.email == email).first():
        return _config_redirect(request, erro="Email ja cadastrado")
    db.add(EmailAlerta(email=email, nome=nome or None, ativo=True))
    db.commit()
    return _config_redirect(request, msg="Email adicionado")


@app.post("/configuracao/emails/{email_id}/excluir",
          dependencies=[Depends(require_admin)])
def email_excluir(email_id: int, request: Request, db: Session = Depends(get_db)):
    e = db.get(EmailAlerta, email_id)
    if e is not None:
        db.delete(e)
        db.commit()
    return _config_redirect(request, msg="Email removido")


@app.post("/configuracao/emails/{email_id}/toggle",
          dependencies=[Depends(require_admin)])
def email_toggle(email_id: int, request: Request, db: Session = Depends(get_db)):
    e = db.get(EmailAlerta, email_id)
    if e is not None:
        e.ativo = not e.ativo
        db.commit()
    return _config_redirect(request, msg="Status atualizado")


# ---- parâmetros gerais ----

@app.post("/configuracao/parametros", dependencies=[Depends(require_admin)])
def parametros_salvar(
    request: Request,
    EMPRESA_NOME: str = Form(""),
    EMPRESA_CNPJ: str = Form(""),
    EMPRESA_LOGO_LIGHT: str = Form(""),
    EMPRESA_LOGO_DARK: str = Form(""),
    ALERT_THRESHOLDS: str = Form(""),
    db: Session = Depends(get_db),
):
    valores = {
        "EMPRESA_NOME": EMPRESA_NOME.strip(),
        "EMPRESA_CNPJ": EMPRESA_CNPJ.strip(),
        "EMPRESA_LOGO_LIGHT": EMPRESA_LOGO_LIGHT.strip(),
        "EMPRESA_LOGO_DARK": EMPRESA_LOGO_DARK.strip(),
        "ALERT_THRESHOLDS": ALERT_THRESHOLDS.strip(),
    }
    if valores["ALERT_THRESHOLDS"]:
        try:
            [int(x) for x in valores["ALERT_THRESHOLDS"].split(",") if x.strip()]
        except ValueError:
            return _config_redirect(
                request,
                erro="Thresholds invalidos (use numeros separados por virgula)")
    for chave, valor in valores.items():
        runtime_config.set_(db, chave, valor)
    return _config_redirect(request, msg="Parametros atualizados")


# ---- SMTP ----

@app.post("/configuracao/smtp", dependencies=[Depends(require_admin)])
def smtp_salvar(
    request: Request,
    SMTP_HOST: str = Form(""),
    SMTP_PORT: str = Form(""),
    SMTP_USER: str = Form(""),
    SMTP_PASSWORD: str = Form(""),
    SMTP_FROM: str = Form(""),
    SMTP_TLS: str = Form(""),
    db: Session = Depends(get_db),
):
    if SMTP_PORT.strip():
        try:
            p = int(SMTP_PORT)
            if not (0 < p < 65536):
                raise ValueError
        except ValueError:
            return _config_redirect(request, erro="Porta SMTP invalida")

    valores = {
        "SMTP_HOST": SMTP_HOST.strip(),
        "SMTP_PORT": SMTP_PORT.strip(),
        "SMTP_USER": SMTP_USER.strip(),
        "SMTP_FROM": SMTP_FROM.strip(),
        # Checkbox HTML envia "on" quando marcado, ausente quando não.
        "SMTP_TLS":  "true" if SMTP_TLS else "false",
    }
    for chave, valor in valores.items():
        runtime_config.set_(db, chave, valor)
    # Senha: só atualiza se o admin digitou algo. Vazio = manter atual.
    if SMTP_PASSWORD:
        runtime_config.set_(db, "SMTP_PASSWORD", SMTP_PASSWORD)
    return _config_redirect(request, msg="SMTP atualizado")


# ---- SEFAZ ----

@app.post("/configuracao/sefaz", dependencies=[Depends(require_admin)])
def sefaz_salvar(
    request: Request,
    SEFAZ_AMBIENTE: str = Form("1"),
    SEFAZ_UF: str = Form(""),
    db: Session = Depends(get_db),
):
    if SEFAZ_AMBIENTE not in ("1", "2"):
        return _config_redirect(request, erro="Ambiente SEFAZ invalido (1 ou 2)")
    uf = SEFAZ_UF.strip().upper()
    from .services.sefaz import UF_COD
    if uf and uf not in UF_COD:
        return _config_redirect(request, erro=f"UF invalida: {uf}")
    runtime_config.set_(db, "SEFAZ_AMBIENTE", SEFAZ_AMBIENTE)
    if uf:
        runtime_config.set_(db, "SEFAZ_UF", uf)
    return _config_redirect(request, msg="SEFAZ atualizado")


# ---- Certificado digital ----

@app.post("/configuracao/certificado", dependencies=[Depends(require_admin)])
async def certificado_upload(
    request: Request,
    arquivo: UploadFile = File(...),
    senha: str = Form(...),
    db: Session = Depends(get_db),
):
    nome = arquivo.filename or "certificado.pfx"
    if not nome.lower().endswith(".pfx"):
        return _config_redirect(request, erro="Arquivo precisa ter extensao .pfx")
    if not senha:
        return _config_redirect(request, erro="Senha do certificado obrigatoria")
    conteudo = await arquivo.read()
    if len(conteudo) > 256 * 1024:  # 256KB — A1 PFX típico tem 4-12KB
        return _config_redirect(request, erro="Arquivo muito grande")
    # Valida abrindo o PFX com a senha antes de gravar.
    try:
        from .services.sefaz import load_pfx_bytes
        load_pfx_bytes(conteudo, senha)
    except Exception:  # noqa: BLE001
        log.exception("Falha validando certificado no upload")
        return _config_redirect(
            request, erro="Falha lendo certificado (arquivo invalido ou senha errada)")
    runtime_config.set_cert(db, pfx_bytes=conteudo, senha=senha, nome_arquivo=nome)
    return _config_redirect(request, msg="Certificado atualizado")


def _reavaliar_alertas(db: Session) -> None:
    """Aciona o avaliador de alertas após mudança manual no consumo.
    Idempotente — não reenvia alertas já registrados para o mesmo
    período/threshold. Falhas são logadas e não propagam."""
    try:
        avaliar_e_alertar(db)
    except Exception:  # noqa: BLE001
        log.exception("Falha reavaliando alertas após mudança manual")


@app.post("/notas/{nota_id}/toggle-cota", dependencies=[Depends(require_login)])
def toggle_cota(nota_id: int, db: Session = Depends(get_db)):
    """Alterna o flag excluida_cota de uma NF (override manual)."""
    nf = db.get(NotaFiscal, nota_id)
    if nf is not None:
        nf.excluida_cota = not nf.excluida_cota
        db.commit()
        _reavaliar_alertas(db)
    return RedirectResponse("/dashboard", status_code=303)


@app.post("/notas/{nota_id}/excluir", dependencies=[Depends(require_admin)])
def excluir_nota(nota_id: int, db: Session = Depends(get_db)):
    """Remove DEFINITIVAMENTE uma NF do sistema. Para corrigir inserções
    incorretas (upload manual errado, NFe que não deveria estar aqui).
    Atenção: a NFe pode voltar via SEFAZ se ainda estiver na faixa de NSU."""
    nf = db.get(NotaFiscal, nota_id)
    if nf is not None:
        db.delete(nf)
        db.commit()
    return RedirectResponse("/dashboard", status_code=303)


SYNC_MANUAL_KEY = "ultima_sync_manual_em"
SYNC_MANUAL_COOLDOWN_SEG = 300  # 5 min entre sincronizações manuais


@app.post("/sync", dependencies=[Depends(require_admin)])
def sync_manual(db: Session = Depends(get_db)):
    """Dispara um ciclo de sincronização SEFAZ sob demanda (admin).

    Usa force=False: respeita o intervalo mínimo entre consultas e o
    bloqueio cStat=656. Além disso aplica um cooldown próprio de 5 min
    entre cliques manuais — impede que cliques repetidos durante um
    catch-up grande acumulem consultas e provoquem o cStat=656.
    O worker automático (2h) não é afetado por esse cooldown."""
    from urllib.parse import quote_plus

    # Cooldown do botão manual (independente do worker).
    raw = get_state(db, SYNC_MANUAL_KEY, "")
    if raw:
        try:
            ultima = datetime.fromisoformat(raw)
            desde = (datetime.now(timezone.utc) - ultima).total_seconds()
            if desde < SYNC_MANUAL_COOLDOWN_SEG:
                falta = int(SYNC_MANUAL_COOLDOWN_SEG - desde)
                m, s = divmod(falta, 60)
                txt = (f"Aguarde {m}min {s:02d}s para sincronizar manualmente de novo "
                       f"(o worker automatico segue rodando normalmente)")
                return RedirectResponse(
                    f"/dashboard?sync_erro={quote_plus(txt)}", status_code=303)
        except ValueError:
            pass

    try:
        r = processar(db, force=False)
    except TooSoonError as e:
        return RedirectResponse(
            f"/dashboard?sync_erro={quote_plus(str(e))}", status_code=303)
    except Exception:  # noqa: BLE001
        log.exception("Erro na sincronização manual")
        return RedirectResponse(
            "/dashboard?sync_erro=Falha+na+sincronizacao+%28ver+logs%29", status_code=303)

    # Registra o clique só quando a sincronização de fato ocorreu.
    set_state(db, SYNC_MANUAL_KEY, datetime.now(timezone.utc).isoformat())
    cont = r.get("contadores", {}) or {}
    novas = cont.get("nfe-diesel", 0) + cont.get("nfe-diesel-nao-venda", 0)
    if novas:
        msg = f"Sincronizacao OK — {novas} NF(s) de diesel nova(s) importada(s)"
    else:
        msg = "Sincronizacao OK — nenhuma NF nova (ja estava tudo em dia)"
    return RedirectResponse(f"/dashboard?sync_ok={quote_plus(msg)}", status_code=303)


@app.post("/notas/upload", dependencies=[Depends(require_login)])
async def upload_xml(
    arquivo: UploadFile = File(...),
    fixar_periodo: str = Form(""),
    db: Session = Depends(get_db),
):
    """Upload manual de XML de NFe (para casos que a SEFAZ não trouxe ou
    que a auto-detecção da natureza precisa ser revisada).

    Se `fixar_periodo` vier marcado, a NF é fixada ao período ATIVO — passa
    a contar na cota dele mesmo que a data de emissão seja de outro período
    (ex.: NF de mês anterior incluída numa cota renovada por liminar)."""
    nome = (arquivo.filename or "").lower()
    if not nome.endswith(".xml"):
        return RedirectResponse(
            "/dashboard?upload_erro=Arquivo+precisa+ter+extensao+.xml", status_code=303)
    conteudo = await arquivo.read()
    if len(conteudo) > 5 * 1024 * 1024:  # 5MB
        return RedirectResponse(
            "/dashboard?upload_erro=Arquivo+muito+grande+%28max+5MB%29", status_code=303)
    fixar_id = None
    if fixar_periodo:
        p_ativo = periodo_svc.get_periodo_ativo(db)
        if p_ativo is not None:
            fixar_id = p_ativo.id
    try:
        r = importar_xml_manual(db, conteudo, fixar_periodo_id=fixar_id)
    except UploadInvalido as e:
        msg = str(e).replace(" ", "+")
        return RedirectResponse(f"/dashboard?upload_erro={msg}", status_code=303)
    except Exception:  # noqa: BLE001
        log.exception("Erro no upload manual de XML")
        return RedirectResponse(
            "/dashboard?upload_erro=Erro+ao+processar+XML", status_code=303)
    msg = (f"NF+{r['acao']}+%28{r['litros']:.0f}+L%29"
           + ("+marcada+como+fora+da+cota" if r["excluida_cota"] else "+incluida+na+cota")
           + ("+e+FIXADA+neste+periodo" if r["fixada"] else ""))
    _reavaliar_alertas(db)
    return RedirectResponse(f"/dashboard?upload_ok={msg}", status_code=303)


@app.post("/notas/{nota_id}/fixar-periodo", dependencies=[Depends(require_login)])
def fixar_periodo_nota(nota_id: int, db: Session = Depends(get_db)):
    """Alterna a fixação de uma NF ao período ATIVO.

    - NF ainda não fixada → fixa ao período ativo (passa a contar na cota
      dele independente da data).
    - NF já fixada ao período ativo → solta (volta a valer pela data).
    Idempotente e reversível."""
    nf = db.get(NotaFiscal, nota_id)
    p = periodo_svc.get_periodo_ativo(db)
    if nf is not None and p is not None:
        nf.periodo_id = None if nf.periodo_id == p.id else p.id
        db.commit()
        _reavaliar_alertas(db)
    return RedirectResponse("/dashboard", status_code=303)

