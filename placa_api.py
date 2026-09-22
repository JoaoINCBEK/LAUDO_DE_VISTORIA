"""Integração com a consulta gratuita da Achecar.

Mantém a mesma interface esperada pelo app.py:
- normalize_plate()
- is_valid_plate()
- load_config()
- lookup_plate()
- Result / Config
- FIELDS / NOT_FOUND_TEXT

Endpoint conhecido (identificado via DevTools, sem chave/cookie/token):
    POST https://www.achecar.com.br/api/free-lookup
    body: {"plate": "ABC1D23"}

O combustível NUNCA é convertido: o texto devolvido pela API (ex.:
"GASOLINA/ALCOOL/ELETRICO") é preservado exatamente como veio.
"""

from __future__ import annotations
import os, re, sys, json
from dataclasses import dataclass, field
from typing import Any, Optional
import requests

ACHECAR_URL = "https://www.achecar.com.br/api/free-lookup"
_PLATE_RE = re.compile(r"^(?:[A-Z]{3}\d[A-Z]\d{2}|[A-Z]{3}\d{4})$")

NOT_FOUND_TEXT = ""

# Campos que o app.py copia automaticamente para dentro de v["..."]
# (etapa "Veículo") sempre que a consulta pela placa tem sucesso. Precisa
# existir com esse nome exato: o app.py faz "for f in placa_api.FIELDS".
FIELDS = (
    "marca",
    "modelo",
    "ano",
    "ano_modelo",
    "cor",
    "combustivel",
    "tipo",
    "carroceria",
    "chassi",
    "numero_motor",
    "municipio",
    "uf",
    "origem",
    "potencia",
    "capacidade_tracao",
    "peso",
    "fipe",
    "marca_modelo_completo",
)


@dataclass
class Config:
    base_url: str = ACHECAR_URL
    timeout: float = 20.0
    config_error: str = ""


@dataclass
class Result:
    status: str
    data: dict = field(default_factory=dict)
    message: str = ""


def load_config(secrets: Optional[dict] = None, environ=None, secrets_error: str = "") -> Config:
    secrets = dict(secrets or {})
    env = os.environ if environ is None else environ
    url = str(secrets.get("base_url") or env.get("ACHECAR_LOOKUP_URL") or ACHECAR_URL).strip()
    try:
        timeout = float(secrets.get("timeout") or env.get("ACHECAR_LOOKUP_TIMEOUT") or 20)
    except (TypeError, ValueError):
        timeout = 20.0
    return Config(url or ACHECAR_URL, max(5.0, min(timeout, 60.0)), secrets_error)


def normalize_plate(value: Any) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", str(value or "")).upper()


def is_valid_plate(value: Any) -> bool:
    return bool(_PLATE_RE.fullmatch(normalize_plate(value)))


def _clean(value: Any) -> str:
    if value is None:
        return ""
    text = re.sub(r"\s+", " ", str(value)).strip()
    if text.lower() in {"", "none", "null", "n/a", "na", "não informado", "nao informado", "-"}:
        return ""
    return text


def _map_response(p: dict) -> dict:
    # Combustível é preservado EXATAMENTE como a Achecar devolve.
    out = {
        "marca": _clean(p.get("brand")).upper(),
        "modelo": _clean(p.get("model")).upper(),
        "ano": _clean(p.get("year")),
        "ano_modelo": _clean(p.get("yearModel")),
        "cor": _clean(p.get("color")).upper(),
        "combustivel": _clean(p.get("fuel")),
        "tipo": _clean(p.get("vehicleType")).upper(),
        "carroceria": _clean(p.get("bodyType")),
        "chassi": _clean(p.get("chassis")),
        "numero_motor": _clean(p.get("engineNumber")),
        "municipio": _clean(p.get("city")).upper(),
        "uf": _clean(p.get("state")).upper(),
        "origem": _clean(p.get("nationality")).upper(),
        "potencia": _clean(p.get("power")),
        "capacidade_tracao": _clean(p.get("tractionCapacity")),
        "peso": _clean(p.get("weight")),
        "fipe": _clean(p.get("keyValue")),
        "placa": normalize_plate(p.get("plate")),
        "data_consulta": _clean(p.get("consultDate")),
        "marca_modelo_completo": _clean(p.get("brandModel")),
    }
    return {k: v for k, v in out.items() if v != ""}


def lookup_plate(plate: str, config: Optional[Config] = None) -> Result:
    plate = normalize_plate(plate)
    if not is_valid_plate(plate):
        return Result("invalid", message="Placa inválida. Informe uma placa válida.")
    config = config or load_config()
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Origin": "https://www.achecar.com.br",
        "Referer": "https://www.achecar.com.br/consulta-gratuita",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36",
    }
    try:
        with requests.Session() as session:
            r = session.post(config.base_url, json={"plate": plate}, headers=headers, timeout=config.timeout)
        if r.status_code != 200:
            return Result("unavailable", message=f"Achecar recusou a consulta (HTTP {r.status_code}). Preencha manualmente ou tente novamente.")
        try:
            body = r.json()
        except ValueError:
            return Result("unavailable", message="A Achecar respondeu, mas não retornou JSON válido.")
        if not isinstance(body, dict):
            return Result("unavailable", message="A Achecar retornou um formato inesperado.")
        if body.get("error") or (body.get("message") and not any(k in body for k in ("brand", "model", "plate", "fuel", "color", "year"))):
            return Result("unavailable", message=_clean(body.get("message") or body.get("error")) or "A Achecar não retornou os dados do veículo.")
        data = _map_response(body)
        if not data:
            return Result("not_found", message="A Achecar não retornou dados do veículo. Preencha manualmente.")
        return Result("ok", data=data)
    except requests.Timeout:
        return Result("unavailable", message="Tempo esgotado ao consultar a Achecar. Tente novamente.")
    except requests.RequestException as exc:
        return Result("unavailable", message=f"Não foi possível consultar a Achecar ({type(exc).__name__}).")
    except Exception as exc:
        return Result("unavailable", message=f"Erro inesperado na consulta ({type(exc).__name__}).")


def _main(argv):
    if len(argv) != 1:
        print("Uso: python placa_api.py HUX2C99")
        return 1
    result = lookup_plate(argv[0])
    print(json.dumps({"status": result.status, "data": result.data, "message": result.message}, ensure_ascii=False, indent=2))
    return 0 if result.status == "ok" else 3


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
