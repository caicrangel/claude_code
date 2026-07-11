"""Parsing de documentos vindos do NFeDistribuicaoDFe.

Três tipos de docZip podem chegar:
  - procNFe / NFe        → NFe completa (parse_nfe)
  - resNFe               → resumo (parse_resumo) - sem itens, só cabeçalho
  - procEventoNFe        → evento (parse_evento) - cancelamento etc.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from lxml import etree

NS = {"nfe": "http://www.portalfiscal.inf.br/nfe"}

# Filtro de diesel: NCM da família 2710.19 (óleos diesel) E descrição contendo
# a palavra "DIESEL". Manter ambos evita falsos-positivos (ex: lubrificantes
# que também caem em 271019 mas não são combustível).
NCM_DIESEL_PREFIXES = ("271019",)

# Unidades de volume (litros) aceitas para contabilizar a galonagem. Emitentes
# usam variações: LT, LTS, L, LTR, LITRO, LITROS, LT. etc. Comparação após
# upper() + strip() + remoção de ponto final.
UNIDADES_LITRO = {"LT", "LTS", "L", "LTR", "LTRS", "LITRO", "LITROS", "LITRO(S)"}


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
    natureza_operacao: str
    itens_diesel: list[ItemDiesel]

    @property
    def litros_diesel(self) -> Decimal:
        # Soma apenas itens com unidade de volume reconhecida (litros).
        # Ignora itens em kg, m³, etc — precisam ser litros explícitos.
        total = Decimal("0")
        for it in self.itens_diesel:
            unit = it.unidade.upper().strip().rstrip(".").strip()
            if unit in UNIDADES_LITRO:
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
    nat_op = ide.findtext("nfe:natOp", "", NS).strip() if ide is not None else ""
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

        desc_upper = desc.upper()
        has_diesel_word = "DIESEL" in desc_upper
        has_diesel_ncm = any(ncm.startswith(p) for p in NCM_DIESEL_PREFIXES)
        is_diesel = has_diesel_word and has_diesel_ncm

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
        natureza_operacao=nat_op,
        itens_diesel=itens,
    )


@dataclass
class ResumoParsed:
    chave: str
    numero: str
    serie: str
    emit_cnpj: str
    emit_nome: str
    data_emissao: datetime | None
    valor_total: Decimal


def parse_resumo(xml: bytes) -> ResumoParsed | None:
    """Parse de um resNFe (resumo da NFe)."""
    root = etree.fromstring(xml)
    # resNFe é o root
    if not root.tag.endswith("resNFe"):
        return None
    chave = (root.findtext("nfe:chNFe", "", NS) or "").strip()
    if not chave:
        return None
    emit_cnpj = (root.findtext("nfe:CNPJ", "", NS) or "").strip()
    emit_nome = (root.findtext("nfe:xNome", "", NS) or "").strip()
    dh = (root.findtext("nfe:dhEmi", "", NS) or "").strip()
    try:
        data_emissao = datetime.fromisoformat(dh) if dh else None
    except ValueError:
        data_emissao = None
    valor = Decimal(root.findtext("nfe:vNF", "0", NS) or "0")
    # número/série não estão no resumo padrão; deduzir da chave
    numero = chave[25:34]
    serie = chave[22:25].lstrip("0") or "0"
    return ResumoParsed(
        chave=chave, numero=numero, serie=serie,
        emit_cnpj=emit_cnpj, emit_nome=emit_nome,
        data_emissao=data_emissao, valor_total=valor,
    )


@dataclass
class EventoParsed:
    chave: str
    tp_evento: str       # "110111" cancelamento, "110110" CCe, "210200/210/220/240" manifestação
    n_seq: int
    cstat: str | None    # quando dentro de procEventoNFe há retEvento

    @property
    def is_cancelamento(self) -> bool:
        return self.tp_evento == "110111"


def parse_evento(xml: bytes) -> EventoParsed | None:
    """Parse de um procEventoNFe."""
    root = etree.fromstring(xml)
    inf = root.find(".//nfe:infEvento", NS)
    if inf is None:
        return None
    chave = (inf.findtext("nfe:chNFe", "", NS) or "").strip()
    tp = (inf.findtext("nfe:tpEvento", "", NS) or "").strip()
    n_seq_raw = inf.findtext("nfe:nSeqEvento", "1", NS) or "1"
    try:
        n_seq = int(n_seq_raw)
    except ValueError:
        n_seq = 1
    ret_inf = root.find(".//nfe:retEvento//nfe:infEvento", NS)
    cstat = ret_inf.findtext("nfe:cStat", "", NS) if ret_inf is not None else None
    return EventoParsed(chave=chave, tp_evento=tp, n_seq=n_seq, cstat=cstat)
