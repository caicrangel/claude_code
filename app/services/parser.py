"""Parsing de NFe pra extrair itens de combustível (diesel)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from lxml import etree

NS = {"nfe": "http://www.portalfiscal.inf.br/nfe"}

# Aceita qualquer NCM da família 2710.19 (óleos diesel e congêneres) +
# fallback por palavra-chave na descrição. Mantenha permissivo: a cota é
# crítica e é melhor falso-positivo que falso-negativo.
NCM_DIESEL_PREFIXES = ("271019",)
DIESEL_KEYWORDS = ("DIESEL", "S10", "S500", "B S10", "B-S10")


@dataclass
class ItemDiesel:
    ncm: str
    cfop: str
    descricao: str
    quantidade: Decimal
    unidade: str
    valor: Decimal


@dataclass
class NFeParsed:
    chave: str
    numero: str
    serie: str
    emit_cnpj: str
    emit_nome: str
    dest_cnpj: str
    data_emissao: datetime | None
    valor_total: Decimal
    itens_diesel: list[ItemDiesel]

    @property
    def litros_diesel(self) -> Decimal:
        # Considera apenas itens com unidade comercial em litros (LT/L/LITRO).
        total = Decimal("0")
        for it in self.itens_diesel:
            unit = it.unidade.upper().strip()
            if unit in ("LT", "L", "LITRO", "LITROS"):
                total += it.quantidade
            else:
                # Fallback: se NCM bate, soma mesmo sem unidade explícita
                total += it.quantidade
        return total


def parse_nfe(xml: bytes) -> NFeParsed | None:
    """Parse de uma NFe completa (procNFe/NFe). Retorna None se for resumo."""
    root = etree.fromstring(xml)
    inf = root.find(".//nfe:infNFe", NS)
    if inf is None:
        return None

    chave = (inf.get("Id") or "")[3:]
    ide = inf.find("nfe:ide", NS)
    emit = inf.find("nfe:emit", NS)
    dest = inf.find("nfe:dest", NS)
    total = inf.find("nfe:total/nfe:ICMSTot", NS)

    numero = ide.findtext("nfe:nNF", "", NS) if ide is not None else ""
    serie = ide.findtext("nfe:serie", "", NS) if ide is not None else ""
    dh = ide.findtext("nfe:dhEmi", "", NS) if ide is not None else ""
    try:
        data_emissao = datetime.fromisoformat(dh) if dh else None
    except ValueError:
        data_emissao = None

    emit_cnpj = emit.findtext("nfe:CNPJ", "", NS) if emit is not None else ""
    emit_nome = emit.findtext("nfe:xNome", "", NS) if emit is not None else ""
    dest_cnpj = dest.findtext("nfe:CNPJ", "", NS) if dest is not None else ""
    valor_total = Decimal(total.findtext("nfe:vNF", "0", NS)) if total is not None else Decimal("0")

    itens: list[ItemDiesel] = []
    for det in inf.findall("nfe:det", NS):
        prod = det.find("nfe:prod", NS)
        if prod is None:
            continue
        ncm = (prod.findtext("nfe:NCM", "", NS) or "").strip()
        cfop = (prod.findtext("nfe:CFOP", "", NS) or "").strip()
        desc = (prod.findtext("nfe:xProd", "", NS) or "").strip()
        ucom = (prod.findtext("nfe:uCom", "", NS) or "").strip()
        qcom = Decimal(prod.findtext("nfe:qCom", "0", NS) or "0")
        vprod = Decimal(prod.findtext("nfe:vProd", "0", NS) or "0")

        is_diesel = (
            any(ncm.startswith(p) for p in NCM_DIESEL_PREFIXES)
            or any(k in desc.upper() for k in DIESEL_KEYWORDS)
        )
        if not is_diesel:
            continue
        itens.append(ItemDiesel(
            ncm=ncm, cfop=cfop, descricao=desc,
            quantidade=qcom, unidade=ucom, valor=vprod,
        ))

    return NFeParsed(
        chave=chave, numero=numero, serie=serie,
        emit_cnpj=emit_cnpj, emit_nome=emit_nome, dest_cnpj=dest_cnpj,
        data_emissao=data_emissao, valor_total=valor_total,
        itens_diesel=itens,
    )
