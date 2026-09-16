from pathlib import Path
from html import escape

BASE_DIR = Path(__file__).resolve().parent
CSS_FILE = BASE_DIR / "style.css"

def page_styles():
    css = CSS_FILE.read_text(encoding="utf-8")
    return f"<style>{css}</style>"

def hero_html(title, subtitle="", meta=""):
    return f"""
    <div class="ac-hero">
        <div class="ac-title">{escape(str(title))}</div>
        <div class="ac-sub hero-sub">{escape(str(subtitle))}</div>
        {f'<div class="ac-meta">{escape(str(meta))}</div>' if meta else ''}
    </div>
    """

def card_html(title, subtitle=""):
    return f"""
    <div class="ac-card">
        <div class="ac-card-title">{escape(str(title))}</div>
        <div class="ac-sub">{escape(str(subtitle))}</div>
    </div>
    """

def card_open():
    return '<div class="ac-card">'

def card_close():
    return '</div>'