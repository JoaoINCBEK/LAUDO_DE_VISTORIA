"""Fluxo completo rodando o app.py de verdade (streamlit.testing), em pasta temporária.

Super Admin cria empresa + administrador -> administrador troca a senha provisória e
cria vistoriador -> vistoriador faz a vistoria pelas 10 etapas e finaliza (PDF real do
pdf_report) -> administrador vê a vistoria/laudo -> Super Admin vê o consumo.
Também abre todas as telas de cada perfil verificando que nenhuma gera exceção.

    python -m unittest tests.test_fluxo_app -v
"""
import base64
import io
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PIL import Image                             # noqa: E402
from streamlit.testing.v1 import AppTest          # noqa: E402

from saas import migracao, servicos as S          # noqa: E402
import paineis                                    # noqa: E402
import _banco                                     # noqa: E402


class FluxoApp(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="laudo_app_"))
        os.environ["LAUDO_DATA_DIR"] = str(self.tmp)
        self.schema = _banco.abrir()
        migracao.garantir_super_admin("dono", "Dono", "SenhaDono2026")
        self.cwd = os.getcwd()
        os.chdir(RAIZ)

    def tearDown(self):
        os.chdir(self.cwd)
        _banco.fechar(self.schema)
        os.environ.pop("LAUDO_DATA_DIR", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- utilidades --------------------------------------------------------
    def app(self, token=None):
        at = AppTest.from_file(str(RAIZ / "app.py"), default_timeout=90)
        if token:
            at.query_params["ac_token"] = token
        at.run()
        self.ok(at, "abrir app")
        return at

    def recarregar(self, at):
        """Nova execução a partir do token da URL (como recarregar a página no celular).
        Necessário porque o AppTest mantém elementos antigos depois de st.rerun()."""
        token = at.query_params.get("ac_token")
        token = token[0] if isinstance(token, list) else token
        return self.app(token)

    def ok(self, at, onde):
        self.assertFalse(at.exception, f"{onde}: {[e.value for e in at.exception]}")

    def entrar(self, at, login, senha):
        at.text_input[0].input(login)
        at.text_input[1].input(senha)
        next(b for b in at.button if b.label == "Entrar").click()
        at.run()
        self.ok(at, f"login {login}")
        self.assertIsNotNone(at.session_state["user"], f"login {login} falhou: {[e.value for e in at.error]}")
        return self.recarregar(at)

    def botao(self, at, rotulo, recarregar=True):
        b = next((b for b in at.button if b.label == rotulo), None)
        self.assertIsNotNone(b, f"botão '{rotulo}' não encontrado: {[x.label for x in at.button]}")
        b.click()
        at.run()
        self.ok(at, rotulo)
        return self.recarregar(at) if recarregar else at

    def todas_as_paginas(self, at, perfil):
        for page, _ in paineis.MENU[perfil] + [("conta", "")]:
            at.session_state["page"] = page
            at.session_state["step"] = 0
            at.run()
            self.ok(at, f"{perfil}/{page}")

    def trocar_senha_obrigatoria(self, at, nova):
        self.assertTrue(any("Crie sua senha" in m.value for m in at.markdown), "tela de troca obrigatória não apareceu")
        at.text_input[0].input(nova)
        at.text_input[1].input(nova)
        return self.botao(at, "Salvar e continuar")

    # -- teste --------------------------------------------------------------
    def test_fluxo_completo(self):
        # 1) Super Admin: cria empresa e o primeiro administrador
        at = self.app()
        at = self.entrar(at, "dono", "SenhaDono2026")
        self.todas_as_paginas(at, "super_admin")
        su = at.session_state["user"]
        pid = S.salvar_plano(su, {"nome": "Plano Anual", "limite_vistorias": 1200, "limite_usuarios": 10, "duracao_meses": 12})
        eid = S.criar_empresa(su, {"nome": "Auto Vistoria Fortaleza", "cnpj": "00.000.000/0001-00", "plano_id": pid,
                                   "limite_vistorias": 1200, "limite_usuarios": 10})
        _, senha_admin = S.criar_usuario(su, {"nome": "Ana Admin", "email": "ana@autovf.com", "perfil": "admin"}, empresa_id=eid)
        at.session_state["page"] = "sa_empresa"
        at.session_state["empresa_aberta"] = eid
        at.run()
        self.ok(at, "gerenciar empresa")
        for aba in ["Editar e plano", "Usuários", "Vistorias", "Veículos", "Laudos", "Logs", "Resumo"]:
            at.radio[0].set_value(aba)
            at.run()
            self.ok(at, f"gerenciar empresa/{aba}")

        # 2) Administrador: senha provisória -> troca obrigatória -> cria vistoriador
        at = self.app()
        at = self.entrar(at, "ana@autovf.com", senha_admin)
        at = self.trocar_senha_obrigatoria(at, "SenhaDaAna2026")
        self.todas_as_paginas(at, "admin")
        admin = at.session_state["user"]
        _, senha_vist = S.criar_usuario(admin, {"nome": "João Silva", "login": "joao", "perfil": "vistoriador"})

        # 3) Vistoriador: nova vistoria pelas 10 etapas e finalização com PDF
        at = self.app()
        at = self.entrar(at, "joao", senha_vist)
        at = self.trocar_senha_obrigatoria(at, "SenhaDoJoao2026")
        self.todas_as_paginas(at, "vistoriador")
        at.session_state["page"] = "inicio_vist"
        at.run()
        at = self.botao(at, "＋ Nova vistoria")
        insp = at.session_state["inspection"]
        self.assertTrue(insp["numero"].endswith("000001"))
        self.assertEqual(at.session_state["step"], 1)
        # dados preenchidos (sem disparar a consulta externa de placa)
        insp["veiculo"].update(marca="HONDA", modelo="CIVIC", placa="ABC1D23", ano="2020", cor="PRETO", km="45000")
        insp["proprietario"].update(nome="Maria Cliente", cpf="123.456.789-00", telefone="85999990000")
        insp["acessorios"]["Estepe"]["status"] = "sim"
        # desenho de avarias salvo pela versão antiga (400x460, traços embutidos): tem que continuar abrindo
        buf = io.BytesIO()
        Image.new("RGB", (400, 460), "white").save(buf, "PNG")
        legado = base64.b64encode(buf.getvalue()).decode()
        insp["avarias"].update(diagrama="sedan", imagem=legado, imagem_ok=True,
                               marcacoes=[{"tipo": "Avaria", "severidade": "Marcada", "descricao": "x"}])
        for passo in range(1, 10):
            at = self.botao(at, "Próxima etapa →")
            self.assertEqual(at.session_state["step"], passo + 1)
        av = at.session_state["inspection"]["avarias"]
        self.assertEqual((av["imagem_legada"], av["imagem"], len(av["marcacoes"])), (legado, legado, 1))
        at = self.botao(at, "✓ Finalizar inspeção e preparar PDF", recarregar=False)
        self.assertTrue(any("finalizada" in s.value for s in at.success), [e.value for e in at.error])
        self.assertTrue(at.session_state["pdf"].startswith(b"%PDF"))
        # assinatura à distância sem Secrets: botão aparece desabilitado e o app segue normal
        env = next((b for b in at.button if b.label == "Enviar para assinatura à distância"), None)
        self.assertIsNotNone(env, [b.label for b in at.button])
        self.assertTrue(env.disabled)
        vist = at.session_state["user"]
        minhas = S.listar_vistorias(vist)
        self.assertEqual(len(minhas), 1)
        self.assertEqual(minhas[0]["status"], "concluida")
        self.assertEqual(minhas[0]["vistoriador_nome"], "João Silva")

        # 4) Administrador vê a vistoria, o laudo (PDF), o veículo e o cliente
        vs = S.listar_vistorias(admin)
        self.assertEqual([v["placa"] for v in vs], ["ABC1D23"])
        pdf, _ = S.pdf_do_laudo(admin, vs[0]["laudo_id"])
        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertEqual(S.listar_veiculos(admin)[0]["placa"], "ABC1D23")
        self.assertEqual(S.listar_clientes(admin)[0]["nome"], "Maria Cliente")
        ind = S.indicadores_empresa(admin)
        self.assertEqual((ind["total_vistorias"], ind["laudos_emitidos"], ind["consumo"]["vistorias_utilizadas"]), (1, 1, 1))
        acoes = [l["acao"] for l in S.listar_logs(admin)]
        for esperado in ("vistoria_iniciada", "vistoria_finalizada", "laudo_gerado", "usuario_criado"):
            self.assertIn(esperado, acoes)
        at = self.app()
        at = self.entrar(at, "ana@autovf.com", "SenhaDaAna2026")
        at.session_state["page"] = "vistorias"
        at.session_state["vistoria_aberta"] = vs[0]["id"]
        at.run()
        self.ok(at, "detalhe da vistoria")
        self.assertTrue(any(b.label == "⬇ Baixar laudo em PDF" for b in at.get("download_button")))

        # 5) Super Admin acompanha o consumo
        emp = [e for e in S.listar_empresas(su) if e["id"] == eid][0]
        self.assertEqual((emp["vistorias_utilizadas"], emp["limite_vistorias"], emp["usuarios_ativos"]), (1, 1200, 2))


if __name__ == "__main__":
    unittest.main()
