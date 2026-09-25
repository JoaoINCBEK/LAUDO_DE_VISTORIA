"""Telas administrativas (Super Admin, Administrador da empresa, Vistoriador).

As telas NÃO acessam o banco diretamente: tudo passa por saas.servicos, que
valida permissão e empresa a cada chamada. A tela de vistoria (etapas 02–11)
continua no app.py, sem alterações de fluxo.
"""
import base64
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timedelta
from html import escape
from typing import Callable

import altair as alt
import pandas as pd
import streamlit as st

import assinatura
import placa_api
from saas import db, rbac, servicos as S
from saas.rbac import AcessoNegado, pode
from templates import brand_html, hero_html, section_html

COR_STATUS = {"Concluída": "#15803D", "Em andamento": "#2F6FB0", "Pendente": "#C98A0B", "Cancelada": "#8A94A0"}
COR_MARCA = "#1B2A38"
COR_DESTAQUE = "#0A6CF0"      # azul CH360 (mesmo --ac-accent do style.css)


@dataclass
class Contexto:
    ator: S.Ator
    gerar_pdf: Callable          # dados da vistoria -> bytes do PDF (pdf_report existente)
    abrir_vistoria: Callable     # vistoria_id -> carrega no fluxo de etapas
    nova_vistoria: Callable      # inicia uma vistoria nova no fluxo de etapas
    tipos_veiculo: dict          # chave do desenho -> nome ("sedan" -> "Sedã")
    miniatura: Callable          # base64 -> bytes JPEG leves
    rotulos_fotos: dict = None   # chave da foto -> nome da posição ("lateral_dir" -> "Lateral direita")
    assinatura_cfg: object = None  # assinatura.Config (provedor de assinatura à distância)


# ---------------------------------------------------------------------------
# Menu por perfil
# ---------------------------------------------------------------------------
MENU = {
    rbac.SUPER_ADMIN: [
        ("sa_dashboard", "Painel da plataforma"), ("sa_empresas", "Empresas"), ("sa_planos", "Planos"),
        ("sa_usuarios", "Usuários"), ("sa_logs", "Logs"), ("sa_integracoes", "Integrações"),
    ],
    rbac.ADMIN: [
        ("dashboard", "Dashboard"), ("vistorias", "Vistorias"), ("veiculos", "Veículos"), ("clientes", "Clientes"),
        ("laudos", "Laudos"), ("usuarios", "Usuários"), ("relatorios", "Relatórios"), ("logs", "Logs"),
        ("configuracoes", "Configurações"),
    ],
    rbac.VISTORIADOR: [
        ("inicio_vist", "Início"), ("vistorias", "Minhas vistorias"), ("laudos", "Meus laudos"),
    ],
}
PAGINA_INICIAL = {rbac.SUPER_ADMIN: "sa_dashboard", rbac.ADMIN: "dashboard", rbac.VISTORIADOR: "inicio_vist"}


def ir(pagina, **estado):
    st.session_state.page = pagina
    for k, v in estado.items():
        st.session_state[k] = v
    st.rerun()


# ---------------------------------------------------------------------------
# Utilidades de tela
# ---------------------------------------------------------------------------
def num(n):
    return "∞" if n is None else f"{int(n):,}".replace(",", ".")


def uso(usado, limite):
    return f"{num(usado)} / {num(limite)}" if limite is not None else f"{num(usado)} / sem limite"


def status_txt(s):
    return S.STATUS_VISTORIA.get(s, s)


def cabecalho(titulo, subtitulo="", meta=None):
    st.markdown(hero_html(titulo, subtitulo, meta or ""), unsafe_allow_html=True)


def kpis(itens):
    """itens = [(rótulo, valor)] em linhas de 4 (2 x 2 no celular)."""
    for i in range(0, len(itens), 4):
        cols = st.columns(4)
        for col, (rot, val) in zip(cols, itens[i:i + 4]):
            col.metric(rot, val)


def periodo(key, padrao="Últimos 30 dias"):
    """Seletor de período. Devolve (inicio, fim) como 'AAAA-MM-DD'."""
    hoje = datetime.strptime(db.hoje(), "%Y-%m-%d").date()
    opcoes = ["Hoje", "Últimos 7 dias", "Últimos 30 dias", "Este mês", "Últimos 3 meses", "Este ano", "Período personalizado"]
    esc = st.selectbox("Período", opcoes, index=opcoes.index(padrao), key=key)
    ini, fim = hoje, hoje
    if esc == "Últimos 7 dias":
        ini = hoje - timedelta(days=6)
    elif esc == "Últimos 30 dias":
        ini = hoje - timedelta(days=29)
    elif esc == "Este mês":
        ini = hoje.replace(day=1)
    elif esc == "Últimos 3 meses":
        ini = hoje - timedelta(days=90)
    elif esc == "Este ano":
        ini = hoje.replace(month=1, day=1)
    elif esc == "Período personalizado":
        v = st.date_input("De / até", value=(hoje - timedelta(days=29), hoje), format="DD/MM/YYYY", key=key + "_datas")
        if isinstance(v, (tuple, list)):
            ini = v[0]
            fim = v[1] if len(v) > 1 else v[0]
        else:
            ini = fim = v
    return ini.isoformat(), fim.isoformat()


def tabela(linhas, colunas, key, altura=None):
    """Tabela + campo "Selecionar" (confiável no toque do celular, ao contrário da
    seleção de linha da grade). colunas = {campo: título}. Devolve a linha escolhida."""
    if not linhas:
        st.info("Nenhum registro encontrado.")
        return None
    df = pd.DataFrame([{t: l.get(c, "") for c, t in colunas.items()} for l in linhas])
    st.dataframe(df, hide_index=True, use_container_width=True, height=altura, key=key)
    ids = [l.get("id", i) for i, l in enumerate(linhas)]
    por_id = dict(zip(ids, linhas))
    campos = list(colunas)[:3]
    # a chave muda quando a lista muda (filtros): a seleção nunca "pula" para outro registro
    chave = f"{key}_sel_{abs(hash(tuple(ids))) % 10**8}"
    escolhido = st.selectbox(f"Selecionar ({len(linhas)} registro(s))", ids, index=None, key=chave,
                             placeholder="Escolha um registro para ver as ações…",
                             format_func=lambda i: " · ".join(str(por_id[i].get(c) or "—") for c in campos))
    return por_id.get(escolhido)


def protegido(fn, *args, **kwargs):
    """Executa uma ação do backend mostrando o erro de forma amigável. Devolve (ok, resultado)."""
    try:
        return True, fn(*args, **kwargs)
    except (AcessoNegado, S.ErroNegocio) as exc:
        st.error(str(exc))
        return False, None


def grafico_barras(df, x, y, titulo_x="", titulo_y="", cor=COR_MARCA, horizontal=False, altura=260):
    if df.empty:
        st.caption("Sem dados no período.")
        return
    if horizontal:
        ch = alt.Chart(df).mark_bar(color=cor, cornerRadiusEnd=3).encode(
            y=alt.Y(f"{x}:N", sort="-x", title=titulo_x), x=alt.X(f"{y}:Q", title=titulo_y),
            tooltip=[x, y])
    else:
        ch = alt.Chart(df).mark_bar(color=cor, cornerRadiusEnd=3).encode(
            x=alt.X(f"{x}:N", title=titulo_x, axis=alt.Axis(labelAngle=0)), y=alt.Y(f"{y}:Q", title=titulo_y),
            tooltip=[x, y])
    st.altair_chart(ch.properties(height=altura), use_container_width=True)


def grafico_vistorias(serie):
    """Barras empilhadas por dia e status."""
    if not serie:
        st.caption("Sem vistorias no período.")
        return
    df = pd.DataFrame(serie)
    df["Status"] = df["status"].map(status_txt)
    # Dia como texto já formatado: datas "de verdade" são convertidas para UTC pelo
    # navegador e uma vistoria das 22h aparecia no dia seguinte.
    df = df.sort_values("dia")
    df["Dia"] = pd.to_datetime(df["dia"]).dt.strftime("%d/%m")
    ordem = list(dict.fromkeys(df["Dia"]))
    ch = alt.Chart(df).mark_bar().encode(
        x=alt.X("Dia:N", title="", sort=ordem, axis=alt.Axis(labelAngle=0)),
        y=alt.Y("sum(total):Q", title="Vistorias"),
        color=alt.Color("Status:N", scale=alt.Scale(domain=list(COR_STATUS), range=list(COR_STATUS.values())),
                        legend=alt.Legend(orient="bottom", title=None)),
        tooltip=["Dia", "Status", alt.Tooltip("sum(total):Q", title="Total")],
    ).properties(height=280)
    st.altair_chart(ch, use_container_width=True)


def links_envio(numero, veiculo, placa, telefone="", link=""):
    """E-mail / WhatsApp com o resumo do laudo. O PDF é anexado pelo próprio usuário
    (sem serviço de e-mail configurado não é possível anexar automaticamente).
    Com `link` (assinatura à distância), a mensagem leva o link de assinatura."""
    texto = f"Laudo de vistoria {numero} — {veiculo or 'veículo'} — placa {placa or '-'}."
    tel = "".join(ch for ch in str(telefone or "") if ch.isdigit())
    if tel and not tel.startswith("55") and len(tel) in (10, 11):
        tel = "55" + tel
    extra = f" Assine o laudo pelo link: {link}" if link else " Segue o PDF em anexo."
    wa = f"https://wa.me/{tel}?text=" + urllib.parse.quote(texto + extra)
    mail = "mailto:?subject=" + urllib.parse.quote(f"Laudo de vistoria {numero}") + "&body=" + urllib.parse.quote(
        texto + ("\n\nAssine o laudo pelo link:\n" + link if link else "\n\nSegue o laudo em PDF em anexo."))
    a, b = st.columns(2)
    a.link_button("Enviar por WhatsApp", wa, use_container_width=True)
    b.link_button("Enviar por e-mail", mail, use_container_width=True)
    if not link:
        st.caption("Baixe o PDF e anexe-o na conversa ou no e-mail.")


# ---------------------------------------------------------------------------
# Assinatura eletrônica à distância (review, detalhe da vistoria e laudos)
# ---------------------------------------------------------------------------
ICONE_ASSINATURA = {"rascunho": "🟡", "aguardando": "🟡", "assinado": "🟢", "recusado": "🔴",
                    "cancelado": "🔴", "expirado": "🔴", "erro": "🔴"}


def painel_assinaturas(ctx, vistoria_id, numero, proprietario=None, permitir_envio=False, chave="x"):
    """Status das solicitações (consulta automática com cache curto) + ações.
    permitir_envio=True: mostra "Enviar para assinatura à distância" (PDF atual já arquivado)."""
    provedor = assinatura.criar_provedor(ctx.assinatura_cfg)
    ok, lista = protegido(S.listar_assinaturas_vistoria, ctx.ator, vistoria_id)
    if not ok or (not lista and not permitir_envio):
        return
    pode_enviar = pode(ctx.ator, "laudos.enviar") and not ctx.ator.super
    for i, a in enumerate(lista):     # consulta automática ao abrir (o banco guarda a hora da última)
        if a["status"] == "aguardando" or (a["status"] == "assinado" and not a["arquivo_assinado"]):
            try:
                lista[i] = S.atualizar_status_assinatura(ctx.ator, a["id"], provedor)
            except (AcessoNegado, S.ErroNegocio):
                pass
    with st.container(border=True, key=f"ac_card_ass_{chave}"):
        st.markdown(section_html("Assinatura eletrônica à distância",
                                 "Envie o link ao cliente: ele confere o laudo e desenha a assinatura no celular."
                                 if provedor.nome == "link" else
                                 "O cliente recebe o link do provedor e assina pelo celular."), unsafe_allow_html=True)
        msg = st.session_state.pop(f"ass_msg_{chave}", None)
        if msg:
            (st.success if msg[0] == "ok" else st.error)(msg[1])
        for a in lista[:5]:
            _linha_assinatura(ctx, a, numero, provedor, pode_enviar, chave, (proprietario or {}).get("telefone", ""))
        if not permitir_envio or not pode_enviar:
            return
        pendente = any(a["status"] in assinatura.STATUS_PENDENTES and not a["versao_anterior"] for a in lista)
        if not provedor.configurado:
            st.button("Enviar para assinatura à distância", disabled=True, use_container_width=True, key=f"ass_btn_{chave}")
            st.caption("Assinatura à distância não configurada." + (f" ({provedor.motivo})" if provedor.motivo else "")
                       + " O administrador da plataforma configura o provedor nos Secrets.")
            return
        if pendente:
            return
        aberto_k = f"ass_form_{chave}"
        if not st.session_state.get(aberto_k):
            if st.button("Enviar para assinatura à distância", type="primary", use_container_width=True, key=f"ass_btn_{chave}"):
                st.session_state[aberto_k] = True
                st.rerun()
            return
        _form_assinatura(ctx, vistoria_id, proprietario or {}, provedor, chave)


def _linha_assinatura(ctx, a, numero, provedor, pode_enviar, chave, telefone=""):
    k = f"{chave}_{a['id']}"
    contato = a["signatario_email"] if a["canal"] == "email" else a["signatario_telefone"]
    canal = assinatura.CANAIS.get(a["canal"], a["canal"])
    link = a["provedor"] == "link"
    quando = ((f" · assinado em {db.br(a['consultado_em'])}" if a["status"] == "assinado" else "") if link
              else (f" · consultado às {db.br(a['consultado_em'])[11:]}" if a["consultado_em"] else ""))
    st.markdown(f"{ICONE_ASSINATURA.get(a['status'], '•')} <b>{S.STATUS_ASSINATURA.get(a['status'], a['status'])}</b> · "
                f"{escape(a['signatario_nome'] or '—')} · {canal} {escape(contato or '')}<br>"
                f"<span style='color:#6B7785;font-size:13px'>Enviado em {db.br(a['created_at'])} · "
                + ("link do sistema" if link else f"provedor {escape(a['provedor'])}") + quando
                + "</span>", unsafe_allow_html=True)
    if a["versao_anterior"]:
        st.caption("⚠ Referente a versão anterior do laudo. Envie a versão atual para uma nova assinatura.")
    if a["status"] == "erro":
        st.error(a["mensagem_erro"] or "Não foi possível enviar para assinatura.")
    elif a["mensagem_erro"]:
        st.caption(f"Última consulta: {a['mensagem_erro']}")
    if a["link_assinatura"] and a["status"] in assinatura.STATUS_PENDENTES:
        st.code(a["link_assinatura"], language=None)
        if pode_enviar:
            links_envio(numero, "", "", telefone if a["canal"] == "whatsapp" else "", link=a["link_assinatura"])
    cols = st.columns(2)
    if a["status"] == "aguardando":
        if cols[0].button("🔄 Atualizar status", use_container_width=True, key=f"ass_upd_{k}"):
            if protegido(S.atualizar_status_assinatura, ctx.ator, a["id"], provedor, True)[0]:
                st.rerun()
        if pode_enviar and cols[1].button("Cancelar solicitação", use_container_width=True, key=f"ass_can_{k}"):
            if protegido(S.cancelar_assinatura, ctx.ator, a["id"], provedor)[0]:
                st.rerun()
    elif a["status"] == "assinado":
        try:
            pdf, nome = S.pdf_assinado(ctx.ator, a["id"], provedor if provedor.configurado else None)
            st.download_button("⬇ Baixar PDF assinado", data=pdf, file_name=nome, mime="application/pdf",
                               type="primary", use_container_width=True, key=f"ass_pdf_{k}")
        except (AcessoNegado, S.ErroNegocio) as exc:
            st.caption(f"PDF assinado indisponível: {exc}")


def _form_assinatura(ctx, vistoria_id, proprietario, provedor, chave):
    with st.form(f"ass_form_{chave}_{vistoria_id}"):
        st.markdown(section_html("Signatário", "Confira os dados: o link de assinatura vai para o contato abaixo."),
                    unsafe_allow_html=True)
        nome = st.text_input("Nome completo", proprietario.get("nome", ""))
        a, b = st.columns(2)
        email = a.text_input("E-mail", "", placeholder="cliente@exemplo.com")
        tel = b.text_input("Celular (com DDD)", proprietario.get("telefone", ""))
        canais = list(provedor.canais)
        canal = st.radio("Enviar o link por", canais, horizontal=True, format_func=lambda c: assinatura.CANAIS.get(c, c))
        a, b = st.columns(2)
        enviar = a.form_submit_button("Gerar link de assinatura" if provedor.nome == "link" else "Enviar para assinatura",
                                      type="primary", use_container_width=True)
        cancelar = b.form_submit_button("Cancelar", use_container_width=True)
    if cancelar:
        st.session_state.pop(f"ass_form_{chave}", None)
        st.rerun()
    if enviar:
        dados = {"nome": nome, "email": email, "telefone": tel, "cpf": proprietario.get("cpf", ""), "canal": canal}
        with st.spinner("Enviando o laudo para assinatura..."):
            ok, aid = protegido(S.solicitar_assinatura, ctx.ator, vistoria_id, dados, provedor)
        if ok:
            st.session_state.pop(f"ass_form_{chave}", None)
            ass = next((x for x in S.listar_assinaturas_vistoria(ctx.ator, vistoria_id) if x["id"] == aid), {})
            if ass.get("status") == "erro":
                st.session_state[f"ass_msg_{chave}"] = ("erro", ass.get("mensagem_erro") or "Erro ao enviar.")
            elif provedor.nome == "link":
                st.session_state[f"ass_msg_{chave}"] = ("ok", "Link de assinatura criado. Envie ao cliente pelo botão "
                                                        f"de {assinatura.CANAIS.get(canal, canal)} abaixo (vale 7 dias).")
            else:
                st.session_state[f"ass_msg_{chave}"] = ("ok", "Laudo enviado. O cliente receberá o link de assinatura por "
                                                        f"{assinatura.CANAIS.get(canal, canal)}.")
            st.rerun()


def b64_bytes(b64):
    try:
        return base64.b64decode(b64)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Detalhe de uma vistoria (laudo completo)
# ---------------------------------------------------------------------------
def detalhe_vistoria(ctx, vid, voltar_para):
    ok, v = protegido(S.obter_vistoria, ctx.ator, vid)
    if not ok:
        return
    d = v["dados"] or {}
    ve = d.get("veiculo", {}) or {}
    if st.button("← Voltar à lista", key="det_voltar"):
        st.session_state.pop("vistoria_aberta", None)
        st.rerun()
    ini = v["data_inicio"] or ""
    cabecalho(f"Vistoria {v['numero']}", v["veiculo_desc"] or "Veículo não informado",
              [f"Placa {v['placa'] or '—'}", status_txt(v["status"]), v["empresa_nome"] if ctx.ator.super and v.get("empresa_nome") else ""])
    with st.container(border=True, key="ac_card_det_resumo"):
        a, b = st.columns(2)
        a.markdown(f"**Realizada por:** {v['vistoriador_nome'] or '—'}  \n"
                   f"**Data:** {db.br(ini, False)}  \n**Horário:** {ini[11:16]}")
        b.markdown(f"**Status:** {status_txt(v['status'])}  \n"
                   f"**Conclusão:** {db.br(v['data_conclusao']) or '—'}  \n"
                   f"**Última alteração:** {db.br(v['updated_at'])}")
        cli = v.get("cliente") or {}
        p = d.get("proprietario", {}) or {}
        st.markdown(f"**Cliente:** {cli.get('nome') or p.get('nome') or '—'}  ·  "
                    f"**Telefone:** {cli.get('telefone') or p.get('telefone') or '—'}  ·  "
                    f"**Tipo de veículo:** {ctx.tipos_veiculo.get(v['tipo_veiculo'], v['tipo_veiculo'] or '—')}")
        st.markdown(f"**Marca/Modelo:** {ve.get('marca', '')} {ve.get('modelo', '')}  ·  **Ano:** {ve.get('ano', '') or '—'}  ·  "
                    f"**Cor:** {ve.get('cor', '') or '—'}  ·  **Km:** {ve.get('km', '') or '—'}")

    laudo = next((l for l in v["laudos"] if l["status"] == "emitido"), None)
    if laudo:
        ok, res = protegido(S.pdf_do_laudo, ctx.ator, laudo["id"], ctx.gerar_pdf)
        if ok:
            pdf, nome = res
            st.download_button("⬇ Baixar laudo em PDF", data=pdf, file_name=nome, mime="application/pdf",
                               type="primary", use_container_width=True, key=f"det_pdf_{laudo['id']}")
            if pode(ctx.ator, "laudos.enviar"):
                links_envio(v["numero"], v["veiculo_desc"], v["placa"], cli.get("telefone") or p.get("telefone"))
    painel_assinaturas(ctx, v["id"], v["numero"], p, permitir_envio=False, chave="det")
    acoes = st.columns(2)
    if v["pode_editar"]:
        rot = "Continuar vistoria" if v["status"] in ("em_andamento", "pendente") else "Abrir para edição"
        if acoes[0].button(rot, key="det_editar", use_container_width=True):
            ctx.abrir_vistoria(v["id"])
    if v["status"] in ("em_andamento", "pendente") and (pode(ctx.ator, "vistorias.cancelar") or (
            v["usuario_id"] == ctx.ator.id and pode(ctx.ator, "vistorias.cancelar_proprias"))):
        if acoes[1].button("Cancelar vistoria", key="det_cancelar", use_container_width=True):
            if protegido(S.cancelar_vistoria, ctx.ator, v["id"])[0]:
                st.rerun()

    fotos = [(k, b) for k, b in (d.get("fotos") or {}).items() if b]
    kd = [(k, b) for k, b in (d.get("chave_documentos") or {}).items() if b and isinstance(b, str)]
    extras = [(f"Acessório {i}", b) for i, b in enumerate(d.get("fotos_acessorios") or [], 1) if b]
    av = d.get("avarias", {}) or {}
    av_fotos = [(f"Avaria {i}" + (f" — {it.get('descricao')}" if it.get("descricao") else ""), it.get("foto"))
                for i, it in enumerate(av.get("fotos") or [], 1) if it and it.get("foto")]
    with st.expander(f"Fotos ({len(fotos) + len(kd) + len(extras) + len(av_fotos)})", expanded=False):
        grupos = [("Veículo", fotos), ("Chave e documentos", kd), ("Acessórios", extras), ("Avarias", av_fotos)]
        for titulo, itens in grupos:
            if not itens:
                continue
            st.markdown(section_html(titulo), unsafe_allow_html=True)
            cols = st.columns(3)
            for i, (rot, b64) in enumerate(itens):
                rotulos = ctx.rotulos_fotos or {}
                if titulo in ("Veículo", "Chave e documentos"):
                    rot = rotulos.get(rot) or ("Foto" if rot == "foto" else str(rot).replace("_", " ").capitalize())
                try:
                    cols[i % 3].image(ctx.miniatura(b64), caption=rot, use_container_width=True)
                except Exception:
                    cols[i % 3].warning(f"{rot}: foto indisponível")
        if not (fotos or kd or extras or av_fotos):
            st.caption("Nenhuma foto registrada.")
    with st.expander("Avarias (desenho) e assinaturas", expanded=False):
        if av.get("imagem"):
            st.image(b64_bytes(av["imagem"]), caption="Desenho das avarias", width=380)
        else:
            st.caption("Sem marcações no desenho.")
        a, b = st.columns(2)
        for col, quem, chave in ((a, "Proprietário", "proprietario"), (b, "Empresa / emitente", "emitente")):
            ass = (d.get(chave) or {}).get("assinatura")
            if ass:
                col.image(b64_bytes(ass), caption=f"Assinatura — {quem}", use_container_width=True)
            else:
                col.caption(f"Sem assinatura do {quem.lower()}.")
    with st.expander("Histórico de alterações", expanded=False):
        if v["historico"]:
            for h in v["historico"]:
                st.markdown(f"**{db.br(h['data_hora'])}** — {h['descricao'] or h['acao']}")
        else:
            st.caption("Sem registros.")
        subst = [l for l in v["laudos"] if l["status"] == "substituido"]
        if subst:
            st.caption(f"{len(subst)} versão(ões) anterior(es) do laudo substituída(s) por alterações.")


# ---------------------------------------------------------------------------
# Páginas da empresa (Administrador; o Super Admin reaproveita com empresa_id)
# ---------------------------------------------------------------------------
def pg_dashboard(ctx, empresa_id=None):
    ok, ind = protegido(S.indicadores_empresa, ctx.ator, empresa_id)
    if not ok:
        return
    c = ind["consumo"]
    if empresa_id is None:
        cabecalho("Dashboard", ind["empresa_nome"], [ctx.ator.nome, db.br(db.hoje() + " 00:00:00", False)])
    if c["vencida"]:
        st.error("O plano da empresa está vencido: novas vistorias estão bloqueadas.")
    elif c["dias_para_vencer"] is not None and c["dias_para_vencer"] <= S.ALERTA_VENCIMENTO_DIAS:
        st.warning(f"O plano vence em {c['dias_para_vencer']} dia(s).")
    if c["percentual"] is not None and c["percentual"] >= S.ALERTA_CONSUMO:
        st.warning(f"Consumo do plano em {round(c['percentual'] * 100)}%.")
    kpis([
        ("Total de vistorias", num(ind["total_vistorias"])),
        ("Vistorias hoje", num(ind["vistorias_hoje"])),
        ("Vistorias do mês", num(ind["vistorias_mes"])),
        ("Pendentes", num(ind["pendentes"])),
        ("Veículos", num(ind["total_veiculos"])),
        ("Usuários", uso(c["usuarios_ativos"], c["limite_usuarios"])),
        ("Laudos emitidos", num(ind["laudos_emitidos"])),
        ("Vistorias restantes", num(c["restantes"]) if c["limite_vistorias"] is not None else "Sem limite"),
    ])
    if c["limite_vistorias"]:
        st.progress(c["percentual"] or 0.0, text=f"Plano: {uso(c['vistorias_utilizadas'], c['limite_vistorias'])} vistorias")
    st.caption("Números calculados a partir dos dados gravados. Vistorias canceladas não entram nos totais.")

    # O administrador também pode realizar vistorias: atalhos para iniciar/continuar.
    if empresa_id is None and pode(ctx.ator, "vistorias.criar"):
        pode_iniciar, msg = S.pode_iniciar_vistoria(ctx.ator)
        if not pode_iniciar:
            st.warning(msg)
        if st.button("＋ Começar nova vistoria", type="primary", use_container_width=True, disabled=not pode_iniciar,
                     key="dash_nova"):
            ctx.nova_vistoria()
        minhas = S.listar_vistorias(ctx.ator, status=["em_andamento", "pendente"], usuario_id=ctx.ator.id, limite=5)
        for v in minhas:
            with st.container(border=True, key=f"ac_card_dpend_{v['id']}"):
                a, b = st.columns([3, 1])
                a.markdown(section_html(f"{v['numero']} · {status_txt(v['status'])}",
                                        f"{v['veiculo_desc'] or 'Veículo não informado'} · {v['placa'] or 'sem placa'}"),
                           unsafe_allow_html=True)
                if b.button("Continuar", use_container_width=True, key=f"dash_cont_{v['id']}"):
                    ctx.abrir_vistoria(v["id"])

    st.subheader("Vistorias no período")
    ini, fim = periodo(f"dash_per_{empresa_id}")
    serie = S.serie_vistorias(ctx.ator, ini, fim, empresa_id)
    grafico_vistorias(serie)
    por_status = {k: 0 for k in S.STATUS_VISTORIA}
    for r in serie:
        por_status[r["status"]] = por_status.get(r["status"], 0) + r["total"]
    kpis([(S.STATUS_VISTORIA[k], num(por_status[k])) for k in ("concluida", "pendente", "em_andamento", "cancelada")])
    st.subheader("Produtividade por vistoriador")
    prod = S.produtividade(ctx.ator, ini, fim, empresa_id)
    grafico_barras(pd.DataFrame(prod), "vistoriador", "total", "", "Vistorias concluídas", horizontal=True,
                   altura=max(120, 38 * len(prod)))


def _filtros_vistorias(ctx, key, empresa_id):
    with st.expander("Filtros", expanded=False):
        a, b = st.columns(2)
        hoje = datetime.strptime(db.hoje(), "%Y-%m-%d").date()
        di = a.date_input("Data inicial", value=None, format="DD/MM/YYYY", key=key + "_di")
        df = b.date_input("Data final", value=None, format="DD/MM/YYYY", key=key + "_df", max_value=hoje + timedelta(days=1))
        a, b = st.columns(2)
        placa = a.text_input("Placa", key=key + "_placa")
        modelo = b.text_input("Modelo", key=key + "_modelo")
        a, b, c = st.columns(3)
        vists = S.vistoriadores(ctx.ator, empresa_id)
        vist = None
        if pode(ctx.ator, "vistorias.ver_todas"):
            nomes = {"Todos": None} | {x["nome"]: x["id"] for x in vists}
            vist = nomes[a.selectbox("Vistoriador", list(nomes), key=key + "_vist")]
        sts = {"Todos": None} | {v: k for k, v in S.STATUS_VISTORIA.items()}
        status = sts[b.selectbox("Status", list(sts), key=key + "_status")]
        tipos = {"Todos": None} | {v: k for k, v in ctx.tipos_veiculo.items()}
        tipo = tipos[c.selectbox("Tipo de veículo", list(tipos), key=key + "_tipo")]
    return dict(data_ini=di.isoformat() if di else None, data_fim=df.isoformat() if df else None, usuario_id=vist,
                placa=placa or None, modelo=modelo or None, status=status, tipo_veiculo=tipo)


def pg_vistorias(ctx, empresa_id=None):
    if st.session_state.get("vistoria_aberta"):
        return detalhe_vistoria(ctx, st.session_state["vistoria_aberta"], "vistorias")
    if empresa_id is None:
        proprias = not pode(ctx.ator, "vistorias.ver_todas")
        cabecalho("Minhas vistorias" if proprias else "Histórico de vistorias",
                  "Consulte, continue ou abra o laudo de cada vistoria.")
    if pode(ctx.ator, "vistorias.criar") and st.button("＋ Nova vistoria", type="primary", key="vist_nova"):
        ctx.nova_vistoria()
    filtros = _filtros_vistorias(ctx, f"fv_{empresa_id}", empresa_id)
    ok, linhas = protegido(S.listar_vistorias, ctx.ator, empresa_id, **filtros)
    if not ok:
        return
    for l in linhas:
        l["data_fmt"] = db.br(l["data_conclusao"] or l["data_inicio"])
        l["status_fmt"] = status_txt(l["status"])
        l["tipo_fmt"] = ctx.tipos_veiculo.get(l["tipo_veiculo"], l["tipo_veiculo"])
    cols = {"data_fmt": "Data", "numero": "Nº", "placa": "Placa", "veiculo_desc": "Veículo",
            "vistoriador_nome": "Vistoriador", "status_fmt": "Status"}
    if ctx.ator.super and empresa_id is None:
        cols["empresa_nome"] = "Empresa"
    sel = tabela(linhas, cols, key=f"tb_vist_{empresa_id}")
    if sel:
        if st.button(f"Visualizar laudo — {sel['numero']}", type="primary", use_container_width=True, key="vist_abrir"):
            st.session_state["vistoria_aberta"] = sel["id"]
            st.rerun()


def pg_veiculos(ctx, empresa_id=None):
    if empresa_id is None:
        cabecalho("Veículos", "Veículos vistoriados pela empresa.")
    if st.session_state.get("vistoria_aberta"):
        return detalhe_vistoria(ctx, st.session_state["vistoria_aberta"], "veiculos")
    aberto = st.session_state.get("veiculo_aberto")
    if aberto:
        ok, res = protegido(S.historico_veiculo, ctx.ator, aberto)
        if not ok:
            st.session_state.pop("veiculo_aberto", None)
            return
        ve, vists = res
        if st.button("← Voltar à lista", key="vei_voltar"):
            st.session_state.pop("veiculo_aberto", None)
            st.rerun()
        with st.container(border=True, key="ac_card_veiculo"):
            st.markdown(section_html(f"{ve['marca']} {ve['modelo']}".strip() or "Veículo", ve["placa"]), unsafe_allow_html=True)
            st.markdown(f"**Ano:** {ve['ano'] or '—'}  ·  **Cor:** {ve['cor'] or '—'}  ·  **Km:** {ve['quilometragem'] or '—'}")
        st.subheader("Histórico de vistorias")
        for l in vists:
            l["data_fmt"] = db.br(l["data_conclusao"] or l["data_inicio"])
            l["status_fmt"] = status_txt(l["status"])
        sel = tabela(vists, {"numero": "Vistoria", "data_fmt": "Data", "vistoriador_nome": "Vistoriador",
                             "status_fmt": "Status"}, key="tb_hist_vei")
        if sel and st.button(f"Visualizar laudo — {sel['numero']}", type="primary", use_container_width=True, key="vei_laudo"):
            st.session_state["vistoria_aberta"] = sel["id"]
            st.rerun()
        return
    busca = st.text_input("Buscar por placa, marca, modelo ou cliente", key=f"vei_busca_{empresa_id}")
    ok, linhas = protegido(S.listar_veiculos, ctx.ator, empresa_id, busca or None)
    if not ok:
        return
    for l in linhas:
        l["ultima_fmt"] = db.br(l["ultima_vistoria"])
        l["status_fmt"] = status_txt(l["ultimo_status"] or "")
    sel = tabela(linhas, {"placa": "Placa", "marca": "Marca", "modelo": "Modelo", "ano": "Ano", "cor": "Cor",
                          "quilometragem": "Km", "cliente_nome": "Cliente", "ultima_fmt": "Última vistoria",
                          "ultimo_vistoriador": "Vistoriador", "status_fmt": "Status"}, key=f"tb_vei_{empresa_id}")
    if sel and st.button(f"Ver histórico — {sel['placa']}", type="primary", use_container_width=True, key="vei_abrir"):
        st.session_state["veiculo_aberto"] = sel["id"]
        st.rerun()


def pg_clientes(ctx, empresa_id=None):
    if empresa_id is None:
        cabecalho("Clientes", "Proprietários registrados nas vistorias.")
    if st.session_state.get("vistoria_aberta"):
        return detalhe_vistoria(ctx, st.session_state["vistoria_aberta"], "clientes")
    aberto = st.session_state.get("cliente_aberto")
    if aberto:
        ok, res = protegido(S.historico_cliente, ctx.ator, aberto)
        if not ok:
            st.session_state.pop("cliente_aberto", None)
            return
        c, vists = res
        if st.button("← Voltar à lista", key="cli_voltar"):
            st.session_state.pop("cliente_aberto", None)
            st.rerun()
        with st.container(border=True, key="ac_card_cliente"):
            st.markdown(section_html(c["nome"] or "Cliente", f"CPF {c['cpf'] or '—'} · Telefone {c['telefone'] or '—'}"),
                        unsafe_allow_html=True)
        for l in vists:
            l["data_fmt"] = db.br(l["data_conclusao"] or l["data_inicio"])
            l["status_fmt"] = status_txt(l["status"])
        sel = tabela(vists, {"numero": "Vistoria", "data_fmt": "Data", "placa": "Placa", "veiculo_desc": "Veículo",
                             "vistoriador_nome": "Vistoriador", "status_fmt": "Status"}, key="tb_hist_cli")
        if sel and st.button(f"Visualizar laudo — {sel['numero']}", type="primary", use_container_width=True, key="cli_laudo"):
            st.session_state["vistoria_aberta"] = sel["id"]
            st.rerun()
        return
    busca = st.text_input("Buscar por nome, CPF ou telefone", key=f"cli_busca_{empresa_id}")
    ok, linhas = protegido(S.listar_clientes, ctx.ator, empresa_id, busca or None)
    if not ok:
        return
    for l in linhas:
        l["ultima_fmt"] = db.br(l["ultima_vistoria"])
    sel = tabela(linhas, {"nome": "Nome", "cpf": "CPF", "telefone": "Telefone", "veiculos": "Veículos",
                          "vistorias": "Vistorias", "ultima_fmt": "Última vistoria"}, key=f"tb_cli_{empresa_id}")
    if sel and st.button(f"Ver histórico — {sel['nome'] or sel['cpf']}", type="primary", use_container_width=True, key="cli_abrir"):
        st.session_state["cliente_aberto"] = sel["id"]
        st.rerun()


def pg_laudos(ctx, empresa_id=None):
    if st.session_state.get("vistoria_aberta"):
        return detalhe_vistoria(ctx, st.session_state["vistoria_aberta"], "laudos")
    if empresa_id is None:
        proprios = not pode(ctx.ator, "laudos.ver_todos")
        cabecalho("Meus laudos" if proprios else "Laudos", "Laudos em PDF emitidos nas vistorias concluídas.")
    with st.expander("Filtros", expanded=False):
        a, b = st.columns(2)
        di = a.date_input("Data inicial", value=None, format="DD/MM/YYYY", key=f"lau_di_{empresa_id}")
        df = b.date_input("Data final", value=None, format="DD/MM/YYYY", key=f"lau_df_{empresa_id}")
        placa = st.text_input("Placa", key=f"lau_placa_{empresa_id}")
    ok, linhas = protegido(S.listar_laudos, ctx.ator, empresa_id, di.isoformat() if di else None,
                           df.isoformat() if df else None, None, placa or None)
    if not ok:
        return
    for l in linhas:
        l["data_fmt"] = db.br(l["data"])
    cols = {"numero": "Laudo", "data_fmt": "Data", "placa": "Placa", "veiculo_desc": "Veículo",
            "cliente_nome": "Cliente", "vistoriador_nome": "Vistoriador"}
    if ctx.ator.super and empresa_id is None:
        cols["empresa_nome"] = "Empresa"
    sel = tabela(linhas, cols, key=f"tb_lau_{empresa_id}")
    if not sel:
        return
    with st.container(border=True, key="ac_card_laudo_sel"):
        st.markdown(section_html(f"Laudo {sel['numero']}", f"{sel['veiculo_desc']} · {sel['placa']}"), unsafe_allow_html=True)
        ok, res = protegido(S.pdf_do_laudo, ctx.ator, sel["id"], ctx.gerar_pdf)
        if ok:
            st.download_button("⬇ Baixar PDF", data=res[0], file_name=res[1], mime="application/pdf", type="primary",
                               use_container_width=True, key=f"lau_pdf_{sel['id']}")
        if st.button("Visualizar laudo completo", use_container_width=True, key="lau_ver"):
            st.session_state["vistoria_aberta"] = sel["vistoria_id"]
            st.rerun()
        if pode(ctx.ator, "laudos.enviar"):
            links_envio(sel["numero"], sel["veiculo_desc"], sel["placa"], sel.get("cliente_telefone"))
        painel_assinaturas(ctx, sel["vistoria_id"], sel["numero"], permitir_envio=False, chave=f"lau{sel['id']}")
        if pode(ctx.ator, "laudos.ver_todos") and not ctx.ator.super:
            if st.button("Gerar PDF novamente com o layout atual", use_container_width=True, key="lau_regen"):
                if protegido(S.regenerar_laudo, ctx.ator, sel["id"], ctx.gerar_pdf)[0]:
                    st.success("PDF atualizado com o layout atual.")


def _mostrar_senha_provisoria(login, senha):
    st.success("Acesso criado. Anote e entregue ao usuário — a senha provisória não será mostrada de novo:")
    st.code(f"Usuário: {login}\nSenha provisória: {senha}", language=None)
    st.caption("No primeiro acesso o sistema exige a criação de uma senha pessoal.")


def pg_usuarios(ctx, empresa_id=None):
    global_ = ctx.ator.super and empresa_id is None
    if empresa_id is None:
        cabecalho("Usuários", "Todos os usuários da plataforma." if global_ else "Pessoas com acesso ao sistema da empresa.")
    msg = st.session_state.pop("usr_msg", None)
    if msg:
        _mostrar_senha_provisoria(*msg)
    if not global_:
        ok, emp = protegido(S.obter_empresa, ctx.ator, empresa_id)
        primeiro = bool(ok and emp["usuarios_ativos"] == 0)
        if ok:
            st.caption(f"Usuários ativos: {uso(emp['usuarios_ativos'], emp['limite_usuarios'])}")
        with st.expander("＋ Novo usuário" if not primeiro else "＋ Criar o primeiro administrador", expanded=primeiro):
            with st.form(f"novo_usr_{empresa_id}", clear_on_submit=True):
                nome = st.text_input("Nome completo")
                a, b = st.columns(2)
                email = a.text_input("E-mail")
                login = b.text_input("Usuário (login)", help="Se ficar em branco, o e-mail será o login.")
                perfil = st.selectbox("Perfil", rbac.PERFIS_DA_EMPRESA, index=0 if primeiro else 1, format_func=rbac.nome_perfil)
                if st.form_submit_button("Criar usuário", type="primary", use_container_width=True):
                    ok, res = protegido(S.criar_usuario, ctx.ator, {"nome": nome, "email": email, "login": login,
                                                                   "perfil": perfil}, empresa_id)
                    if ok:
                        st.session_state["usr_msg"] = (login or email.strip().lower(), res[1])
                        st.rerun()
    ok, linhas = protegido(S.listar_usuarios, ctx.ator, empresa_id)
    if not ok:
        return
    for l in linhas:
        l["perfil_fmt"] = rbac.nome_perfil(l["perfil"])
        l["status_fmt"] = S.STATUS_USUARIO.get(l["status"], l["status"])
        l["acesso_fmt"] = db.br(l["ultimo_acesso"]) or "Nunca"
    cols = {"nome": "Nome", "email": "E-mail", "login": "Usuário", "perfil_fmt": "Perfil", "status_fmt": "Status",
            "acesso_fmt": "Último acesso", "vistorias": "Vistorias"}
    if global_:
        cols["empresa_nome"] = "Empresa"
    sel = tabela(linhas, cols, key=f"tb_usr_{empresa_id}")
    if not sel:
        return
    with st.container(border=True, key="ac_card_usr_sel"):
        st.markdown(section_html(sel["nome"], f"{sel['login']} · {sel['perfil_fmt']}"), unsafe_allow_html=True)
        if sel["id"] == ctx.ator.id:
            st.caption("Este é o seu usuário. Para trocar a senha use “Minha conta”.")
            return
        with st.form(f"edit_usr_{sel['id']}"):
            nome = st.text_input("Nome", sel["nome"])
            email = st.text_input("E-mail", sel["email"])
            perfil = sel["perfil"]
            if sel["perfil"] != rbac.SUPER_ADMIN:
                perfil = st.selectbox("Perfil", rbac.PERFIS_DA_EMPRESA, index=rbac.PERFIS_DA_EMPRESA.index(sel["perfil"])
                                      if sel["perfil"] in rbac.PERFIS_DA_EMPRESA else 1, format_func=rbac.nome_perfil)
            if st.form_submit_button("Salvar alterações", use_container_width=True):
                if protegido(S.atualizar_usuario, ctx.ator, sel["id"], {"nome": nome, "email": email, "perfil": perfil})[0]:
                    st.success("Usuário atualizado.")
                    st.rerun()
        a, b = st.columns(2)
        ativo = sel["status"] == "ativo"
        if a.button("Desativar usuário" if ativo else "Ativar usuário", use_container_width=True, key=f"usr_st_{sel['id']}"):
            if protegido(S.definir_status_usuario, ctx.ator, sel["id"], "inativo" if ativo else "ativo")[0]:
                st.rerun()
        if b.button("Redefinir acesso", use_container_width=True, key=f"usr_reset_{sel['id']}"):
            ok, senha = protegido(S.redefinir_acesso, ctx.ator, sel["id"])
            if ok:
                st.session_state["usr_msg"] = (sel["login"], senha)
                st.rerun()


def pg_relatorios(ctx, empresa_id=None):
    if empresa_id is None:
        cabecalho("Relatórios", "Resumo do período e exportação para planilha.")
    ini, fim = periodo(f"rel_per_{empresa_id}", "Este mês")
    ok, linhas = protegido(S.listar_vistorias, ctx.ator, empresa_id, data_ini=ini, data_fim=fim, limite=100000)
    if not ok:
        return
    concl = [l for l in linhas if l["status"] == "concluida"]
    kpis([("Vistorias no período", num(len([l for l in linhas if l["status"] != "cancelada"]))),
          ("Concluídas", num(len(concl))),
          ("Pendentes / em andamento", num(len([l for l in linhas if l["status"] in ("pendente", "em_andamento")]))),
          ("Canceladas", num(len([l for l in linhas if l["status"] == "cancelada"])))])
    if pode(ctx.ator, "empresa.dashboard"):
        st.subheader("Produtividade")
        prod = S.produtividade(ctx.ator, ini, fim, empresa_id)
        if prod:
            st.dataframe(pd.DataFrame(prod).rename(columns={"vistoriador": "Vistoriador", "total": "Concluídas"}),
                         hide_index=True, use_container_width=True)
        else:
            st.caption("Nenhuma vistoria concluída no período.")
    st.subheader("Por tipo de veículo")
    if concl:
        df = pd.DataFrame([{"Tipo": ctx.tipos_veiculo.get(l["tipo_veiculo"], l["tipo_veiculo"] or "—")} for l in concl])
        grafico_barras(df.value_counts("Tipo").reset_index(name="Vistorias"), "Tipo", "Vistorias", horizontal=True,
                       altura=max(120, 34 * df["Tipo"].nunique()))
    else:
        st.caption("Sem dados.")
    exp = pd.DataFrame([{
        "Número": l["numero"], "Data início": db.br(l["data_inicio"]), "Data conclusão": db.br(l["data_conclusao"]),
        "Placa": l["placa"], "Veículo": l["veiculo_desc"], "Tipo": ctx.tipos_veiculo.get(l["tipo_veiculo"], l["tipo_veiculo"]),
        "Cliente": l["cliente_nome"] or "", "Vistoriador": l["vistoriador_nome"], "Status": status_txt(l["status"]),
    } for l in linhas])
    st.download_button("⬇ Exportar vistorias do período (CSV)", data=exp.to_csv(index=False, sep=";").encode("utf-8-sig"),
                       file_name=f"vistorias_{ini}_{fim}.csv", mime="text/csv", use_container_width=True,
                       disabled=exp.empty, key=f"rel_csv_{empresa_id}")


def pg_logs(ctx, empresa_id=None):
    global_ = ctx.ator.super and empresa_id is None
    if empresa_id is None:
        cabecalho("Logs e auditoria", "Ações registradas em toda a plataforma." if global_ else "Ações registradas na empresa.")
    ini, fim = periodo(f"log_per_{empresa_id}", "Últimos 7 dias")
    ok, linhas = protegido(S.listar_logs, ctx.ator, empresa_id, ini, fim)
    if not ok:
        return
    for l in linhas:
        l["data_fmt"] = db.br(l["data_hora"])
    cols = {"data_fmt": "Data/hora", "usuario_nome": "Usuário", "acao": "Ação", "descricao": "Descrição", "ip": "IP"}
    if global_:
        cols["empresa_nome"] = "Empresa"
    if not linhas:
        st.info("Nenhum registro no período.")
        return
    st.dataframe(pd.DataFrame([{t: l.get(c) or "" for c, t in cols.items()} for l in linhas]),
                 hide_index=True, use_container_width=True)
    st.caption(f"{len(linhas)} registro(s) (máximo de 500 por consulta).")


def _form_dados_empresa(prefixo, e):
    a, b = st.columns(2)
    d = {"nome": a.text_input("Nome da empresa", e.get("nome", ""), key=prefixo + "nome"),
         "cnpj": b.text_input("CNPJ", e.get("cnpj", ""), key=prefixo + "cnpj")}
    a, b = st.columns(2)
    d["razao_social"] = a.text_input("Razão social", e.get("razao_social", ""), key=prefixo + "razao")
    d["responsavel"] = b.text_input("Nome do responsável", e.get("responsavel", ""), key=prefixo + "resp")
    a, b = st.columns(2)
    d["email"] = a.text_input("E-mail", e.get("email", ""), key=prefixo + "email")
    d["telefone"] = b.text_input("Telefone", e.get("telefone", ""), key=prefixo + "tel")
    d["endereco"] = st.text_input("Endereço", e.get("endereco", ""), key=prefixo + "end")
    return d


def pg_configuracoes(ctx):
    cabecalho("Configurações", "Dados da empresa, plano e emitentes salvos.")
    ok, e = protegido(S.obter_empresa, ctx.ator)
    if not ok:
        return
    with st.container(border=True, key="ac_card_plano"):
        st.markdown(section_html("Plano contratado", e.get("plano_nome") or "Sem plano definido"), unsafe_allow_html=True)
        st.markdown(f"**Vistorias:** {uso(e['vistorias_utilizadas'], e['limite_vistorias'])}  \n"
                    f"**Usuários ativos:** {uso(e['usuarios_ativos'], e['limite_usuarios'])}  \n"
                    f"**Início:** {db.br(e['data_inicio'], False) or '—'}  ·  **Vencimento:** "
                    f"{db.br(e['data_vencimento'], False) or 'Sem vencimento'}")
        st.caption("Plano e limites são definidos pela administração da plataforma.")
    with st.form("cfg_empresa"):
        st.markdown(section_html("Dados da empresa"), unsafe_allow_html=True)
        d = _form_dados_empresa("cfg_", e)
        if st.form_submit_button("Salvar dados da empresa", type="primary", use_container_width=True):
            if protegido(S.atualizar_empresa, ctx.ator, None, d)[0]:
                st.success("Dados atualizados.")
    st.subheader("Emitentes salvos")
    st.caption("Usados na etapa 10 • Emitente para preencher o PDF mais rápido.")
    for em in S.listar_emitentes(ctx.ator):
        a, b = st.columns([4, 1])
        a.markdown(f"**{em['empresa']}** · {em['responsavel'] or '—'} · {em['telefone'] or '—'}")
        if b.button("Excluir", key=f"emit_del_{em['id']}", use_container_width=True):
            protegido(S.excluir_emitente, ctx.ator, em["id"])
            st.rerun()
    if pode(ctx.ator, "vistorias.apagar"):
        st.subheader("Apagar vistorias")
        st.caption("Apaga todas as vistorias e laudos DESTA empresa. O consumo do plano não é devolvido.")
        if not st.session_state.get("confirm_clear_inspections"):
            if st.button("Apagar todas as vistorias", use_container_width=True, key="cfg_apagar"):
                st.session_state.confirm_clear_inspections = True
                st.rerun()
        else:
            st.warning("Isso apagará todas as vistorias da empresa e os PDFs correspondentes. Não é possível desfazer.")
            a, b = st.columns(2)
            if a.button("Cancelar", use_container_width=True, key="cfg_apagar_nao"):
                st.session_state.confirm_clear_inspections = False
                st.rerun()
            if b.button("Confirmar apagamento", type="primary", key="confirm_clear_btn", use_container_width=True):
                ok, n = protegido(S.apagar_vistorias_empresa, ctx.ator)
                st.session_state.confirm_clear_inspections = False
                if ok:
                    st.success(f"{n} vistoria(s) apagada(s).")


# ---------------------------------------------------------------------------
# Vistoriador
# ---------------------------------------------------------------------------
def pg_inicio_vistoriador(ctx):
    cabecalho("Início", "Suas vistorias e laudos.", [ctx.ator.nome, db.br(db.hoje() + " 00:00:00", False)])
    pode_iniciar, msg = S.pode_iniciar_vistoria(ctx.ator)
    if not pode_iniciar:
        st.warning(msg)
    if st.button("＋ Nova vistoria", type="primary", use_container_width=True, disabled=not pode_iniciar, key="vi_nova"):
        ctx.nova_vistoria()
    abertas = S.listar_vistorias(ctx.ator, status=["em_andamento", "pendente"], usuario_id=ctx.ator.id)
    st.subheader("Para continuar")
    if not abertas:
        st.caption("Nenhuma vistoria pendente.")
    for v in abertas:
        with st.container(border=True, key=f"ac_card_pend_{v['id']}"):
            st.markdown(section_html(f"{v['numero']} · {status_txt(v['status'])}",
                                     f"{v['veiculo_desc'] or 'Veículo não informado'} · {v['placa'] or 'sem placa'} · "
                                     f"iniciada em {db.br(v['data_inicio'])}"), unsafe_allow_html=True)
            a, b = st.columns(2)
            if a.button("Continuar", type="primary", use_container_width=True, key=f"vi_cont_{v['id']}"):
                ctx.abrir_vistoria(v["id"])
            if b.button("Cancelar", use_container_width=True, key=f"vi_canc_{v['id']}"):
                if protegido(S.cancelar_vistoria, ctx.ator, v["id"])[0]:
                    st.rerun()
    st.subheader("Concluídas recentemente")
    recentes = S.listar_vistorias(ctx.ator, status="concluida", limite=8)
    if not recentes:
        st.caption("Nenhuma vistoria concluída ainda.")
    with st.container(border=True, key="ac_card_recent"):
        for v in recentes:
            st.write(f"**{v['numero']}** • {v['veiculo_desc']} • {v['placa']} • {db.br(v['data_conclusao'])}")
    if recentes and st.button("Ver todos os meus laudos", use_container_width=True, key="vi_laudos"):
        ir("laudos")


# ---------------------------------------------------------------------------
# Minha conta / troca de senha
# ---------------------------------------------------------------------------
def pg_minha_conta(ctx):
    cabecalho("Minha conta", f"{ctx.ator.nome} · {rbac.nome_perfil(ctx.ator.perfil)}", [f"Usuário: {ctx.ator.login}"])
    with st.form("troca_senha", clear_on_submit=True):
        atual = st.text_input("Senha atual", type="password")
        nova = st.text_input("Nova senha", type="password", help="Mínimo de 8 caracteres. Letras maiúsculas e minúsculas são diferentes.")
        conf = st.text_input("Confirmar nova senha", type="password")
        if st.form_submit_button("Alterar senha", type="primary", use_container_width=True):
            if nova != conf:
                st.error("As senhas não conferem.")
            elif protegido(S.trocar_senha, ctx.ator, atual, nova)[0]:
                st.success("Senha alterada.")


def tela_troca_obrigatoria(ator, ao_concluir):
    with st.container(key="ac_login"):
        st.markdown(brand_html(), unsafe_allow_html=True)
        st.markdown(section_html("Crie sua senha pessoal",
                                 "Por segurança, a senha provisória precisa ser trocada antes de continuar."),
                    unsafe_allow_html=True)
        with st.form("troca_obrigatoria"):
            nova = st.text_input("Nova senha", type="password", help="Mínimo de 8 caracteres.")
            conf = st.text_input("Confirmar nova senha", type="password")
            if st.form_submit_button("Salvar e continuar", type="primary", use_container_width=True):
                if nova != conf:
                    st.error("As senhas não conferem.")
                elif protegido(S.trocar_senha, ator, None, nova, obrigatoria=True)[0]:
                    ao_concluir()


# ---------------------------------------------------------------------------
# Super Admin
# ---------------------------------------------------------------------------
def pg_super_dashboard(ctx):
    cabecalho("Painel da plataforma", "Visão geral de todas as empresas.", [ctx.ator.nome])
    ind = S.indicadores_plataforma(ctx.ator)
    kpis([("Total de empresas", num(ind["total_empresas"])), ("Empresas ativas", num(ind["empresas_ativas"])),
          ("Total de usuários", num(ind["total_usuarios"])), ("Total de vistorias", num(ind["total_vistorias"])),
          ("Vistorias no mês", num(ind["vistorias_mes"])), ("Próximas do limite", num(ind["empresas_proximas_limite"])),
          ("Empresas inativas/bloqueadas", num(ind["empresas_inativas"]))])
    st.caption(f"'Próximas do limite': consumo ≥ {round(S.ALERTA_CONSUMO * 100)}% do plano ou vencimento em até "
               f"{S.ALERTA_VENCIMENTO_DIAS} dias.")
    ini, fim = periodo("sa_per")
    ser = S.series_plataforma(ctx.ator, ini, fim)
    a, b = st.columns(2)
    with a:
        st.subheader("Vistorias na plataforma")
        df = pd.DataFrame(ser["vistorias_por_dia"])
        if not df.empty:
            df["dia"] = pd.to_datetime(df["dia"]).dt.strftime("%d/%m")
        grafico_barras(df, "dia", "total", "", "Vistorias")
    with b:
        st.subheader("Atividade (ações registradas)")
        df = pd.DataFrame(ser["atividade_por_dia"])
        if not df.empty:
            df["dia"] = pd.to_datetime(df["dia"]).dt.strftime("%d/%m")
        grafico_barras(df, "dia", "total", "", "Ações", cor=COR_DESTAQUE)
    a, b = st.columns(2)
    with a:
        st.subheader("Crescimento de empresas")
        df = pd.DataFrame(ser["empresas_por_mes"])
        if not df.empty:
            df["acumulado"] = df["total"].cumsum()
            df["mes"] = pd.to_datetime(df["mes"] + "-01").dt.strftime("%m/%Y")
        grafico_barras(df, "mes", "acumulado", "", "Empresas (acumulado)")
    with b:
        st.subheader("Empresas por plano")
        grafico_barras(pd.DataFrame(ser["empresas_por_plano"]), "plano", "total", "", "Empresas", horizontal=True)
    a, b = st.columns(2)
    with a:
        st.subheader("Consumo por empresa")
        grafico_barras(pd.DataFrame(ser["consumo_por_empresa"]), "empresa", "utilizadas", "", "Vistorias utilizadas",
                       horizontal=True)
    with b:
        st.subheader("Usuários por empresa")
        grafico_barras(pd.DataFrame(ser["usuarios_por_empresa"]), "empresa", "total", "", "Usuários ativos",
                       horizontal=True, cor=COR_DESTAQUE)
    st.subheader("Empresas")
    _tabela_empresas(ctx, "tb_emp_dash")


def _linhas_empresas(ctx):
    linhas = S.listar_empresas(ctx.ator)
    for e in linhas:
        e["status_fmt"] = S.STATUS_EMPRESA.get(e["status"], e["status"]) + (" (vencida)" if e["vencida"] else "")
        e["plano_fmt"] = e["plano_nome"] or "Sem plano"
        e["vist_fmt"] = uso(e["vistorias_utilizadas"], e["limite_vistorias"])
        e["usr_fmt"] = uso(e["usuarios_ativos"], e["limite_usuarios"])
        e["ini_fmt"] = db.br(e["data_inicio"], False)
        e["venc_fmt"] = db.br(e["data_vencimento"], False) or "—"
        e["acesso_fmt"] = db.br(e["ultimo_acesso"]) or "Nunca"
    return linhas


def _tabela_empresas(ctx, key):
    linhas = _linhas_empresas(ctx)
    sel = tabela(linhas, {"nome": "Empresa", "cnpj": "CNPJ", "plano_fmt": "Plano", "status_fmt": "Status",
                          "usr_fmt": "Usuários", "vist_fmt": "Vistorias", "ini_fmt": "Contratação",
                          "venc_fmt": "Vencimento", "acesso_fmt": "Último acesso"}, key=key)
    if not sel:
        return
    with st.container(border=True, key=f"ac_card_emp_{key}"):
        st.markdown(section_html(sel["nome"], f"{sel['plano_fmt']} · {sel['vist_fmt']} vistorias · {sel['usr_fmt']} usuários"),
                    unsafe_allow_html=True)
        a, b, c = st.columns(3)
        if a.button("Visualizar / Gerenciar", type="primary", use_container_width=True, key=f"{key}_ger"):
            ir("sa_empresa", empresa_aberta=sel["id"], sa_emp_aba=None)
        if b.button("Editar", use_container_width=True, key=f"{key}_edit"):
            ir("sa_empresa", empresa_aberta=sel["id"], sa_emp_aba="editar")
        if sel["status"] == "ativa":
            if c.button("Bloquear", use_container_width=True, key=f"{key}_bloq"):
                if protegido(S.definir_status_empresa, ctx.ator, sel["id"], "bloqueada")[0]:
                    st.rerun()
        else:
            if c.button("Desbloquear / ativar", use_container_width=True, key=f"{key}_desb"):
                if protegido(S.definir_status_empresa, ctx.ator, sel["id"], "ativa")[0]:
                    st.rerun()


def _form_plano_limites(prefixo, e, planos):
    opcoes = [None] + [p["id"] for p in planos]
    nomes = {None: "Sem plano"} | {p["id"]: p["nome"] for p in planos}
    atual = e.get("plano_id")
    plano_id = st.selectbox("Plano", opcoes, index=opcoes.index(atual) if atual in opcoes else 0,
                            format_func=lambda i: nomes[i], key=prefixo + "plano")
    plano = next((p for p in planos if p["id"] == plano_id), None)
    usar_plano = plano is not None and st.checkbox("Usar os limites padrão do plano", value=not e.get("id"),
                                                   key=prefixo + "usar_plano")
    a, b = st.columns(2)
    lv = e.get("limite_vistorias") if not usar_plano else plano["limite_vistorias"]
    lu = e.get("limite_usuarios") if not usar_plano else plano["limite_usuarios"]
    lim_v = a.number_input("Limite de vistorias (0 = sem limite)", min_value=0, step=1, value=int(lv or 0),
                           key=prefixo + f"limv_{usar_plano}_{plano_id}", disabled=usar_plano)
    lim_u = b.number_input("Limite de usuários (0 = sem limite)", min_value=0, step=1, value=int(lu or 0),
                           key=prefixo + f"limu_{usar_plano}_{plano_id}", disabled=usar_plano)
    a, b = st.columns(2)
    hoje = datetime.strptime(db.hoje(), "%Y-%m-%d").date()
    ini_padrao = datetime.strptime(e["data_inicio"][:10], "%Y-%m-%d").date() if e.get("data_inicio") else hoje
    ini = a.date_input("Data de início", value=ini_padrao, format="DD/MM/YYYY", key=prefixo + "ini")
    venc_padrao = None
    if e.get("data_vencimento"):
        venc_padrao = datetime.strptime(e["data_vencimento"][:10], "%Y-%m-%d").date()
    elif plano and plano.get("duracao_meses") and not e.get("id"):
        venc_padrao = (pd.Timestamp(ini) + pd.DateOffset(months=int(plano["duracao_meses"]))).date()
    venc = b.date_input("Data de vencimento (vazio = sem vencimento)", value=venc_padrao, format="DD/MM/YYYY",
                        key=prefixo + f"venc_{plano_id}")
    return {"plano_id": plano_id, "limite_vistorias": lim_v, "limite_usuarios": lim_u,
            "data_inicio": ini.isoformat() if ini else None, "data_vencimento": venc.isoformat() if venc else None}


def pg_empresas(ctx):
    cabecalho("Empresas", "Clientes da plataforma, planos, limites e situação.")
    if st.session_state.pop("emp_criada", None):
        st.success("Empresa criada. Agora crie o primeiro administrador.")
    msg_exc = st.session_state.pop("emp_excluida", None)
    if msg_exc:
        st.success(msg_exc)
    with st.expander("＋ Nova empresa", expanded=False):
        d = _form_dados_empresa("ne_", {})
        st.markdown(section_html("Plano e limites"), unsafe_allow_html=True)
        d.update(_form_plano_limites("ne_", {}, S.listar_planos(ctx.ator, somente_ativos=True)))
        if st.button("Criar empresa", type="primary", use_container_width=True, key="ne_criar"):
            ok, eid = protegido(S.criar_empresa, ctx.ator, d)
            if ok:
                st.session_state["emp_criada"] = True
                ir("sa_empresa", empresa_aberta=eid, sa_emp_aba="usuarios")
    _tabela_empresas(ctx, "tb_emp")


def pg_gerenciar_empresa(ctx):
    eid = st.session_state.get("empresa_aberta")
    if not eid:
        return ir("sa_empresas")
    ok, e = protegido(S.obter_empresa, ctx.ator, eid)
    if not ok:
        return
    if st.button("← Voltar às empresas", key="ge_voltar"):
        for k in ("empresa_aberta", "vistoria_aberta", "veiculo_aberto", "cliente_aberto"):
            st.session_state.pop(k, None)
        ir("sa_empresas")
    cabecalho(e["nome"], e.get("razao_social") or e.get("cnpj") or "",
              [S.STATUS_EMPRESA.get(e["status"], e["status"]), e.get("plano_nome") or "Sem plano",
               f"Vistorias {uso(e['vistorias_utilizadas'], e['limite_vistorias'])}",
               f"Usuários {uso(e['usuarios_ativos'], e['limite_usuarios'])}"])
    if st.session_state.pop("emp_criada", None):
        st.success("Empresa criada. Crie agora o primeiro administrador na aba Usuários.")
    abas = ["Resumo", "Editar e plano", "Usuários", "Vistorias", "Veículos", "Laudos", "Logs"]
    mapa = {"editar": "Editar e plano", "usuarios": "Usuários"}
    escolha = st.radio("Seção", abas, horizontal=True, label_visibility="collapsed", key=f"ge_aba_{eid}",
                       index=abas.index(mapa.get(st.session_state.pop("sa_emp_aba", None) or "", "Resumo")))
    if escolha == "Resumo":
        pg_dashboard(ctx, eid)
    elif escolha == "Editar e plano":
        with st.container(border=True, key="ac_card_ge_form"):
            d = _form_dados_empresa(f"ge_{eid}_", e)
            st.markdown(section_html("Plano e limites"), unsafe_allow_html=True)
            d.update(_form_plano_limites(f"ge_{eid}_", e, S.listar_planos(ctx.ator)))
            if st.button("Salvar alterações", type="primary", use_container_width=True, key=f"ge_salvar_{eid}"):
                if protegido(S.atualizar_empresa, ctx.ator, eid, d)[0]:
                    st.success("Empresa atualizada.")
        with st.container(border=True, key="ac_card_ge_status"):
            st.markdown(section_html("Situação da conta", S.STATUS_EMPRESA.get(e["status"], e["status"])), unsafe_allow_html=True)
            a, b, c = st.columns(3)
            for col, stt, rot in ((a, "ativa", "Ativar"), (b, "bloqueada", "Bloquear"), (c, "inativa", "Desativar conta")):
                if col.button(rot, use_container_width=True, disabled=e["status"] == stt, key=f"ge_st_{stt}"):
                    if protegido(S.definir_status_empresa, ctx.ator, eid, stt)[0]:
                        st.rerun()
            st.caption("Bloquear/desativar encerra imediatamente as sessões dos usuários da empresa.")
        with st.container(border=True, key="ac_card_ge_consumo"):
            st.markdown(section_html("Consumo do plano", f"Utilizadas: {num(e['vistorias_utilizadas'])} · "
                                     f"em andamento (reservadas): {num(e['reservadas'])}"), unsafe_allow_html=True)
            novo = st.number_input("Ajustar vistorias utilizadas (ex.: renovação do plano)", min_value=0, step=1,
                                   value=int(e["vistorias_utilizadas"]), key=f"ge_cons_{eid}")
            if st.button("Aplicar ajuste", use_container_width=True, key="ge_cons_btn"):
                if protegido(S.ajustar_consumo, ctx.ator, eid, novo)[0]:
                    st.rerun()
        with st.container(border=True, key="ac_card_ge_excluir"):
            st.markdown(section_html("Excluir empresa", "Ação permanente"), unsafe_allow_html=True)
            st.warning(f"Apaga a empresa **{e['nome']}** com todas as vistorias, laudos (PDFs), usuários, "
                       "clientes, veículos e emitentes. Os logs são mantidos. Não é possível desfazer.")
            nome_conf = st.text_input("Para confirmar, digite o nome da empresa", key=f"ge_exc_nome_{eid}")
            if st.button("Excluir empresa definitivamente", type="primary", use_container_width=True,
                         disabled=nome_conf.strip() != e["nome"].strip(), key=f"ge_exc_btn_{eid}"):
                ok, r = protegido(S.excluir_empresa, ctx.ator, eid, nome_conf)
                if ok:
                    for k in ("empresa_aberta", "vistoria_aberta", "veiculo_aberto", "cliente_aberto"):
                        st.session_state.pop(k, None)
                    st.session_state["emp_excluida"] = (f"Empresa {e['nome']} excluída: {r['vistorias']} vistoria(s), "
                                                        f"{r['laudos']} laudo(s), {r['usuarios']} usuário(s).")
                    ir("sa_empresas")
    elif escolha == "Usuários":
        pg_usuarios(ctx, eid)
    elif escolha == "Vistorias":
        pg_vistorias(ctx, eid)
    elif escolha == "Veículos":
        pg_veiculos(ctx, eid)
    elif escolha == "Laudos":
        pg_laudos(ctx, eid)
    elif escolha == "Logs":
        pg_logs(ctx, eid)


def pg_planos(ctx):
    cabecalho("Planos", "Modelos de contratação com limites padrão.")
    st.caption("Os limites de cada empresa podem ser ajustados individualmente em Empresas › Editar e plano.")
    planos = S.listar_planos(ctx.ator)
    sel_id = st.session_state.get("plano_sel")
    with st.expander("＋ Novo plano" if not sel_id else "Editar plano", expanded=bool(sel_id)):
        p = next((x for x in planos if x["id"] == sel_id), {}) if sel_id else {}
        with st.form(f"plano_form_{sel_id}"):
            nome = st.text_input("Nome do plano", p.get("nome", ""))
            desc = st.text_input("Descrição", p.get("descricao", ""))
            a, b, c = st.columns(3)
            lv = a.number_input("Limite de vistorias (0 = sem limite)", min_value=0, step=1, value=int(p.get("limite_vistorias") or 0))
            lu = b.number_input("Limite de usuários (0 = sem limite)", min_value=0, step=1, value=int(p.get("limite_usuarios") or 0))
            dm = c.number_input("Duração (meses, 0 = indefinida)", min_value=0, step=1, value=int(p.get("duracao_meses") or 0))
            ativo = st.checkbox("Disponível para novas empresas", value=bool(p.get("ativo", 1)))
            if st.form_submit_button("Salvar plano", type="primary", use_container_width=True):
                if protegido(S.salvar_plano, ctx.ator, {"nome": nome, "descricao": desc, "limite_vistorias": lv,
                                                        "limite_usuarios": lu, "duracao_meses": dm, "ativo": ativo}, sel_id)[0]:
                    st.session_state.pop("plano_sel", None)
                    st.rerun()
        if sel_id and st.button("Cancelar edição", key="plano_cancel"):
            st.session_state.pop("plano_sel", None)
            st.rerun()
    for p in planos:
        p["lv"] = num(p["limite_vistorias"]) if p["limite_vistorias"] else "Sem limite"
        p["lu"] = num(p["limite_usuarios"]) if p["limite_usuarios"] else "Sem limite"
        p["dm"] = f"{p['duracao_meses']} meses" if p["duracao_meses"] else "—"
        p["at"] = "Sim" if p["ativo"] else "Não"
    sel = tabela(planos, {"nome": "Plano", "descricao": "Descrição", "lv": "Vistorias", "lu": "Usuários",
                          "dm": "Duração", "empresas": "Empresas", "at": "Disponível"}, key="tb_planos")
    if sel and st.button(f"Editar plano — {sel['nome']}", use_container_width=True, key="plano_edit"):
        st.session_state["plano_sel"] = sel["id"]
        st.rerun()


def pg_integracoes(ctx):
    rbac.exigir(ctx.ator, "plataforma.gerenciar")
    cabecalho("Integrações", "Serviços externos usados pela plataforma.")
    with st.container(border=True, key="ac_card_int_placa"):
        cfg = st.session_state.get("_placa_cfg")
        st.markdown(section_html("Consulta automática de placa", "Achecar (consulta gratuita)"), unsafe_allow_html=True)
        if cfg is not None:
            st.markdown(f"**Endereço:** `{cfg.base_url}`  \n**Tempo limite:** {cfg.timeout:.0f} s")
            if cfg.config_error:
                st.caption(f"Observação da configuração: {cfg.config_error}. Usando o padrão.")
        st.caption("A configuração fica nos Secrets do servidor (seção [placa_api]) e nunca é exibida no navegador.")
        placa = st.text_input("Testar com uma placa", key="int_placa")
        if st.button("Testar consulta", use_container_width=True, key="int_testar", disabled=not placa):
            with st.spinner("Consultando..."):
                r = placa_api.lookup_plate(placa, cfg)
            (st.success if r.status == "ok" else st.warning)(
                f"Status: {r.status}. " + (r.message or ", ".join(f"{k}: {v}" for k, v in list(r.data.items())[:5])))
            S.registrar(ctx.ator, "integracao_teste", f"Teste da consulta de placa: {r.status}")
    with st.container(border=True, key="ac_card_int_ass"):
        cfg = ctx.assinatura_cfg
        nome = {"clicksign": "Clicksign (API v3)", "d4sign": "D4Sign"}.get(getattr(cfg, "provider", ""), "Nenhum")
        st.markdown(section_html("Assinatura eletrônica à distância", nome), unsafe_allow_html=True)
        prov = assinatura.criar_provedor(cfg)
        if prov.configurado:
            host = cfg.base_url.split("//", 1)[-1].split("/", 1)[0]
            st.success(f"Configurado · ambiente: {cfg.ambiente} · servidor: {host} · tempo limite: {cfg.timeout:.0f} s")
            st.caption("Chave da API: configurada (nunca é exibida).")
        else:
            st.warning("Não configurado: o botão “Enviar para assinatura à distância” aparece desabilitado."
                       + (f" Motivo: {prov.motivo or getattr(cfg, 'config_error', '')}."
                          if (prov.motivo or getattr(cfg, "config_error", "")) else ""))
        st.caption("Configuração nos Secrets do servidor, seção [assinatura] (provider, api_key, base_url, timeout). "
                   "Sem webhook: o status é consultado ao abrir o laudo ou em “Atualizar status”.")
    with st.container(border=True, key="ac_card_int_db"):
        st.markdown(section_html("Banco de dados", db.descricao_banco()), unsafe_allow_html=True)
        if db.usando_postgres():
            st.caption("Banco permanente: os dados não se perdem quando o app reinicia.")
        else:
            st.warning("Banco local (SQLite). Em hospedagens com disco temporário (ex.: Streamlit Community Cloud) "
                       "os dados se perdem ao reiniciar. Configure a seção [database] nos Secrets.")
