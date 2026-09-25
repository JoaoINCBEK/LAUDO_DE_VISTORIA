"""Provedor padrão quando não há credenciais: o app funciona, só não envia para assinatura."""
from .base import ErroProvedor, ProvedorAssinatura

MENSAGEM = "Assinatura à distância não configurada."


class ProvedorDesativado(ProvedorAssinatura):
    nome = "desativado"
    configurado = False

    def __init__(self, motivo=""):
        self.motivo = motivo or ""

    def _falhar(self, *_a, **_k):
        raise ErroProvedor(MENSAGEM)

    criar_solicitacao = enviar_documento = adicionar_signatario = _falhar
    consultar_status = baixar_documento_assinado = cancelar = _falhar

    def obter_link(self, *_a, **_k):
        return ""
