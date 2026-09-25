"""Guarda o token do login no navegador (cookie + localStorage) — componente próprio, sem build npm.

Por que não só st.context.cookies: no Streamlit Community Cloud o cookie gravado pelo site
não chega ao servidor (o proxy da plataforma não o repassa), então o F5 voltava ao login.
Aqui o próprio navegador lê o token e o devolve ao Python pelo protocolo de componentes.
"""
from pathlib import Path

import streamlit.components.v1 as components

_componente = components.declare_component("laudo_sessao", path=str(Path(__file__).resolve().parent / "frontend"))


def navegador(acao, *, token="", nome="ac_token", segundos=0, key):
    """acao "ler" | "gravar" | "apagar". Devolve None enquanto o navegador não respondeu;
    depois, o dict enviado por ele (em "ler": {"acao": "ler", "token": "..."} — vazio se não há)."""
    v = _componente(acao=acao, token=token, nome=nome, segundos=segundos, key=key, default=None)
    return v if isinstance(v, dict) and v.get("acao") == acao else None
