"""Geração do PDF de alerta de cota com gráficos.

Usa matplotlib (PdfPages) — sem reportlab — para reduzir dependências.
Layout em duas páginas:
  1) Resumo: cabeçalho, KPIs e gauge de uso da cota.
  2) Gráficos: top fornecedores + consumo mensal.

Todos os números seguem formato pt-BR via app.format.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from io import BytesIO

import matplotlib

matplotlib.use("Agg")  # backend não-interativo, obrigatório em servidor
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.patches import Rectangle
from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import runtime_config
from ..format import fmt_data_curta, fmt_inteiro, fmt_litros, fmt_moeda, fmt_pct
from ..models import CotaPeriodo, NotaFiscal


def _base_query(db: Session, periodo: CotaPeriodo):
    # Import local para evitar ciclo (quota importa report_pdf).
    from .quota import cond_pertence_periodo
    return db.query(NotaFiscal).filter(
        cond_pertence_periodo(periodo),
        NotaFiscal.is_resumo.is_(False),
        NotaFiscal.cancelada.is_(False),
        NotaFiscal.excluida_cota.is_(False),
    )


def _desenhar_logo(fig, db: Session) -> None:
    """Desenha a logo da empresa no canto superior direito da página.
    Silencioso se não houver logo ou o arquivo não puder ser lido."""
    from .logo_email import logo_para_email
    _, inline = logo_para_email(db)  # (cid, bytes, mimetype) para arquivo local
    if inline is None:
        return
    try:
        import matplotlib.image as mpimg
        img = mpimg.imread(BytesIO(inline[1]))
    except Exception:  # noqa: BLE001
        return
    # Caixa em coords da figura (canto sup. direito), sem distorcer (aspect equal).
    ax = fig.add_axes([0.60, 0.895, 0.32, 0.06], zorder=10)
    ax.imshow(img)
    ax.set_aspect("equal")
    ax.set_anchor("E")
    ax.axis("off")


def _top_fornecedores(db: Session, periodo: CotaPeriodo, limite: int = 10):
    rows = (
        _base_query(db, periodo)
        .with_entities(
            NotaFiscal.emitente_nome,
            NotaFiscal.emitente_cnpj,
            func.sum(NotaFiscal.litros_diesel).label("litros"),
        )
        .group_by(NotaFiscal.emitente_nome, NotaFiscal.emitente_cnpj)
        .order_by(func.sum(NotaFiscal.litros_diesel).desc())
        .limit(limite)
        .all()
    )
    return rows


def _consumo_mensal(db: Session, periodo: CotaPeriodo):
    rows = (
        _base_query(db, periodo)
        .with_entities(
            func.date_trunc("month", NotaFiscal.data_emissao).label("mes"),
            func.sum(NotaFiscal.litros_diesel).label("litros"),
        )
        .group_by("mes")
        .order_by("mes")
        .all()
    )
    return rows


# ---------- páginas do PDF ----------

def _pagina_resumo(pdf: PdfPages, *, db: Session, periodo: CotaPeriodo,
                   consumo: Decimal, cota: Decimal, pct: float, restante: Decimal,
                   threshold: int) -> None:
    fig, ax = plt.subplots(figsize=(8.27, 11.69))  # A4 retrato
    ax.axis("off")
    _desenhar_logo(fig, db)

    # Título
    fig.text(0.08, 0.94, "Relatório de Cota — Diesel", fontsize=20, fontweight="bold",
             color="#0f172a")
    fig.text(0.08, 0.905, runtime_config.empresa_nome(db), fontsize=14, color="#334155")
    fig.text(0.08, 0.885, f"CNPJ {runtime_config.cnpj_limpo(db)}", fontsize=10, color="#64748b")

    # Faixa de alerta
    cor_alerta = "#dc2626" if threshold >= 100 else ("#d97706" if threshold >= 85 else "#0284c7")
    fig.patches.append(Rectangle((0.08, 0.83), 0.84, 0.035, transform=fig.transFigure,
                                 facecolor=cor_alerta, edgecolor="none"))
    fig.text(0.5, 0.847, f"Limite atingido: {threshold}% da cota",
             fontsize=14, fontweight="bold", color="white", ha="center", va="center")

    # Bloco de informações
    y = 0.78
    linhas = [
        ("Período de apuração",
         f"{fmt_data_curta(periodo.inicio)}  a  {fmt_data_curta(periodo.fim)}"),
        ("Cota total",        fmt_litros(cota)),
        ("Consumido",         f"{fmt_litros(consumo)}  ({fmt_pct(pct)})"),
        ("Restante",          fmt_litros(restante)),
    ]
    for label, valor in linhas:
        fig.text(0.08, y, label, fontsize=10, color="#64748b")
        fig.text(0.08, y - 0.025, valor, fontsize=15, fontweight="bold", color="#0f172a")
        y -= 0.07

    # Gauge horizontal — barra de uso
    gauge_x, gauge_y, gauge_w, gauge_h = 0.08, 0.40, 0.84, 0.04
    fig.patches.append(Rectangle((gauge_x, gauge_y), gauge_w, gauge_h,
                                 transform=fig.transFigure,
                                 facecolor="#e2e8f0", edgecolor="#cbd5e1"))
    pct_clamp = min(pct, 100.0) / 100.0
    cor_fill = "#dc2626" if pct >= 95 else ("#d97706" if pct >= 70 else "#16a34a")
    fig.patches.append(Rectangle((gauge_x, gauge_y), gauge_w * pct_clamp, gauge_h,
                                 transform=fig.transFigure,
                                 facecolor=cor_fill, edgecolor="none"))
    fig.text(0.08, gauge_y + gauge_h + 0.015, "Uso da cota", fontsize=10, color="#64748b")
    fig.text(0.92, gauge_y + gauge_h + 0.015, fmt_pct(pct), fontsize=12,
             fontweight="bold", color=cor_fill, ha="right")

    # Rodapé
    fig.text(0.08, 0.04,
             "Relatório gerado automaticamente pelo sistema de cota — "
             f"{fmt_data_curta(date.today())}",
             fontsize=8, color="#94a3b8")

    pdf.savefig(fig)
    plt.close(fig)


def _grafico_fornecedores(ax, fornecedores) -> None:
    if not fornecedores:
        ax.text(0.5, 0.5, "Sem dados de fornecedores no período",
                ha="center", va="center", fontsize=11, color="#94a3b8",
                transform=ax.transAxes)
        ax.axis("off")
        return
    def _abrev(s: str, lim: int = 26) -> str:
        s = s or "—"
        return s if len(s) <= lim else s[:lim - 1].rstrip() + "…"
    nomes = [_abrev(f.emitente_nome or f.emitente_cnpj or "—") for f in fornecedores]
    litros = [float(f.litros or 0) for f in fornecedores]
    nomes_inv = list(reversed(nomes))
    litros_inv = list(reversed(litros))
    bars = ax.barh(nomes_inv, litros_inv, color="#0284c7")
    ax.set_title("Top fornecedores (litros)", fontsize=12, fontweight="bold",
                 color="#0f172a", loc="left", pad=10)
    ax.tick_params(axis="y", labelsize=9)
    ax.tick_params(axis="x", labelsize=8)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.xaxis.set_major_formatter(
        plt.FuncFormatter(lambda v, _: fmt_inteiro(v)))
    for bar, valor in zip(bars, litros_inv):
        ax.text(bar.get_width(), bar.get_y() + bar.get_height() / 2,
                f"  {fmt_litros(valor)}", va="center", fontsize=8, color="#334155")


def _grafico_mensal(ax, mensal) -> None:
    if not mensal:
        ax.text(0.5, 0.5, "Sem dados mensais no período",
                ha="center", va="center", fontsize=11, color="#94a3b8",
                transform=ax.transAxes)
        ax.axis("off")
        return
    labels = [m.mes.strftime("%m/%Y") if m.mes else "—" for m in mensal]
    litros = [float(m.litros or 0) for m in mensal]
    bars = ax.bar(labels, litros, color="#16a34a")
    ax.set_title("Consumo mensal (litros)", fontsize=12, fontweight="bold",
                 color="#0f172a", loc="left", pad=10)
    ax.tick_params(axis="x", labelsize=9, rotation=0)
    ax.tick_params(axis="y", labelsize=8)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.yaxis.set_major_formatter(
        plt.FuncFormatter(lambda v, _: fmt_inteiro(v)))
    for bar, valor in zip(bars, litros):
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height(),
                fmt_litros(valor), ha="center", va="bottom", fontsize=8, color="#334155")


def _pagina_graficos(pdf: PdfPages, *, db: Session, periodo: CotaPeriodo) -> None:
    fornecedores = _top_fornecedores(db, periodo)
    mensal = _consumo_mensal(db, periodo)
    fig, axes = plt.subplots(2, 1, figsize=(8.27, 11.69),
                             gridspec_kw={"height_ratios": [1.2, 1]})
    fig.subplots_adjust(left=0.30, right=0.94, top=0.94, bottom=0.08, hspace=0.35)
    _grafico_fornecedores(axes[0], fornecedores)
    _grafico_mensal(axes[1], mensal)
    pdf.savefig(fig)
    plt.close(fig)


# ---------- API pública ----------

def gerar_pdf_alerta(
    db: Session,
    *,
    periodo: CotaPeriodo,
    consumo: Decimal,
    cota: Decimal,
    pct: float,
    restante: Decimal,
    threshold: int,
) -> bytes:
    """Gera o PDF de alerta. Retorna bytes para anexar no e-mail."""
    buf = BytesIO()
    with PdfPages(buf) as pdf:
        _pagina_resumo(pdf, db=db, periodo=periodo, consumo=consumo, cota=cota,
                       pct=pct, restante=restante, threshold=threshold)
        _pagina_graficos(pdf, db=db, periodo=periodo)
    return buf.getvalue()


def _pagina_panorama(pdf: PdfPages, *, db: Session, periodo: CotaPeriodo,
                     mes_inicio: date, mes_fim: date,
                     consumo_mes: Decimal, valor_mes: Decimal,
                     preco_medio_mes: Decimal,
                     consumo_acum: Decimal,
                     cota: Decimal, pct_acum: float, restante: Decimal) -> None:
    """Página resumo do panorama mensal — sem faixa de alerta de limite."""
    fig, ax = plt.subplots(figsize=(8.27, 11.69))
    ax.axis("off")
    _desenhar_logo(fig, db)

    fig.text(0.08, 0.94, "Panorama Mensal — Diesel", fontsize=20, fontweight="bold",
             color="#0f172a")
    fig.text(0.08, 0.905, runtime_config.empresa_nome(db), fontsize=14, color="#334155")
    fig.text(0.08, 0.885, f"CNPJ {runtime_config.cnpj_limpo(db)}", fontsize=10, color="#64748b")

    fig.patches.append(Rectangle((0.08, 0.83), 0.84, 0.035, transform=fig.transFigure,
                                 facecolor="#0f172a", edgecolor="none"))
    fig.text(0.5, 0.847,
             f"Mês de referência: {fmt_data_curta(mes_inicio)} a {fmt_data_curta(mes_fim)}",
             fontsize=13, fontweight="bold", color="white", ha="center", va="center")

    y = 0.78
    linhas = [
        ("Período de apuração",
         f"{fmt_data_curta(periodo.inicio)}  a  {fmt_data_curta(periodo.fim)}"),
        ("Consumido no mês", fmt_litros(consumo_mes)),
        ("Valor gasto no mês", fmt_moeda(valor_mes)),
        ("Preço médio do litro", fmt_moeda(preco_medio_mes)),
        ("Acumulado no período", f"{fmt_litros(consumo_acum)}  ({fmt_pct(pct_acum)})"),
        ("Cota total", fmt_litros(cota)),
        ("Restante", fmt_litros(restante)),
    ]
    for label, valor in linhas:
        fig.text(0.08, y, label, fontsize=10, color="#64748b")
        fig.text(0.08, y - 0.025, valor, fontsize=15, fontweight="bold", color="#0f172a")
        y -= 0.052

    gauge_x, gauge_y, gauge_w, gauge_h = 0.08, 0.30, 0.84, 0.04
    fig.patches.append(Rectangle((gauge_x, gauge_y), gauge_w, gauge_h,
                                 transform=fig.transFigure,
                                 facecolor="#e2e8f0", edgecolor="#cbd5e1"))
    pct_clamp = min(pct_acum, 100.0) / 100.0
    cor_fill = "#dc2626" if pct_acum >= 95 else ("#d97706" if pct_acum >= 70 else "#16a34a")
    fig.patches.append(Rectangle((gauge_x, gauge_y), gauge_w * pct_clamp, gauge_h,
                                 transform=fig.transFigure,
                                 facecolor=cor_fill, edgecolor="none"))
    fig.text(0.08, gauge_y + gauge_h + 0.015, "Uso acumulado da cota",
             fontsize=10, color="#64748b")
    fig.text(0.92, gauge_y + gauge_h + 0.015, fmt_pct(pct_acum), fontsize=12,
             fontweight="bold", color=cor_fill, ha="right")

    fig.text(0.08, 0.04,
             f"Relatório gerado automaticamente — {fmt_data_curta(date.today())}",
             fontsize=8, color="#94a3b8")

    pdf.savefig(fig)
    plt.close(fig)


def gerar_pdf_panorama(
    db: Session,
    *,
    periodo: CotaPeriodo,
    mes_inicio: date,
    mes_fim: date,
    consumo_mes: Decimal,
    valor_mes: Decimal,
    preco_medio_mes: Decimal,
    consumo_acum: Decimal,
    cota: Decimal,
    pct_acum: float,
    restante: Decimal,
) -> bytes:
    """Panorama mensal (fechamento de mês). Reaproveita os gráficos do alerta."""
    buf = BytesIO()
    with PdfPages(buf) as pdf:
        _pagina_panorama(pdf, db=db, periodo=periodo,
                         mes_inicio=mes_inicio, mes_fim=mes_fim,
                         consumo_mes=consumo_mes, valor_mes=valor_mes,
                         preco_medio_mes=preco_medio_mes,
                         consumo_acum=consumo_acum,
                         cota=cota, pct_acum=pct_acum, restante=restante)
        _pagina_graficos(pdf, db=db, periodo=periodo)
    return buf.getvalue()
