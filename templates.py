import re
from pathlib import Path
from html import escape

BASE_DIR = Path(__file__).resolve().parent
CSS_FILE = BASE_DIR / "style.css"

# Marca do sistema: escudo com "check" (vistoria / segurança). SVG inline, sem arquivos extras.
BRAND_MARK = (
    '<svg class="ac-mark" viewBox="0 0 32 32" aria-hidden="true">'
    '<rect width="32" height="32" rx="7" fill="#E3751C"/>'
    '<path d="M16 6.5l7.5 3v5.6c0 4.7-3.1 8.7-7.5 10.4-4.4-1.7-7.5-5.7-7.5-10.4V9.5z" '
    'fill="none" stroke="#fff" stroke-width="2" stroke-linejoin="round"/>'
    '<path d="M12.4 15.8l2.6 2.6 4.8-5" fill="none" stroke="#fff" stroke-width="2.2" '
    'stroke-linecap="round" stroke-linejoin="round"/></svg>'
)

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

def brand_html(subtitle="Inspeção veicular digital"):
    """Marca completa (tela de login)."""
    return _compact(f"""
    <div class="ac-brand">
        {BRAND_MARK}
        <div>
            <div class="ac-brand-name">LAUDO DE VISTORIA</div>
            <div class="ac-brand-sub">{escape(str(subtitle))}</div>
        </div>
    </div>
    """)

def sidebar_brand_html(name, role):
    return _compact(f"""
    <div class="ac-side-brand">
        {BRAND_MARK}
        <div class="ac-brand-name">LAUDO DE VISTORIA</div>
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
