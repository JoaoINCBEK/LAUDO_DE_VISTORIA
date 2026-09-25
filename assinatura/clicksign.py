"""Clicksign — API v3 ("envelopes"), formato JSON:API.

Documentação oficial consultada (developers.clicksign.com):
- Ambientes/autenticação: https://developers.clicksign.com/docs/informacoes-gerais
    sandbox  https://sandbox.clicksign.com/api/v3   |   produção  https://app.clicksign.com/api/v3
    cabeçalhos: Authorization: <access_token>, Accept/Content-Type: application/vnd.api+json
- Fluxo: https://developers.clicksign.com/docs/veja-como-funciona-na-pr%C3%A1tica
    POST  /envelopes                                  cria o envelope (status "draft")
    POST  /envelopes/{id}/documents                   filename + content_base64 (data URI)
    POST  /envelopes/{id}/signers                     name, email, phone_number, communicate_events...
    POST  /envelopes/{id}/requirements                agree/sign + provide_evidence/auth
    PATCH /envelopes/{id}                             status "running" (ativa)
    POST  /envelopes/{id}/notifications               envia o aviso ao signatário
- Signatário: https://developers.clicksign.com/reference/api-criar-signatario
    (a resposta NÃO traz link de assinatura: o link chega ao cliente pelo canal escolhido)
- Consulta: https://developers.clicksign.com/docs/gerenciamento-consultas-envelope
    GET /envelopes/{id} -> attributes.status: draft | running | closed | canceled
    DELETE /envelopes/{id} (somente rascunho)
- Documento: https://developers.clicksign.com/reference/editar-documento
    PATCH /envelopes/{id}/documents/{doc} status "canceled" (somente "running");
    a resposta traz links.files.original
- Eventos: https://developers.clicksign.com/reference/eventos-de-um-documento
    GET /envelopes/{id}/documents/{doc}/events  (nomes: refusal, deadline, cancel...)

TODO (validar no sandbox antes de produção):
- link do PDF assinado: a documentação mostra links.files.original na resposta do documento;
  o arquivo assinado é lido de links.files.signed quando o envelope está "closed".
- auth do requisito provide_evidence para WhatsApp/SMS: a documentação exemplifica "email";
  aqui é usado o mesmo nome do canal ("whatsapp" / "sms").
"""
import base64
import json
import logging

import requests

from .base import ErroProvedor, ProvedorAssinatura, ResultadoStatus, cpf_valido, mascarar_texto

log = logging.getLogger("assinatura.clicksign")
JSONAPI = "application/vnd.api+json"

_STATUS = {"draft": "rascunho", "running": "aguardando", "closed": "assinado", "canceled": "cancelado"}


class ProvedorClicksign(ProvedorAssinatura):
    nome = "clicksign"
    canais = ("email", "whatsapp", "sms")

    def __init__(self, cfg):
        self._url = cfg.base_url.rstrip("/")
        self._token = cfg.api_key
        self._timeout = cfg.timeout

    # -- HTTP -----------------------------------------------------------------
    def _req(self, metodo, caminho, corpo=None):
        headers = {"Authorization": self._token, "Accept": JSONAPI, "Content-Type": JSONAPI}
        try:
            r = requests.request(metodo, self._url + caminho, headers=headers, timeout=self._timeout,
                                 data=json.dumps(corpo) if corpo is not None else None)
        except requests.Timeout:
            log.warning("clicksign %s %s: tempo esgotado", metodo, caminho)
            raise ErroProvedor("O serviço de assinatura não respondeu a tempo. Tente novamente em instantes.")
        except requests.RequestException as exc:
            log.warning("clicksign %s %s: %s", metodo, caminho, type(exc).__name__)
            raise ErroProvedor(f"Não foi possível conectar ao serviço de assinatura ({type(exc).__name__}).")
        log.info("clicksign %s %s -> HTTP %s", metodo, caminho, r.status_code)
        if r.status_code >= 400:
            raise ErroProvedor(self._mensagem_erro(r))
        if r.status_code == 204 or not r.content:
            return {}
        try:
            return r.json()
        except ValueError:
            raise ErroProvedor("O serviço de assinatura respondeu em um formato inesperado.")

    @staticmethod
    def _mensagem_erro(r):
        if r.status_code in (401, 403):
            return ("O serviço de assinatura recusou as credenciais. Confira a chave (api_key) "
                    "e o ambiente (base_url) nos Secrets.")
        if r.status_code == 404:
            return "Registro não encontrado no serviço de assinatura."
        if r.status_code == 429:
            return "Muitas requisições ao serviço de assinatura. Aguarde um minuto e tente de novo."
        if r.status_code >= 500:
            return f"Serviço de assinatura indisponível no momento (HTTP {r.status_code}). Tente mais tarde."
        detalhes = []
        try:
            for e in (r.json().get("errors") or [])[:3]:
                detalhes.append(str(e.get("detail") or e.get("title") or "")[:160])
        except Exception:
            pass
        extra = "; ".join(d for d in detalhes if d)
        return mascarar_texto("Dados recusados pelo serviço de assinatura" + (f": {extra}" if extra else f" (HTTP {r.status_code})."))

    @staticmethod
    def _id(resp):
        try:
            return str(resp["data"]["id"])
        except (KeyError, TypeError):
            raise ErroProvedor("O serviço de assinatura não devolveu o identificador esperado.")

    # -- contrato ---------------------------------------------------------------
    def criar_solicitacao(self, titulo):
        return self._id(self._req("POST", "/envelopes", {
            "data": {"type": "envelopes", "attributes": {"name": titulo[:200]}}}))

    def enviar_documento(self, id_externo, nome_arquivo, pdf_bytes):
        conteudo = "data:application/pdf;base64," + base64.b64encode(pdf_bytes).decode("ascii")
        return self._id(self._req("POST", f"/envelopes/{id_externo}/documents", {
            "data": {"type": "documents", "attributes": {"filename": nome_arquivo, "content_base64": conteudo}}}))

    def adicionar_signatario(self, id_externo, documento_id, signatario):
        s = signatario
        attrs = {"name": s.nome}
        if s.email:
            attrs["email"] = s.email
        if s.telefone:
            attrs["phone_number"] = s.telefone
        if cpf_valido(s.cpf):   # CPF inválido não vai: a Clicksign pede o CPF ao cliente na assinatura
            attrs["documentation"] = f"{s.cpf[:3]}.{s.cpf[3:6]}.{s.cpf[6:9]}-{s.cpf[9:]}"
        attrs["communicate_events"] = {
            "signature_request": s.canal,
            "signature_reminder": "email" if s.email else "none",
            "document_signed": "email" if s.email else "whatsapp",
        }
        sig = self._id(self._req("POST", f"/envelopes/{id_externo}/signers",
                                 {"data": {"type": "signers", "attributes": attrs}}))
        rel = {"document": {"data": {"type": "documents", "id": documento_id}},
               "signer": {"data": {"type": "signers", "id": sig}}}
        self._req("POST", f"/envelopes/{id_externo}/requirements", {
            "data": {"type": "requirements", "attributes": {"action": "agree", "role": "sign"}, "relationships": rel}})
        self._req("POST", f"/envelopes/{id_externo}/requirements", {
            "data": {"type": "requirements", "attributes": {"action": "provide_evidence", "auth": s.canal},
                     "relationships": rel}})
        return sig

    def ativar(self, id_externo):
        self._req("PATCH", f"/envelopes/{id_externo}", {
            "data": {"id": id_externo, "type": "envelopes", "attributes": {"status": "running"}}})
        try:
            self._req("POST", f"/envelopes/{id_externo}/notifications",
                      {"data": {"type": "notifications", "attributes": {}}})
        except ErroProvedor as exc:        # envelope já está ativo; o aviso pode ser reenviado depois
            log.warning("clicksign: aviso ao signatário não enviado (%s)", exc)

    def obter_link(self, id_externo, signatario_id):
        return ""   # API v3 não devolve o link: a Clicksign o envia pelo canal escolhido

    def _eventos(self, id_externo, documento_id):
        if not documento_id:
            return set()
        try:
            r = self._req("GET", f"/envelopes/{id_externo}/documents/{documento_id}/events")
            return {str((e.get("attributes") or {}).get("name", "")) for e in (r.get("data") or [])}
        except ErroProvedor:
            return set()

    def consultar_status(self, id_externo, documento_id):
        r = self._req("GET", f"/envelopes/{id_externo}")
        bruto = str(((r.get("data") or {}).get("attributes") or {}).get("status", ""))
        status = _STATUS.get(bruto)
        if status is None:
            return ResultadoStatus("aguardando", f"status desconhecido: {bruto[:30]}")
        if status in ("aguardando", "cancelado"):
            eventos = self._eventos(id_externo, documento_id)
            if "refusal" in eventos:
                return ResultadoStatus("recusado")
            if status == "cancelado" and "deadline" in eventos:
                return ResultadoStatus("expirado")
        return ResultadoStatus(status)

    def baixar_documento_assinado(self, id_externo, documento_id):
        r = self._req("GET", f"/envelopes/{id_externo}/documents/{documento_id}")
        arquivos = (((r.get("data") or {}).get("links") or {}).get("files") or {})
        url = arquivos.get("signed")          # TODO: confirmar o nome no sandbox (ver docstring)
        if not url:
            raise ErroProvedor("O PDF assinado ainda não está disponível no serviço de assinatura.")
        try:
            # URL temporária do próprio provedor: sem o cabeçalho Authorization
            a = requests.get(url, timeout=self._timeout)
        except requests.RequestException as exc:
            raise ErroProvedor(f"Não foi possível baixar o PDF assinado ({type(exc).__name__}).")
        if a.status_code != 200 or not a.content.startswith(b"%PDF"):
            raise ErroProvedor(f"Não foi possível baixar o PDF assinado (HTTP {a.status_code}).")
        return a.content

    def cancelar(self, id_externo, documento_id):
        r = self._req("GET", f"/envelopes/{id_externo}")
        bruto = str(((r.get("data") or {}).get("attributes") or {}).get("status", ""))
        if bruto == "draft":
            self._req("DELETE", f"/envelopes/{id_externo}")
        elif bruto == "running":
            if not documento_id:
                raise ErroProvedor("Documento não identificado para cancelar no serviço de assinatura.")
            self._req("PATCH", f"/envelopes/{id_externo}/documents/{documento_id}", {
                "data": {"id": documento_id, "type": "documents", "attributes": {"status": "canceled"}}})
        elif bruto == "closed":
            raise ErroProvedor("O documento já foi assinado e não pode mais ser cancelado.")
