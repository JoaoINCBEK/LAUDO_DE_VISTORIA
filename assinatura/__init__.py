"""Assinatura eletrônica à distância (desacoplada do Streamlit e do banco).

    cfg = carregar_config(secrets, os.environ)
    provedor = criar_provedor(cfg)          # Clicksign / desativado / fake (só testes)

O fluxo com o banco (isolamento por empresa, auditoria) fica em saas/servicos.py.
"""
from .base import (CANAIS, STATUS, STATUS_PENDENTES, ErroProvedor, ProvedorAssinatura, ResultadoEnvio,
                   ResultadoStatus, Signatario, mascarar_email, mascarar_telefone, mascarar_texto)
from .config import Config, carregar_config
from .desativado import ProvedorDesativado


def criar_provedor(cfg):
    if cfg is None or not cfg.configurado:
        return ProvedorDesativado(getattr(cfg, "config_error", "") if cfg else "")
    if cfg.provider == "clicksign":
        from .clicksign import ProvedorClicksign
        return ProvedorClicksign(cfg)
    if cfg.provider == "fake":
        from .fake import ProvedorFake
        return ProvedorFake.compartilhado()
    # D4Sign: preparado na configuração, mas a integração ainda não foi implementada.
    return ProvedorDesativado(f"Provedor {cfg.provider} ainda não implementado.")
