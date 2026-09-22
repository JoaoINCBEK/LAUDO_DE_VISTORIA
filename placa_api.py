"""Consulta de veículos pelo HTML do Placa FIPE.

Usa uma requests.Session() para:
1. abrir a página inicial e receber cookies;
2. manter os cookies na consulta da placa;
3. enviar headers semelhantes aos de um navegador;
4. ler o HTML retornado com BeautifulSoup.

Observação: isto não contorna CAPTCHA, Cloudflare ou outros bloqueios
anti-bot. Se o servidor continuar respondendo HTTP 403, a consulta será
informada como indisponível.
"""

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

# A troca aqui é: em vez do `requests` puro, usamos o `curl_cffi`, que fala
# TLS/HTTP2 com a "impressão digital" (fingerprint) de um Chrome de verdade.
# É isso, e não os headers, que o Placa FIPE está usando para bloquear com
# HTTP 403 (ver explicação completa na resposta do chat).
from curl_cffi import requests
from curl_cffi.requests import exceptions as requests_exceptions
from bs4 import BeautifulSoup


FIELDS = ("marca", "modelo", "ano", "cor", "combustivel", "tipo")

# Texto usado pelo app.py quando um campo não veio na consulta (placa
# encontrada, mas sem aquele dado específico). Não inventamos o valor.
NOT_FOUND_TEXT = ""

_PLATE_RE = re.compile(r"^[A-Z]{3}[0-9][A-Z0-9][0-9]{2}$")

log = logging.getLogger("placa_api")
if not log.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("[placa_api] %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    log.propagate = False


@dataclass
class Config:
    base_url: str = "https://placafipe.com"
    timeout: float = 20.0
    config_error: str = ""


@dataclass
class Result:
    status: str
    data: dict = field(default_factory=dict)
    message: str = ""


def normalize_plate(value: Any) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", str(value or "")).upper()


def is_valid_plate(value: Any) -> bool:
    return bool(_PLATE_RE.match(normalize_plate(value)))


def load_config(
    secrets: Optional[dict] = None,
    environ=None,
    secrets_error: str = "",
) -> Config:
    secrets = dict(secrets or {})
    env = os.environ if environ is None else environ

    def pick(name: str) -> str:
        return str(
            secrets.get(name)
            or env.get("PLACA_FIPE_" + name.upper())
            or ""
        ).strip()

    base_url = pick("base_url") or "https://placafipe.com"

    try:
        timeout = float(pick("timeout") or 20)
    except ValueError:
        timeout = 20.0

    return Config(
        base_url=base_url.rstrip("/"),
        timeout=max(5.0, min(timeout, 60.0)),
        config_error=secrets_error or "",
    )


def _clean(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if text.lower() in {
        "", "none", "null", "n/a", "na", "não informado",
        "nao informado", "-"
    }:
        return ""
    return text


def _normalize_label(value: str) -> str:
    value = _clean(value).lower()
    replacements = str.maketrans(
        "áàãâéêíóôõúç",
        "aaaaeeiooouc",
    )
    value = value.translate(replacements)
    return re.sub(r"[^a-z0-9]+", "", value)


_LABELS = {
    "marca": {"marca", "fabricante"},
    "modelo": {"modelo", "model"},
    "ano": {"ano", "anofabricacao", "anofab"},
    "ano_modelo": {"anomodelo", "anomodel"},
    "cor": {"cor", "color"},
    "combustivel": {
        "combustivel",
        "combustivelmotor",
        "fuel",
        "tipocombustivel",
    },
    "tipo": {
        "tipo",
        "tipoveiculo",
        "veiculotipo",
        "categoria",
    },
}


def _extract_table_rows(html: str) -> Dict[str, str]:
    soup = BeautifulSoup(html, "html.parser")

    # O print enviado mostra esta tabela:
    # <table class="fipeTablePriceDetail">
    #   <tr><td><b>Marca:</b></td><td>VOLKSWAGEN</td></tr>
    # </table>
    tables = soup.select("table.fipeTablePriceDetail")
    if not tables:
        tables = soup.find_all("table")

    rows: Dict[str, str] = {}

    for table in tables:
        for tr in table.find_all("tr"):
            cells = tr.find_all(["td", "th"])
            if len(cells) < 2:
                continue

            label = _normalize_label(cells[0].get_text(" ", strip=True))
            value = _clean(cells[1].get_text(" ", strip=True))

            if label and value and label not in rows:
                rows[label] = value

    return rows


def _first(rows: Dict[str, str], names: set[str]) -> str:
    for name in names:
        key = _normalize_label(name)
        if key in rows:
            return _clean(rows[key])
    return ""


def extract_vehicle(html: str) -> dict:
    rows = _extract_table_rows(html)

    marca = _first(rows, _LABELS["marca"])
    modelo = _first(rows, _LABELS["modelo"])
    ano = _first(rows, _LABELS["ano"])
    ano_modelo = _first(rows, _LABELS["ano_modelo"])
    cor = _first(rows, _LABELS["cor"])
    combustivel = _first(rows, _LABELS["combustivel"])
    tipo = _first(rows, _LABELS["tipo"])

    if not ano:
        ano = ano_modelo

    years = re.findall(r"(?:19|20)\d{2}", ano)
    if years:
        ano = years[-1]

    result = {
        "marca": marca.upper(),
        "modelo": modelo.upper(),
        "ano": ano,
        "cor": cor.upper(),
        "combustivel": combustivel.upper(),
        "tipo": tipo.upper(),
    }

    if ano_modelo:
        result["ano_modelo"] = ano_modelo

    return {k: v for k, v in result.items() if v}


def _browser_headers(base_url: str) -> dict:
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/153.0.0.0 Safari/537.36"
        ),
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;"
            "q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8"
        ),
        "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-User": "?1",
        "Referer": base_url.rstrip("/") + "/",
    }


def _new_session(base_url: str) -> requests.Session:
    # impersonate="chrome" faz o curl_cffi usar o mesmo ClientHello TLS/JA3
    # e os mesmos quadros HTTP/2 de um Chrome real — é essa camada, abaixo
    # dos headers HTTP, que estava entregando a requisição como automatizada.
    session = requests.Session(impersonate="chrome")

    # Cookies recebidos da página inicial ficam automaticamente
    # disponíveis para a segunda requisição.
    session.headers.update(_browser_headers(base_url))

    # Mantém uma ordem de headers parecida com um navegador comum.
    session.headers.update({
        "Connection": "keep-alive",
    })

    return session


def _get_homepage(session: requests.Session, base_url: str, timeout: float):
    home_url = base_url.rstrip("/") + "/"

    response = session.get(
        home_url,
        timeout=timeout,
        allow_redirects=True,
    )

    log.info(
        "GET página inicial -> HTTP %s | cookies recebidos: %s",
        response.status_code,
        "SIM" if session.cookies else "NÃO",
    )

    return response


def lookup_plate(plate: str, config: Optional[Config]) -> Result:
    plate = normalize_plate(plate)

    if not _PLATE_RE.match(plate):
        return Result("invalid", message="Placa inválida.")

    config = config or Config()

    base_url = config.base_url.rstrip("/")
    url = f"{base_url}/placa/{plate}"

    log.info("iniciando sessão de consulta")

    session = _new_session(base_url)

    try:
        # Primeira visita: recebe cookies e eventuais dados de sessão.
        home = _get_homepage(session, base_url, config.timeout)

        # Se a página inicial estiver indisponível, ainda tentamos a URL
        # direta, pois alguns servidores bloqueiam somente a home.
        if home.status_code >= 500:
            log.info(
                "página inicial indisponível (HTTP %s); "
                "tentando consulta direta",
                home.status_code,
            )

        # Referer agora representa a página inicial visitada.
        session.headers.update({
            "Referer": home.url or base_url + "/",
        })

        response = session.get(
            url,
            timeout=config.timeout,
            allow_redirects=True,
        )

        log.info(
            "GET /placa/<placa> -> HTTP %s | cookies na sessão: %s",
            response.status_code,
            "SIM" if session.cookies else "NÃO",
        )

        if response.status_code == 403:
            return Result(
                "unavailable",
                message=(
                    "O Placa FIPE recusou a consulta (HTTP 403). "
                    "O servidor está bloqueando esta requisição automática. "
                    "Preencha manualmente ou tente novamente."
                ),
            )

        if response.status_code == 429:
            return Result(
                "unavailable",
                message=(
                    "O Placa FIPE limitou temporariamente as consultas "
                    "(HTTP 429). Tente novamente mais tarde."
                ),
            )

        if response.status_code == 404:
            return Result(
                "not_found",
                message="Veículo/placa não encontrado no Placa FIPE.",
            )

        if response.status_code >= 400:
            return Result(
                "unavailable",
                message=(
                    f"O Placa FIPE respondeu HTTP {response.status_code}."
                ),
            )

        vehicle = extract_vehicle(response.text)

        if not vehicle:
            return Result(
                "not_found",
                message=(
                    "A página foi carregada, mas os dados do veículo "
                    "não foram encontrados no HTML."
                ),
            )

        vehicle["_source"] = "placafipe.com"
        vehicle["_url"] = url

        log.info(
            "campos extraídos: %s",
            ", ".join(k for k in FIELDS if vehicle.get(k)) or "nenhum",
        )

        return Result("ok", data=vehicle)

    except requests_exceptions.Timeout:
        return Result(
            "unavailable",
            message=(
                f"Tempo esgotado ao consultar o Placa FIPE "
                f"(>{config.timeout:g}s)."
            ),
        )

    except requests_exceptions.ConnectionError:
        return Result(
            "unavailable",
            message="Não foi possível conectar ao Placa FIPE.",
        )

    except requests_exceptions.RequestException as exc:
        return Result(
            "unavailable",
            message=(
                "Erro de rede ao consultar o Placa FIPE "
                f"({type(exc).__name__})."
            ),
        )

    except Exception as exc:
        log.exception("falha inesperada na consulta")
        return Result(
            "unavailable",
            message=f"Falha inesperada na consulta ({type(exc).__name__}).",
        )

    finally:
        session.close()


if __name__ == "__main__":
    import json
    import sys

    if len(sys.argv) != 2:
        print("Uso: python placa_api.py HUX8C99")
        raise SystemExit(1)

    result = lookup_plate(sys.argv[1], Config())

    print(
        json.dumps(
            {
                "status": result.status,
                "data": result.data,
                "message": result.message,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
