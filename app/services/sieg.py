"""Cliente da API SIEG (api.sieg.com) — cofre de XMLs.

A SIEG já faz manifestação de ciência e armazena os XMLs completos das NFe
emitidas contra o CNPJ. O endpoint /BaixarXmls devolve um array de strings
base64; cada item pode ser:
  - XML cru (texto)
  - XML zipado (.zip - assinatura PK\\x03\\x04)
  - XML gzipado (assinatura 0x1f 0x8b)

Docs: https://api.sieg.com/swagger/index.html
"""
from __future__ import annotations

import base64
import gzip
import io
import logging
import zipfile
from datetime import date

import requests

log = logging.getLogger(__name__)

SIEG_BASE = "https://api.sieg.com"
XML_TYPE_NFE = 1


def _extrair_xml(b64: str) -> bytes:
    raw = base64.b64decode(b64)
    if raw[:2] == b"\x1f\x8b":
        return gzip.decompress(raw)
    if raw[:4] == b"PK\x03\x04":
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            for name in z.namelist():
                if name.lower().endswith(".xml"):
                    return z.read(name)
        raise RuntimeError("ZIP do SIEG sem .xml interno")
    return raw


class SiegClient:
    def __init__(self, api_key: str):
        if not api_key:
            raise ValueError("SIEG_API_KEY vazio")
        self.api_key = api_key
        self.session = requests.Session()

    def baixar_nfe_destinatario(
        self,
        cnpj_dest: str,
        data_ini: date,
        data_fim: date,
        page_size: int = 50,
    ) -> list[bytes]:
        """Baixa todos os XMLs de NFe (entrada) do CNPJ no período."""
        out: list[bytes] = []
        skip = 0
        while True:
            body = {
                "XmlType": XML_TYPE_NFE,
                "Take": page_size,
                "Skip": skip,
                "DataEmissaoInicio": data_ini.isoformat(),
                "DataEmissaoFim": data_fim.isoformat(),
                "CnpjDest": cnpj_dest,
                "Downloadevent": False,
            }
            r = self.session.post(
                f"{SIEG_BASE}/BaixarXmls",
                params={"api_key": self.api_key},
                json=body,
                timeout=120,
            )
            # Sem mais resultados: SIEG costuma devolver 404 ou lista vazia
            if r.status_code in (404, 204):
                break
            r.raise_for_status()
            data = r.json()
            if not isinstance(data, list) or not data:
                break
            for item in data:
                try:
                    out.append(_extrair_xml(item))
                except Exception:  # noqa: BLE001
                    log.exception("Falha decodificando XML do SIEG")
            if len(data) < page_size:
                break
            skip += page_size
        return out
