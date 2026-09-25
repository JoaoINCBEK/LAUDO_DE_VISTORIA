"""Provedor falso, em memória. Usado SOMENTE nos testes automáticos
(config.carregar_config recusa provider = "fake" fora de LAUDO_TESTE)."""
import itertools

from .base import ErroProvedor, ProvedorAssinatura, ResultadoStatus

PDF_ASSINADO = b"%PDF-1.4 documento assinado (fake)"


class ProvedorFake(ProvedorAssinatura):
    nome = "fake"
    canais = ("email", "whatsapp", "sms")

    def __init__(self, falha=None, link=True):
        """falha: None | "timeout" (em qualquer chamada) | nome de um método que deve falhar."""
        self.falha, self.com_link = falha, link
        self.envelopes = {}
        self.chamadas = []
        self._seq = itertools.count(1)

    _unico = None

    @classmethod
    def compartilhado(cls):
        """Mesma instância entre execuções do app (teste manual no navegador com LAUDO_TESTE)."""
        if cls._unico is None:
            cls._unico = cls()
        return cls._unico

    def _passo(self, metodo, id_externo=None):
        self.chamadas.append(metodo)
        if id_externo is not None and id_externo not in self.envelopes:
            raise ErroProvedor("Registro não encontrado no serviço de assinatura.")
        if self.falha == "timeout":
            raise ErroProvedor("O serviço de assinatura não respondeu a tempo. Tente novamente em instantes.")
        if self.falha == metodo:
            raise ErroProvedor(f"Falha simulada em {metodo}.")

    def criar_solicitacao(self, titulo):
        self._passo("criar_solicitacao")
        eid = f"env-{next(self._seq)}"
        self.envelopes[eid] = {"titulo": titulo, "status": "rascunho", "pdf": None, "signatarios": []}
        return eid

    def enviar_documento(self, id_externo, nome_arquivo, pdf_bytes):
        self._passo("enviar_documento")
        self.envelopes[id_externo]["pdf"] = pdf_bytes
        return f"doc-{id_externo}"

    def adicionar_signatario(self, id_externo, documento_id, signatario):
        self._passo("adicionar_signatario")
        self.envelopes[id_externo]["signatarios"].append(signatario)
        return f"sig-{id_externo}"

    def ativar(self, id_externo):
        self._passo("ativar")
        self.envelopes[id_externo]["status"] = "aguardando"

    def obter_link(self, id_externo, signatario_id):
        return f"https://assinatura.exemplo/{signatario_id}" if self.com_link else ""

    def consultar_status(self, id_externo, documento_id):
        self._passo("consultar_status", id_externo)
        return ResultadoStatus(self.envelopes[id_externo]["status"])

    def baixar_documento_assinado(self, id_externo, documento_id):
        self._passo("baixar_documento_assinado", id_externo)
        if self.envelopes[id_externo]["status"] != "assinado":
            raise ErroProvedor("O documento ainda não foi assinado.")
        return PDF_ASSINADO

    def cancelar(self, id_externo, documento_id):
        self.chamadas.append("cancelar")
        if id_externo in self.envelopes:
            self.envelopes[id_externo]["status"] = "cancelado"

    # -- simulações usadas pelos testes --------------------------------------
    def simular(self, id_externo, status):
        self.envelopes[id_externo]["status"] = status
