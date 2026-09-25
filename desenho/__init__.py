"""Área de desenho responsiva (assinaturas e avarias) — componente próprio, sem build npm.

O navegador (frontend/index.html) mede a largura real do container e a altura visível da
tela (visualViewport), redimensiona o canvas em resize / orientationchange / visualViewport /
fullscreenchange e redesenha os traços, guardados em coordenadas relativas: girar o
celular não apaga nem distorce o desenho. Devolve só a lista de traços (JSON pequeno);
o PNG final é gerado aqui no servidor (desenho.render), igual em qualquer aparelho.
"""
from pathlib import Path

import streamlit.components.v1 as components

from . import render

_componente = components.declare_component("laudo_canvas", path=str(Path(__file__).resolve().parent / "frontend"))


def area_desenho(*, modo, versao, key, tracos=None, imagem=None, proporcao=None,
                 cor=render.COR_ASSINATURA, espessura=0.012):
    """Mostra a área de desenho. Devolve a nova lista de traços (já validada) quando o usuário
    desenhou/desfez/refez/limpou, ou None se nada mudou desde o último envio.

    modo      "assinatura" | "avarias"
    versao    muda para recomeçar do zero (Limpar desenho, troca de veículo)
    tracos    traços já salvos (usados só quando a área é criada ou a versão muda)
    imagem    avarias: data URL do desenho-base; proporcao = largura / altura dele
    """
    v = _componente(modo=modo, versao=str(versao), tracos=tracos or [], imagem=imagem,
                    proporcao=proporcao, cor=cor, espessura=espessura, key=key, default=None)
    if not isinstance(v, dict) or v.get("versao") != str(versao):
        return None      # nada novo, ou resposta de uma versão anterior (já limpa)
    return render.limpar_tracos(v.get("tracos"), modo)
