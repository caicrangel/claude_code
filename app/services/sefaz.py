"""
Cliente NFeDistribuicaoDFe (SEFAZ-AN).

Este serviço entrega ao destinatário (CNPJ consultante) os documentos
emitidos contra ele, via NSU sequencial. Requer certificado A1 (.pfx).

Doc oficial: https://www.nfe.fazenda.gov.br/portal/listaConteudo.aspx?tipoConteudo=Wak0FwB7dKs=
WSDL prod: https://www1.nfe.fazenda.gov.br/NFeDistribuicaoDFe/NFeDistribuicaoDFe.asmx
WSDL hom : https://hom1.nfe.fazenda.gov.br/NFeDistribuicaoDFe/NFeDistribuicaoDFe.asmx
"""
from __future__ import annotations

import base64
import gzip
import logging
import ssl
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import requests
from cryptography.hazmat.primitives.serialization import (
    pkcs12, BestAvailableEncryption, NoEncryption, Encoding, PrivateFormat,
)
from lxml import etree

log = logging.getLogger(__name__)

WS_URL = {
    1: "https://www1.nfe.fazenda.gov.br/NFeDistribuicaoDFe/NFeDistribuicaoDFe.asmx",
    2: "https://hom1.nfe.fazenda.gov.br/NFeDistribuicaoDFe/NFeDistribuicaoDFe.asmx",
}

SOAP_ACTION = "http://www.portalfiscal.inf.br/nfe/wsdl/NFeDistribuicaoDFe/nfeDistDFeInteresse"

UF_COD = {
    "AC": "12", "AL": "27", "AM": "13", "AP": "16", "BA": "29", "CE": "23",
    "DF": "53", "ES": "32", "GO": "52", "MA": "21", "MG": "31", "MS": "50",
    "MT": "51", "PA": "15", "PB": "25", "PE": "26", "PI": "22", "PR": "41",
    "RJ": "33", "RN": "24", "RO": "11", "RR": "14", "RS": "43", "SC": "42",
    "SE": "28", "SP": "35", "TO": "17",
}

NS_NFE = "http://www.portalfiscal.inf.br/nfe"


@dataclass
class DocDFe:
    nsu: str
    schema: str
    xml: bytes        # XML decodificado (NFe ou resumo)
    chave: str | None


class CertAdapter(requests.adapters.HTTPAdapter):
    """Adapter que carrega cert/key PEM em memória pra mTLS."""

    def __init__(self, cert_pem: bytes, key_pem: bytes, *args, **kwargs):
        self._cert_pem = cert_pem
        self._key_pem = key_pem
        super().__init__(*args, **kwargs)

    def init_poolmanager(self, *args, **kwargs):
        ctx = ssl.create_default_context()
        # requests não aceita cert/key em memória diretamente, então gravamos
        # arquivos temporários com permissão restrita.
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pem") as cf, \
             tempfile.NamedTemporaryFile(delete=False, suffix=".pem") as kf:
            cf.write(self._cert_pem)
            kf.write(self._key_pem)
            self._cert_file = cf.name
            self._key_file = kf.name
        ctx.load_cert_chain(self._cert_file, self._key_file)
        kwargs["ssl_context"] = ctx
        return super().init_poolmanager(*args, **kwargs)


def _load_pfx(pfx_path: str, password: str) -> tuple[bytes, bytes]:
    data = Path(pfx_path).read_bytes()
    key, cert, extra = pkcs12.load_key_and_certificates(data, password.encode())
    cert_pem = cert.public_bytes(Encoding.PEM)
    if extra:
        for c in extra:
            cert_pem += c.public_bytes(Encoding.PEM)
    key_pem = key.private_bytes(
        encoding=Encoding.PEM,
        format=PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=NoEncryption(),
    )
    return cert_pem, key_pem


def _build_envelope(uf_cod: str, ambiente: int, cnpj: str, ult_nsu: str) -> bytes:
    body = f"""<?xml version="1.0" encoding="UTF-8"?>
<soap12:Envelope xmlns:soap12="http://www.w3.org/2003/05/soap-envelope">
  <soap12:Body>
    <nfeDistDFeInteresse xmlns="http://www.portalfiscal.inf.br/nfe/wsdl/NFeDistribuicaoDFe">
      <nfeDadosMsg>
        <distDFeInt xmlns="{NS_NFE}" versao="1.01">
          <tpAmb>{ambiente}</tpAmb>
          <cUFAutor>{uf_cod}</cUFAutor>
          <CNPJ>{cnpj}</CNPJ>
          <distNSU><ultNSU>{ult_nsu.zfill(15)}</ultNSU></distNSU>
        </distDFeInt>
      </nfeDadosMsg>
    </nfeDistDFeInteresse>
  </soap12:Body>
</soap12:Envelope>"""
    return body.encode("utf-8")


class SefazClient:
    def __init__(self, cert_path: str, cert_password: str, ambiente: int, uf: str):
        self.ambiente = ambiente
        self.uf_cod = UF_COD[uf.upper()]
        self.url = WS_URL[ambiente]
        cert_pem, key_pem = _load_pfx(cert_path, cert_password)
        self.session = requests.Session()
        self.session.mount("https://", CertAdapter(cert_pem, key_pem))

    def consultar(self, cnpj: str, ult_nsu: str = "0") -> tuple[str, str, list[DocDFe]]:
        """Retorna (cStat, ultNSU_recebido, docs)."""
        envelope = _build_envelope(self.uf_cod, self.ambiente, cnpj, ult_nsu)
        headers = {
            "Content-Type": "application/soap+xml; charset=utf-8",
            "SOAPAction": SOAP_ACTION,
        }
        r = self.session.post(self.url, data=envelope, headers=headers, timeout=60)
        r.raise_for_status()
        return self._parse_response(r.content)

    def _parse_response(self, body: bytes) -> tuple[str, str, list[DocDFe]]:
        root = etree.fromstring(body)
        ns = {
            "soap": "http://www.w3.org/2003/05/soap-envelope",
            "nfe": NS_NFE,
        }
        ret = root.find(".//nfe:retDistDFeInt", ns)
        if ret is None:
            raise RuntimeError(f"Resposta inesperada do SEFAZ: {body[:500]!r}")
        cstat = (ret.findtext("nfe:cStat", default="", namespaces=ns) or "").strip()
        ult_nsu = (ret.findtext("nfe:ultNSU", default="0", namespaces=ns) or "0").strip()
        docs: list[DocDFe] = []
        loteEl = ret.find("nfe:loteDistDFeInt", ns)
        if loteEl is not None:
            for doc in loteEl.findall("nfe:docZip", ns):
                nsu = doc.get("NSU", "")
                schema = doc.get("schema", "")
                raw = base64.b64decode(doc.text or "")
                xml = gzip.decompress(raw)
                chave = self._extrair_chave(xml)
                docs.append(DocDFe(nsu=nsu, schema=schema, xml=xml, chave=chave))
        return cstat, ult_nsu, docs

    @staticmethod
    def _extrair_chave(xml: bytes) -> str | None:
        try:
            root = etree.fromstring(xml)
        except Exception:
            return None
        # NFe completa: //infNFe/@Id = "NFe<chave>"
        for el in root.iter("{%s}infNFe" % NS_NFE):
            ident = el.get("Id", "")
            if ident.startswith("NFe"):
                return ident[3:]
        # Resumo (resNFe): chNFe
        for el in root.iter("{%s}chNFe" % NS_NFE):
            return (el.text or "").strip() or None
        return None
