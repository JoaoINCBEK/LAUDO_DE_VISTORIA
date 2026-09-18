"""Consulta OPCIONAL de dados do veículo pela placa.

Princípios:
- Nenhuma API é inventada: sem configuração, a consulta fica desligada e o
  formulário funciona 100% manual.
- A chave/token NUNCA vai para o navegador: a chamada é feita pelo servidor
  (o próprio processo Python do Streamlit).
- Qualquer falha (sem internet, timeout, HTTP de erro, JSON inválido) vira um
  resultado "unavailable" - nunca uma exceção que trave o formulário.

Configuração (use UMA das formas):

1) .streamlit/secrets.toml  (ou "Secrets" do Streamlit Cloud)

    [placa_api]
    url    = "https://SEU-PROVEDOR/consulta/{placa}"      # {placa} e {token} são substituídos
    token  = "SUA-CHAVE"
    header = "Authorization"     # opcional: nome do cabeçalho que leva a chave
    prefix = "Bearer "           # opcional: prefixo do valor do cabeçalho
    timeout = 6                  # opcional (segundos)

2) Variáveis de ambiente: PLACA_API_URL, PLACA_API_TOKEN, PLACA_API_HEADER,
   PLACA_API_PREFIX, PLACA_API_TIMEOUT.

Alguns provedores colocam a chave na própria URL, ex.:
    url = "https://SEU-PROVEDOR/consulta/{placa}/{token}"
"""
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

NOT_FOUND_TEXT = "NÃO ENCONTRADO"
FIELDS = ("marca", "modelo", "ano", "cor")

# Placa antiga (ABC1234) e Mercosul (ABC1D23)
_PLATE_RE = re.compile(r"^[A-Z]{3}[0-9][A-Z0-9][0-9]{2}$")

# chaves possíveis no JSON de provedores diferentes (comparadas sem acento/símbolos, minúsculas)
_KEYS = {
    "marca": ["marca", "brand", "make", "fabricante"],
    "modelo": ["modelo", "model", "versao"],
    "ano": ["anomodelo", "ano", "year", "modelyear", "anofabricacao", "anofab"],
    "cor": ["cor", "color", "colour"],
    "combinado": ["marcamodelo", "marcamodel", "brandmodel"],
}


def normalize_plate(value):
    return re.sub(r"[^A-Za-z0-9]", "", str(value or "")).upper()


def is_valid_plate(value):
    return bool(_PLATE_RE.match(normalize_plate(value)))


@dataclass
class Config:
    url: str
    token: str = ""
    header: str = ""
    prefix: str = ""
    timeout: float = 6.0
    allow_http: bool = False  # só para testes locais


@dataclass
class Result:
    status: str                      # ok | not_found | unavailable | not_configured | invalid
    data: dict = field(default_factory=dict)
    message: str = ""


def load_config(secrets=None, environ=None):
    """Monta a configuração a partir de um dict de secrets e/ou variáveis de
    ambiente. Devolve None se nada estiver configurado."""
    secrets = dict(secrets or {})
    env = os.environ if environ is None else environ

    def pick(name):
        return str(secrets.get(name) or env.get("PLACA_API_" + name.upper()) or "").strip()

    url = pick("url")
    if not url:
        return None
    try:
        timeout = float(pick("timeout") or 6)
    except ValueError:
        timeout = 6.0
    return Config(url=url, token=pick("token"), header=pick("header"), prefix=str(secrets.get("prefix") or env.get("PLACA_API_PREFIX") or ""),
                  timeout=max(1.0, min(timeout, 20.0)))


def _squash(key):
    return re.sub(r"[^a-z]", "", str(key).lower())


def _walk(obj):
    """Percorre dicts/listas aninhados devolvendo (chave_normalizada, valor)."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (dict, list)):
                yield from _walk(v)
            else:
                yield _squash(k), v
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk(item)


def _clean(value):
    text = re.sub(r"\s+", " ", str(value if value is not None else "")).strip()
    if text.lower() in ("", "none", "null", "n/a", "nao informado", "não informado", "-"):
        return ""
    return text


def extract_vehicle(payload):
    """Extrai marca/modelo/ano/cor de um JSON arbitrário. Devolve dict só com o que achou."""
    flat = {}
    for k, v in _walk(payload):
        v = _clean(v)
        if v and k not in flat:
            flat[k] = v

    def first(candidates):
        for cand in candidates:
            if cand in flat:
                return flat[cand]
        return ""

    marca, modelo = first(_KEYS["marca"]), first(_KEYS["modelo"])
    combinado = first(_KEYS["combinado"])
    if combinado and "/" in combinado:
        m, _, md = combinado.partition("/")
        marca = marca or m.strip()
        modelo = modelo or md.strip()
    elif combinado and not (marca or modelo):
        modelo = combinado

    ano_raw = first(_KEYS["ano"])
    years = re.findall(r"(?:19|20)\d{2}", ano_raw)
    ano = years[-1] if years else ""  # "2018/2019" -> 2019 (ano modelo)

    out = {"marca": marca.upper(), "modelo": modelo.upper(), "ano": ano, "cor": first(_KEYS["cor"]).upper()}
    return {k: v for k, v in out.items() if v}


def lookup_plate(plate, config):
    """Consulta a placa. Nunca levanta exceção."""
    plate = normalize_plate(plate)
    if not _PLATE_RE.match(plate):
        return Result("invalid", message="Placa inválida.")
    if config is None or not config.url:
        return Result("not_configured", message="Consulta automática de placa não configurada.")

    url = config.url.replace("{placa}", urllib.parse.quote(plate)).replace("{token}", urllib.parse.quote(config.token or ""))
    scheme = urllib.parse.urlparse(url).scheme.lower()
    if scheme != "https" and not (config.allow_http and scheme == "http"):
        return Result("unavailable", message="A URL da consulta precisa usar HTTPS.")

    headers = {"Accept": "application/json", "User-Agent": "laudo-vistoria/1.0"}
    if config.header and config.token:
        headers[config.header] = f"{config.prefix}{config.token}"

    try:
        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=config.timeout) as resp:
            body = resp.read(1_000_000)
        payload = json.loads(body.decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        if exc.code in (400, 404, 422):
            return Result("not_found", message="Placa não encontrada.")
        return Result("unavailable", message=f"Consulta indisponível no momento (HTTP {exc.code}).")
    except (urllib.error.URLError, TimeoutError, OSError):
        return Result("unavailable", message="Sem conexão com o serviço de consulta de placa.")
    except (ValueError, UnicodeDecodeError):
        return Result("unavailable", message="Resposta inesperada do serviço de consulta de placa.")
    except Exception:  # última barreira: o formulário nunca pode travar
        return Result("unavailable", message="Não foi possível consultar a placa agora.")

    data = extract_vehicle(payload)
    if not data:
        return Result("not_found", message="Placa não encontrada.")
    return Result("ok", data=data)
