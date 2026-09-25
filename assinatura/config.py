"""Configuração da assinatura à distância (só no servidor; a chave nunca vai ao navegador).

Secrets (.streamlit/secrets.toml ou Secrets do Streamlit Cloud):
    [assinatura]
    provider = "clicksign"                               # ou "desativado"
    api_key  = "..."                                     # token de acesso da API
    base_url = "https://sandbox.clicksign.com/api/v3"    # produção: https://app.clicksign.com/api/v3
    timeout  = 20

Sem a seção: variáveis ASSINATURA_PROVIDER / ASSINATURA_API_KEY / ASSINATURA_BASE_URL /
ASSINATURA_TIMEOUT. Sem nada disso: provedor "desativado" (o app funciona normalmente).
"""
import os
from dataclasses import dataclass, field
from typing import Optional

PROVEDORES = ("clicksign", "d4sign", "desativado", "fake")
URL_PADRAO = {"clicksign": "https://sandbox.clicksign.com/api/v3"}   # padrão seguro: sandbox


@dataclass
class Config:
    provider: str = "desativado"
    api_key: str = field(default="", repr=False)      # repr=False: não aparece em logs/prints
    base_url: str = ""
    timeout: float = 20.0
    config_error: str = ""

    @property
    def configurado(self):
        if self.provider == "fake":
            return True
        return self.provider not in ("desativado", "") and bool(self.api_key) and not self.config_error

    @property
    def ambiente(self):
        return "sandbox (testes)" if "sandbox" in self.base_url else "produção"


def carregar_config(secrets: Optional[dict] = None, environ=None, secrets_error: str = "") -> Config:
    s = dict(secrets or {})
    env = os.environ if environ is None else environ
    provider = str(s.get("provider") or env.get("ASSINATURA_PROVIDER") or "desativado").strip().lower()
    api_key = str(s.get("api_key") or env.get("ASSINATURA_API_KEY") or "").strip()
    base_url = str(s.get("base_url") or env.get("ASSINATURA_BASE_URL") or URL_PADRAO.get(provider, "")).strip()
    try:
        timeout = float(s.get("timeout") or env.get("ASSINATURA_TIMEOUT") or 20)
    except (TypeError, ValueError):
        timeout = 20.0
    timeout = max(5.0, min(timeout, 60.0))
    erro = secrets_error or ""

    if provider not in PROVEDORES:
        erro, provider = f"Provedor de assinatura desconhecido: {provider[:30]}", "desativado"
    elif provider == "fake" and not env.get("LAUDO_TESTE"):
        # o provedor falso existe só para os testes automáticos
        erro, provider = "Provedor 'fake' só é aceito nos testes automáticos", "desativado"
    elif provider in ("clicksign", "d4sign"):
        if not api_key:
            erro = erro or "Chave da API (api_key) não configurada"
        elif not base_url.startswith("https://"):
            erro = "base_url precisa começar com https://"
    return Config(provider, api_key, base_url, timeout, erro)
