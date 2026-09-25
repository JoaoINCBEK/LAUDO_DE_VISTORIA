"""Assinatura eletrônica à distância: configuração, provedor fake, Clicksign (HTTP simulado),
isolamento por empresa, laudo substituído e criação da tabela em banco existente.

    python -m unittest tests.test_assinatura -v
"""
import json
import logging
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import requests                                                   # noqa: E402

import assinatura                                                 # noqa: E402
from assinatura.clicksign import ProvedorClicksign                # noqa: E402
from assinatura.fake import PDF_ASSINADO, ProvedorFake            # noqa: E402
from saas import db, servicos as S                                # noqa: E402
from saas.rbac import AcessoNegado                                # noqa: E402
from test_saas import Base, dados_vistoria                        # noqa: E402
import _banco                                                     # noqa: E402

SIGNATARIO = {"nome": "Maria Souza", "email": "maria.souza@cliente.com", "telefone": "(85) 99999-0000",
              "cpf": "123.456.789-00", "canal": "email"}
TOKEN = "TOKEN-SECRETO-123"


class TestConfig(unittest.TestCase):
    def test_sem_secrets_fica_desativado(self):
        cfg = assinatura.carregar_config({}, {})
        self.assertEqual(cfg.provider, "desativado")
        self.assertFalse(cfg.configurado)
        prov = assinatura.criar_provedor(cfg)
        self.assertFalse(prov.configurado)
        with self.assertRaises(assinatura.ErroProvedor):
            prov.criar_solicitacao("x")

    def test_clicksign_sem_chave_nao_configura(self):
        cfg = assinatura.carregar_config({"provider": "clicksign"}, {})
        self.assertFalse(cfg.configurado)
        self.assertIn("api_key", cfg.config_error)
        self.assertEqual(cfg.base_url, "https://sandbox.clicksign.com/api/v3")   # padrão seguro

    def test_secrets_e_fallback_de_ambiente(self):
        cfg = assinatura.carregar_config({}, {"ASSINATURA_PROVIDER": "clicksign", "ASSINATURA_API_KEY": TOKEN,
                                              "ASSINATURA_BASE_URL": "https://app.clicksign.com/api/v3",
                                              "ASSINATURA_TIMEOUT": "999"})
        self.assertTrue(cfg.configurado)
        self.assertEqual(cfg.ambiente, "produção")
        self.assertEqual(cfg.timeout, 60.0)
        self.assertNotIn(TOKEN, repr(cfg))                     # a chave não aparece em logs/prints
        cfg2 = assinatura.carregar_config({"provider": "clicksign", "api_key": "k", "base_url": "http://inseguro"}, {})
        self.assertFalse(cfg2.configurado)

    def test_fake_so_nos_testes(self):
        self.assertFalse(assinatura.carregar_config({"provider": "fake"}, {}).configurado)
        self.assertTrue(assinatura.carregar_config({"provider": "fake"}, {"LAUDO_TESTE": "1"}).configurado)

    def test_mascaras(self):
        self.assertEqual(assinatura.mascarar_email("maria.souza@cliente.com"), "ma***@cliente.com")
        self.assertEqual(assinatura.mascarar_telefone("(85) 99999-0000"), "•••••0000")
        txt = assinatura.mascarar_texto("email maria@x.com telefone 85999990000 inválido")
        self.assertNotIn("maria@x.com", txt)
        self.assertNotIn("85999990000", txt)


class TestFluxoFake(Base):
    def setUp(self):
        super().setUp()
        self.eid, self.admin, self.vist = self.nova_empresa("Omega")
        self.vid, self.lid = self.vistoria_concluida(self.vist)
        self.prov = ProvedorFake()

    def test_criar_aguardando_assinado_baixar(self):
        aid = S.solicitar_assinatura(self.vist, self.vid, SIGNATARIO, self.prov)
        a = S.listar_assinaturas_vistoria(self.vist, self.vid)[0]
        self.assertEqual((a["id"], a["status"], a["laudo_id"]), (aid, "aguardando", self.lid))
        self.assertEqual(a["signatario_email"], "ma***@cliente.com")          # contato só mascarado
        self.assertEqual(a["signatario_telefone"], "•••••0000")
        self.assertFalse(a["versao_anterior"])
        env = self.prov.envelopes[a["id_externo"]]
        self.assertEqual(env["pdf"], (self.tmp / "pdfs" / f"emp_{self.eid}" / f"{S.obter_vistoria(self.vist, self.vid)['numero']}.pdf").read_bytes())
        self.assertEqual(env["signatarios"][0].telefone, "85999990000")
        # não pode duplicar enquanto aguarda
        with self.assertRaises(S.ErroNegocio):
            S.solicitar_assinatura(self.vist, self.vid, SIGNATARIO, self.prov)
        # cache: sem forçar, não consulta o provedor de novo logo em seguida
        n = self.prov.chamadas.count("consultar_status")
        S.atualizar_status_assinatura(self.vist, aid, self.prov)
        self.assertEqual(self.prov.chamadas.count("consultar_status"), n)
        # cliente assina -> "Atualizar status" -> PDF assinado arquivado ao lado do original
        self.prov.simular(a["id_externo"], "assinado")
        a = S.atualizar_status_assinatura(self.vist, aid, self.prov, forcar=True)
        self.assertEqual(a["status"], "assinado")
        self.assertTrue(a["arquivo_assinado"].endswith("_assinado.pdf"))
        self.assertEqual((self.tmp / a["arquivo_assinado"]).read_bytes(), PDF_ASSINADO)
        pdf, nome = S.pdf_assinado(self.admin, aid)
        self.assertEqual(pdf, PDF_ASSINADO)
        # disco temporário (Streamlit Cloud): arquivo apagado -> baixa de novo pelo id_externo
        (self.tmp / a["arquivo_assinado"]).unlink()
        self.assertEqual(S.pdf_assinado(self.vist, aid, self.prov)[0], PDF_ASSINADO)
        acoes = [l["acao"] for l in S.listar_logs(self.admin)]
        for esperado in ("assinatura_solicitada", "assinatura_assinado", "laudo_assinado_arquivado"):
            self.assertIn(esperado, acoes)

    def test_recusa_e_cancelamento(self):
        aid = S.solicitar_assinatura(self.vist, self.vid, SIGNATARIO, self.prov)
        a = S.listar_assinaturas_vistoria(self.vist, self.vid)[0]
        self.prov.simular(a["id_externo"], "recusado")
        self.assertEqual(S.atualizar_status_assinatura(self.vist, aid, self.prov, forcar=True)["status"], "recusado")
        aid2 = S.solicitar_assinatura(self.vist, self.vid, SIGNATARIO, self.prov)    # recusado libera reenvio
        S.cancelar_assinatura(self.vist, aid2, self.prov)
        self.assertEqual(S.listar_assinaturas_vistoria(self.vist, self.vid)[0]["status"], "cancelado")

    def test_erro_de_api_e_timeout_viram_status_erro(self):
        for falha, trecho in (("timeout", "não respondeu a tempo"), ("adicionar_signatario", "Falha simulada")):
            prov = ProvedorFake(falha=falha)
            aid = S.solicitar_assinatura(self.vist, self.vid, SIGNATARIO, prov)      # não levanta exceção
            a = [x for x in S.listar_assinaturas_vistoria(self.vist, self.vid) if x["id"] == aid][0]
            self.assertEqual(a["status"], "erro")
            self.assertIn(trecho, a["mensagem_erro"])
        self.assertIn("cancelar", prov.chamadas)          # envelope criado pela metade é descartado
        self.assertIn("assinatura_erro", [l["acao"] for l in S.listar_logs(self.admin)])

    def test_validacao_e_permissoes(self):
        for ruim in ({"nome": "Maria"}, dict(SIGNATARIO, email="x@"), dict(SIGNATARIO, telefone="123"),
                     dict(SIGNATARIO, canal="whatsapp", telefone=""), dict(SIGNATARIO, canal="email", email="")):
            with self.assertRaises(S.ErroNegocio):
                S.solicitar_assinatura(self.vist, self.vid, ruim, self.prov)
        with self.assertRaises(S.ErroNegocio):                                      # provedor desativado
            S.solicitar_assinatura(self.vist, self.vid, SIGNATARIO, assinatura.criar_provedor(None))
        with self.assertRaises(AcessoNegado):                                       # super admin não envia
            S.solicitar_assinatura(self.su, self.vid, SIGNATARIO, self.prov)
        vid2, _ = S.iniciar_vistoria(self.vist, dados_vistoria())
        with self.assertRaises(S.ErroNegocio):                                      # vistoria não concluída
            S.solicitar_assinatura(self.vist, vid2, SIGNATARIO, self.prov)

    def test_laudo_substituido_com_assinatura_pendente(self):
        aid = S.solicitar_assinatura(self.vist, self.vid, SIGNATARIO, self.prov)
        d = dados_vistoria()
        S.finalizar_vistoria(self.vist, self.vid, d, b"%PDF-1.4 nova versao")          # finaliza de novo
        a = S.listar_assinaturas_vistoria(self.vist, self.vid)[0]
        self.assertTrue(a["versao_anterior"])
        self.assertEqual(a["status"], "aguardando")
        self.assertEqual(S.cancelar_assinaturas_substituidas(self.vist, self.vid, self.prov), 1)
        self.assertEqual(S.listar_assinaturas_vistoria(self.vist, self.vid)[0]["status"], "cancelado")
        # reenvio: vai o PDF novo
        aid2 = S.solicitar_assinatura(self.vist, self.vid, SIGNATARIO, self.prov)
        self.assertNotEqual(aid, aid2)
        novo = S.listar_assinaturas_vistoria(self.vist, self.vid)[0]
        self.assertEqual(self.prov.envelopes[novo["id_externo"]]["pdf"], b"%PDF-1.4 nova versao")
        self.assertFalse(novo["versao_anterior"])

    def test_pdf_do_laudo_ausente(self):
        rel = S.listar_laudos(self.vist)[0]["arquivo_pdf"]
        (self.tmp / rel).unlink()
        with self.assertRaises(S.ErroNegocio):
            S.solicitar_assinatura(self.vist, self.vid, SIGNATARIO, self.prov)


class TestIsolamentoAssinaturas(Base):
    def test_outra_empresa_nao_acessa(self):
        _, admin_a, vist_a = self.nova_empresa("Alfa")
        _, admin_b, vist_b = self.nova_empresa("Beta")
        vid_a, _ = self.vistoria_concluida(vist_a)
        prov = ProvedorFake()
        aid = S.solicitar_assinatura(vist_a, vid_a, SIGNATARIO, prov)
        prov.simular(S.listar_assinaturas_vistoria(vist_a, vid_a)[0]["id_externo"], "assinado")
        S.atualizar_status_assinatura(vist_a, aid, prov, forcar=True)
        for ator in (admin_b, vist_b):
            for acao in (lambda: S.listar_assinaturas_vistoria(ator, vid_a),
                         lambda: S.atualizar_status_assinatura(ator, aid, prov, True),
                         lambda: S.salvar_documento_assinado(ator, aid, prov),
                         lambda: S.pdf_assinado(ator, aid, prov),
                         lambda: S.cancelar_assinatura(ator, aid, prov),
                         lambda: S.solicitar_assinatura(ator, vid_a, SIGNATARIO, prov)):
                with self.assertRaises(AcessoNegado):
                    acao()
        self.assertEqual(len(S.listar_assinaturas_vistoria(admin_a, vid_a)), 1)   # admin da própria empresa vê
        self.assertEqual(len(S.listar_assinaturas_vistoria(self.su, vid_a)), 1)    # super admin vê
        n_antes = len(list((self.tmp / "pdfs").rglob("*_assinado.pdf")))
        S.apagar_vistorias_empresa(admin_a)                                          # apaga também as assinaturas
        with db.conectar() as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM assinaturas_remotas").fetchone()[0], 0)
        self.assertEqual(len(list((self.tmp / "pdfs").rglob("*_assinado.pdf"))), n_antes - 1)


class TestTabelaEmBancoExistente(unittest.TestCase):
    """Banco criado pela versão anterior (schema 1, sem assinaturas_remotas) continua abrindo."""
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="laudo_schema_"))
        os.environ["LAUDO_DATA_DIR"] = str(self.tmp)
        self.schema = _banco.abrir()

    def tearDown(self):
        _banco.fechar(self.schema)
        os.environ.pop("LAUDO_DATA_DIR", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_tabela_criada_sem_perder_dados(self):
        db.inicializar()
        with db.conectar() as con:              # simula o banco antigo
            con.execute("DROP TABLE assinaturas_remotas")
            con.execute("UPDATE meta SET valor = '1' WHERE chave = 'schema_version'")
            con.execute("INSERT INTO planos(nome, created_at, updated_at) VALUES ('Antigo', '2026-01-01', '2026-01-01')")
        db.inicializar()                        # abrir o app de novo
        self.assertEqual(db.meta_get("schema_version"), str(db.SCHEMA_VERSION))
        with db.conectar() as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM assinaturas_remotas").fetchone()[0], 0)
            self.assertEqual(con.execute("SELECT nome FROM planos").fetchone()[0], "Antigo")
        self.assertIn("assinaturas_remotas", db.TABELAS)

    def test_schema_postgres_convertido(self):
        s = db._schema_pg()
        trecho = s[s.index("CREATE TABLE IF NOT EXISTS assinaturas_remotas"):]
        trecho = trecho[:trecho.index(");") + 2]
        self.assertIn("BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY", trecho)
        self.assertIn("empresa_id          BIGINT NOT NULL REFERENCES empresas(id)", trecho)
        self.assertNotIn("AUTOINCREMENT", trecho)
        self.assertNotIn("--", trecho)


def _resp(status=200, corpo=None, conteudo=None):
    r = mock.Mock()
    r.status_code = status
    r.content = conteudo if conteudo is not None else (json.dumps(corpo).encode() if corpo is not None else b"")
    r.json = (lambda: corpo) if corpo is not None else mock.Mock(side_effect=ValueError)
    return r


class TestClicksignHTTP(unittest.TestCase):
    """Requisições montadas conforme a documentação oficial (sem rede: requests simulado)."""
    def setUp(self):
        self.cfg = assinatura.carregar_config({"provider": "clicksign", "api_key": TOKEN}, {})
        self.prov = assinatura.criar_provedor(self.cfg)
        self.assertIsInstance(self.prov, ProvedorClicksign)

    def test_fluxo_de_envio(self):
        respostas = [_resp(201, {"data": {"id": "ENV"}}), _resp(201, {"data": {"id": "DOC"}}),
                     _resp(201, {"data": {"id": "SIG"}}), _resp(201, {"data": {"id": "R1"}}),
                     _resp(201, {"data": {"id": "R2"}}), _resp(200, {"data": {"id": "ENV"}}),
                     _resp(201, {"data": {"id": "N"}})]
        sig = assinatura.Signatario("Maria Souza", "maria@cliente.com", "85999990000", "52998224725", "whatsapp")
        with mock.patch("assinatura.clicksign.requests.request", side_effect=respostas) as req:
            res = self.prov.enviar_para_assinatura("Laudo X", "X.pdf", b"%PDF-1.4 x", sig)
        self.assertEqual((res.id_externo, res.documento_id, res.signatario_id, res.link), ("ENV", "DOC", "SIG", ""))
        chamadas = [(c.args[0], c.args[1].replace(self.cfg.base_url, "")) for c in req.call_args_list]
        self.assertEqual(chamadas, [("POST", "/envelopes"), ("POST", "/envelopes/ENV/documents"),
                                    ("POST", "/envelopes/ENV/signers"), ("POST", "/envelopes/ENV/requirements"),
                                    ("POST", "/envelopes/ENV/requirements"), ("PATCH", "/envelopes/ENV"),
                                    ("POST", "/envelopes/ENV/notifications")])
        for c in req.call_args_list:
            self.assertEqual(c.kwargs["headers"]["Authorization"], TOKEN)
            self.assertEqual(c.kwargs["headers"]["Content-Type"], "application/vnd.api+json")
            self.assertEqual(c.kwargs["timeout"], 20.0)
        doc = json.loads(req.call_args_list[1].kwargs["data"])["data"]["attributes"]
        self.assertTrue(doc["content_base64"].startswith("data:application/pdf;base64,"))
        signer = json.loads(req.call_args_list[2].kwargs["data"])["data"]["attributes"]
        self.assertEqual(signer["phone_number"], "85999990000")
        self.assertEqual(signer["documentation"], "529.982.247-25")
        self.assertEqual(signer["communicate_events"]["signature_request"], "whatsapp")
        ativa = json.loads(req.call_args_list[5].kwargs["data"])["data"]
        self.assertEqual(ativa["attributes"]["status"], "running")
        # CPF inválido (ex.: 123.456.789-00) não é enviado: a Clicksign recusaria ("documentation - inválido")
        respostas = [_resp(201, {"data": {"id": "SIG"}}), _resp(201, {"data": {"id": "R1"}}), _resp(201, {"data": {"id": "R2"}})]
        with mock.patch("assinatura.clicksign.requests.request", side_effect=respostas) as req:
            self.prov.adicionar_signatario("ENV", "DOC", assinatura.Signatario("Maria Souza", "m@x.com", "", "12345678900", "email"))
        self.assertNotIn("documentation", json.loads(req.call_args_list[0].kwargs["data"])["data"]["attributes"])

    def test_cpf_valido(self):
        from assinatura.base import cpf_valido
        self.assertTrue(cpf_valido("529.982.247-25"))
        for ruim in ("123.456.789-00", "111.111.111-11", "123", "", None):
            self.assertFalse(cpf_valido(ruim))

    def test_status_normalizado(self):
        casos = [("draft", [], "rascunho"), ("running", [], "aguardando"), ("closed", [], "assinado"),
                 ("canceled", [], "cancelado"), ("running", ["refusal"], "recusado"), ("canceled", ["deadline"], "expirado")]
        for bruto, eventos, esperado in casos:
            resp = [_resp(200, {"data": {"attributes": {"status": bruto}}})]
            if bruto in ("running", "canceled"):
                resp.append(_resp(200, {"data": [{"attributes": {"name": n}} for n in eventos]}))
            with mock.patch("assinatura.clicksign.requests.request", side_effect=resp):
                self.assertEqual(self.prov.consultar_status("ENV", "DOC").status, esperado, bruto)

    def test_timeout_e_erros_viram_mensagem_amigavel_sem_token(self):
        with self.assertLogs("assinatura", level=logging.INFO) as logs:
            for efeito, trecho in ((requests.Timeout("x"), "não respondeu a tempo"),
                                   (requests.ConnectionError("x"), "Não foi possível conectar"),
                                   ([_resp(401, {"errors": []})], "recusou as credenciais"),
                                   ([_resp(503, {})], "indisponível"),
                                   ([_resp(422, {"errors": [{"detail": "email maria@cliente.com inválido"}]})], "recusados")):
                with mock.patch("assinatura.clicksign.requests.request", side_effect=efeito):
                    with self.assertRaises(assinatura.ErroProvedor) as ctx:
                        self.prov.criar_solicitacao("Laudo")
                self.assertIn(trecho, str(ctx.exception))
                self.assertNotIn(TOKEN, str(ctx.exception))
                self.assertNotIn("maria@cliente.com", str(ctx.exception))
        self.assertFalse(any(TOKEN in linha for linha in logs.output))

    def test_baixar_assinado(self):
        resp = _resp(200, {"data": {"links": {"files": {"original": "https://s3/o", "signed": "https://s3/s"}}}})
        with mock.patch("assinatura.clicksign.requests.request", return_value=resp), \
                mock.patch("assinatura.clicksign.requests.get", return_value=_resp(200, conteudo=b"%PDF-assinado")) as g:
            self.assertEqual(self.prov.baixar_documento_assinado("ENV", "DOC"), b"%PDF-assinado")
        self.assertNotIn("headers", g.call_args.kwargs)            # token não vai para a URL de download
        sem = _resp(200, {"data": {"links": {"files": {"original": "https://s3/o"}}}})
        with mock.patch("assinatura.clicksign.requests.request", return_value=sem):
            with self.assertRaises(assinatura.ErroProvedor):
                self.prov.baixar_documento_assinado("ENV", "DOC")


if __name__ == "__main__":
    unittest.main()
