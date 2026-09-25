import re
from pathlib import Path
from html import escape

BASE_DIR = Path(__file__).resolve().parent
CSS_FILE = BASE_DIR / "style.css"
ASSETS_DIR = BASE_DIR / "assets"

def _svg_inline(nome, classe, rotulo=None):
    """SVG da marca (assets/) em uma linha, para usar dentro do HTML.
    Tira largura/altura fixas (o tamanho vem do CSS) e, se for decorativo, o <title>."""
    svg = (ASSETS_DIR / nome).read_text(encoding="utf-8").strip()
    tag, resto = svg.split(">", 1)
    tag = re.sub(r'\s(?:width|height|role|aria-label)="[^"]*"', "", tag)
    extra = f' role="img" aria-label="{escape(rotulo)}"' if rotulo else ' aria-hidden="true"'
    if not rotulo:
        resto = re.sub(r"<title>.*?</title>", "", resto)
    return f'{tag} class="{classe}"{extra}>{resto}'.replace("\n", "")

# Marca CH360 (vetorial: nítida em qualquer tela). Os arquivos-fonte ficam em assets/.
BRAND_MARK = _svg_inline("ch360_icone.svg", "ac-mark")                                   # só o ícone
BRAND_LOGO = _svg_inline("ch360_logo.svg", "ac-brand-logo", "CH360 Vistoria Veicular")   # fundo claro
BRAND_SIDE = _svg_inline("ch360_marca_escuro.svg", "ac-side-logo", "CH360")              # fundo escuro

def _compact(html):
    """Remove recuos/linhas vazias: evita que o Markdown trate o HTML como código."""
    return "".join(line.strip() for line in html.splitlines())

def page_styles():
    css = CSS_FILE.read_text(encoding="utf-8")
    return f"<style>{css}</style>"

def _meta_items(meta):
    if not meta:
        return []
    if isinstance(meta, (list, tuple)):
        return [str(m) for m in meta if m]
    return [m.strip() for m in str(meta).split("•") if m.strip()]

def hero_html(title, subtitle="", meta="", progress=None):
    """Cabeçalho de página. Títulos no formato "02 • Nome" ganham o selo numérico da etapa."""
    title = str(title)
    m = re.match(r"^\s*(\d{1,2})\s*•\s*(.+)$", title)
    num, name = (m.group(1), m.group(2)) if m else (None, title)
    badge = f'<div class="ac-step-num">{escape(num)}</div>' if num else ""
    eyebrow = f'<div class="ac-eyebrow">Etapa {escape(num)}</div>' if num else ""
    chips = "".join(f'<span class="ac-chip">{escape(x)}</span>' for x in _meta_items(meta))
    bar = ""
    if progress is not None:
        pct = max(0, min(100, round(float(progress) * 100)))
        bar = f'<div class="ac-progress" role="progressbar" aria-valuenow="{pct}" aria-valuemin="0" aria-valuemax="100"><span style="width:{pct}%"></span></div>'
    sub = f'<div class="ac-sub hero-sub">{escape(str(subtitle))}</div>' if subtitle else ""
    meta_html = f'<div class="ac-meta">{chips}</div>' if chips else ""
    # HTML numa linha só: linhas em branco/recuadas viram bloco de código no Markdown.
    return (
        f'<div class="ac-hero{" is-step" if num else ""}">'
        f'<div class="ac-hero-row">{badge}<div class="ac-hero-text">{eyebrow}'
        f'<div class="ac-title">{escape(name)}</div>{sub}</div></div>'
        f'{meta_html}{bar}</div>'
    )

def card_html(title, subtitle=""):
    return _compact(f"""
    <div class="ac-card">
        <div class="ac-card-title">{escape(str(title))}</div>
        <div class="ac-sub">{escape(str(subtitle))}</div>
    </div>
    """)

def brand_html(subtitle=""):
    """Marca completa (tela de login). "VISTORIA VEICULAR" já faz parte da logo:
    o subtítulo só aparece se for outro texto."""
    sub = str(subtitle or "").strip()
    sub_html = f'<div class="ac-brand-sub">{escape(sub)}</div>' if sub and sub.lower() != "vistoria veicular" else ""
    return f'<div class="ac-brand">{BRAND_LOGO}{sub_html}</div>'

def sidebar_brand_html(name, role):
    return _compact(f"""
    <div class="ac-side-brand">
        {BRAND_SIDE}
    </div>
    <div class="ac-side-user">
        <div class="ac-avatar">{escape(str(name or "?")[:1].upper())}</div>
        <div>
            <div class="ac-side-user-name">{escape(str(name))}</div>
            <div class="ac-side-user-role">{escape(str(role))}</div>
        </div>
    </div>
    """)

def section_html(title, subtitle=""):
    """Título de bloco dentro de uma etapa (ex.: cada pneu, cada foto)."""
    sub = f'<div class="ac-section-sub">{escape(str(subtitle))}</div>' if subtitle else ""
    return f'<div class="ac-section-title">{escape(str(title))}</div>{sub}'

def card_open():
    return '<div class="ac-card">'

def card_close():
    return '</div>'
