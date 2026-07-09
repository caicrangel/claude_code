"""Cota e disparo de alertas."""
from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal
from html import escape

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from .. import runtime_config
from ..format import fmt_data_curta, fmt_litros, fmt_pct
from ..models import Alerta, CotaPeriodo, EmailAlerta, NotaFiscal, NotaPeriodo
from .email import send_email
from .logo_email import bloco_html as bloco_logo
from .logo_email import logo_para_email
from .periodo import get_periodo_ativo
from .report_pdf import gerar_pdf_alerta

log = logging.getLogger(__name__)


def cond_pertence_periodo(periodo: CotaPeriodo, inicio: date | None = None,
                          fim: date | None = None):
    """Condição SQLAlchemy: a NF pertence a este período para efeito de cota.

    Modelo ADITIVO:
      - Pertencimento natural: data_emissao dentro do intervalo do período, OU
      - Inclusão explícita: existe linha em nota_periodo (nota, período).

    A inclusão é ADITIVA — não remove a NF do período natural dela. Assim a
    mesma NF pode compor a cota de dois períodos (ex.: período inicial +
    cota renovada por liminar). `inicio`/`fim` restringem o intervalo de
    datas (relatórios); as NFs incluídas explicitamente entram sempre."""
    ini = inicio if inicio is not None else periodo.inicio
    f = fim if fim is not None else periodo.fim
    incluidas = select(NotaPeriodo.nota_id).where(
        NotaPeriodo.periodo_id == periodo.id)
    return or_(
        and_(
            NotaFiscal.data_emissao >= ini,
            NotaFiscal.data_emissao <= f,
        ),
        NotaFiscal.id.in_(incluidas),
    )


def incluir_nota_periodo(db: Session, nota_id: int, periodo_id: int) -> None:
    """Inclui a NF no período (idempotente)."""
    existe = db.get(NotaPeriodo, {"nota_id": nota_id, "periodo_id": periodo_id})
    if existe is None:
        db.add(NotaPeriodo(nota_id=nota_id, periodo_id=periodo_id))
        db.commit()


def remover_nota_periodo(db: Session, nota_id: int, periodo_id: int) -> None:
    """Remove a inclusão da NF no período (idempotente)."""
    existe = db.get(NotaPeriodo, {"nota_id": nota_id, "periodo_id": periodo_id})
    if existe is not None:
        db.delete(existe)
        db.commit()


def esta_incluida(db: Session, nota_id: int, periodo_id: int) -> bool:
    return db.get(NotaPeriodo, {"nota_id": nota_id, "periodo_id": periodo_id}) is not None


def incluidas_do_periodo(db: Session, periodo_id: int) -> set[int]:
    """IDs das NFs incluídas EXPLICITAMENTE neste período."""
    rows = db.query(NotaPeriodo.nota_id).filter(
        NotaPeriodo.periodo_id == periodo_id).all()
    return {r[0] for r in rows}


def litros_consumidos(db: Session, *, periodo: CotaPeriodo | None = None,
                      inicio: date | None = None, fim: date | None = None) -> Decimal:
    if periodo is None:
        periodo = get_periodo_ativo(db)
        if periodo is None:
            return Decimal(0)
    total = (
        db.query(func.coalesce(func.sum(NotaFiscal.litros_diesel), 0))
        .filter(
            cond_pertence_periodo(periodo, inicio, fim),
            NotaFiscal.is_resumo.is_(False),
            NotaFiscal.cancelada.is_(False),
            NotaFiscal.excluida_cota.is_(False),
        )
        .scalar()
    )
    return Decimal(total or 0)


def percentual(consumo: Decimal, cota: Decimal) -> float:
    if cota <= 0:
        return 0.0
    return float((consumo / cota) * 100)


def _montar_corpo_texto(db: Session, p: CotaPeriodo, consumo: Decimal, cota: Decimal,
                        pct: float, restante: Decimal, thr: int) -> str:
    nome = runtime_config.empresa_nome(db)
    cnpj = runtime_config.cnpj_limpo(db)
    return (
        f"Empresa: {nome} (CNPJ {cnpj})\n"
        f"Período: {fmt_data_curta(p.inicio)} a {fmt_data_curta(p.fim)}\n"
        f"Cota: {fmt_litros(cota)}\n"
        f"Consumido: {fmt_litros(consumo)} ({fmt_pct(pct)})\n"
        f"Restante: {fmt_litros(restante)}\n\n"
        f"Limite atingido: {thr}% da cota.\n\n"
        f"Veja o PDF anexo para o relatório completo com gráficos."
    )


def _montar_corpo_html(db: Session, p: CotaPeriodo, consumo: Decimal, cota: Decimal,
                       pct: float, restante: Decimal, thr: int,
                       logo_html: str = "") -> str:
    cor_alerta = "#dc2626" if thr >= 100 else ("#d97706" if thr >= 85 else "#0284c7")
    cor_uso = "#dc2626" if pct >= 95 else ("#d97706" if pct >= 70 else "#16a34a")
    pct_fill = min(pct, 100.0)
    empresa = escape(runtime_config.empresa_nome(db))
    cnpj = escape(runtime_config.cnpj_limpo(db))
    return f"""\
<!doctype html>
<html lang="pt-br"><head><meta charset="utf-8"></head>
<body style="margin:0;padding:24px;background:#f1f5f9;
             font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
             color:#0f172a;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
         style="max-width:600px;margin:0 auto;background:#ffffff;
                border:1px solid #e2e8f0;border-radius:12px;overflow:hidden;">
    <tr><td style="background:{cor_alerta};color:#ffffff;
                   padding:18px 24px;font-size:18px;font-weight:700;">
      Limite atingido: {thr}% da cota
    </td></tr>
    <tr><td style="padding:24px;">
      {logo_html}
      <div style="font-size:16px;font-weight:600;margin-bottom:4px;">{empresa}</div>
      <div style="font-size:13px;color:#64748b;margin-bottom:20px;">CNPJ {cnpj}</div>

      <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
             style="border-collapse:collapse;font-size:14px;">
        <tr>
          <td style="padding:8px 0;color:#64748b;width:45%;">Período de apuração</td>
          <td style="padding:8px 0;font-weight:600;text-align:right;">
            {fmt_data_curta(p.inicio)} a {fmt_data_curta(p.fim)}
          </td>
        </tr>
        <tr>
          <td style="padding:8px 0;color:#64748b;border-top:1px solid #e2e8f0;">Cota total</td>
          <td style="padding:8px 0;font-weight:600;text-align:right;
                     border-top:1px solid #e2e8f0;">{fmt_litros(cota)}</td>
        </tr>
        <tr>
          <td style="padding:8px 0;color:#64748b;border-top:1px solid #e2e8f0;">Consumido</td>
          <td style="padding:8px 0;font-weight:600;text-align:right;
                     border-top:1px solid #e2e8f0;color:{cor_uso};">
            {fmt_litros(consumo)} ({fmt_pct(pct)})
          </td>
        </tr>
        <tr>
          <td style="padding:8px 0;color:#64748b;border-top:1px solid #e2e8f0;">Restante</td>
          <td style="padding:8px 0;font-weight:600;text-align:right;
                     border-top:1px solid #e2e8f0;">{fmt_litros(restante)}</td>
        </tr>
      </table>

      <div style="margin-top:20px;">
        <div style="font-size:12px;color:#64748b;margin-bottom:6px;">Uso da cota</div>
        <div style="background:#e2e8f0;border-radius:6px;height:14px;overflow:hidden;">
          <div style="background:{cor_uso};width:{pct_fill:.1f}%;height:100%;"></div>
        </div>
      </div>

      <p style="margin:24px 0 0;font-size:13px;color:#64748b;line-height:1.5;">
        O relatório completo com gráficos de fornecedores e consumo mensal
        está disponível no <strong>PDF anexo</strong>.
      </p>
    </td></tr>
    <tr><td style="background:#f8fafc;padding:14px 24px;font-size:11px;
                   color:#94a3b8;border-top:1px solid #e2e8f0;">
      E-mail automático do sistema de cota — não responder.
    </td></tr>
  </table>
</body></html>"""


def avaliar_e_alertar(db: Session) -> dict:
    p = get_periodo_ativo(db)
    if p is None:
        return {"consumo": 0.0, "cota": 0.0, "pct": 0.0, "restante": 0.0,
                "alertas_disparados": []}
    consumo = litros_consumidos(db, periodo=p)
    cota = Decimal(p.cota_litros or 0)
    pct = percentual(consumo, cota)
    restante = cota - consumo
    periodo_iso = p.inicio.isoformat()

    disparados: list[int] = []
    destinatarios = [
        e.email for e in db.query(EmailAlerta)
        .filter(EmailAlerta.ativo.is_(True)).all()
        if (e.email or "").strip()
    ]
    nome_empresa = runtime_config.empresa_nome(db)
    logo_src, logo_img = logo_para_email(db)
    logo_html = bloco_logo(logo_src)
    for thr in runtime_config.thresholds(db):
        if pct < thr:
            continue
        existe = db.query(Alerta).filter(
            Alerta.threshold_pct == thr,
            Alerta.periodo_inicio_iso == periodo_iso,
        ).first()
        if existe:
            continue
        msg_text = _montar_corpo_texto(db, p, consumo, cota, pct, restante, thr)
        msg_html = _montar_corpo_html(db, p, consumo, cota, pct, restante, thr, logo_html)
        try:
            pdf_bytes = gerar_pdf_alerta(
                db, periodo=p, consumo=consumo, cota=cota,
                pct=pct, restante=restante, threshold=thr,
            )
        except Exception:  # noqa: BLE001
            log.exception("Falha gerando PDF do alerta — enviando e-mail sem anexo")
            pdf_bytes = None
        anexos = []
        if pdf_bytes:
            nome_pdf = f"alerta-cota-{p.inicio.isoformat()}-{thr}pct.pdf"
            anexos.append((nome_pdf, pdf_bytes, "application/pdf"))
        ok_any = False
        for dest in destinatarios:
            ok = send_email(
                to=dest,
                subject=f"[Cota Diesel] {nome_empresa} atingiu {thr}%",
                body=msg_text,
                html_body=msg_html,
                attachments=anexos or None,
                inline_images=[logo_img] if logo_img else None,
                db=db,
            )
            ok_any = ok_any or ok
        if not destinatarios:
            log.warning("Sem destinatários ativos em email_alertas — alerta %d%% não enviado", thr)
        db.add(Alerta(
            threshold_pct=thr,
            litros_consumidos=consumo,
            canal="email" if ok_any else "log",
            mensagem=msg_text,
            periodo_inicio_iso=periodo_iso,
        ))
        disparados.append(thr)
    if disparados:
        db.commit()
    return {
        "consumo": float(consumo),
        "cota": float(cota),
        "pct": pct,
        "restante": float(restante),
        "alertas_disparados": disparados,
    }
