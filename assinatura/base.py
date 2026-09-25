"""Interface comum dos provedores de assinatura eletrônica à distância.

Nada aqui importa o Streamlit nem o banco: o provedor só conversa com a API externa.
Quem grava no banco, confere permissão e registra auditoria é saas/servicos.py.

Status normalizado (igual para todos os provedores):
    rascunho | aguardando | assinado | recusado | cancelado | expirado | erro
"""
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass

STATUS = ("rascunho", "aguardando", "assinado", "recusado", "cancelado", "expirado", "erro")
STATUS_PENDENTES = ("rascunho", "aguardando")
CANAIS = {"email": "E-mail", "whatsapp": "WhatsApp", "sms": "SMS"}


class ErroProvedor(Exception):
    """Falha do provedor com mensagem pronta para o usuário.
    Nunca contém token, conteúdo do PDF ou e-mail/telefone completos."""

    def __init__(self, mensagem, id_externo=""):
        super().__init__(mascarar_texto(mensagem))
        self.id_externo = id_externo or ""


@dataclass
class Signatario:
    nome: str
    email: str = ""
    telefone: str = ""          # só dígitos, com DDD (10 ou 11 dígitos, sem o 55)
    cpf: str = ""               # só dígitos (opcional)
    canal: str = "email"        # por onde o provedor manda o link: email | whatsapp | sms


@dataclass
class ResultadoEnvio:
    id_externo: str             # envelope / documento no provedor
    documento_id: str = ""
    signatario_id: str = ""
    link: str = ""              # vazio quando o provedor não devolve o link
    status: str = "aguardando"


@dataclass
class ResultadoStatus:
    status: str
    detalhe: str = ""


class ProvedorAssinatura(ABC):
    """Contrato de um provedor. `configurado` = pode enviar de verdade."""
    nome = ""
    configurado = True
    motivo = ""                 # por que não está configurado (aparece na tela)
    canais = ("email",)

    @abstractmethod
    def criar_solicitacao(self, titulo):
        """Cria o envelope/solicitação. Devolve o id externo."""

    @abstractmethod
    def enviar_documento(self, id_externo, nome_arquivo, pdf_bytes):
        """Anexa o PDF. Devolve o id do documento no provedor."""

    @abstractmethod
    def adicionar_signatario(self, id_externo, documento_id, signatario):
        """Cadastra quem assina. Devolve o id do signatário no provedor."""

    @abstractmethod
    def obter_link(self, id_externo, signatario_id):
        """Link de assinatura, se o provedor o informar (senão "")."""

    @abstractmethod
    def consultar_status(self, id_externo, documento_id):
        """Devolve ResultadoStatus com o status normalizado."""

    @abstractmethod
    def baixar_documento_assinado(self, id_externo, documento_id):
        """Bytes do PDF assinado."""

    @abstractmethod
    def cancelar(self, id_externo, documento_id):
        """Cancela a solicitação no provedor."""

    def ativar(self, id_externo):
        """Libera a solicitação para assinatura e dispara o aviso ao signatário (se preciso)."""

    def enviar_para_assinatura(self, titulo, nome_arquivo, pdf_bytes, signatario):
        """Fluxo completo. Se algo falhar depois de criar a solicitação, tenta descartá-la
        e devolve o id externo na exceção (fica registrado para conferência)."""
        id_ext = self.criar_solicitacao(titulo)
        doc = ""
        try:
            doc = self.enviar_documento(id_ext, nome_arquivo, pdf_bytes)
            sig = self.adicionar_signatario(id_ext, doc, signatario)
            self.ativar(id_ext)
            link = self.obter_link(id_ext, sig) or ""
        except ErroProvedor as exc:
            try:
                self.cancelar(id_ext, doc)
            except Exception:
                pass
            exc.id_externo = id_ext
            raise
        return ResultadoEnvio(id_ext, doc, sig, link)


# ---------------------------------------------------------------------------
# Máscaras (logs, banco e mensagens nunca guardam contato completo)
# ---------------------------------------------------------------------------
_RE_EMAIL = re.compile(r"([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")
_RE_DIGITOS = re.compile(r"\d[\d .()-]{7,}\d")


def mascarar_email(email):
    email = (email or "").strip()
    if "@" not in email:
        return ""
    usuario, dominio = email.split("@", 1)
    return f"{usuario[:2]}***@{dominio}"


def mascarar_telefone(tel):
    d = re.sub(r"\D", "", str(tel or ""))
    return f"•••••{d[-4:]}" if len(d) >= 8 else ""


def mascarar_texto(texto):
    """Mascara e-mails e sequências longas de dígitos (telefone/CPF) em mensagens livres."""
    texto = _RE_EMAIL.sub(lambda m: f"{m.group(1)}***@{m.group(2)}", str(texto or ""))
    return _RE_DIGITOS.sub(lambda m: "•••" + re.sub(r"\D", "", m.group(0))[-2:], texto)
