"""
Clientes dos webservices SEFAZ-AN usados pela automação.

- NFeDistribuicaoDFe: entrega ao destinatário (CNPJ consultante) os
  documentos emitidos contra ele. Modos:
    distNSU      → varredura sequencial (polling)
    consNSU      → consulta um NSU específico
    consChNFe    → consulta uma NFe pela chave (devolve completa após
                   manifestação de ciência)
- NFeRecepcaoEvento4: recepção de eventos da NFe, incluindo a
  manifestação do destinatário (210210 — ciência da operação).

Ambos exigem certificado A1 (mTLS).

Doc: https://www.nfe.fazenda.gov.br/portal/listaConteudo.aspx?tipoConteudo=Wak0FwB7dKs=
"""
from __future__ import annotations

import base64
import gzip
import logging
import ssl
import tempfile
from dataclasses import dataclass
from pathlib import Path

import requests
from cryptography.hazmat.primitives.serialization import (
    Encoding, NoEncryption, PrivateFormat, pkcs12,
)
from lxml import etree

log = logging.getLogger(__name__)

WS_DISTRIBUICAO = {
    1: "https://www1.nfe.fazenda.gov.br/NFeDistribuicaoDFe/NFeDistribuicaoDFe.asmx",
    2: "https://hom1.nfe.fazenda.gov.br/NFeDistribuicaoDFe/NFeDistribuicaoDFe.asmx",
}
WS_RECEPCAO_EVENTO = {
    1: "https://www1.nfe.fazenda.gov.br/NFeRecepcaoEvento4/NFeRecepcaoEvento4.asmx",
    2: "https://hom1.nfe.fazenda.gov.br/NFeRecepcaoEvento4/NFeRecepcaoEvento4.asmx",
}

ACTION_DIST = "http://www.portalfiscal.inf.br/nfe/wsdl/NFeDistribuicaoDFe/nfeDistDFeInteresse"
ACTION_EVENTO = "http://www.portalfiscal.inf.br/nfe/wsdl/NFeRecepcaoEvento4/nfeRecepcaoEvento"

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
    schema: str   # ex: "procNFe_v4.00", "resNFe_v1.01", "procEventoNFe_v1.00"
    xml: bytes
    chave: str | None


@dataclass
class RetornoEvento:
    cstat: str
    motivo: str
    xml: bytes


class CertAdapter(requests.adapters.HTTPAdapter):
    """Adapter de mTLS — carrega cert/chave PEM em memória."""

    def __init__(self, cert_pem: bytes, key_pem: bytes, *args, **kwargs):
        self._cert_pem = cert_pem
        self._key_pem = key_pem
        super().__init__(*args, **kwargs)

    def init_poolmanager(self, *args, **kwargs):
        ctx = ssl.create_default_context()
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pem") as cf, \
             tempfile.NamedTemporaryFile(delete=False, suffix=".pem") as kf:
            cf.write(self._cert_pem)
            kf.write(self._key_pem)
            cert_file = cf.name
            key_file = kf.name
        ctx.load_cert_chain(cert_file, key_file)
        kwargs["ssl_context"] = ctx
        return super().init_poolmanager(*args, **kwargs)


def load_pfx_bytes(data: bytes, password: str) -> tuple[bytes, bytes]:
    """Decodifica um .pfx (bytes) e devolve (cert_chain_pem, key_pem).
    Levanta exceção da `cryptography` se a senha estiver errada."""
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


def load_pfx(pfx_path: str, password: str) -> tuple[bytes, bytes]:
    """Wrapper que lê do arquivo. Mantido para compatibilidade com o caminho
    do .env. Internamente delega para `load_pfx_bytes`."""
    return load_pfx_bytes(Path(pfx_path).read_bytes(), password)


def _envelope_dist(uf_cod: str, ambiente: int, cnpj: str, body_inner: str) -> bytes:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<soap12:Envelope xmlns:soap12="http://www.w3.org/2003/05/soap-envelope">
  <soap12:Body>
    <nfeDistDFeInteresse xmlns="http://www.portalfiscal.inf.br/nfe/wsdl/NFeDistribuicaoDFe">
      <nfeDadosMsg>
        <distDFeInt xmlns="{NS_NFE}" versao="1.01">
          <tpAmb>{ambiente}</tpAmb>
          <cUFAutor>{uf_cod}</cUFAutor>
          <CNPJ>{cnpj}</CNPJ>
          {body_inner}
        </distDFeInt>
      </nfeDadosMsg>
    </nfeDistDFeInteresse>
  </soap12:Body>
</soap12:Envelope>""".encode("utf-8")


def _envelope_evento(env_evento_xml: bytes) -> bytes:
    inner = env_evento_xml.decode("utf-8")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<soap12:Envelope xmlns:soap12="http://www.w3.org/2003/05/soap-envelope">
  <soap12:Body>
    <nfeDadosMsg xmlns="http://www.portalfiscal.inf.br/nfe/wsdl/NFeRecepcaoEvento4">{inner}</nfeDadosMsg>
  </soap12:Body>
</soap12:Envelope>""".encode("utf-8")


def _extrair_chave(xml: bytes) -> str | None:
    try:
        root = etree.fromstring(xml)
    except Exception:  # noqa: BLE001
        return None
    for el in root.iter("{%s}infNFe" % NS_NFE):
        ident = el.get("Id", "")
        if ident.startswith("NFe"):
            return ident[3:]
    for el in root.iter("{%s}chNFe" % NS_NFE):
        return (el.text or "").strip() or None
    for el in root.iter("{%s}infEvento" % NS_NFE):
        ch = el.findtext("{%s}chNFe" % NS_NFE)
        if ch:
            return ch.strip()
    return None


class SefazClient:
    def __init__(self, *, cert_path: str | None = None, cert_bytes: bytes | None = None,
                 cert_password: str, ambiente: int, uf: str):
        if cert_bytes is None and cert_path is None:
            raise ValueError("É necessário fornecer cert_bytes ou cert_path")
        self.ambiente = ambiente
        self.uf = uf.upper()
        self.uf_cod = UF_COD[self.uf]
        if cert_bytes is not None:
            self.cert_pem, self.key_pem = load_pfx_bytes(cert_bytes, cert_password)
        else:
            self.cert_pem, self.key_pem = load_pfx(cert_path, cert_password)
        self.session = requests.Session()
        self.session.mount("https://", CertAdapter(self.cert_pem, self.key_pem))

    # ------- DistribuicaoDFe -------

    def consultar_nsu(self, cnpj: str, ult_nsu: str = "0") -> tuple[str, str, list[DocDFe]]:
        body_inner = f"<distNSU><ultNSU>{ult_nsu.zfill(15)}</ultNSU></distNSU>"
        return self._consultar(cnpj, body_inner)

    def consultar_chave(self, cnpj: str, chave: str) -> tuple[str, str, list[DocDFe]]:
        body_inner = f"<consChNFe><chNFe>{chave}</chNFe></consChNFe>"
        return self._consultar(cnpj, body_inner)

    def _consultar(self, cnpj: str, body_inner: str) -> tuple[str, str, list[DocDFe]]:
        envelope = _envelope_dist(self.uf_cod, self.ambiente, cnpj, body_inner)
        r = self.session.post(
            WS_DISTRIBUICAO[self.ambiente],
            data=envelope,
            headers={
                "Content-Type": "application/soap+xml; charset=utf-8",
                "SOAPAction": ACTION_DIST,
            },
            timeout=60,
        )
        r.raise_for_status()
        return self._parse_distribuicao(r.content)

    @staticmethod
    def _parse_distribuicao(body: bytes) -> tuple[str, str, list[DocDFe]]:
        root = etree.fromstring(body)
        ns = {"nfe": NS_NFE}
        ret = root.find(".//nfe:retDistDFeInt", ns)
        if ret is None:
            raise RuntimeError(f"Resposta inesperada SEFAZ: {body[:500]!r}")
        cstat = (ret.findtext("nfe:cStat", "", ns) or "").strip()
        ult_nsu = (ret.findtext("nfe:ultNSU", "0", ns) or "0").strip()
        docs: list[DocDFe] = []
        lote = ret.find("nfe:loteDistDFeInt", ns)
        if lote is not None:
            for d in lote.findall("nfe:docZip", ns):
                nsu = d.get("NSU", "")
                schema = d.get("schema", "")
                raw = base64.b64decode(d.text or "")
                xml = gzip.decompress(raw)
                docs.append(DocDFe(nsu=nsu, schema=schema, xml=xml, chave=_extrair_chave(xml)))
        return cstat, ult_nsu, docs

    # ------- RecepcaoEvento4 -------

    def enviar_evento(self, env_evento_xml: bytes) -> RetornoEvento:
        envelope = _envelope_evento(env_evento_xml)
        r = self.session.post(
            WS_RECEPCAO_EVENTO[self.ambiente],
            data=envelope,
            headers={
                "Content-Type": "application/soap+xml; charset=utf-8",
                "SOAPAction": ACTION_EVENTO,
            },
            timeout=60,
        )
        r.raise_for_status()
        root = etree.fromstring(r.content)
        ns = {"nfe": NS_NFE}
        ret = root.find(".//nfe:retEnvEvento", ns)
        if ret is None:
            raise RuntimeError(f"Resposta inesperada RecepcaoEvento: {r.content[:500]!r}")
        # cStat de envelope; cada infEvento tem o seu cStat individual
        cstat = (ret.findtext("nfe:cStat", "", ns) or "").strip()
        motivo = (ret.findtext("nfe:xMotivo", "", ns) or "").strip()
        # Se houver retEvento individual, prefere o cStat de lá
        ret_individual = ret.find("nfe:retEvento/nfe:infEvento", ns)
        if ret_individual is not None:
            cstat = (ret_individual.findtext("nfe:cStat", cstat, ns) or cstat).strip()
            motivo = (ret_individual.findtext("nfe:xMotivo", motivo, ns) or motivo).strip()
        return RetornoEvento(cstat=cstat, motivo=motivo, xml=r.content)
