"""Testes da camada multiempresa (não usam o Streamlit nem os dados reais).

Rodar na pasta do projeto:
    python -m unittest discover -s tests -v

Cada teste usa uma pasta temporária própria (LAUDO_DATA_DIR), então os dados
reais em autocheck_data/ nunca são tocados.
"""
import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from saas import db, migracao, seguranca, servicos as S          # noqa: E402
from saas.rbac import AcessoNegado                               # noqa: E402
import _banco                                                    # noqa: E402

PDF = b"%PDF-1.4 teste"


def dados_vistoria(placa="ABC1D23", dono="Maria Souza", cpf="123.456.789-00"):
    return {
        "numero": "", "status": "Em andamento",
        "veiculo": {"marca": "HONDA", "modelo": "CIVIC", "ano": "2020", "placa": placa, "cor": "PRETO", "km": "50000"},
        "combustivel": {"tipo": "Flex", "nivel": "1/2", "percentual": 50},
        "acessorios": {}, "pneus": {}, "fotos": {}, "fotos_acessorios": [],
        "avarias": {"diagrama": "sedan", "imagem": None, "marcacoes": [], "fotos": []},
        "proprietario": {"nome": dono, "cpf": cpf, "telefone": "85999990000", "assinatura": None},
        "emitente": {},
    }


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="laudo_teste_"))
        os.environ["LAUDO_DATA_DIR"] = str(self.tmp)
        self.schema = _banco.abrir()
        db.inicializar()
        migracao.garantir_super_admin("root", "Dono da Plataforma", "senhaForte123")
        self.su, _ = S.autenticar("root", "senhaForte123")

    def tearDown(self):
        _banco.fechar(self.schema)
        os.environ.pop("LAUDO_DATA_DIR", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def entrar(self, login, senha):
        ator, msg = S.autenticar(login, senha)
        self.assertIsNotNone(ator, msg)
        return ator

    def nova_empresa(self, nome, limite_vistorias=None, limite_usuarios=None):
        eid = S.criar_empresa(self.su, {"nome": nome, "limite_vistorias": limite_vistorias,
                                        "limite_usuarios": limite_usuarios})
        _, senha = S.criar_usuario(self.su, {"nome": f"Admin {nome}", "email": f"admin@{nome.lower()}.com",
                                             "perfil": "admin", "senha": "adminSenha1"}, empresa_id=eid)
        admin = self.entrar(f"admin@{nome.lower()}.com", "adminSenha1")
        S.criar_usuario(admin, {"nome": f"Vist {nome}", "login": f"vist_{nome.lower()}", "perfil": "vistoriador",
                                "senha": "vistSenha1"})
        vist = self.entrar(f"vist_{nome.lower()}", "vistSenha1")
        return eid, admin, vist

    def vistoria_concluida(self, vist, **kw):
        d = dados_vistoria(**kw)
        vid, numero = S.iniciar_vistoria(vist, d)
        d["numero"] = numero
        lid = S.finalizar_vistoria(vist, vid, d, PDF)
        return vid, lid


class TestIsolamento(Base):
    def setUp(self):
        super().setUp()
        self.ea, self.admin_a, self.vist_a = self.nova_empresa("Alfa")
        self.eb, self.admin_b, self.vist_b = self.nova_empresa("Beta")
        self.vid_a, self.lid_a = self.vistoria_concluida(self.vist_a, placa="AAA1A11", dono="Cliente Alfa", cpf="111")
        self.vid_b, self.lid_b = self.vistoria_concluida(self.vist_b, placa="BBB2B22", dono="Cliente Beta", cpf="222")

    def _checar_isolado(self, admin, vist, meu_vid, outro_vid, outro_lid, outra_emp, outra_placa):
        for ator in (admin, vist):
            vs = S.listar_vistorias(ator)
            self.assertTrue(all(v["empresa_id"] == ator.empresa_id for v in vs))
            self.assertNotIn(outro_vid, [v["id"] for v in vs])
            self.assertIn(meu_vid, [v["id"] for v in vs])
            ls = S.listar_laudos(ator)
            self.assertNotIn(outro_lid, [l["id"] for l in ls])
            with self.assertRaises(AcessoNegado):
                S.obter_vistoria(ator, outro_vid)
            with self.assertRaises(AcessoNegado):
                S.pdf_do_laudo(ator, outro_lid)
            with self.assertRaises(AcessoNegado):
                S.listar_vistorias(ator, empresa_id=outra_emp)       # empresa_id forjado na "tela"
            with self.assertRaises(AcessoNegado):
                S.retomar_vistoria(ator, outro_vid)
            with self.assertRaises(AcessoNegado):
                S.finalizar_vistoria(ator, outro_vid, dados_vistoria(), PDF)
        # admin: veículos, clientes, usuários, logs, indicadores
        placas = [v["placa"] for v in S.listar_veiculos(admin)]
        self.assertNotIn(outra_placa, placas)
        self.assertEqual(len(placas), 1)
        self.assertEqual(len(S.listar_clientes(admin)), 1)
        self.assertTrue(all(u["empresa_id"] == admin.empresa_id for u in S.listar_usuarios(admin)))
        self.assertTrue(all(l["empresa_id"] == admin.empresa_id for l in S.listar_logs(admin)))
        ind = S.indicadores_empresa(admin)
        self.assertEqual(ind["total_vistorias"], 1)
        self.assertEqual(ind["total_veiculos"], 1)
        self.assertEqual(ind["laudos_emitidos"], 1)
        for fn in (S.listar_veiculos, S.listar_clientes, S.listar_usuarios, S.listar_logs,
                   S.indicadores_empresa, S.obter_empresa):
            with self.assertRaises(AcessoNegado):
                fn(admin, empresa_id=outra_emp)
        outro_veic = S.listar_veiculos(self.su, empresa_id=outra_emp)[0]["id"]
        with self.assertRaises(AcessoNegado):
            S.historico_veiculo(admin, outro_veic)
        outro_cli = S.listar_clientes(self.su, empresa_id=outra_emp)[0]["id"]
        with self.assertRaises(AcessoNegado):
            S.historico_cliente(admin, outro_cli)
        outro_user = [u for u in S.listar_usuarios(self.su, empresa_id=outra_emp)][0]["id"]
        for acao in (lambda: S.atualizar_usuario(admin, outro_user, {"nome": "x"}),
                     lambda: S.definir_status_usuario(admin, outro_user, "inativo"),
                     lambda: S.redefinir_acesso(admin, outro_user)):
            with self.assertRaises(AcessoNegado):
                acao()
        with self.assertRaises(AcessoNegado):
            S.atualizar_empresa(admin, outra_emp, {"nome": "Invadida"})

    def test_empresa_b_nao_ve_empresa_a(self):
        self._checar_isolado(self.admin_b, self.vist_b, self.vid_b, self.vid_a, self.lid_a, self.ea, "AAA1A11")

    def test_empresa_a_nao_ve_empresa_b(self):
        self._checar_isolado(self.admin_a, self.vist_a, self.vid_a, self.vid_b, self.lid_b, self.eb, "BBB2B22")

    def test_mesma_placa_em_duas_empresas_fica_separada(self):
        self.vistoria_concluida(self.vist_a, placa="ZZZ9Z99", dono="X", cpf="999")
        self.vistoria_concluida(self.vist_b, placa="ZZZ9Z99", dono="Y", cpf="999")
        va = [v for v in S.listar_veiculos(self.admin_a) if v["placa"] == "ZZZ9Z99"]
        vb = [v for v in S.listar_veiculos(self.admin_b) if v["placa"] == "ZZZ9Z99"]
        self.assertEqual(len(va), 1)
        self.assertEqual(len(vb), 1)
        self.assertNotEqual(va[0]["id"], vb[0]["id"])
        self.assertEqual(S.historico_veiculo(self.admin_a, va[0]["id"])[0]["cliente_id"],
                         S.listar_clientes(self.admin_a, busca="X")[0]["id"])

    def test_numeracao_por_empresa(self):
        na = S.obter_vistoria(self.admin_a, self.vid_a)["numero"]
        nb = S.obter_vistoria(self.admin_b, self.vid_b)["numero"]
        self.assertTrue(na.endswith("000001") and nb.endswith("000001"))

    def test_super_admin_ve_tudo_mas_nao_edita_vistorias(self):
        self.assertEqual(len(S.listar_vistorias(self.su)), 2)
        self.assertEqual(len(S.listar_vistorias(self.su, empresa_id=self.ea)), 1)
        S.obter_vistoria(self.su, self.vid_a)
        with self.assertRaises(AcessoNegado):
            S.iniciar_vistoria(self.su, dados_vistoria())
        with self.assertRaises(AcessoNegado):
            S.retomar_vistoria(self.su, self.vid_a)

    def test_super_admin_exclui_empresa_por_completo(self):
        S.salvar_emitente(self.admin_a, {"empresa": "Emitente Alfa"})
        token_a = S.criar_sessao(self.admin_a)
        pdf_a = S.caminho_pdf(S.listar_laudos(self.su, empresa_id=self.ea)[0]["arquivo_pdf"])
        self.assertTrue(pdf_a.exists())
        # só o Super Admin, e só com o nome exato
        for ator in (self.admin_a, self.vist_a, self.admin_b):
            with self.assertRaises(AcessoNegado):
                S.excluir_empresa(ator, self.ea, "Alfa")
        with self.assertRaises(S.ErroNegocio):
            S.excluir_empresa(self.su, self.ea, "Beta")

        r = S.excluir_empresa(self.su, self.ea, "Alfa")
        self.assertEqual((r["vistorias"], r["laudos"], r["usuarios"]), (1, 1, 2))
        self.assertNotIn(self.ea, [e["id"] for e in S.listar_empresas(self.su)])
        self.assertFalse(pdf_a.exists())
        self.assertIsNone(S.autenticar("admin@alfa.com", "adminSenha1")[0])
        self.assertIsNone(S.validar_sessao(token_a))
        with db.conectar() as con:
            for tabela in ("vistorias", "laudos", "usuarios", "clientes", "veiculos", "emitentes",
                           "sessoes", "assinaturas_remotas"):
                n = con.execute(f"SELECT COUNT(*) FROM {tabela} WHERE empresa_id = ?", (self.ea,)).fetchone()[0]
                self.assertEqual(n, 0, tabela)
        # logs antigos mantidos (marcados com o nome) + registro da exclusão
        logs = S.listar_logs(self.su, limite=1000)
        self.assertTrue(any(l["descricao"].startswith("[Alfa] ") for l in logs))
        self.assertTrue(any(l["acao"] == "empresa_excluida" and "Alfa" in l["descricao"] for l in logs))
        # a outra empresa não foi afetada
        self.assertEqual(len(S.listar_vistorias(self.admin_b)), 1)
        self.assertTrue(S.caminho_pdf(S.listar_laudos(self.admin_b)[0]["arquivo_pdf"]).exists())


class TestPermissoes(Base):
    def setUp(self):
        super().setUp()
        self.ea, self.admin, self.vist = self.nova_empresa("Gama")

    def test_vistoriador_sem_acesso_administrativo(self):
        v = self.vist
        for acao in (lambda: S.listar_usuarios(v), lambda: S.criar_usuario(v, {"nome": "x", "login": "xxx"}),
                     lambda: S.listar_empresas(v), lambda: S.criar_empresa(v, {"nome": "x"}),
                     lambda: S.listar_logs(v), lambda: S.listar_veiculos(v), lambda: S.listar_clientes(v),
                     lambda: S.indicadores_empresa(v), lambda: S.salvar_plano(v, {"nome": "x"}),
                     lambda: S.apagar_vistorias_empresa(v), lambda: S.ajustar_consumo(v, self.ea, 0),
                     lambda: S.atualizar_empresa(v, None, {"nome": "x"})):
            with self.assertRaises(AcessoNegado):
                acao()

    def test_admin_nao_gerencia_plataforma(self):
        a = self.admin
        for acao in (lambda: S.listar_empresas(a), lambda: S.criar_empresa(a, {"nome": "x"}),
                     lambda: S.definir_status_empresa(a, self.ea, "ativa"), lambda: S.salvar_plano(a, {"nome": "p"}),
                     lambda: S.ajustar_consumo(a, self.ea, 0),
                     lambda: S.criar_usuario(a, {"nome": "x", "login": "rootx", "perfil": "super_admin", "senha": "senhaForte1"})):
            with self.assertRaises(AcessoNegado):
                acao()
        # admin não altera plano/limites (só dados cadastrais)
        S.atualizar_empresa(a, None, {"nome": "Gama Nova", "limite_vistorias": 99999})
        self.assertIsNone(S.obter_empresa(a)["limite_vistorias"])
        self.assertEqual(S.obter_empresa(a)["nome"], "Gama Nova")

    def test_vistoriador_so_ve_as_proprias(self):
        S.criar_usuario(self.admin, {"nome": "Outro", "login": "outro", "perfil": "vistoriador", "senha": "outroSenha1"})
        outro = self.entrar("outro", "outroSenha1")
        vid, _ = self.vistoria_concluida(self.vist)
        self.assertEqual(S.listar_vistorias(outro), [])
        self.assertEqual(S.listar_laudos(outro), [])
        with self.assertRaises(AcessoNegado):
            S.obter_vistoria(outro, vid)
        self.assertEqual(len(S.listar_vistorias(self.admin)), 1)       # admin vê todas da empresa
        # filtro por outro usuário não "fura" a restrição do vistoriador
        self.assertEqual(S.listar_vistorias(outro, usuario_id=self.vist.id), [])

    def test_admin_nao_troca_proprio_perfil_nem_se_desativa(self):
        with self.assertRaises(S.ErroNegocio):
            S.definir_status_usuario(self.admin, self.admin.id, "inativo")
        with self.assertRaises(S.ErroNegocio):
            S.atualizar_usuario(self.admin, self.admin.id, {"nome": "A", "perfil": "vistoriador"})


class TestLimites(Base):
    def test_limite_de_vistorias(self):
        eid, admin, vist = self.nova_empresa("Delta", limite_vistorias=2)
        v1, _ = S.iniciar_vistoria(vist, dados_vistoria())
        v2, _ = S.iniciar_vistoria(vist, dados_vistoria())
        self.assertFalse(S.pode_iniciar_vistoria(vist)[0])
        with self.assertRaises(S.ErroNegocio):
            S.iniciar_vistoria(vist, dados_vistoria())
        S.cancelar_vistoria(vist, v2)                           # cancelar libera a reserva
        self.assertTrue(S.pode_iniciar_vistoria(vist)[0])
        S.finalizar_vistoria(vist, v1, dados_vistoria(), PDF)
        S.finalizar_vistoria(vist, v1, dados_vistoria(), PDF)   # finalizar de novo não consome outra vez
        self.assertEqual(S.obter_empresa(admin)["vistorias_utilizadas"], 1)
        v3, _ = S.iniciar_vistoria(vist, dados_vistoria())
        S.finalizar_vistoria(vist, v3, dados_vistoria(), PDF)
        self.assertEqual(S.obter_empresa(admin)["restantes"], 0)
        self.assertFalse(S.pode_iniciar_vistoria(vist)[0])
        S.atualizar_empresa(self.su, eid, {"nome": "Delta", "limite_vistorias": 5})
        self.assertTrue(S.pode_iniciar_vistoria(vist)[0])
        # laudo anterior fica como 'substituido', apenas 1 emitido por vistoria
        self.assertEqual(len([l for l in S.listar_laudos(admin, incluir_substituidos=True) if l["vistoria_id"] == v1]), 2)
        self.assertEqual(len([l for l in S.listar_laudos(admin) if l["vistoria_id"] == v1]), 1)

    def test_plano_vencido_bloqueia_novas(self):
        eid, admin, vist = self.nova_empresa("Epsilon")
        S.atualizar_empresa(self.su, eid, {"nome": "Epsilon", "data_inicio": "2020-01-01", "data_vencimento": "2020-12-31"})
        self.assertFalse(S.pode_iniciar_vistoria(vist)[0])

    def test_limite_de_usuarios(self):
        eid, admin, vist = self.nova_empresa("Zeta", limite_usuarios=2)   # admin + vistoriador = 2
        with self.assertRaises(S.ErroNegocio):
            S.criar_usuario(admin, {"nome": "Terceiro", "login": "terceiro", "senha": "terceiro123"})
        S.definir_status_usuario(admin, vist.id, "inativo")
        S.criar_usuario(admin, {"nome": "Terceiro", "login": "terceiro", "senha": "terceiro123"})
        with self.assertRaises(S.ErroNegocio):
            S.definir_status_usuario(admin, vist.id, "ativo")      # reativar também respeita o limite


class TestSessoesESenhas(Base):
    def test_sessao_revogada_por_bloqueio_desativacao_e_reset(self):
        eid, admin, vist = self.nova_empresa("Eta")
        tok = S.criar_sessao(vist)
        self.assertEqual(S.validar_sessao(tok).id, vist.id)
        S.definir_status_empresa(self.su, eid, "bloqueada")
        self.assertIsNone(S.validar_sessao(tok))
        self.assertIsNone(S.autenticar("vist_eta", "vistSenha1")[0])
        S.definir_status_empresa(self.su, eid, "ativa")
        tok = S.criar_sessao(self.entrar("vist_eta", "vistSenha1"))
        S.definir_status_usuario(admin, vist.id, "inativo")
        self.assertIsNone(S.validar_sessao(tok))
        S.definir_status_usuario(admin, vist.id, "ativo")
        tok = S.criar_sessao(self.entrar("vist_eta", "vistSenha1"))
        provisoria = S.redefinir_acesso(admin, vist.id)
        self.assertIsNone(S.validar_sessao(tok))
        nova = self.entrar("vist_eta", provisoria)
        self.assertTrue(S.precisa_trocar_senha(nova))
        S.trocar_senha(nova, None, "minhaNova123", obrigatoria=True)
        self.assertFalse(S.precisa_trocar_senha(nova))
        tok = S.criar_sessao(nova)
        S.encerrar_sessao(tok, nova)
        self.assertIsNone(S.validar_sessao(tok))
        self.assertIsNone(S.validar_sessao("token-inventado"))

    def test_sessao_expirada(self):
        tok = S.criar_sessao(self.su)
        with db.conectar() as con:
            con.execute("UPDATE sessoes SET expira_em = '2000-01-01 00:00:00'")
        self.assertIsNone(S.validar_sessao(tok))

    def test_bloqueio_por_tentativas(self):
        for _ in range(seguranca.MAX_TENTATIVAS):
            self.assertIsNone(S.autenticar("root", "errada")[0])
        ator, msg = S.autenticar("root", "senhaForte123")
        self.assertIsNone(ator)
        self.assertIn("tentativas", msg)

    def test_senha_nunca_em_texto_puro(self):
        with db.conectar() as con:
            h = con.execute("SELECT senha_hash FROM usuarios WHERE login = 'root'").fetchone()[0]
        self.assertTrue(h.startswith("pbkdf2_sha256$"))
        self.assertNotIn("senhaForte123", h)

    def test_senha_aceita_minusculas(self):
        eid, admin, _ = self.nova_empresa("Teta")
        S.criar_usuario(admin, {"nome": "Min", "login": "minusculo", "senha": "tudominusculo"})
        self.entrar("minusculo", "tudominusculo")


class TestMigracao(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="laudo_mig_"))
        os.environ["LAUDO_DATA_DIR"] = str(self.tmp)
        self.schema = _banco.abrir()
        sha = lambda s: hashlib.sha256(s.encode()).hexdigest()
        (self.tmp / "users.json").write_text(json.dumps([
            {"usuario": "admin", "nome": "Administrador", "senha": sha("admin123"), "perfil": "Administrador", "ativo": True},
            {"usuario": "joao", "nome": "João", "senha": sha("senhaDoJoao"), "perfil": "Inspetor", "ativo": True},
        ]), encoding="utf-8")
        insp = dados_vistoria()
        insp.update(numero="CHK-2026-000001", status="Concluído", inspetor="João",
                    criado_em="16/09/2026 21:26", finalizado_em="16/09/2026 21:41")
        (self.tmp / "inspections.json").write_text(json.dumps([insp]), encoding="utf-8")
        (self.tmp / "emitentes.json").write_text(json.dumps([{"empresa": "REBOQUE TESTE", "documento": "123"}]), encoding="utf-8")
        (self.tmp / "pdfs").mkdir()
        (self.tmp / "pdfs" / "CHK-2026-000001.pdf").write_bytes(PDF)
        self.hashes = {p.name: p.read_bytes() for p in self.tmp.glob("*.json")}

    def tearDown(self):
        _banco.fechar(self.schema)
        os.environ.pop("LAUDO_DATA_DIR", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_migracao(self):
        r = migracao.importar_legado()
        self.assertEqual((r["usuarios"], r["vistorias"], r["emitentes"]), (2, 1, 1))
        self.assertIsNone(migracao.importar_legado())                         # idempotente
        for nome, conteudo in self.hashes.items():                            # originais intactos
            self.assertEqual((self.tmp / nome).read_bytes(), conteudo)
        self.assertTrue(any(self.tmp.glob("backup_legado_*/users.json")))
        joao, _ = S.autenticar("joao", "senhaDoJoao")                        # senha antiga continua valendo
        self.assertEqual(joao.perfil, "vistoriador")
        with db.conectar() as con:                                            # e o hash foi atualizado
            self.assertTrue(con.execute("SELECT senha_hash FROM usuarios WHERE login='joao'").fetchone()[0]
                            .startswith("pbkdf2"))
        admin, _ = S.autenticar("admin", "admin123")
        self.assertEqual(admin.perfil, "admin")
        self.assertTrue(S.precisa_trocar_senha(admin))                        # senha de demonstração
        vs = S.listar_vistorias(admin)
        self.assertEqual(len(vs), 1)
        self.assertEqual(vs[0]["usuario_id"], joao.id)
        self.assertEqual(vs[0]["data_conclusao"], "2026-09-16 21:41:00")
        self.assertEqual(S.listar_vistorias(joao)[0]["id"], vs[0]["id"])      # João vê a vistoria dele
        pdf, nome = S.pdf_do_laudo(admin, vs[0]["laudo_id"])
        self.assertEqual(pdf, PDF)
        self.assertEqual(S.obter_empresa(admin)["nome"], "REBOQUE TESTE")
        self.assertEqual(S.obter_empresa(admin)["vistorias_utilizadas"], 1)
        self.assertEqual(len(S.listar_emitentes(admin)), 1)
        # a numeração da empresa migrada continua de onde parou
        _, numero = S.iniciar_vistoria(admin, dados_vistoria())
        self.assertTrue(numero.endswith("000002"))


if __name__ == "__main__":
    unittest.main()
