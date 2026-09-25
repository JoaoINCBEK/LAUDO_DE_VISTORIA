"""Traços -> imagem (PNG), no servidor. Sem Streamlit: testável isoladamente.

Formato de um traço (vem do navegador; é validado aqui):
    {"c": "#ef4444", "w": 0.01, "p": [[x, y], [x, y], ...]}
- modo "avarias":    x e y de 0 a 1 (fração da largura/altura do desenho-base);
                     w = espessura em fração da largura.
- modo "assinatura": unidade = altura da área de assinatura (y de 0 a 1, x de 0 até a
                     proporção da área); w = espessura na mesma unidade.
Assim o desenho não depende do tamanho da tela: girar o celular só muda a escala.
"""
import math

from PIL import Image, ImageDraw

COR_ASSINATURA = "#111827"
COR_ARRANHAO = "#ef4444"
COR_AMASSADO = "#2563eb"
CORES = {COR_ASSINATURA, COR_ARRANHAO, COR_AMASSADO}
MAX_TRACOS = 400
MAX_PONTOS = 3000

# Assinatura exportada: recortada no traço (com margem), no máximo 1000 x 300 px.
# O PDF a coloca em 200 x 66 pt (SIGNATURE_MAX_W/H): 300 px de altura ~ 330 dpi.
ASSINATURA_MAX_W, ASSINATURA_MAX_H = 1000, 300


def limpar_tracos(tracos, modo):
    """Valida o que veio do navegador: cores conhecidas, números finitos, limites de tamanho."""
    if not isinstance(tracos, list):
        return []
    lim_x = 1.0 if modo == "avarias" else 20.0
    out = []
    for t in tracos[:MAX_TRACOS]:
        if not isinstance(t, dict):
            continue
        cor = str(t.get("c", "")).lower()
        if cor not in CORES:
            continue
        try:
            w = float(t.get("w", 0.01))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(w):
            continue
        pts = []
        for p in (t.get("p") or [])[:MAX_PONTOS]:
            try:
                x, y = float(p[0]), float(p[1])
            except (TypeError, ValueError, IndexError):
                continue
            if math.isfinite(x) and math.isfinite(y):
                pts.append([round(min(max(x, 0.0), lim_x), 4), round(min(max(y, 0.0), 1.0), 4)])
        if pts:
            out.append({"c": cor, "w": round(min(max(w, 0.001), 0.08), 4), "p": pts})
    return out


def _rgb(hexcor):
    h = hexcor.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _pincel(draw, tracos, fx, fy, fw):
    """Desenha os traços com pontas e junções arredondadas. fx/fy: coordenada -> pixel;
    fw: espessura -> pixel."""
    for t in tracos:
        cor = _rgb(t["c"]) + (255,)
        larg = max(1, int(round(fw(t["w"]))))
        pts = [(fx(x), fy(y)) for x, y in t["p"]]
        if len(pts) > 1:
            draw.line(pts, fill=cor, width=larg, joint="curve")
        r = larg / 2.0
        for px, py in (pts[0], pts[-1]):
            draw.ellipse((px - r, py - r, px + r, py + r), fill=cor)


def render_avarias(base, tracos, ss=2):
    """Desenho-base + traços, no tamanho da imagem-base. Superamostragem = traço suave."""
    base = base.convert("RGB")
    w, h = base.size
    camada = Image.new("RGBA", (w * ss, h * ss), (0, 0, 0, 0))
    if tracos:
        _pincel(ImageDraw.Draw(camada), tracos, lambda x: x * w * ss, lambda y: y * h * ss, lambda e: e * w * ss)
    camada = camada.resize((w, h), Image.Resampling.LANCZOS)
    return Image.alpha_composite(base.convert("RGBA"), camada).convert("RGB")


def render_assinatura(tracos, ss=3):
    """PNG de fundo branco recortado na assinatura (com margem). None se não houver traço."""
    if not tracos:
        return None
    meia = max(t["w"] for t in tracos) / 2
    xs = [x for t in tracos for x, _ in t["p"]]
    ys = [y for t in tracos for _, y in t["p"]]
    folga = 0.08 + meia
    x0, x1, y0, y1 = min(xs) - folga, max(xs) + folga, min(ys) - folga, max(ys) + folga
    # tamanho mínimo: um rabisco curto não vira uma assinatura gigante no PDF
    if x1 - x0 < 1.0:
        cx = (x0 + x1) / 2
        x0, x1 = cx - 0.5, cx + 0.5
    if y1 - y0 < 0.4:
        cy = (y0 + y1) / 2
        y0, y1 = cy - 0.2, cy + 0.2
    ppu = min(ASSINATURA_MAX_W / (x1 - x0), ASSINATURA_MAX_H / (y1 - y0))
    w, h = max(1, math.ceil((x1 - x0) * ppu)), max(1, math.ceil((y1 - y0) * ppu))
    img = Image.new("RGBA", (w * ss, h * ss), (255, 255, 255, 255))
    _pincel(ImageDraw.Draw(img), tracos, lambda x: (x - x0) * ppu * ss, lambda y: (y - y0) * ppu * ss,
            lambda e: e * ppu * ss)
    return img.resize((w, h), Image.Resampling.LANCZOS).convert("RGB")


def marcacoes(tracos):
    """Uma ocorrência por traço, pela cor (mesma regra de antes: vermelho = Arranhão)."""
    return [{"tipo": "Arranhão" if t["c"] == COR_ARRANHAO else "Amassado",
             "severidade": "Marcada", "descricao": "Avaria indicada no desenho do veículo."}
            for t in tracos if t["c"] in (COR_ARRANHAO, COR_AMASSADO)]


def migrar_avarias(av):
    """Prepara av (dict da vistoria) para o formato com traços, sem perder nada:
    - formato novo ("tracos" presente): nada muda;
    - vistoria antiga com desenho salvo (imagem 400x460 com os traços "embutidos"):
      a imagem vira a base (imagem_legada) e as ocorrências antigas são preservadas;
    - sem desenho: traços vazios (o desenho-base limpo é usado).
    Devolve True se houve migração de desenho antigo."""
    if isinstance(av.get("tracos"), list):
        return False
    migrou = False
    if av.get("imagem") and av.get("imagem_ok"):
        av["imagem_legada"] = av["imagem"]
        av["marcacoes_legadas"] = list(av.get("marcacoes") or [])
        migrou = True
    elif av.get("imagem"):          # mesma regra de antes: imagem não confirmada é descartada
        av["imagem"] = None
    av["tracos"] = []
    return migrou
