"""Área de desenho: validação dos traços, imagens geradas (assinatura / avarias),
compatibilidade com vistorias antigas e com o PDF.

    python -m unittest tests.test_desenho -v
"""
import base64
import io
import sys
import unittest
from pathlib import Path

from PIL import Image

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

import pdf_report                                  # noqa: E402
from desenho import render                         # noqa: E402

RISCO = {"c": render.COR_ARRANHAO, "w": 0.01, "p": [[0.1, 0.1], [0.3, 0.4], [0.5, 0.2]]}
AMASSADO = {"c": render.COR_AMASSADO, "w": 0.01, "p": [[0.7, 0.7]]}
ASSINATURA = [{"c": render.COR_ASSINATURA, "w": 0.012, "p": [[0.2, 0.5], [0.8, 0.3], [1.6, 0.7], [2.2, 0.4]]}]


def b64png(img):
    out = io.BytesIO()
    img.save(out, "PNG")
    return base64.b64encode(out.getvalue()).decode()


class TestTracos(unittest.TestCase):
    def test_limpar_tracos_rejeita_lixo(self):
        entrada = [RISCO, {"c": "#000000", "w": 0.01, "p": [[0, 0]]}, {"c": render.COR_AMASSADO, "w": "x", "p": [[0, 0]]},
                   {"c": render.COR_AMASSADO, "w": 0.01, "p": [[5, -3], ["a", 1], [float("nan"), 1]]}, "texto", None]
        out = render.limpar_tracos(entrada, "avarias")
        self.assertEqual(len(out), 2)
        self.assertEqual(out[1]["p"], [[1.0, 0.0]])                     # coordenadas limitadas à área
        self.assertEqual(render.limpar_tracos("não é lista", "avarias"), [])
        self.assertLessEqual(len(render.limpar_tracos([RISCO] * 1000, "avarias")), render.MAX_TRACOS)

    def test_marcacoes_pela_cor(self):
        m = render.marcacoes([RISCO, AMASSADO, RISCO])
        self.assertEqual([x["tipo"] for x in m], ["Arranhão", "Amassado", "Arranhão"])
        # o PDF ignora o texto automático (mesma regra de antes)
        self.assertEqual(pdf_report.real_marks({"marcacoes": m}), [])


class TestImagens(unittest.TestCase):
    def test_avarias_mantem_tamanho_e_pinta_traco(self):
        base = Image.new("RGB", (900, 403), "white")
        img = render.render_avarias(base, [RISCO])
        self.assertEqual(img.size, (900, 403))
        r, g, b = img.getpixel((round(0.3 * 900), round(0.4 * 403)))
        self.assertTrue(r > 200 and g < 120 and b < 120, (r, g, b))     # vermelho no ponto do traço
        self.assertEqual(img.getpixel((850, 380)), (255, 255, 255))      # resto intacto
        self.assertEqual(render.render_avarias(base, []).getpixel((10, 10)), (255, 255, 255))

    def test_assinatura_fundo_branco_recortada(self):
        self.assertIsNone(render.render_assinatura([]))
        img = render.render_assinatura(ASSINATURA)
        self.assertEqual(img.mode, "RGB")
        self.assertLessEqual(img.width, render.ASSINATURA_MAX_W)
        self.assertLessEqual(img.height, render.ASSINATURA_MAX_H)
        self.assertGreater(img.width, img.height)                          # proporção da assinatura
        self.assertEqual(img.getpixel((0, 0)), (255, 255, 255))
        self.assertLess(min(img.convert("L").getdata()), 80)             # tem traço escuro
        # resolução suficiente para 200 x 66 pt no PDF (>= 150 dpi)
        escala = min(pdf_report.SIGNATURE_MAX_W / img.width, pdf_report.SIGNATURE_MAX_H / img.height)
        self.assertGreaterEqual(72 / escala, 150)

    def test_rabisco_curto_nao_vira_gigante(self):
        img = render.render_assinatura([{"c": render.COR_ASSINATURA, "w": 0.012, "p": [[1.0, 0.5]]}])
        self.assertGreaterEqual(img.width / img.height, 2.0)


class TestCompatibilidade(unittest.TestCase):
    def test_vistoria_antiga_400x460_migra_sem_perder(self):
        antiga = b64png(Image.new("RGB", (400, 460), "white"))
        marc = [{"tipo": "Avaria", "severidade": "Marcada", "descricao": "Avaria indicada no desenho do veículo."}]
        av = {"diagrama": "sedan", "imagem": antiga, "imagem_ok": True, "marcacoes": list(marc)}
        self.assertTrue(render.migrar_avarias(av))
        self.assertEqual((av["imagem_legada"], av["imagem"], av["tracos"], av["marcacoes_legadas"]), (antiga, antiga, [], marc))
        self.assertFalse(render.migrar_avarias(av))                      # idempotente
        # imagem nunca confirmada (imagem_ok falso): descartada, como antes
        av2 = {"imagem": antiga, "imagem_ok": False}
        render.migrar_avarias(av2)
        self.assertIsNone(av2["imagem"])

    def test_pdf_com_desenho_novo_antigo_e_assinatura(self):
        nova = b64png(render.render_avarias(Image.new("RGB", (900, 403), "white"), [RISCO]))
        antiga = b64png(Image.new("RGB", (400, 460), "white"))
        sig = b64png(render.render_assinatura(ASSINATURA))
        for imagem in (nova, antiga):
            c = {"numero": "CHK-TESTE", "avarias": {"diagrama": "sedan", "imagem": imagem, "marcacoes": []},
                 "proprietario": {"nome": "Maria", "assinatura": sig}, "emitente": {"empresa": "X", "assinatura": sig}}
            avisos = []
            pdf = pdf_report.build_pdf(c, {}, avisos)
            self.assertTrue(pdf.startswith(b"%PDF"))
            self.assertEqual(avisos, [])


if __name__ == "__main__":
    unittest.main()
