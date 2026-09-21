"""Integração com o fluxo de consulta da Placaí.

Fluxo (observado no navegador; nenhum endpoint foi inventado):
1) POST https://www.placai.com/api/placaOrder
2) pega data.transaction_id
3) GET  https://www.placai.com/api/resultado/{transaction_id}/preview
4) extrai marca / modelo / ano / cor de data.veiculo.

IMPORTANTE:
- A Placaí observada no navegador envia um Cookie no placaOrder.
- NUNCA coloque o cookie neste arquivo. Ele fica só em
  .streamlit/secrets.toml (seção [placa_api]) ou na variável de ambiente
  PLACA_API_COOKIE. Cookies expiram e devem ser tratados como segredo.
- O cookie NUNCA é impresso em logs nem em mensagens de erro.
- O código não tenta obter, renovar ou contornar autenticação.

Teste pelo terminal (não expõe o cookie):
    python placa_api.py --check        # só confere a configuração
    python placa_api.py ABC1D23        # confere a configuração e consulta
"""

import json
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, Optional
from urllib.parse import quote, urlparse

import requests


NOT_FOUND_TEXT = ""
FIELDS = ("marca", "modelo", "ano", "cor")

_PLATE_RE = re.compile(r"^[A-Z]{3}[0-9][A-Z0-9][0-9]{2}$")

_KEYS = {
    "marca": ["marca", "brand", "make", "fabricante"],
    "modelo": ["modelo", "model", "versao"],
    "ano": ["anomodelo", "ano", "year", "modelyear", "anofabricacao", "anofab"],
    "cor": ["cor", "color", "colour"],
    "combinado": ["marcamodelo", "marcamodel", "brandmodel"],
}

# --- Log seguro -------------------------------------------------------------
# Só registra status HTTP, etapas e "SIM/NÃO". NUNCA registra cookie/headers.
log = logging.getLogger("placa_api")
if not log.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("[placa_api] %(message)s"))
    log.addHandler(_handler)
    log.setLevel(logging.INFO)
    log.propagate = False


def normalize_plate(value: Any) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", str(value or "")).upper()


def is_valid_plate(value: Any) -> bool:
    return bool(_PLATE_RE.match(normalize_plate(value)))


@dataclass
class Config:
    base_url: str = "https://www.placai.com"
    cookie: str = ""
    product_id: int = 1
    referrer: str = "https://www.placai.com/"
    timeout: float = 20.0
    # Motivo (sem valores sensíveis) de os Secrets não terem sido lidos.
    config_error: str = ""


@dataclass
class Result:
    status: str
    data: dict = field(default_factory=dict)
    message: str = ""


def load_config(
    secrets: Optional[dict] = None,
    environ=None,
    secrets_error: str = "",
) -> Optional[Config]:
    """Carrega configuração de secrets.toml ([placa_api]) ou variáveis de ambiente.

    Chaves aceitas em [placa_api]: cookie, product_id, referrer, timeout, base_url.
    Variáveis de ambiente equivalentes: PLACA_API_COOKIE, PLACA_API_PRODUCT_ID, ...
    `secrets_error` é o motivo (texto seguro) de os Secrets não terem sido lidos.
    """
    secrets = dict(secrets or {})
    env = os.environ if environ is None else environ

    def pick(name: str) -> str:
        return str(
            secrets.get(name)
            or env.get("PLACA_API_" + name.upper())
            or ""
        ).strip()

    cookie = pick("cookie")
    base_url = pick("base_url") or "https://www.placai.com"
    referrer = pick("referrer") or "https://www.placai.com/"

    try:
        product_id = int(pick("product_id") or 1)
    except ValueError:
        product_id = 1

    try:
        timeout = float(pick("timeout") or 20)
    except ValueError:
        timeout = 20.0

    return Config(
        base_url=base_url.rstrip("/"),
        cookie=cookie,
        product_id=product_id,
        referrer=referrer,
        timeout=max(5.0, min(timeout, 60.0)),
        config_error=secrets_error,
    )


# --- Extração dos dados do veículo -----------------------------------------

def _squash(key: Any) -> str:
    return re.sub(r"[^a-z]", "", str(key).lower())


def _walk(obj: Any):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (dict, list)):
                yield from _walk(v)
            else:
                yield _squash(k), v
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk(item)


def _clean(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value if value is not None else "")).strip()
    if text.lower() in (
        "", "none", "null", "n/a", "nao informado", "não informado", "-"
    ):
        return ""
    # A Placaí usa **** para ocultar dados no preview.
    if set(text) == {"*"}:
        return ""
    return text


def extract_vehicle(payload: Any) -> dict:
    """Extrai marca/modelo/ano/cor do JSON do preview (data.veiculo)."""
    data = payload.get("data") if isinstance(payload, dict) else None
    vehicle = data.get("veiculo") if isinstance(data, dict) else None
    if not isinstance(vehicle, dict):
        vehicle = {}

    flat = {}
    for k, v in _walk(vehicle):
        v = _clean(v)
        if v and k not in flat:
            flat[k] = v

    def first(candidates):
        for cand in candidates:
            if cand in flat:
                return flat[cand]
        return ""

    marca = first(_KEYS["marca"])
    modelo = first(_KEYS["modelo"])
    combinado = first(_KEYS["combinado"])

    if combinado and "/" in combinado:
        m, _, md = combinado.partition("/")
        marca = marca or m.strip()
        modelo = modelo or md.strip()
    elif combinado and not (marca or modelo):
        modelo = combinado

    ano_raw = first(_KEYS["ano"])
    years = re.findall(r"(?:19|20)\d{2}", ano_raw)
    ano = years[-1] if years else ""

    out = {
        "marca": marca.upper(),
        "modelo": modelo.upper(),
        "ano": ano,
        "cor": first(_KEYS["cor"]).upper(),
    }
    return {k: v for k, v in out.items() if v}


# --- Cookie / headers -------------------------------------------------------

def _parse_cookie(raw: str) -> Dict[str, str]:
    """Converte 'a=1; b=2' em dict. Tolera 'Cookie: ' na frente e quebras de linha
    (comuns ao copiar do DevTools). Não valida nem imprime valores."""
    raw = re.sub(r"[\r\n]+", " ", str(raw or "")).strip()
    raw = re.sub(r"^cookie\s*:\s*", "", raw, flags=re.I)
    out: Dict[str, str] = {}
    for part in raw.split(";"):
        name, sep, value = part.strip().partition("=")
        name = name.strip()
        if sep and name:
            out[name] = value.strip()
    return out


def _cookie_header(pairs: Dict[str, str]) -> str:
    return "; ".join(f"{k}={v}" for k, v in pairs.items())


def _headers(config: Config, cookies: Dict[str, str], *, json_body: bool) -> dict:
    headers = {
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0",
        "Origin": "https://www.placai.com",
        "Referer": config.referrer,
    }
    if json_body:
        headers["Content-Type"] = "application/json"
    # O navegador usa Cookie. Não inventamos Authorization/API-Key.
    if cookies:
        headers["Cookie"] = _cookie_header(cookies)
    return headers


# --- Consulta ---------------------------------------------------------------

def _fail(status: str, message: str) -> Result:
    log.warning("%s: %s", status, message)
    return Result(status, message=message)


def _not_configured_message(config: Config) -> str:
    if config.config_error:
        return (
            f"{config.config_error}. Corrija .streamlit/secrets.toml "
            "(veja secrets_toml.example) ou defina PLACA_API_COOKIE."
        )
    return (
        "Cookie da Placaí ausente. Preencha 'cookie' na seção [placa_api] dos "
        "Secrets (ou defina PLACA_API_COOKIE)."
    )


def _http_problem(code: int, step: str, *, not_found_is_result: bool) -> Optional[Result]:
    """Traduz status HTTP de erro em Result. None = status aceitável."""
    if code in (401, 403):
        return _fail(
            "unavailable",
            f"A Placaí recusou a sessão (HTTP {code}) {step}. O cookie pode ter "
            "expirado: copie um cookie novo do navegador para os Secrets.",
        )
    if code == 404 and not_found_is_result:
        return _fail("not_found", "Resultado não encontrado.")
    if code == 429:
        return _fail("unavailable", "Muitas consultas à Placaí (HTTP 429). Aguarde um pouco.")
    if code >= 400:
        # 400/422/404 na criação do pedido NÃO significam "placa inexistente":
        # podem ser payload/sessão recusados. Não sobrescrevemos o formulário.
        return _fail("unavailable", f"A Placaí respondeu HTTP {code} {step}.")
    return None


def _json_or_none(resp: requests.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return None


def lookup_plate(plate: str, config: Optional[Config]) -> Result:
    """Cria o pedido na Placaí e busca o preview.

    status: ok | not_found | invalid | not_configured | unavailable
    """
    plate = normalize_plate(plate)

    if not _PLATE_RE.match(plate):
        return Result("invalid", message="Placa inválida.")

    if config is None:
        return Result("not_configured", message="Placaí não configurada.")

    cookies = _parse_cookie(config.cookie)
    log.info("consulta iniciada | cookie configurado: %s", "SIM" if cookies else "NÃO")
    if not cookies:
        return _fail("not_configured", _not_configured_message(config))

    payload = {
        "placa": plate,
        "product_id": config.product_id,
        "referrer": config.referrer,
        "score": "0.0",
        "utm": {
            "utm_term": None,
            "utm_campaign": None,
            "utm_source": None,
            "utm_medium": None,
        },
    }

    try:
        session = requests.Session()

        # 1) Cria o pedido.
        r = session.post(
            f"{config.base_url}/api/placaOrder",
            json=payload,
            headers=_headers(config, cookies, json_body=True),
            timeout=config.timeout,
        )
        log.info("POST /api/placaOrder -> HTTP %s", r.status_code)
        problem = _http_problem(r.status_code, "ao criar a consulta", not_found_is_result=False)
        if problem:
            return problem

        order_payload = _json_or_none(r)
        if order_payload is None:
            return _fail("unavailable", "Resposta inesperada da Placaí ao criar a consulta (não é JSON).")

        data = order_payload.get("data") if isinstance(order_payload, dict) else None
        transaction_id = data.get("transaction_id") if isinstance(data, dict) else None
        transaction_id = str(transaction_id).strip() if transaction_id else ""
        log.info("transaction_id recebido: %s", "SIM" if transaction_id else "NÃO")
        if not transaction_id:
            return _fail("unavailable", "A Placaí não retornou transaction_id.")

        # Se a Placaí definiu cookies novos no POST, o navegador os reenviaria no GET.
        server_cookies = r.cookies.get_dict()
        merged = dict(cookies)
        merged.update(server_cookies)
        if server_cookies:
            log.info("cookies novos definidos pelo servidor no POST: %d", len(server_cookies))

        # 2) Busca diretamente o preview (não usa /api/order nem /api/auth/get-session).
        preview = session.get(
            f"{config.base_url}/api/resultado/{quote(transaction_id, safe='')}/preview",
            headers=_headers(config, merged, json_body=False),
            timeout=config.timeout,
        )
        log.info("GET /api/resultado/<transaction_id>/preview -> HTTP %s", preview.status_code)
        problem = _http_problem(preview.status_code, "ao buscar o preview", not_found_is_result=True)
        if problem:
            return problem

        result_payload = _json_or_none(preview)
        if result_payload is None:
            return _fail("unavailable", "Resposta inesperada da Placaí no preview (não é JSON).")

        vehicle = extract_vehicle(result_payload)
        log.info(
            "campos extraídos: %s | sem valor no preview: %s",
            ", ".join(k for k in FIELDS if k in vehicle) or "nenhum",
            ", ".join(k for k in FIELDS if k not in vehicle) or "nenhum",
        )
        if not vehicle:
            return _fail("not_found", "O preview não trouxe dados públicos do veículo.")

        out = dict(vehicle)
        # Mantém também o transaction_id para diagnóstico/uso posterior.
        out["_transaction_id"] = transaction_id
        return Result("ok", data=out)

    except requests.Timeout:
        return _fail("unavailable", f"Tempo esgotado ao consultar a Placaí (>{config.timeout:g}s).")
    except requests.ConnectionError:
        return _fail("unavailable", "Não foi possível conectar à Placaí (rede/DNS).")
    except requests.RequestException as exc:
        # Só o tipo do erro: nunca cookie/headers.
        return _fail("unavailable", f"Erro de rede ao consultar a Placaí ({type(exc).__name__}).")
    except Exception as exc:  # noqa: BLE001 - nunca derruba o formulário
        return _fail("unavailable", f"Falha inesperada na consulta ({type(exc).__name__}).")


# --- Diagnóstico pelo terminal ---------------------------------------------

def _toml_error_hint(exc: Exception) -> str:
    """Tipo do erro + posição (sem trechos do arquivo, para não vazar o cookie)."""
    text = str(exc)
    m = re.search(r"line (\d+)(?:, column (\d+))?", text) or re.search(r"char (\d+)", text)
    pos = f" (posição: {m.group(0)})" if m else ""
    return f"{type(exc).__name__}{pos}"


def _load_toml(text: str) -> dict:
    try:
        import tomllib as _toml  # Python 3.11+
        return _toml.loads(text)
    except ImportError:
        pass
    try:
        import tomli as _toml
        return _toml.loads(text)
    except ImportError:
        import toml as _toml  # instalado junto com o Streamlit 1.40
        return _toml.loads(text)


def _read_secrets_file():
    """Lê .streamlit/secrets.toml sem o Streamlit. Retorna (seção, erro_seguro, caminho)."""
    for base in (os.getcwd(), os.path.expanduser("~")):
        path = os.path.join(base, ".streamlit", "secrets.toml")
        if os.path.isfile(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    data = _load_toml(fh.read())
            except Exception as exc:  # noqa: BLE001
                return {}, f"TOML inválido: {_toml_error_hint(exc)}", path
            section = data.get("placa_api")
            if not isinstance(section, dict):
                return {}, "seção [placa_api] não encontrada", path
            return dict(section), "", path
    return {}, "arquivo .streamlit/secrets.toml não encontrado", ""


def _main(argv) -> int:
    check_only = "--check" in argv
    args = [a for a in argv if not a.startswith("--")]
    if not check_only and len(args) != 1:
        print("Uso: python placa_api.py --check | python placa_api.py ABC1D23")
        return 1

    secrets, err, path = _read_secrets_file()
    config = load_config(secrets=secrets, secrets_error=err)
    has_cookie = bool(_parse_cookie(config.cookie))

    print("Arquivo de Secrets:", path or "(não encontrado)")
    print("Leitura dos Secrets:", "OK" if not err else f"PROBLEMA -> {err}")
    print("Cookie configurado:", "SIM" if has_cookie else "NÃO")
    print("product_id:", config.product_id, "| timeout:", config.timeout,
          "| base_url:", urlparse(config.base_url).netloc or config.base_url)
    if check_only:
        return 0 if has_cookie else 2

    result = lookup_plate(args[0], config)
    print(json.dumps({"status": result.status, "data": result.data,
                      "message": result.message}, ensure_ascii=False, indent=2))
    return 0 if result.status == "ok" else 3


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
