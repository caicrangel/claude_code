"""Notificações por e-mail acionadas pelo worker.

- notificar_nfs_novas: 1 e-mail agrupado por ciclo do worker contendo todas
  as NFs de diesel que entraram naquele ciclo. Dispara em ingest.processar
  apenas para NFs novas (não para updates de NFs já existentes).
- enviar_panorama_mensal: relatório de fechamento de mês enviado no dia 1
  do mês seguinte. Inclui PDF anexo. Dedup via tabela `state`.
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from decimal import Decimal
from html import escape

from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import runtime_config
from ..format import fmt_data, fmt_data_curta, fmt_litros, fmt_moeda, fmt_pct
from ..models import EmailAlerta, NotaFiscal, get_state, set_state
from . import telegram
from .email import send_email
from .logo_email import bloco_html as bloco_logo
from .logo_email import logo_para_email
from .periodo import get_periodo_ativo
from .quota import cond_pertence_periodo, litros_consumidos, percentual
from .report_pdf import gerar_pdf_panorama

log = logging.getLogger(__name__)

PANORAMA_STATE_PREFIX = "panorama_mensal_"


def _totais_mes(db: Session, *, periodo, inicio: date, fim: date) -> tuple[Decimal, Decimal]:
    """(litros, valor) das NFs na cota, no intervalo do mês. O mês está dentro
    do período, então a data no mês já é pertencimento natural. NFs incluídas
    em OUTROS períodos são datadas fora deste mês, logo não aparecem aqui."""
    row = (
        db.query(
            func.coalesce(func.sum(NotaFiscal.litros_diesel), 0),
            func.coalesce(func.sum(NotaFiscal.valor_total), 0),
        )
        .filter(
            NotaFiscal.data_emissao >= inicio,
            NotaFiscal.data_emissao <= fim,
            NotaFiscal.is_resumo.is_(False),
            NotaFiscal.cancelada.is_(False),
            NotaFiscal.excluida_cota.is_(False),
        )
        .one()
    )
    return Decimal(row[0] or 0), Decimal(row[1] or 0)


def _destinatarios_ativos(db: Session) -> list[str]:
    return [
        e.email for e in db.query(EmailAlerta)
        .filter(EmailAlerta.ativo.is_(True)).all()
        if (e.email or "").strip()
    ]


# ---------- NFs novas (agrupado por ciclo) ----------

def _html_nfs_novas(empresa: str, cnpj: str, nfs: list[NotaFiscal],
                    resumo_cota: dict, logo_html: str = "", acu: dict | None = None) -> str:
    pct = float(resumo_cota.get("pct") or 0)
    consumo = Decimal(str(resumo_cota.get("consumo") or 0))
    cota = Decimal(str(resumo_cota.get("cota") or 0))
    restante = Decimal(str(resumo_cota.get("restante") or 0))
    cor_uso = "#dc2626" if pct >= 95 else ("#d97706" if pct >= 70 else "#16a34a")

    bloco_acu = ""
    if acu:
        linhas_forn = "".join(
            f"""<tr>
              <td style="padding:7px 8px;border-top:1px solid #e2e8f0;font-size:12px;">{escape(f['nome'][:34])}</td>
              <td style="padding:7px 8px;border-top:1px solid #e2e8f0;font-size:12px;text-align:right;">{f['notas']}</td>
              <td style="padding:7px 8px;border-top:1px solid #e2e8f0;font-size:12px;text-align:right;white-space:nowrap;">{fmt_litros(f['litros'])}</td>
              <td style="padding:7px 8px;border-top:1px solid #e2e8f0;font-size:12px;text-align:right;white-space:nowrap;">{fmt_moeda(f['valor'])}</td>
              <td style="padding:7px 8px;border-top:1px solid #e2e8f0;font-size:12px;text-align:right;white-space:nowrap;">{fmt_moeda(f['preco'])}</td>
            </tr>"""
            for f in acu["fornecedores"]
        )
        bloco_acu = f"""
      <div style="margin:22px 0 0;padding:16px 18px;background:#eff6ff;
                  border:1px solid #bfdbfe;border-radius:10px;">
        <div style="font-size:12px;color:#1d4ed8;font-weight:700;
                    text-transform:uppercase;letter-spacing:0.03em;margin-bottom:10px;">
          📆 Compras acumuladas de {escape(acu['mes'])} — por fornecedor
        </div>
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
               style="border-collapse:collapse;background:#ffffff;border-radius:8px;overflow:hidden;">
          <thead>
            <tr style="background:#dbeafe;">
              <th style="padding:7px 8px;text-align:left;font-size:10px;color:#1e40af;text-transform:uppercase;">Fornecedor</th>
              <th style="padding:7px 8px;text-align:right;font-size:10px;color:#1e40af;text-transform:uppercase;">Notas</th>
              <th style="padding:7px 8px;text-align:right;font-size:10px;color:#1e40af;text-transform:uppercase;">Litros</th>
              <th style="padding:7px 8px;text-align:right;font-size:10px;color:#1e40af;text-transform:uppercase;">Valor</th>
              <th style="padding:7px 8px;text-align:right;font-size:10px;color:#1e40af;text-transform:uppercase;">R$/L</th>
            </tr>
          </thead>
          <tbody>{linhas_forn}</tbody>
          <tfoot>
            <tr style="background:#eff6ff;">
              <td style="padding:9px 8px;border-top:2px solid #bfdbfe;font-size:12px;font-weight:800;color:#0f172a;">TOTAL</td>
              <td style="padding:9px 8px;border-top:2px solid #bfdbfe;font-size:12px;font-weight:800;text-align:right;">{acu['total_notas']}</td>
              <td style="padding:9px 8px;border-top:2px solid #bfdbfe;font-size:12px;font-weight:800;text-align:right;white-space:nowrap;">{fmt_litros(acu['total_litros'])}</td>
              <td style="padding:9px 8px;border-top:2px solid #bfdbfe;font-size:12px;font-weight:800;text-align:right;white-space:nowrap;">{fmt_moeda(acu['total_valor'])}</td>
              <td style="padding:9px 8px;border-top:2px solid #bfdbfe;font-size:12px;font-weight:800;text-align:right;white-space:nowrap;color:#1d4ed8;">{fmt_moeda(acu['preco'])}</td>
            </tr>
          </tfoot>
        </table>
      </div>"""

    total_litros = sum((Decimal(nf.litros_diesel or 0) for nf in nfs), Decimal(0))
    total_valor = sum((Decimal(nf.valor_total or 0) for nf in nfs), Decimal(0))

    linhas = "".join(
        f"""
        <tr>
          <td style="padding:8px;border-top:1px solid #e2e8f0;font-size:12px;">{escape(fmt_data(nf.data_emissao) if nf.data_emissao else '—')}</td>
          <td style="padding:8px;border-top:1px solid #e2e8f0;font-size:12px;text-align:right;">{escape(nf.numero or '—')}</td>
          <td style="padding:8px;border-top:1px solid #e2e8f0;font-size:12px;">{escape((nf.emitente_nome or '—')[:40])}</td>
          <td style="padding:8px;border-top:1px solid #e2e8f0;font-size:12px;text-align:right;">{escape(fmt_litros(nf.litros_diesel or 0))}</td>
          <td style="padding:8px;border-top:1px solid #e2e8f0;font-size:12px;text-align:right;">{escape(fmt_moeda(nf.valor_total or 0))}</td>
        </tr>"""
        for nf in nfs
    )

    return f"""\
<!doctype html>
<html lang="pt-br"><head><meta charset="utf-8"></head>
<body style="margin:0;padding:24px;background:#f1f5f9;
             font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
             color:#0f172a;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
         style="max-width:680px;margin:0 auto;background:#ffffff;
                border:1px solid #e2e8f0;border-radius:12px;overflow:hidden;">
    <tr><td style="background:#0284c7;color:#ffffff;padding:18px 24px;
                   font-size:16px;font-weight:700;">
      {len(nfs)} nova{'s' if len(nfs) > 1 else ''} NF de diesel registrada{'s' if len(nfs) > 1 else ''}
    </td></tr>
    <tr><td style="padding:24px;">
      {logo_html}
      <div style="font-size:16px;font-weight:600;">{escape(empresa)}</div>
      <div style="font-size:13px;color:#64748b;margin-bottom:18px;">CNPJ {escape(cnpj)}</div>

      <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
             style="border-collapse:collapse;font-size:13px;">
        <tr>
          <td style="padding:6px 0;color:#64748b;">Total deste lote</td>
          <td style="padding:6px 0;font-weight:600;text-align:right;">
            {fmt_litros(total_litros)} · {fmt_moeda(total_valor)}
          </td>
        </tr>
        <tr>
          <td style="padding:6px 0;color:#64748b;border-top:1px solid #e2e8f0;">Consumo acumulado</td>
          <td style="padding:6px 0;font-weight:600;text-align:right;
                     border-top:1px solid #e2e8f0;color:{cor_uso};">
            {fmt_litros(consumo)} ({fmt_pct(pct)})
          </td>
        </tr>
        <tr>
          <td style="padding:6px 0;color:#64748b;border-top:1px solid #e2e8f0;">Restante da cota</td>
          <td style="padding:6px 0;font-weight:600;text-align:right;
                     border-top:1px solid #e2e8f0;">{fmt_litros(restante)} de {fmt_litros(cota)}</td>
        </tr>
      </table>

      <h3 style="margin:24px 0 8px;font-size:14px;color:#0f172a;">Detalhamento</h3>
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
             style="border-collapse:collapse;">
        <thead>
          <tr style="background:#f8fafc;">
            <th style="padding:8px;text-align:left;font-size:11px;color:#64748b;
                       text-transform:uppercase;">Emissão</th>
            <th style="padding:8px;text-align:right;font-size:11px;color:#64748b;
                       text-transform:uppercase;">Nº NF</th>
            <th style="padding:8px;text-align:left;font-size:11px;color:#64748b;
                       text-transform:uppercase;">Emitente</th>
            <th style="padding:8px;text-align:right;font-size:11px;color:#64748b;
                       text-transform:uppercase;">Litros</th>
            <th style="padding:8px;text-align:right;font-size:11px;color:#64748b;
                       text-transform:uppercase;">Valor</th>
          </tr>
        </thead>
        <tbody>{linhas}</tbody>
      </table>
      {bloco_acu}
    </td></tr>
    <tr><td style="background:#f8fafc;padding:14px 24px;font-size:11px;
                   color:#94a3b8;border-top:1px solid #e2e8f0;">
      E-mail automático do sistema de cota — não responder.
    </td></tr>
  </table>
</body></html>"""


def _texto_nfs_novas(empresa: str, cnpj: str, nfs: list[NotaFiscal],
                     resumo_cota: dict, acu: dict | None = None) -> str:
    pct = float(resumo_cota.get("pct") or 0)
    consumo = Decimal(str(resumo_cota.get("consumo") or 0))
    cota = Decimal(str(resumo_cota.get("cota") or 0))
    total_litros = sum((Decimal(nf.litros_diesel or 0) for nf in nfs), Decimal(0))
    linhas = "\n".join(
        f"  - {fmt_data(nf.data_emissao) if nf.data_emissao else '—'} "
        f"NF {nf.numero or '—'} {nf.emitente_nome or '—'} "
        f"{fmt_litros(nf.litros_diesel or 0)} {fmt_moeda(nf.valor_total or 0)}"
        for nf in nfs
    )
    bloco_acu = ""
    if acu:
        forn = "\n".join(
            f"  - {f['nome']}: {f['notas']} NF · {fmt_litros(f['litros'])} · "
            f"{fmt_moeda(f['valor'])} · {fmt_moeda(f['preco'])}/L"
            for f in acu["fornecedores"]
        )
        bloco_acu = (
            f"\nCompras acumuladas de {acu['mes']} (por fornecedor):\n"
            f"{forn}\n"
            f"  TOTAL: {acu['total_notas']} NF · {fmt_litros(acu['total_litros'])} · "
            f"{fmt_moeda(acu['total_valor'])} · {fmt_moeda(acu['preco'])}/L\n"
        )
    return (
        f"{empresa} (CNPJ {cnpj})\n\n"
        f"{len(nfs)} nova(s) NF de diesel registrada(s):\n"
        f"Total do lote: {fmt_litros(total_litros)}\n\n"
        f"{linhas}\n\n"
        f"Consumo acumulado: {fmt_litros(consumo)} ({fmt_pct(pct)} de {fmt_litros(cota)})\n"
        f"{bloco_acu}"
    )


def _acumulado_mes(db: Session, ref, periodo=None) -> dict:
    """Descritivo do mês-calendário de `ref`, agrupado POR FORNECEDOR
    (notas, litros, valor, R$/L) + totais. Considera só NFs na cota
    (não resumo/cancelada/excluída) e — quando `periodo` é informado —
    apenas as que pertencem ao período de cota ativo (respeita a virada
    de período no meio do mês, ex.: liminar). Zera a cada virada de mês."""
    ini = date(ref.year, ref.month, 1)
    prox = date(ref.year + 1, 1, 1) if ref.month == 12 else date(ref.year, ref.month + 1, 1)
    filtros = [
        NotaFiscal.data_emissao >= ini,
        NotaFiscal.data_emissao < prox,
        NotaFiscal.is_resumo.is_(False),
        NotaFiscal.cancelada.is_(False),
        NotaFiscal.excluida_cota.is_(False),
    ]
    if periodo is not None:
        filtros.append(cond_pertence_periodo(periodo))
    rows = (
        db.query(
            NotaFiscal.emitente_nome,
            NotaFiscal.emitente_cnpj,
            func.count(NotaFiscal.id).label("notas"),
            func.coalesce(func.sum(NotaFiscal.litros_diesel), 0).label("litros"),
            func.coalesce(func.sum(NotaFiscal.valor_total), 0).label("valor"),
        )
        .filter(*filtros)
        .group_by(NotaFiscal.emitente_nome, NotaFiscal.emitente_cnpj)
        .order_by(func.sum(NotaFiscal.litros_diesel).desc())
        .all()
    )
    fornecedores = []
    tot_litros = Decimal(0)
    tot_valor = Decimal(0)
    tot_notas = 0
    for r in rows:
        litros = Decimal(r.litros or 0)
        valor = Decimal(r.valor or 0)
        fornecedores.append({
            "nome": r.emitente_nome or "—",
            "cnpj": r.emitente_cnpj or "—",
            "notas": int(r.notas or 0),
            "litros": litros,
            "valor": valor,
            "preco": (valor / litros) if litros else Decimal(0),
        })
        tot_litros += litros
        tot_valor += valor
        tot_notas += int(r.notas or 0)
    return {
        "mes": ini.strftime("%m/%Y"),
        "fornecedores": fornecedores,
        "total_notas": tot_notas,
        "total_litros": tot_litros,
        "total_valor": tot_valor,
        "preco": (tot_valor / tot_litros) if tot_litros else Decimal(0),
    }


def notificar_nfs_novas(db: Session, nfs: list[NotaFiscal], resumo_cota: dict,
                        *, via_email: bool = True, via_telegram: bool = True) -> int:
    """Envia 1 e-mail/telegram com as NFs informadas. Retorna nº de e-mails OK.
    `via_email`/`via_telegram` escolhem os canais (usado no disparo manual)."""
    if not nfs:
        return 0
    empresa = runtime_config.empresa_nome(db)
    cnpj = runtime_config.cnpj_limpo(db)
    pct = float(resumo_cota.get("pct") or 0)
    total_litros = sum((Decimal(nf.litros_diesel or 0) for nf in nfs), Decimal(0))
    plural = "s" if len(nfs) > 1 else ""

    # Acumulado do mês da NF emitida, respeitando o período de cota ativo
    # (zera a cada virada de mês; não mistura NFs de outro período).
    datas = [nf.data_emissao for nf in nfs if nf.data_emissao]
    ref = max(datas) if datas else date.today()
    acu = _acumulado_mes(db, ref, get_periodo_ativo(db))

    if via_telegram:
        tg_linhas = "\n".join(
            f"• {escape(fmt_data(nf.data_emissao) if nf.data_emissao else '—')} "
            f"NF {escape(nf.numero or '—')} — <b>{escape((nf.emitente_nome or '—')[:40])}</b> "
            f"— {escape(fmt_litros(nf.litros_diesel or 0))}"
            for nf in nfs[:15]
        )
        tg_text = (
            f"🆕 <b>{escape(empresa)}</b>\n"
            f"{len(nfs)} NF de diesel — total {escape(fmt_litros(total_litros))}\n"
            f"Consumo acumulado: <b>{escape(fmt_pct(pct))}</b> da cota\n\n{tg_linhas}"
            + ("\n…" if len(nfs) > 15 else "")
            + f"\n\n📆 <b>Compras de {escape(acu['mes'])} (por fornecedor)</b>\n"
            + "\n".join(
                f"• {escape(f['nome'][:30])}: {escape(fmt_litros(f['litros']))} · "
                f"{escape(fmt_moeda(f['valor']))} · {escape(fmt_moeda(f['preco']))}/L"
                for f in acu["fornecedores"]
            )
            + f"\n<b>TOTAL: {escape(fmt_litros(acu['total_litros']))} · "
            + f"{escape(fmt_moeda(acu['total_valor']))} · "
            + f"{escape(fmt_moeda(acu['preco']))}/L</b>"
        )
        telegram.send_message(tg_text, db=db)

    destinatarios = _destinatarios_ativos(db) if via_email else []
    if not destinatarios:
        return 0
    subject = (f"[Cota Diesel] {empresa} — {len(nfs)} nova{plural} "
               f"NF · {fmt_pct(pct)} da cota")
    logo_src, logo_img = logo_para_email(db)
    text = _texto_nfs_novas(empresa, cnpj, nfs, resumo_cota, acu)
    html = _html_nfs_novas(empresa, cnpj, nfs, resumo_cota, bloco_logo(logo_src), acu)
    inline = [logo_img] if logo_img else None
    enviados = 0
    for dest in destinatarios:
        if send_email(to=dest, subject=subject, body=text, html_body=html,
                      inline_images=inline, db=db):
            enviados += 1
    log.info("Notificação de NFs novas: %d NFs → %d/%d destinatários OK",
             len(nfs), enviados, len(destinatarios))
    return enviados


# ---------- disparo manual: situação atual da cota ----------

def _html_situacao(empresa: str, cnpj: str, periodo, consumo: Decimal,
                   cota: Decimal, pct: float, restante: Decimal,
                   logo_html: str = "") -> str:
    cor_uso = "#dc2626" if pct >= 95 else ("#d97706" if pct >= 70 else "#16a34a")
    pct_fill = min(pct, 100.0)
    return f"""\
<!doctype html>
<html lang="pt-br"><head><meta charset="utf-8"></head>
<body style="margin:0;padding:24px;background:#f1f5f9;
             font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
             color:#0f172a;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
         style="max-width:600px;margin:0 auto;background:#ffffff;
                border:1px solid #e2e8f0;border-radius:12px;overflow:hidden;">
    <tr><td style="background:#0284c7;color:#ffffff;padding:18px 24px;
                   font-size:18px;font-weight:700;">Situação atual da cota</td></tr>
    <tr><td style="padding:24px;">
      {logo_html}
      <div style="font-size:16px;font-weight:600;">{escape(empresa)}</div>
      <div style="font-size:13px;color:#64748b;margin-bottom:20px;">CNPJ {escape(cnpj)}</div>
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
             style="border-collapse:collapse;font-size:14px;">
        <tr><td style="padding:8px 0;color:#64748b;width:55%;">Período de apuração</td>
          <td style="padding:8px 0;font-weight:600;text-align:right;">
            {fmt_data_curta(periodo.inicio)} a {fmt_data_curta(periodo.fim)}</td></tr>
        <tr><td style="padding:8px 0;color:#64748b;border-top:1px solid #e2e8f0;">Cota total</td>
          <td style="padding:8px 0;font-weight:600;text-align:right;
                     border-top:1px solid #e2e8f0;">{fmt_litros(cota)}</td></tr>
        <tr><td style="padding:8px 0;color:#64748b;border-top:1px solid #e2e8f0;">Consumido</td>
          <td style="padding:8px 0;font-weight:600;text-align:right;
                     border-top:1px solid #e2e8f0;color:{cor_uso};">
            {fmt_litros(consumo)} ({fmt_pct(pct)})</td></tr>
        <tr><td style="padding:8px 0;color:#64748b;border-top:1px solid #e2e8f0;">Restante</td>
          <td style="padding:8px 0;font-weight:600;text-align:right;
                     border-top:1px solid #e2e8f0;">{fmt_litros(restante)}</td></tr>
      </table>
      <div style="margin-top:20px;">
        <div style="font-size:12px;color:#64748b;margin-bottom:6px;">Uso da cota</div>
        <div style="background:#e2e8f0;border-radius:6px;height:14px;overflow:hidden;">
          <div style="background:{cor_uso};width:{pct_fill:.1f}%;height:100%;"></div></div>
      </div>
    </td></tr>
    <tr><td style="background:#f8fafc;padding:14px 24px;font-size:11px;
                   color:#94a3b8;border-top:1px solid #e2e8f0;">
      Disparo manual do sistema de cota — não responder.</td></tr>
  </table>
</body></html>"""


def enviar_situacao_cota(db: Session, *, via_email: bool = True,
                         via_telegram: bool = True) -> dict:
    """Disparo manual: envia um snapshot da cota atual (consumo, %, restante)
    pelos canais escolhidos. Não passa por dedup — é acionado sob demanda."""
    p = get_periodo_ativo(db)
    if p is None:
        return {"ok": False, "motivo": "sem-periodo-ativo"}
    consumo = litros_consumidos(db, periodo=p)
    cota = Decimal(p.cota_litros or 0)
    pct = percentual(consumo, cota)
    restante = cota - consumo
    empresa = runtime_config.empresa_nome(db)
    cnpj = runtime_config.cnpj_limpo(db)

    email_ok = 0
    if via_email:
        destinatarios = _destinatarios_ativos(db)
        logo_src, logo_img = logo_para_email(db)
        text = (f"{empresa} (CNPJ {cnpj})\n\nSituação atual da cota\n"
                f"Período: {fmt_data_curta(p.inicio)} a {fmt_data_curta(p.fim)}\n"
                f"Cota: {fmt_litros(cota)}\n"
                f"Consumido: {fmt_litros(consumo)} ({fmt_pct(pct)})\n"
                f"Restante: {fmt_litros(restante)}")
        html = _html_situacao(empresa, cnpj, p, consumo, cota, pct, restante,
                              bloco_logo(logo_src))
        inline = [logo_img] if logo_img else None
        for dest in destinatarios:
            if send_email(to=dest, subject=f"[Cota Diesel] Situação atual — {empresa}",
                          body=text, html_body=html, inline_images=inline, db=db):
                email_ok += 1

    tg_ok = False
    if via_telegram:
        cor = "🔴" if pct >= 95 else ("🟠" if pct >= 70 else "🟢")
        tg_text = (
            f"{cor} <b>{escape(empresa)} — Situação da cota</b>\n"
            f"Consumido: <b>{escape(fmt_litros(consumo))}</b> ({escape(fmt_pct(pct))})\n"
            f"Restante: {escape(fmt_litros(restante))} de {escape(fmt_litros(cota))}\n"
            f"Período: {escape(fmt_data_curta(p.inicio))} a {escape(fmt_data_curta(p.fim))}"
        )
        tg_ok = telegram.send_message(tg_text, db=db)

    log.info("Situação da cota (manual): email=%d telegram=%s", email_ok, tg_ok)
    return {"ok": (email_ok > 0 or tg_ok), "email_ok": email_ok, "telegram": tg_ok,
            "pct": pct}


# ---------- panorama mensal ----------

def _mes_anterior(hoje: date) -> tuple[date, date, str]:
    """Retorna (inicio_mes, fim_mes, chave_dedup) do mês imediatamente anterior a hoje."""
    primeiro_dia_mes_atual = date(hoje.year, hoje.month, 1)
    # último dia do mês anterior = primeiro_dia_mes_atual - 1 dia
    from datetime import timedelta
    ult_mes_anterior = primeiro_dia_mes_atual - timedelta(days=1)
    ini_mes_anterior = date(ult_mes_anterior.year, ult_mes_anterior.month, 1)
    chave = f"{PANORAMA_STATE_PREFIX}{ini_mes_anterior.strftime('%Y-%m')}"
    return ini_mes_anterior, ult_mes_anterior, chave


def enviar_panorama_mensal(db: Session, *, hoje: date | None = None,
                            forcar: bool = False, via_email: bool = True,
                            via_telegram: bool = True) -> dict:
    """Envia panorama do mês ANTERIOR (relatório de fechamento).

    Programado para rodar dia 1 do mês às 08h. Dedup via tabela `state`:
    se já enviou esse mês, retorna sem reenviar (a não ser que forcar=True).
    `via_email`/`via_telegram` permitem escolher os canais (disparo manual)."""
    hoje = hoje or date.today()
    inicio, fim, chave_dedup = _mes_anterior(hoje)

    if not forcar and get_state(db, chave_dedup, ""):
        return {"enviado": False, "motivo": "ja-enviado", "mes": inicio.isoformat()}

    p = get_periodo_ativo(db)
    if p is None:
        return {"enviado": False, "motivo": "sem-periodo-ativo"}

    # Limita o mês de referência ao período ativo (não inventa dados fora dele)
    if fim < p.inicio or inicio > p.fim:
        return {"enviado": False, "motivo": "mes-fora-do-periodo",
                "mes": inicio.isoformat()}
    ini_efetivo = max(inicio, p.inicio)
    fim_efetivo = min(fim, p.fim)

    destinatarios = _destinatarios_ativos(db) if via_email else []
    tg_cfg = telegram._resolver_cfg(db) if via_telegram else None
    if not destinatarios and tg_cfg is None:
        return {"enviado": False, "motivo": "sem-destinatarios"}

    # KPIs do mês de referência (fatia mensal, ciente de fixação de período)
    consumo_mes, valor_mes = _totais_mes(db, periodo=p,
                                         inicio=ini_efetivo, fim=fim_efetivo)
    preco_medio_mes = (valor_mes / consumo_mes) if consumo_mes else Decimal(0)
    # KPIs acumulados do período inteiro até o fim do mês (ciente de fixação)
    consumo_acum = litros_consumidos(db, periodo=p, inicio=p.inicio, fim=fim_efetivo)
    cota = Decimal(p.cota_litros or 0)
    pct_acum = percentual(consumo_acum, cota)
    restante = cota - consumo_acum
    empresa = runtime_config.empresa_nome(db)
    cnpj = runtime_config.cnpj_limpo(db)

    try:
        pdf_bytes = gerar_pdf_panorama(
            db, periodo=p, mes_inicio=ini_efetivo, mes_fim=fim_efetivo,
            consumo_mes=consumo_mes, valor_mes=valor_mes,
            preco_medio_mes=preco_medio_mes,
            consumo_acum=consumo_acum,
            cota=cota, pct_acum=pct_acum, restante=restante,
        )
    except Exception:  # noqa: BLE001
        log.exception("Falha gerando PDF do panorama mensal — enviando sem anexo")
        pdf_bytes = None

    mes_label = inicio.strftime("%m/%Y")
    subject = f"[Cota Diesel] Panorama de {mes_label} — {empresa}"
    text = (
        f"{empresa} (CNPJ {cnpj})\n\n"
        f"Panorama de fechamento — {mes_label}\n"
        f"Período de apuração: {fmt_data_curta(p.inicio)} a {fmt_data_curta(p.fim)}\n\n"
        f"Consumido no mês: {fmt_litros(consumo_mes)}\n"
        f"Valor gasto no mês: {fmt_moeda(valor_mes)}\n"
        f"Preço médio do litro: {fmt_moeda(preco_medio_mes)}\n\n"
        f"Consumido acumulado: {fmt_litros(consumo_acum)} ({fmt_pct(pct_acum)})\n"
        f"Restante da cota: {fmt_litros(restante)} de {fmt_litros(cota)}\n\n"
        f"Veja o PDF anexo para o relatório completo com gráficos."
    )
    logo_src, logo_img = logo_para_email(db)
    html = _html_panorama(empresa, cnpj, p, ini_efetivo, fim_efetivo,
                          consumo_mes, valor_mes, preco_medio_mes,
                          consumo_acum, cota, pct_acum, restante,
                          bloco_logo(logo_src))
    anexos = []
    if pdf_bytes:
        nome_pdf = f"panorama-{inicio.strftime('%Y-%m')}.pdf"
        anexos.append((nome_pdf, pdf_bytes, "application/pdf"))
    inline = [logo_img] if logo_img else None

    enviados = 0
    for dest in destinatarios:
        if send_email(to=dest, subject=subject, body=text, html_body=html,
                      attachments=anexos or None, inline_images=inline, db=db):
            enviados += 1

    # Telegram: resumo + PDF (se houver)
    tg_text = (
        f"📊 <b>{escape(empresa)} — Panorama de {escape(mes_label)}</b>\n"
        f"Consumido no mês: <b>{escape(fmt_litros(consumo_mes))}</b> "
        f"({escape(fmt_moeda(valor_mes))})\n"
        f"Preço médio: {escape(fmt_moeda(preco_medio_mes))}/L\n"
        f"Acumulado: <b>{escape(fmt_pct(pct_acum))}</b> "
        f"({escape(fmt_litros(consumo_acum))})\n"
        f"Restante: {escape(fmt_litros(restante))} de {escape(fmt_litros(cota))}"
    )
    tg_ok = False
    if tg_cfg is not None:
        tg_ok = telegram.send_message(tg_text, db=db)
        if pdf_bytes:
            telegram.send_document(f"panorama-{inicio.strftime('%Y-%m')}.pdf",
                                   pdf_bytes, caption=f"Panorama {mes_label}", db=db)

    if enviados or tg_ok:
        set_state(db, chave_dedup, datetime.utcnow().isoformat())

    log.info("Panorama mensal %s: e-mail %d/%d, telegram=%s",
             mes_label, enviados, len(destinatarios), tg_ok)
    return {"enviado": (enviados > 0 or tg_ok), "mes": inicio.isoformat(),
            "destinatarios": len(destinatarios), "enviados_ok": enviados,
            "telegram": tg_ok}


def _html_panorama(empresa: str, cnpj: str, periodo, ini_mes: date, fim_mes: date,
                   consumo_mes: Decimal, valor_mes: Decimal,
                   preco_medio_mes: Decimal, consumo_acum: Decimal,
                   cota: Decimal, pct_acum: float, restante: Decimal,
                   logo_html: str = "") -> str:
    cor_uso = "#dc2626" if pct_acum >= 95 else ("#d97706" if pct_acum >= 70 else "#16a34a")
    pct_fill = min(pct_acum, 100.0)
    mes_label = ini_mes.strftime("%m/%Y")
    return f"""\
<!doctype html>
<html lang="pt-br"><head><meta charset="utf-8"></head>
<body style="margin:0;padding:24px;background:#f1f5f9;
             font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
             color:#0f172a;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
         style="max-width:600px;margin:0 auto;background:#ffffff;
                border:1px solid #e2e8f0;border-radius:12px;overflow:hidden;">
    <tr><td style="background:#0f172a;color:#ffffff;padding:18px 24px;
                   font-size:18px;font-weight:700;">
      Panorama de {escape(mes_label)}
    </td></tr>
    <tr><td style="padding:24px;">
      {logo_html}
      <div style="font-size:16px;font-weight:600;">{escape(empresa)}</div>
      <div style="font-size:13px;color:#64748b;margin-bottom:20px;">CNPJ {escape(cnpj)}</div>

      <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
             style="border-collapse:collapse;font-size:14px;">
        <tr>
          <td style="padding:8px 0;color:#64748b;width:55%;">Mês de referência</td>
          <td style="padding:8px 0;font-weight:600;text-align:right;">
            {fmt_data_curta(ini_mes)} a {fmt_data_curta(fim_mes)}
          </td>
        </tr>
        <tr>
          <td style="padding:8px 0;color:#64748b;border-top:1px solid #e2e8f0;">Consumido no mês</td>
          <td style="padding:8px 0;font-weight:600;text-align:right;
                     border-top:1px solid #e2e8f0;">{fmt_litros(consumo_mes)}</td>
        </tr>
        <tr>
          <td style="padding:8px 0;color:#64748b;border-top:1px solid #e2e8f0;">Valor gasto no mês</td>
          <td style="padding:8px 0;font-weight:600;text-align:right;
                     border-top:1px solid #e2e8f0;">{fmt_moeda(valor_mes)}</td>
        </tr>
        <tr>
          <td style="padding:8px 0;color:#64748b;border-top:1px solid #e2e8f0;">Preço médio do litro</td>
          <td style="padding:8px 0;font-weight:600;text-align:right;
                     border-top:1px solid #e2e8f0;">{fmt_moeda(preco_medio_mes)}</td>
        </tr>
        <tr>
          <td style="padding:8px 0;color:#64748b;border-top:1px solid #e2e8f0;">Acumulado no período</td>
          <td style="padding:8px 0;font-weight:600;text-align:right;
                     border-top:1px solid #e2e8f0;color:{cor_uso};">
            {fmt_litros(consumo_acum)} ({fmt_pct(pct_acum)})
          </td>
        </tr>
        <tr>
          <td style="padding:8px 0;color:#64748b;border-top:1px solid #e2e8f0;">Cota total</td>
          <td style="padding:8px 0;font-weight:600;text-align:right;
                     border-top:1px solid #e2e8f0;">{fmt_litros(cota)}</td>
        </tr>
        <tr>
          <td style="padding:8px 0;color:#64748b;border-top:1px solid #e2e8f0;">Restante</td>
          <td style="padding:8px 0;font-weight:600;text-align:right;
                     border-top:1px solid #e2e8f0;">{fmt_litros(restante)}</td>
        </tr>
      </table>

      <div style="margin-top:20px;">
        <div style="font-size:12px;color:#64748b;margin-bottom:6px;">Uso acumulado da cota</div>
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
