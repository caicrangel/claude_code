"""Construção e assinatura de eventos NFe (manifestação de ciência 210210).

Especificação:
- Padrão XML: NT2014.002 (Manifestação do Destinatário)
- Web service: NFeRecepcaoEvento4 (AN)
- Assinatura: XMLDSig enveloped, RSA-SHA1, c14n, referência ao Id do infEvento

Estrutura do envelope assinado:

  <envEvento xmlns="..." versao="1.00">
    <idLote>...</idLote>
    <evento versao="1.00">
      <infEvento Id="ID210210<chave><nseq>"> ... </infEvento>
      <Signature> ... </Signature>      <-- assina o infEvento
    </evento>
  </envEvento>
"""
from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from cryptography.hazmat.primitives.serialization import load_pem_private_key
from lxml import etree
from signxml import XMLSigner, methods

from ..config import settings
from .sefaz import SefazClient, load_pfx

log = logging.getLogger(__name__)

NS_NFE = "http://www.portalfiscal.inf.br/nfe"
TP_EVENTO_CIENCIA = "210210"
DESC_EVENTO_CIENCIA = "Ciencia da Operacao"
VERSAO_EVENTO = "1.00"
TZ_BR = ZoneInfo("America/Sao_Paulo")


def _agora_br_iso() -> str:
    # Formato dhEvento: 2024-05-04T17:30:00-03:00
    s = datetime.now(TZ_BR).strftime("%Y-%m-%dT%H:%M:%S%z")
    return s[:-2] + ":" + s[-2:]


def _construir_inf_evento(chave: str, cnpj: str, n_seq: int, ambiente: int) -> etree._Element:
    # cOrgao=91 (Ambiente Nacional / AN) para manifestação do destinatário
    inf_id = f"ID{TP_EVENTO_CIENCIA}{chave}{n_seq:02d}"
    nsmap = {None: NS_NFE}
    inf = etree.Element("infEvento", attrib={"Id": inf_id}, nsmap=nsmap)
    etree.SubElement(inf, "cOrgao").text = "91"
    etree.SubElement(inf, "tpAmb").text = str(ambiente)
    etree.SubElement(inf, "CNPJ").text = cnpj
    etree.SubElement(inf, "chNFe").text = chave
    etree.SubElement(inf, "dhEvento").text = _agora_br_iso()
    etree.SubElement(inf, "tpEvento").text = TP_EVENTO_CIENCIA
    etree.SubElement(inf, "nSeqEvento").text = str(n_seq)
    etree.SubElement(inf, "verEvento").text = VERSAO_EVENTO
    det = etree.SubElement(inf, "detEvento", attrib={"versao": VERSAO_EVENTO})
    etree.SubElement(det, "descEvento").text = DESC_EVENTO_CIENCIA
    return inf


def _carregar_cripto():
    cert_pem, key_pem = load_pfx(settings.CERT_PATH, settings.CERT_PASSWORD)
    key = load_pem_private_key(key_pem, password=None)
    return key, cert_pem


def construir_envelope_ciencia(chave: str) -> bytes:
    """Constrói o envEvento assinado para manifestação de ciência (210210)."""
    cnpj = settings.cnpj_limpo
    if not cnpj:
        raise RuntimeError("EMPRESA_CNPJ não configurado")

    n_seq = 1
    inf = _construir_inf_evento(chave, cnpj, n_seq, settings.SEFAZ_AMBIENTE)
    evento = etree.Element("evento", attrib={"versao": VERSAO_EVENTO}, nsmap={None: NS_NFE})
    evento.append(inf)

    key, cert_pem = _carregar_cripto()
    signer = XMLSigner(
        method=methods.enveloped,
        signature_algorithm="rsa-sha1",
        digest_algorithm="sha1",
        c14n_algorithm="http://www.w3.org/TR/2001/REC-xml-c14n-20010315",
    )
    signed_evento = signer.sign(
        evento,
        key=key,
        cert=cert_pem.decode("utf-8"),
        reference_uri="#" + inf.get("Id"),
    )

    env = etree.Element("envEvento", attrib={"versao": VERSAO_EVENTO}, nsmap={None: NS_NFE})
    etree.SubElement(env, "idLote").text = datetime.now().strftime("%Y%m%d%H%M%S")
    env.append(signed_evento)
    return etree.tostring(env, xml_declaration=True, encoding="UTF-8", standalone=True)


def manifestar_ciencia(client: SefazClient, chave: str) -> dict:
    """Envia manifestação de ciência (210210) para uma chave."""
    xml = construir_envelope_ciencia(chave)
    log.info("Manifestando ciência da chave %s", chave)
    ret = client.enviar_evento(xml)
    ok = ret.cstat in ("135", "136", "155")  # 135=registrado, 155=registrado fora prazo
    log.info("Manifestação chave=%s cStat=%s motivo=%s ok=%s",
             chave, ret.cstat, ret.motivo, ok)
    return {"ok": ok, "cstat": ret.cstat, "motivo": ret.motivo}
