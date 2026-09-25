"""Assinatura pelo link do próprio sistema (sem serviço externo).

O vistoriador envia ao cliente (WhatsApp / e-mail) um link do app com um token aleatório.
O cliente abre no celular, confere o laudo e DESENHA a assinatura, que entra no PDF embaixo
de "Proprietário" (sem página extra). O banco guarda só o hash do token em `id_externo`.
O status muda quando o cliente assina (saas.servicos.assinar_por_link), não por consulta.
"""
import hashlib
import secrets

from .base import ErroProvedor, ProvedorAssinatura, ResultadoEnvio, ResultadoStatus

VALIDADE_DIAS = 7


def hash_token(token):
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


class ProvedorLink(ProvedorAssinatura):
    nome = "link"
    canais = ("whatsapp", "email")

    def __init__(self, base_url=""):
        self.base_url = (base_url or "").rstrip("/")

    def enviar_para_assinatura(self, titulo, nome_arquivo, pdf_bytes, signatario):
        token = secrets.token_urlsafe(24)
        return ResultadoEnvio(hash_token(token), link=f"{self.base_url}/?assinar={token}")

    # O fluxo acima não passa pelas etapas de um provedor externo.
    def criar_solicitacao(self, titulo):
        raise ErroProvedor("Use enviar_para_assinatura.")

    def enviar_documento(self, id_externo, nome_arquivo, pdf_bytes):
        raise ErroProvedor("Use enviar_para_assinatura.")

    def adicionar_signatario(self, id_externo, documento_id, signatario):
        raise ErroProvedor("Use enviar_para_assinatura.")

    def obter_link(self, id_externo, signatario_id):
        return ""

    def consultar_status(self, id_externo, documento_id):
        return ResultadoStatus("aguardando")

    def baixar_documento_assinado(self, id_externo, documento_id):
        raise ErroProvedor("O PDF assinado fica no próprio sistema.")

    def cancelar(self, id_externo, documento_id):
        pass
