import streamlit as st
import streamlit.components.v1 as components
from pathlib import Path
from datetime import datetime
import json, hashlib, base64, io, re, secrets, time
from PIL import Image, ImageDraw, ImageOps
from streamlit.errors import StreamlitAPIException
import pdf_report, placa_api
import os
from saas import db as sdb, migracao, rbac, seguranca, servicos as S
import paineis
import assinatura
import desenho
from desenho import render as desenho_render
import sessao_navegador

import unicodedata
APP = "CH360"
DATA_DIR = Path(os.environ.get("LAUDO_DATA_DIR", "autocheck_data"))
DATA_DIR.mkdir(exist_ok=True)
PDF_DIR = DATA_DIR / "pdfs"
PDF_DIR.mkdir(exist_ok=True)
USERS_FILE = DATA_DIR / "users.json"
INSPECTIONS_FILE = DATA_DIR / "inspections.json"
ISSUERS_FILE = DATA_DIR / "emitentes.json"
SESSIONS_FILE = DATA_DIR / "sessions.json"
DRAFTS_DIR = DATA_DIR / "drafts"
DRAFTS_DIR.mkdir(exist_ok=True)

FUEL_TYPES = ["Gasolina","Etanol","Flex","Diesel","GNV","Elétrico","Híbrido"]
FUEL_LEVELS = [("Reserva",5),("1/4",25),("1/2",50),("3/4",75),("Cheio",100)]
ACCESSORIES = [
    "Veículo envelopado","Blindado","Funciona","Segredo","Painel","Quebra sol","Break light","Retrovisor",
    "Macaco","Chave de roda","Estepe","Extintor","Triângulo","Tapetes","Isqueiro","Alto falantes","Aparelho de som",
    "Antena","Faróis auxiliares","Aerofólio","Engate traseiro","Quebra mato","Modulo (carro)","Modulo (som)",
    "Rack","Estribo","Tampão porta malas","Bateria e marca",
    "Veículo limpo","Outros acessórios"
]
TIRES = [("Dianteiro esquerdo","DE"),("Dianteiro direito","DD"),("Traseiro esquerdo","TE"),("Traseiro direito","TD"),("Estepe","ESP")]
PHOTO_SLOTS = [("frente","Frente"),("traseira","Traseira"),("lateral_esq","Lateral esquerda"),("lateral_dir","Lateral direita"),("interior","Interior"),("painel","Painel / km")]
KEY_DOC_ITEMS = [("chave_principal","Chave principal"),("chave_reserva","Chave reserva"),("manual","Manual do veículo"),("documento","Documento do veículo")]
KEY_DOC_ICONS = {"chave_principal":"🔑","chave_reserva":"🗝️","manual":"📘","documento":"📄"}
KEY_DOC_LABELS = dict(KEY_DOC_ITEMS)

VEHICLE_BRANDS = [
    "CHEVROLET", "FIAT", "FORD", "VOLKSWAGEN", "TOYOTA", "HONDA", "HYUNDAI", "RENAULT", "NISSAN", "JEEP",
    "PEUGEOT", "CITROËN", "MITSUBISHI", "KIA", "BMW", "BYD", "GEELY", "GWM", "MERCEDES-BENZ", "AUDI", "VOLVO", "LAND ROVER",
    "SUZUKI", "YAMAHA", "KAWASAKI", "TRIUMPH", "HARLEY-DAVIDSON", "IVECO", "SCANIA", "VOLVO TRUCKS",
    "MERCEDES-BENZ CAMINHÕES", "AGRALE", "TROLLER", "RAM", "CHERY/CAOA"
]
TIRE_BRANDS = [
    "Pirelli","Goodyear","Michelin","Continental","Bridgestone","Firestone","Dunlop",
    "General Tire","Yokohama","Maxxis","Hankook","Kumho","Cooper","Nexen"
]

_current_year = datetime.now().year
VEHICLE_YEARS = [str(y) for y in range(_current_year + 1, 1979, -1)]



def pick_or_type(label, options, current, key, allow_blank=False):
    """Selectbox com lista pré-definida + opção 'Outro' com campo livre.
    allow_blank=True acrescenta uma 1ª opção em branco: assim o campo pode ficar
    realmente vazio (e a consulta pela placa consegue preenchê-lo)."""
    outro = "Outro (digitar)"
    choices = ([""] if allow_blank else []) + list(options) + [outro]
    by_upper = {str(o).upper(): o for o in options}   # compara sem diferenciar maiúsculas/minúsculas
    cur = str(current or "")
    if cur.upper() in by_upper:
        idx = choices.index(by_upper[cur.upper()])
    elif cur:
        idx = len(choices) - 1  # valor já digitado que não está na lista -> cai em "Outro"
    else:
        idx = 0
    sel = st.selectbox(label, choices, index=idx, key=key + "_sel",
                       format_func=lambda o: "Selecione..." if o == "" else o)
    if sel == outro:
        return st.text_input(label + " (digite)", cur if cur.upper() not in by_upper else "", key=key + "_free")
    return sel
STEPS = [
    ("veiculo","02","Veículo"),("combustivel","03","Combustível"),
    ("chave_documentos","04","Chave e Documentos"),("acessorios","05","Acessórios"),
    ("pneus","06","Pneus"),("avarias","07","Avarias"),("fotos","08","Fotos"),
    ("proprietario","09","Proprietário"),("emitente","10","Emitente"),("revisao","11","Revisão / PDF")
]

def load_json(path, default):
    if not path.exists(): return default
    try: return json.loads(path.read_text(encoding="utf-8"))
    except Exception: return default

def save_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def hash_pw(pw): return hashlib.sha256(pw.encode("utf-8")).hexdigest()
def now(): return datetime.now(sdb.TZ).strftime("%d/%m/%Y %H:%M")   # horário de Brasília mesmo em servidor UTC

def seed_users():
    if not USERS_FILE.exists():
        save_json(USERS_FILE, [
            {"usuario":"admin","nome":"Administrador","senha":hash_pw("admin123"),"perfil":"Administrador","ativo":True},
            {"usuario":"inspetor","nome":"Inspetor Demo","senha":hash_pw("inspetor123"),"perfil":"Inspetor","ativo":True}
        ])
# seed_users() não é mais chamado: os usuários agora ficam no banco (saas/) e novas contas
# são criadas pelo Super Admin / administradores. Os JSON antigos são importados pela migração.

def next_inspection_number():
    year = datetime.now().year
    items = load_json(INSPECTIONS_FILE, [])
    nums = []
    for item in items:
        m = re.fullmatch(rf"CHK-{year}-(\d+)", str(item.get("numero", "")))
        if m:
            nums.append(int(m.group(1)))
    seq = max(nums, default=0) + 1
    return f"CHK-{year}-{seq:06d}"

def pdf_path(numero):
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", numero)
    return PDF_DIR / f"{safe}.pdf"

def save_pdf_file(numero, data):
    path = pdf_path(numero)
    path.write_bytes(data)
    return path

def new_inspection():
    return {
        "numero": "",   # definido pelo banco (por empresa) quando a vistoria é registrada: ver registrar_vistoria_atual()
        "criado_em": now(), "finalizado_em": None, "status":"Em andamento", "inspetor":"",
        "veiculo": {"marca":"","modelo":"","ano":"","placa":"","cor":"","tipo":"","chassi":"","km":"","observacoes":""},
        "combustivel":{"tipo":"","nivel":"1/2","percentual":50},
        "acessorios":{a:{"status":"","obs":""} for a in ACCESSORIES},
        "chave_documentos":{"foto": None}, "chave_documentos_obs":"",
        "pneus":{k:{"estado":"","marca":"","medida":"","observacao":""} for _,k in TIRES},
        "avarias":{"diagrama":None,"imagem":None,"marcacoes":[],"fotos":[]},
        "fotos":{}, "fotos_acessorios":[],
        "proprietario":{"nome":"","cpf":"","telefone":"","assinatura":None},
        "emitente":{"empresa":"","documento":"","telefone":"","email":"","endereco":"","responsavel":"","assinatura":None}
    }

def normalize_tires(inspection):
    """Remove campos antigos de medida/observação individual dos pneus."""
    for it in inspection.get("pneus", {}).values():
        it.pop("medida", None)
        it.pop("observacao", None)
    inspection.setdefault("pneus_observacao", "")
    # Chave e Documentos: mantém TODAS as fotos. Vistorias no formato antigo
    # (chave_principal / chave_reserva / manual / documento) não perdem mais fotos.
    kd = inspection.setdefault("chave_documentos", {"foto": None})
    kd.setdefault("foto", None)
    inspection.setdefault("chave_documentos_obs", "")   # observação opcional da etapa
    # Avarias: lista de fotos (uma entrada por foto, com descrição opcional).
    av = inspection.setdefault("avarias", {"diagrama": None, "imagem": None, "marcacoes": []})
    if not isinstance(av.get("fotos"), list):
        av["fotos"] = []
    return inspection


def b64_image(upload):
    if upload is None: return None
    img = ImageOps.exif_transpose(Image.open(upload)).convert("RGB")  # respeita a orientação da câmera
    img.thumbnail((1400,1000))
    out=io.BytesIO(); img.save(out,"JPEG",quality=78,optimize=True)
    return base64.b64encode(out.getvalue()).decode()

def b64_pil(img):
    out=io.BytesIO(); img.convert("RGB").save(out,"PNG")
    return base64.b64encode(out.getvalue()).decode()

def b64_jpeg(img, quality=85):
    """Desenho das avarias: JPEG fica ~5x menor que PNG (900 px ~ 115 KB, o mesmo tamanho
    do PNG antigo de 400x460) e o PDF usa os bytes direto."""
    out=io.BytesIO(); img.convert("RGB").save(out,"JPEG",quality=quality,optimize=True)
    return base64.b64encode(out.getvalue()).decode()

def pil_b64(s):
    return Image.open(io.BytesIO(base64.b64decode(s))).convert("RGB")

@st.cache_data(show_spinner=False, max_entries=512)
def thumb_bytes(b64, max_side=480):
    """Miniatura leve (JPEG) para exibir no celular sem carregar a foto inteira."""
    im = pil_b64(b64)
    im.thumbnail((max_side, max_side))
    out = io.BytesIO()
    im.save(out, "JPEG", quality=80)
    return out.getvalue()

VEHICLE_DIAGRAMS = [
    ("sedan","🚗","Sedã"),
    ("hatch","🚙","Hatch"),
    ("suv","🚘","Suv/SW"),
    ("picape_simples","🛻","Picape simples"),
    ("picape_dupla","🛻","Picape cab. dupla"),
    ("van","🚐","Van/Furgão"),
    ("moto_naked","🏍️","Moto naked"),
    ("moto_esportiva","🏍️","Moto esportiva"),
    ("scooter","🛵","Scooter"),
]

DIAGRAM_FILES = {
    "sedan": "sedan.png",
    "hatch": "hatch.png",
    "suv": "suv.png",
    "picape_simples": "picape_simples.png",
    "picape_dupla": "picape_dupla.png",
    "van": "van.png",
    "moto_naked": "moto_naked.png",
    "moto_esportiva": "moto_esportiva.png",
    "scooter": "scooter.png",
}

DIAGRAM_DIR = Path(__file__).resolve().parent / "diagramas"

# Desenho das avarias. Antes: caixa fixa de 400x460 com o desenho (que é horizontal)
# centralizado, sobrando branco em cima e embaixo. Agora a área tem a MESMA proporção do
# desenho, então ela ocupa a largura toda (e na horizontal fica bem maior).
DIAGRAM_EXPORT_W = 900     # largura da imagem salva em av["imagem"] (PDF: até 380 pt -> ~170 dpi)
DIAGRAM_SCREEN_W = 1400    # largura enviada ao navegador (nítida em telas dpr 2-3)

@st.cache_data(show_spinner=False, max_entries=64)
def vehicle_diagram_png(tipo, largura):
    """Desenho real do veículo na proporção original, fundo branco, como PNG."""
    filename = DIAGRAM_FILES.get(tipo, DIAGRAM_FILES["sedan"])
    path = DIAGRAM_DIR / filename
    if path.exists():
        src = Image.open(path).convert("RGBA")
        bg = Image.new("RGBA", src.size, "white")
        src = Image.alpha_composite(bg, src).convert("RGB")
    else:
        src = Image.new("RGB", (760, 460), "white")
        d = ImageDraw.Draw(src)
        d.text((20,20), "Desenho não encontrado", fill="#444444")
    altura = max(1, round(largura * src.height / src.width))
    out = io.BytesIO()
    src.resize((largura, altura), Image.Resampling.LANCZOS).save(out, "PNG", optimize=True)
    return out.getvalue(), src.width / src.height

def vehicle_diagram(tipo, largura=DIAGRAM_EXPORT_W):
    return Image.open(io.BytesIO(vehicle_diagram_png(tipo, largura)[0])).convert("RGB")

@st.cache_data(show_spinner=False, max_entries=64)
def diagram_screen_url(tipo):
    """(data URL em JPEG leve, proporção) do desenho-base para a área de desenho no navegador."""
    png, prop = vehicle_diagram_png(tipo, DIAGRAM_SCREEN_W)
    out = io.BytesIO()
    Image.open(io.BytesIO(png)).convert("RGB").save(out, "JPEG", quality=88, optimize=True)
    return "data:image/jpeg;base64," + base64.b64encode(out.getvalue()).decode("ascii"), prop

@st.cache_data(show_spinner=False, max_entries=32)
def legacy_screen_url(b64):
    """Desenho salvo por versões antigas (400x460 com traços embutidos): vira a base."""
    im = pil_b64(b64)
    return "data:image/png;base64," + b64, im.width / im.height

def pdf_bytes(c, warnings=None):
    """Gera o PDF da vistoria (layout em pdf_report.py). `warnings` recebe avisos de
    fotos que não puderam ser incluídas (nenhuma foto é descartada em silêncio)."""
    labels = {
        "tires": {k: n for n, k in TIRES},
        "photos": dict(PHOTO_SLOTS),
        "keydoc": dict(KEY_DOC_ITEMS),
        "diagrams": {k: lbl for k, _icon, lbl in VEHICLE_DIAGRAMS},
    }
    return pdf_report.build_pdf(c, labels, warnings)

def inspection_fingerprint(c):
    """Assinatura dos dados da vistoria: detecta PDF desatualizado."""
    return hashlib.md5(json.dumps(c, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")).hexdigest()

def draft_path(token):
    return DRAFTS_DIR / f"{token}.json"

def client_ip():
    """IP do cliente para a auditoria (cabeçalhos do proxy; vazio se indisponível)."""
    try:
        h = st.context.headers
        return (h.get("X-Forwarded-For", "") or "").split(",")[0].strip() or h.get("X-Real-Ip", "") or ""
    except Exception:
        return ""

# Sessões ficam no banco (saas.servicos): token aleatório, só o hash no banco, validade máxima,
# revogação ao sair / bloquear / desativar / redefinir acesso.
# O token fica num COOKIE do navegador (não mais na URL: link copiado, print ou histórico não
# entregam o login). Links antigos com ?ac_token= ainda entram uma vez e o parâmetro é apagado.
# st.session_state.user passa a ser um saas.servicos.Ator (id, empresa_id, perfil, nome, login).
COOKIE_TOKEN = "ac_token"
REVALIDAR_SESSAO_S = 45   # revalida no banco no máximo a cada 45 s (antes: a cada clique)

def session_token():
    return st.session_state.get("_ac_token")

def _cookie_token():
    try:
        return st.context.cookies.get(COOKIE_TOKEN) or None
    except Exception:
        return None

def create_login_session(user):
    token = S.criar_sessao(user)
    st.session_state._ac_token = token
    st.session_state._sessao_ok_t = time.time()
    st.session_state.pop("_senha_ok", None)
    return token

def restore_login_session():
    ss = st.session_state
    if "_ac_token" not in ss:
        # 1ª execução desta aba: link antigo (?ac_token=, que sai da URL), cookie visto pelo
        # servidor ou o token lido pelo próprio navegador (ler_token_do_navegador).
        do_navegador = ss.pop("_token_navegador", None)
        candidatos = [st.query_params.get("ac_token"), _cookie_token(), do_navegador]
        if "ac_token" in st.query_params:
            del st.query_params["ac_token"]
        ss._ac_token = None
        for tok in candidatos:
            ator = S.validar_sessao(tok, client_ip()) if tok else None
            if ator is not None:
                ss._ac_token, ss._sessao_ok_t = tok, time.time()
                return ator
        return None
    token = session_token()
    if not token:
        return None
    ator = S.validar_sessao(token, client_ip())
    if ator is not None:
        ss._sessao_ok_t = time.time()
    return ator

def _tem_navegador():
    """Há um navegador de verdade do outro lado? (no AppTest não chega nenhum cabeçalho HTTP)"""
    try:
        return bool(st.context.headers.get("User-Agent") or st.context.headers.get("Host"))
    except Exception:
        return False

def ler_token_do_navegador():
    """F5 / nova aba: antes de decidir entre login e painel, pergunta ao navegador pelo token
    salvo (no Streamlit Cloud o cookie não chega ao servidor). Enquanto ele não responde
    (uma fração de segundo), mostra só "Carregando…"."""
    ss = st.session_state
    if "user" in ss or "_ac_token" in ss or st.query_params.get("ac_token") or _cookie_token():
        return
    if not _tem_navegador():   # testes (AppTest): sem navegador, o componente nunca responderia
        return
    lido = sessao_navegador.navegador("ler", key="ac_sessao_ler")
    if lido is None:
        st.caption("Carregando…")
        st.stop()
    ss._token_navegador = lido.get("token") or None
    ss._navegador_token = lido.get("token") or ""    # o que o navegador já guarda

def cookie_sync():
    """Grava/apaga o token no navegador (cookie + localStorage) quando ele difere do token
    desta sessão. Continua desenhado até o navegador confirmar."""
    ss = st.session_state
    token = session_token() or ""
    if ss.get("_navegador_token") == token:
        return
    if token:
        r = sessao_navegador.navegador("gravar", token=token, nome=COOKIE_TOKEN,
                                       segundos=seguranca.SESSAO_DIAS * 86400, key="ac_sessao_g_" + token[:12])
    else:
        r = sessao_navegador.navegador("apagar", nome=COOKIE_TOKEN, key=f"ac_sessao_a_{ss.get('_logout_n', 0)}")
    if r is not None:
        ss._navegador_token = token

def sync_vistoria_db(inspection, force=False):
    """Guarda o andamento da vistoria no banco (para continuar depois / o admin acompanhar).
    Só grava ao trocar de etapa ou a cada 20 s, para não pesar a cada toque."""
    vid = (inspection or {}).get("_vistoria_id")
    user = st.session_state.get("user")
    if not vid or not user:
        return
    ss = st.session_state
    step = ss.get("step", 0)
    if not force and ss.get("_sync_step") == step and time.time() - ss.get("_sync_t", 0) < 20:
        return
    ss["_sync_step"], ss["_sync_t"] = step, time.time()
    try:
        S.salvar_rascunho(user, vid, inspection)
    except Exception:
        pass

def save_current_state(leve=False):
    """Grava o rascunho (para recarregar a página sem perder nada).
    leve=True: toques rápidos (SIM/NÃO, pneus, combustível) gravam no máximo a cada 10 s na
    mesma etapa — o rascunho inteiro (com as fotos) pesava em cada clique. A troca de etapa
    e as fotos sempre gravam na hora."""
    user = st.session_state.get("user")
    token = session_token()
    if not user or not token:
        return
    ss = st.session_state
    step = ss.get("step", 0)
    if leve and ss.get("_draft_step") == step and time.time() - ss.get("_draft_t", 0) < 10:
        return
    ss["_draft_step"], ss["_draft_t"] = step, time.time()
    inspection = st.session_state.get("inspection")
    if inspection is None:
        inspection = new_inspection()
    data = {
        "step": st.session_state.get("step", 0),
        "damage_view": st.session_state.get("damage_view", "lateral"),
        "page": st.session_state.get("page"),
        "inspection": inspection
    }
    save_json(draft_path(token), data)
    sync_vistoria_db(inspection)

def load_current_state(user):
    token = session_token()
    if not token:
        return False
    data = load_json(draft_path(token), {})
    if not data:
        return False
    st.session_state.step = int(data.get("step", 0))
    st.session_state.damage_view = data.get("damage_view", "lateral")
    st.session_state.page = data.get("page")
    if data.get("inspection"):
        st.session_state.inspection = normalize_tires(data["inspection"])
    return True

def clear_login_session():
    token = session_token()
    st.session_state._ac_token = None      # o cookie_sync() apaga o token no navegador
    st.session_state._logout_n = st.session_state.get("_logout_n", 0) + 1
    st.session_state.pop("_senha_ok", None)
    if token:
        try:
            S.encerrar_sessao(token, st.session_state.get("user"))
        except Exception:
            pass
        try:
            draft_path(token).unlink(missing_ok=True)
        except Exception:
            pass
    st.query_params.clear()

st.set_page_config(page_title="CH360 • Vistoria Veicular",
                   page_icon=Image.open(Path(__file__).resolve().parent / "assets" / "ch360_icone_512.png"),
                   layout="wide",initial_sidebar_state="collapsed")
from templates import page_styles, hero_html, card_html, card_open, card_close, brand_html, sidebar_brand_html, section_html
st.markdown(page_styles(), unsafe_allow_html=True)

def ler_secrets_banco():
    """([database], [superadmin]) dos Secrets. Sem arquivo: ({}, {}) e nenhum aviso na tela."""
    try:
        if st.secrets.load_if_toml_exists():
            banco = dict(st.secrets["database"]) if "database" in st.secrets else {}
            cfg = dict(st.secrets["superadmin"]) if "superadmin" in st.secrets else {}
            return banco, cfg
    except Exception:
        pass
    return {}, {}

@st.cache_resource(show_spinner=False)
def preparar_banco(banco_alvo, versao_esquema):
    """Uma vez por processo e por banco/versão do esquema: cria as tabelas, importa os JSON
    antigos (sem apagá-los) e, se houver [superadmin] nos Secrets, garante que o Super Admin exista.
    Os argumentos só formam a chave do cache: se o banco em uso ou o esquema mudar, roda de novo."""
    sdb.inicializar()
    resumo = migracao.importar_legado()
    _banco, cfg = ler_secrets_banco()
    if cfg.get("login") and cfg.get("senha"):
        migracao.garantir_super_admin(cfg["login"], cfg.get("nome", ""), cfg["senha"])
    return resumo

def escolher_banco():
    """Em TODA execução: [database] url nos Secrets = PostgreSQL permanente; sem isso, SQLite local.
    Fica fora do cache porque, ao recarregar um módulo alterado (ex.: saas/db.py com o app aberto),
    o Streamlit zera a configuração do banco; antes, o app caía no SQLite local sem perceber."""
    banco, _cfg = ler_secrets_banco()
    # LAUDO_TESTE: os testes automáticos nunca usam o banco real configurado nos Secrets.
    if (str(banco.get("url", "")).startswith("postgres") and not sdb.usando_postgres()
            and not os.environ.get("LAUDO_TESTE")):
        sdb.configurar_postgres(banco["url"])
    return preparar_banco(sdb.descricao_banco(), sdb.SCHEMA_VERSION)
escolher_banco()
ler_token_do_navegador()

if "user" not in st.session_state:
    st.session_state.user = restore_login_session()
    st.session_state._restored_state = False
elif (st.session_state.user is not None
      and time.time() - st.session_state.get("_sessao_ok_t", 0) >= REVALIDAR_SESSAO_S):
    # Revalida a sessão no banco (no máximo a cada REVALIDAR_SESSAO_S): usuário desativado,
    # empresa bloqueada, acesso redefinido ou sessão expirada perdem o acesso em segundos,
    # sem pagar 5-6 consultas ao banco em cada clique.
    _ator = restore_login_session()
    if _ator is None:
        st.session_state.user = None
        st.session_state.page = None
        st.session_state["_sessao_encerrada"] = True
    else:
        st.session_state.user = _ator
if "page" not in st.session_state: st.session_state.page = None
if "step" not in st.session_state: st.session_state.step = 0
if "inspection" not in st.session_state: st.session_state.inspection = normalize_tires(new_inspection())
if "damage_view" not in st.session_state: st.session_state.damage_view = None
if "mark_mode" not in st.session_state: st.session_state.mark_mode = "x"
if st.session_state.user and not st.session_state.get("_restored_state", False):
    load_current_state(st.session_state.user)
    st.session_state._restored_state = True


# ---------------------------------------------------------------------------
# Ajustes de navegador (celular)
# 1) TROCA DE ETAPA -> TOPO. O Streamlit rola um container interno (o elemento muda de
#    versão para versão: .main, .stMain, stMain...), por isso seletores fixos falham.
#    Aqui o navegador descobre sozinho: zera QUALQUER elemento que esteja rolado, chama
#    scrollIntoView no topo do conteúdo e também rola os quadros pai. Dois gatilhos
#    independentes: (a) um marcador no DOM (.ac-view-marker) que muda a cada etapa e é
#    observado continuamente; (b) um iframe disparado pelo servidor na troca de etapa.
# 2) SELECTS: no celular, o campo de busca do selectbox abria o teclado virtual, que cobria a
#    lista de opções. Em telas de toque o campo vira somente leitura (inputmode=none).
# (O antigo item 3, que reduzia o canvas com transform: scale, saiu: a área de desenho
#  agora é o componente desenho/, que mede a tela e se redimensiona sozinho, sem escala dupla.)
# O código é injetado no documento pai (não fica preso ao iframe, que o Streamlit recria).
# ---------------------------------------------------------------------------
CLIENT_JS = r"""
(function () {
  var doc = document, win = window;
  function isTouch() { try { return win.matchMedia('(pointer: coarse)').matches; } catch (e) { return false; } }

  /* ---------- 1) topo ---------- */
  function resetEl(el) { try { if (el.scrollTop) el.scrollTop = 0; if (el.scrollLeft) el.scrollLeft = 0; } catch (e) {} }
  function scrollTopNow() {
    ['[data-testid="stMain"]', 'section.main', '.stMain', '[data-testid="stAppViewContainer"]', '.stApp', 'html', 'body']
      .forEach(function (s) {
        doc.querySelectorAll(s).forEach(function (el) {
          try { el.scrollTo({ top: 0, left: 0, behavior: 'instant' }); } catch (e) { resetEl(el); }
        });
      });
    doc.querySelectorAll('*').forEach(function (el) { if (el.scrollTop > 0) resetEl(el); });
    var top = doc.querySelector('[data-testid="stMainBlockContainer"]') || doc.querySelector('.block-container');
    if (top && top.scrollIntoView) {
      try { top.scrollIntoView({ block: 'start', inline: 'nearest', behavior: 'instant' }); } catch (e) { top.scrollIntoView(true); }
    }
    var w = win;
    for (var i = 0; i < 4; i++) {
      try { w.scrollTo(0, 0); } catch (e) {}
      if (!w.parent || w.parent === w) break;
      w = w.parent;
    }
  }
  win.__acScrollTop = function () {
    scrollTopNow();
    [80, 250, 600].forEach(function (t) { win.setTimeout(scrollTopNow, t); });
  };
  var lastView = null;
  function checkView() {
    var m = doc.querySelector('.ac-view-marker');
    if (!m) return;
    var v = m.className;
    if (lastView === null) { lastView = v; return; }
    if (v !== lastView) { lastView = v; win.__acScrollTop(); }
  }

  /* ---------- 2) selects sem teclado ---------- */
  function noKeyboard(i) {
    if (!isTouch() || i.getAttribute('data-ac-ro')) return;
    i.setAttribute('inputmode', 'none'); i.setAttribute('readonly', 'readonly'); i.setAttribute('data-ac-ro', '1');
  }
  function scan() { doc.querySelectorAll('[data-baseweb="select"] input').forEach(noKeyboard); }

  var pending = false;
  new MutationObserver(function () {
    if (pending) return; pending = true;
    win.requestAnimationFrame(function () { pending = false; scan(); checkView(); });
  }).observe(doc.body, { childList: true, subtree: true });
  doc.addEventListener('focusin', function (e) {
    var t = e.target;
    if (t && t.matches && t.matches('[data-baseweb="select"] input')) noKeyboard(t);
  }, true);
  win.setInterval(function () { scan(); checkView(); }, 350);
  scan(); checkView();
})();
"""

def view_signature():
    """Identifica a tela atual (etapa, histórico, admin...). Muda a cada troca de etapa."""
    ss = st.session_state
    insp = ss.get("inspection") or {}
    return (ss.get("user") is not None, ss.get("step", 0), ss.get("page"), ss.get("vistoria_aberta"),
            ss.get("empresa_aberta"), insp.get("numero"))

def view_marker_html():
    """Marcador invisível no topo da página; sua classe muda a cada etapa (o JS observa)."""
    sig = re.sub(r"[^a-z0-9]+", "-", "-".join(str(x).lower() for x in view_signature())).strip("-")
    return f'<div class="ac-view-marker v-{sig}"></div>'

def client_helpers(scroll_top=False):
    """Instala os ajustes de navegador (uma vez) e, se pedido, força a volta ao topo."""
    boot = json.dumps(CLIENT_JS)
    call = "if (window.parent.__acScrollTop) { window.parent.__acScrollTop(); }" if scroll_top else ""
    components.html(
        "<script>(function(){ try { var d = window.parent.document;"
        " if (!d.getElementById('ac-helpers')) { var s = d.createElement('script'); s.id = 'ac-helpers';"
        " s.text = " + boot + "; d.head.appendChild(s); } " + call + " } catch (e) {} })();"
        " // " + (str(time.time_ns()) if scroll_top else "static") + "</script>",
        height=0,
    )

def login_screen():
    with st.container(key="ac_login"):
        st.markdown(brand_html(), unsafe_allow_html=True)
        st.markdown(card_html("Acessar o sistema", "Entre com seu usuário cadastrado."), unsafe_allow_html=True)
        if st.session_state.pop("_sessao_encerrada", False):
            st.info("Sua sessão foi encerrada. Entre novamente.")
        with st.form("login"):
            u=st.text_input("Usuário ou e-mail")
            p=st.text_input("Senha",type="password")
            if st.form_submit_button("Entrar",type="primary",use_container_width=True):
                found, erro = S.autenticar(u, p, client_ip())
                if found:
                    st.session_state.user=found
                    st.session_state.step=0
                    st.session_state.page=None
                    st.session_state.inspection=normalize_tires(new_inspection())
                    create_login_session(found)
                    save_current_state(); st.rerun()
                else:
                    st.error(erro)
        st.caption("O cadastro de novos logins é controlado pelo administrador. Peça a ele para criar ou liberar seu acesso.")

def audit(acao, descricao):
    """Registra na auditoria uma ação feita dentro da vistoria atual (nunca interrompe a tela)."""
    user = st.session_state.get("user")
    if user is None:
        return
    S.registrar(user, acao, f"{user.nome} {descricao}", vistoria_id=(st.session_state.get("inspection") or {}).get("_vistoria_id"))

def ir_para(page=None, step=None):
    """Troca de tela pelo menu lateral (limpa seleções das telas administrativas)."""
    for k in ("vistoria_aberta", "veiculo_aberto", "cliente_aberto", "empresa_aberta", "plano_sel"):
        st.session_state.pop(k, None)
    st.session_state.page = page
    if step is not None:
        st.session_state.step = step
    save_current_state(); st.rerun()

def iniciar_nova_vistoria():
    """"Nova vistoria": o mesmo formulário de sempre, já registrado no banco com o número da empresa,
    o vistoriador e a data/hora de início. Respeita o limite do plano."""
    ator = st.session_state.user
    insp = normalize_tires(new_inspection())
    try:
        vid, numero = S.iniciar_vistoria(ator, insp)
    except (rbac.AcessoNegado, S.ErroNegocio) as exc:
        st.error(str(exc))
        return
    insp["numero"], insp["_vistoria_id"] = numero, vid
    st.session_state.inspection = insp
    st.session_state.damage_view = None
    st.session_state.page = None
    st.session_state.step = 1
    save_current_state(); st.rerun()

def abrir_vistoria_existente(vid):
    """Continuar uma vistoria pendente ou abrir uma concluída para edição (conforme permissão)."""
    try:
        dados = S.retomar_vistoria(st.session_state.user, vid)
    except (rbac.AcessoNegado, S.ErroNegocio) as exc:
        st.error(str(exc))
        return
    st.session_state.inspection = normalize_tires(dados)
    st.session_state.damage_view = (dados.get("avarias") or {}).get("diagrama") or None
    st.session_state.page = None
    st.session_state.step = 1
    for k in ("vistoria_aberta", "veiculo_aberto", "cliente_aberto"):
        st.session_state.pop(k, None)
    save_current_state(); st.rerun()

def registrar_vistoria_atual():
    """Etapa aberta direto pelo menu, sem "Nova vistoria": registra a vistoria agora.
    Devolve False (e mostra o motivo) se o plano/perfil não permitir."""
    insp = st.session_state.inspection
    if insp.get("_vistoria_id"):
        return True
    ator = st.session_state.user
    ok, msg = S.pode_iniciar_vistoria(ator)
    if ok:
        try:
            vid, numero = S.iniciar_vistoria(ator, insp)
            insp["numero"], insp["_vistoria_id"] = numero, vid
            save_current_state()
            return True
        except (rbac.AcessoNegado, S.ErroNegocio) as exc:
            msg = str(exc)
    st.markdown(hero_html("Vistoria indisponível", msg), unsafe_allow_html=True)
    if st.button("Voltar ao início", type="primary", use_container_width=True):
        ir_para(None, 0)
    return False

def contexto_paineis():
    return paineis.Contexto(
        ator=st.session_state.user,
        gerar_pdf=lambda dados: pdf_bytes(dados),
        abrir_vistoria=abrir_vistoria_existente,
        nova_vistoria=iniciar_nova_vistoria,
        tipos_veiculo={k: lbl for k, _ic, lbl in VEHICLE_DIAGRAMS},
        miniatura=thumb_bytes,
        rotulos_fotos=dict(PHOTO_SLOTS) | dict(KEY_DOC_ITEMS),
        assinatura_cfg=get_assinatura_config(),
    )

def sidebar():
    user = st.session_state.user
    st.sidebar.markdown(sidebar_brand_html(user.nome, rbac.nome_perfil(user.perfil)), unsafe_allow_html=True)
    em_vistoria = st.session_state.page is None and st.session_state.step >= 1
    for page, label in paineis.MENU.get(user.perfil, []):
        ativo = st.session_state.page == page or (page == "sa_empresas" and st.session_state.page == "sa_empresa")
        if st.sidebar.button(label, key="sb_" + page, use_container_width=True, type="primary" if ativo else "secondary"):
            ir_para(page, 0)
    if rbac.pode(user, "vistorias.criar"):
        insp = st.session_state.inspection
        if insp.get("_vistoria_id"):
            # Etapas da vistoria aberta (mesmo fluxo de antes)
            st.sidebar.markdown(f'<div class="ac-side-sep"></div><div class="ac-side-label">Vistoria {insp.get("numero","")}</div>',
                                unsafe_allow_html=True)
            for i,(sid,num,label) in enumerate(STEPS,1):
                if st.sidebar.button(f"{num}  {label}",key="nav_"+sid,use_container_width=True,type="primary" if em_vistoria and st.session_state.step==i else "secondary"):
                    ir_para(None, i)
        if st.sidebar.button("＋ Nova vistoria",key="sb_new",use_container_width=True):
            iniciar_nova_vistoria()
    st.sidebar.markdown('<div class="ac-side-sep"></div>', unsafe_allow_html=True)
    if st.sidebar.button("Minha conta",key="sb_conta",use_container_width=True,type="primary" if st.session_state.page=="conta" else "secondary"):
        ir_para("conta", 0)
    if st.sidebar.button("Sair",key="sb_logout",use_container_width=True):
        clear_login_session()
        st.session_state.user=None; st.session_state.page=None; st.session_state.step=0
        st.session_state.inspection=normalize_tires(new_inspection())
        st.rerun()

def topbar(title,subtitle):
    c=st.session_state.inspection
    progress=st.session_state.step/len(STEPS)
    st.markdown(hero_html(title, subtitle, [f'Vistoria {c["numero"]}', st.session_state.user.nome, f'{st.session_state.step} de {len(STEPS)}'], progress), unsafe_allow_html=True)

def rerun_etapa():
    """Dentro de um @st.fragment: redesenha só a etapa atual (sem rodar o app inteiro, que é
    o que deixava os botões lentos). Fora de um fragmento, cai no rerun completo."""
    try:
        st.rerun(scope="fragment")
    except StreamlitAPIException:
        st.rerun()

def nav(fragment=False):
    with st.container(key="ac_nav"):
        x,y=st.columns([1,1])
        with x:
            if st.button("← Voltar",use_container_width=True,disabled=st.session_state.step<=1):
                st.session_state.step-=1
                save_current_state()
                st.rerun(scope="app") if fragment else st.rerun()
        with y:
            if st.session_state.step<len(STEPS) and st.button("Próxima etapa →",type="primary",use_container_width=True):
                st.session_state.step+=1
                save_current_state()
                st.rerun(scope="app") if fragment else st.rerun()

def get_placa_config():
    """Configuração da consulta de placa (só no servidor). None = desligada."""
    secret, err = {}, ""
    try:
        # Sem arquivo de Secrets, st.secrets[...] mostrava na tela o aviso "No secrets found".
        # A consulta funciona com a configuração padrão, então só checamos antes.
        if not st.secrets.load_if_toml_exists():
            return placa_api.load_config({}, secrets_error="Arquivo de Secrets não encontrado")
        secret = dict(st.secrets["placa_api"])
    except KeyError:
        err = "Seção [placa_api] não encontrada nos Secrets"
        try:
            if "VEHICLE_LOOKUP_URL" in st.secrets:
                err += " (há chaves do formato antigo VEHICLE_LOOKUP_*, que não são mais usadas)"
        except Exception:
            pass
    except FileNotFoundError:
        err = "Arquivo de Secrets não encontrado"
    except Exception as exc:
        # Só o tipo do erro: a mensagem do parser pode conter trechos do arquivo (cookie).
        err = f"Falha ao ler os Secrets ({type(exc).__name__}) — provável erro de sintaxe TOML"
    return placa_api.load_config(secret, secrets_error=err)

def get_assinatura_config():
    """Assinatura à distância: seção [assinatura] dos Secrets ou variáveis ASSINATURA_*.
    Sem nada configurado: provedor "desativado" (o app funciona normalmente)."""
    secret, err = {}, ""
    try:
        if st.secrets.load_if_toml_exists() and "assinatura" in st.secrets:
            secret = dict(st.secrets["assinatura"])
    except Exception as exc:
        # Só o tipo do erro: a mensagem do parser pode conter trechos do arquivo.
        err = f"Falha ao ler os Secrets ({type(exc).__name__})"
    return assinatura.carregar_config(secret, os.environ, secrets_error=err)

def plate_lookup_ui(v, placa_antes):
    """Consulta OPCIONAL pela placa. Só preenche campos vazios (ou preenchidos antes
    por uma consulta) e nunca bloqueia o formulário: se falhar, o usuário digita."""
    plate = placa_api.normalize_plate(v.get("placa"))
    changed = plate != placa_api.normalize_plate(placa_antes)
    status = v.get("_placa_status") or {}
    same = status.get("placa") == plate
    retry = False
    if same and status.get("status") in ("unavailable", "not_configured"):
        st.warning(status.get("msg") or "Consulta de placa indisponível. Preencha os dados manualmente.")
        retry = st.button("🔄 Tentar consultar novamente", key="placa_retry", use_container_width=True)
    elif same and status.get("msg"):
        st.caption(status["msg"])
    if not (changed or retry) or not placa_api.is_valid_plate(plate):
        return

    with st.spinner("Consultando placa..."):
        res = placa_api.lookup_plate(plate, get_placa_config())

    msg = ""
    if res.status == "not_configured":
        msg = "Consulta automática de placa não configurada. " + (res.message or "") + " Preencha os dados manualmente."
    elif res.status == "unavailable":
        msg = (res.message or "Consulta indisponível.") + " Preencha manualmente ou tente novamente."
    elif res.status in ("ok", "not_found"):
        auto = v.setdefault("_placa_auto", {})
        found = res.data if res.status == "ok" else {}
        for f in placa_api.FIELDS:
            cur = str(v.get(f, "") or "").strip()
            if not cur or cur.upper() == str(auto.get(f, "")).upper():   # vazio, ou preenchido antes pela consulta
                new = found.get(f) or placa_api.NOT_FOUND_TEXT
                v[f] = new
                auto[f] = new

        # O formulário mantém combustível em uma seção própria.
        # IMPORTANTE: preservamos EXATAMENTE o texto retornado pelo Placa FIPE.
        # Ex.: "GASOLINA/ALCOOL/ELETRICO" não vira "Híbrido" nem "Gasolina".
        fuel_data = st.session_state.inspection.setdefault("combustivel", {})
        fuel_from_plate = str(found.get("combustivel", "") or "").strip()

        if fuel_from_plate:
            fuel_data["_placa_auto"] = fuel_from_plate
            # Guarda o valor bruto retornado pelo site. A etapa 03 vai
            # acrescentá-lo temporariamente às opções do selectbox quando
            # ele não existir em FUEL_TYPES.
            fuel_data["tipo"] = fuel_from_plate
        else:
            # Se a consulta não trouxe combustível, não assumimos Gasolina.
            # O usuário poderá escolher manualmente na etapa 03.
            fuel_data["_placa_auto"] = ""
            fuel_data["tipo"] = ""

        v["_placa_ver"] = v.get("_placa_ver", 0) + 1
        msg = ("Dados preenchidos pela placa. Confira antes de continuar." if res.status == "ok"
               else "Placa não encontrada. Preencha os dados manualmente.")
    v["_placa_status"] = {"placa": plate, "status": res.status, "msg": msg}
    save_current_state()
    try:
        st.rerun(scope="fragment")   # redesenha marca/modelo e mostra a mensagem de status
    except StreamlitAPIException:
        st.rerun()

@st.fragment
def vehicle():
    c=st.session_state.inspection; v=c["veiculo"]; topbar("02 • Informações do veículo","Tipo, identificação e dados complementares.")
    st.subheader("Informações do Veículo")

    placa_antes = v.get("placa", "")
    a,b,d=st.columns(3)
    _ver = v.get("_placa_ver", 0)   # muda quando a consulta da placa preenche: força a lista a exibir o novo valor
    v["placa"] = a.text_input("Placa / Renavam", v["placa"]).upper()
    with b: v["marca"]=pick_or_type("Marca",VEHICLE_BRANDS,v["marca"],f"veic_marca_{_ver}",allow_blank=True)
    v["modelo"] = d.text_input("Modelo", v["modelo"])
    plate_lookup_ui(v, placa_antes)
    a,b,d=st.columns(3)
    with a: v["ano"]=pick_or_type("Ano",VEHICLE_YEARS,v["ano"],f"veic_ano_{_ver}",allow_blank=True)
    v["cor"] = b.text_input("Cor", v["cor"])
    v["km"] = d.text_input("Km", v["km"])
    # "Tipo de veículo" não aparece mais aqui (a pedido). O valor de v["tipo"]
    # continua existindo e sendo preenchido pela consulta de placa quando
    # disponível — ele só não tem mais um campo editável nesta etapa. Outras
    # partes do sistema (ex.: diagrama de avarias) continuam usando v["tipo"]
    # normalmente.
    nav(fragment=True)

@st.fragment
def fuel():
    c=st.session_state.inspection; f=c["combustivel"]; topbar("03 • Combustível","Tipo, nível e percentual exato.")

    # A consulta da placa incrementa _placa_ver. Usamos essa versão na chave
    # do widget para criar uma nova chave quando um combustível automático
    # acabou de chegar. Antes de criar o selectbox, colocamos o valor exato
    # da opção na própria chave do widget. Assim o Streamlit não reaproveita
    # uma seleção antiga e o combustível vindo da placa aparece selecionado.
    _fuel_ver = c["veiculo"].get("_placa_ver", 0)
    _fuel_key = f"combustivel_tipo_{_fuel_ver}"

    current_fuel = str(f.get("tipo", "") or "").strip()

    # Mantém as opções padrão e, quando a consulta da placa retorna um texto
    # específico que não existe na lista, adiciona esse texto somente nesta
    # consulta. Ex.: GASOLINA/ALCOOL/ELETRICO.
    fuel_options = list(FUEL_TYPES)
    if current_fuel and not any(
        option.strip().casefold() == current_fuel.casefold()
        for option in fuel_options
    ):
        fuel_options.append(current_fuel)

    fuel_match = next(
        (fuel_name for fuel_name in fuel_options
         if fuel_name.strip().casefold() == current_fuel.casefold()),
        None,
    )

    # A chave muda quando uma nova consulta de placa termina, evitando que
    # o Streamlit reaproveite a seleção anterior do selectbox.
    if _fuel_key not in st.session_state:
        st.session_state[_fuel_key] = fuel_match if fuel_match else None

    selected_fuel = st.selectbox(
        "Tipo de combustível",
        fuel_options,
        # O valor já vem de st.session_state[_fuel_key] (definido logo acima). Passar também
        # um índice gerava o aviso "widget criado com valor padrão e também via Session State".
        index=0,
        key=_fuel_key,
        placeholder="Selecione o tipo de combustível",
    )
    if selected_fuel:
        f["tipo"] = selected_fuel

    st.subheader("Nível do tanque"); cols=st.columns(5)
    for col,(lab,pct) in zip(cols,FUEL_LEVELS):
        with col:
            if st.button(f"{lab}  \n{pct}%",key="fuel_"+lab,use_container_width=True,type="primary" if f["nivel"]==lab else "secondary"):
                f["nivel"]=lab; f["percentual"]=pct; save_current_state(leve=True); rerun_etapa()
    f["percentual"]=st.slider("Percentual exato",0,100,int(f["percentual"]),5);
    nav(fragment=True)

@st.fragment
def key_documents():
    c=st.session_state.inspection
    topbar("04 • Chave e Documentos","Tire uma única foto da chave, dos documentos e etc...")
    c.setdefault("chave_documentos", {"foto": None})
    kd=c["chave_documentos"]

    usar_cam=st.checkbox(
        "Usar câmera direta do navegador (avançado, requer permissão)",
        value=st.session_state.get("usar_cam_navegador", False),
        key="usar_cam_navegador"
    )

    with st.container(border=True, key="ac_card_kd"):
        st.markdown(section_html("Chave e documentos", "Chave principal, chave reserva, manual e documentos em uma única foto."), unsafe_allow_html=True)

        if kd.get("foto"):
            st.image(thumb_bytes(kd["foto"], 900), use_container_width=True)
            if st.button("Remover foto", key="remove_kd_unica", use_container_width=True):
                kd["foto"]=None
                save_current_state()
                rerun_etapa()
        else:
            src=None
            if usar_cam:
                src=st.camera_input("Tirar foto", key="kdcam_unica", label_visibility="collapsed")
            if src is None:
                src=st.file_uploader("Tirar", type=["jpg","jpeg","png","webp"], key="kd_unica", label_visibility="collapsed")
            if src is not None:
                try:
                    kd["foto"]=b64_image(src)
                    audit("foto_adicionada", "adicionou foto: chave e documentos")
                    save_current_state()
                    rerun_etapa()
                except Exception:
                    st.error("Não foi possível salvar a foto.")

    # Fotos do formato antigo (chave principal / reserva / manual / documento): continuam
    # visíveis, entram no PDF e podem ser removidas.
    for k, val in [(k, x) for k, x in kd.items() if k != "foto" and x]:
        with st.container(border=True, key="ac_card_kd_"+k):
            st.markdown(section_html(KEY_DOC_LABELS.get(k, k)), unsafe_allow_html=True)
            st.image(thumb_bytes(val, 900), use_container_width=True)
            if st.button("Remover foto", key="remove_kd_"+k, use_container_width=True):
                kd[k] = None
                save_current_state()
                rerun_etapa()

    # Observação opcional (vazia = nada aparece no PDF).
    c["chave_documentos_obs"] = st.text_area("Observação (opcional)", c.get("Alguma_obs", ""),
                                        key="kd_obs", height=90, placeholder="Ex.: CHAVE RESERVA FALTANDO, DOCUMENTO SEM CRLV...")

    nav(fragment=True)

@st.fragment
def accessories():
    c=st.session_state.inspection; topbar("05 • Acessórios","Botões rápidos para presença, ausência ou não aplicável.")
    for item in ACCESSORIES:
        c["acessorios"].setdefault(item, {"status":"","obs":""})
    
    for i,a in enumerate(ACCESSORIES):
        it=c["acessorios"][a]
        with st.container(border=True, key=f"ac_card_acc_{i}"):
            st.markdown(f'<div class="ac-item-title">{a}</div>', unsafe_allow_html=True)
            cc=st.columns([1,1,1,3])
            for col,opt,label in zip(cc[:3],["sim","nao","na"],["✓ Sim","✕ Não","N/A"]):
                with col:
                    if st.button(label,key=f"acc_{i}_{opt}",use_container_width=True,type="primary" if it["status"]==opt else "secondary"):
                        it["status"]=opt; save_current_state(leve=True); rerun_etapa()
            it["obs"]=cc[3].text_input("Observação",it["obs"],key=f"accobs_{i}",label_visibility="collapsed",placeholder="Observação opcional")
    nav(fragment=True)

@st.fragment
def tires():
    c=st.session_state.inspection
    topbar("06 • Pneus","Estado e marca dos pneus.")

    # Primeiro pneu: a escolha da marca dele vira a marca padrão.
    first_key = TIRES[0][1]
    first_it = c["pneus"][first_key]

    with st.container(border=True, key="ac_card_tire_"+first_key):
        st.markdown(section_html(TIRES[0][0], "Marca escolhida aqui é sugerida para os demais pneus."), unsafe_allow_html=True)
        oc=st.columns(3)
        for ocol,opt in zip(oc,["Bom","Regular","Ruim"]):
            with ocol:
                if st.button(
                    opt,
                    key=f"t_{first_key}_{opt}",
                    use_container_width=True,
                    type="primary" if first_it.get("estado") == opt else "secondary",
                ):
                    first_it["estado"] = opt
                    save_current_state(leve=True)
                    rerun_etapa()

        # A primeira marca da lista aparece pré-selecionada por padrão; a opção de
        # não informar marca fica visível por último, em vez de aparecer em branco.
        brand_choices = TIRE_BRANDS + ["Outro (digitar)", "Nenhuma / não informar"]
        current_brand = first_it.get("marca","")
        if current_brand in TIRE_BRANDS:
            brand_index = brand_choices.index(current_brand)
        elif current_brand:
            brand_index = brand_choices.index("Outro (digitar)")
        else:
            brand_index = 0

        selected = st.selectbox(
            "Marca",
            brand_choices,
            index=brand_index,
            key=f"tm_{first_key}_sel"
        )
        if selected == "Outro (digitar)":
            first_it["marca"] = st.text_input(
                "Marca (digite)",
                current_brand if current_brand not in TIRE_BRANDS else "",
                key=f"tm_{first_key}_free"
            )
        elif selected == "Nenhuma / não informar":
            first_it["marca"] = ""
        else:
            first_it["marca"] = selected

    first_brand = first_it.get("marca","")

    # Demais pneus: recebem a marca do primeiro automaticamente quando
    # ainda não possuem uma marca. Depois disso continuam independentes.
    for label,key in TIRES[1:]:
        it=c["pneus"][key]

        if first_brand and not it.get("marca"):
            it["marca"] = first_brand

        with st.container(border=True, key="ac_card_tire_"+key):
            st.markdown(section_html(label), unsafe_allow_html=True)
            oc=st.columns(3)
            for ocol,opt in zip(oc,["Bom","Regular","Ruim"]):
                with ocol:
                    if st.button(
                        opt,
                        key=f"t_{key}_{opt}",
                        use_container_width=True,
                        type="primary" if it.get("estado") == opt else "secondary",
                    ):
                        it["estado"] = opt
                        save_current_state(leve=True)
                        rerun_etapa()

            current_brand = it.get("marca","")
            if current_brand in TIRE_BRANDS:
                brand_index = brand_choices.index(current_brand)
            elif current_brand:
                brand_index = brand_choices.index("Outro (digitar)")
            else:
                brand_index = 0

            selected = st.selectbox(
                "Marca",
                brand_choices,
                index=brand_index,
                key=f"tm_{key}_sel"
            )
            if selected == "Outro (digitar)":
                it["marca"] = st.text_input(
                    "Marca (digite)",
                    current_brand if current_brand not in TIRE_BRANDS else "",
                    key=f"tm_{key}_free"
                )
            elif selected == "Nenhuma / não informar":
                it["marca"] = ""
            else:
                it["marca"] = selected

    c["pneus_observacao"] = st.text_area(
        "Observação",
        c.get("pneus_observacao", ""),
        key="pneus_observacao",
        height=100,
        placeholder="Digite uma observação sobre os pneus..."
    )

    # Navegação da etapa 06
    nav(fragment=True)


def _zerar_desenho_avarias(av):
    """Volta ao desenho-base limpo (também descarta um desenho antigo migrado)."""
    av["imagem"] = None
    av["imagem_ok"] = False
    av["marcacoes"] = []
    av["tracos"] = []
    av.pop("imagem_legada", None)
    av.pop("marcacoes_legadas", None)

def reset_damage_canvas(av, view):
    """Recomeça o desenho do veículo do zero (nova área com o desenho-base limpo)."""
    ver_key = f"canvas_version_{view}"
    _zerar_desenho_avarias(av)
    st.session_state[ver_key] = st.session_state.get(ver_key, 0) + 1
    save_current_state()
    st.rerun()

def damage():
    c=st.session_state.inspection; av=c["avarias"]
    topbar("07 • Avarias","Marque arranhões/avarias com vermelho e amassados com azul direto no desenho do veículo.")

    if not st.session_state.get("damage_view"):
        st.session_state.damage_view = av.get("diagrama") or c["veiculo"].get("tipo") or "sedan"
    if not av.get("diagrama"):
        av["diagrama"] = st.session_state.damage_view
    view = st.session_state.damage_view

    # Versão da área de desenho: só muda para recomeçar do zero (Limpar desenho / trocar de veículo).
    # Desfazer, refazer e limpar os traços ficam na própria área (iguais aos das assinaturas).
    ver_key = f"canvas_version_{view}"

    with st.expander(f"Tipo de veículo: {next((lbl for k,_i,lbl in VEHICLE_DIAGRAMS if k==view), view)}", expanded=False):
        # 3 colunas por linha: melhor encaixe em telas de celular do que 5 colunas.
        for row_start in range(0, len(VEHICLE_DIAGRAMS), 3):
            row_items = VEHICLE_DIAGRAMS[row_start:row_start+3]
            dc = st.columns(3)
            for col,(key,ic,label) in zip(dc, row_items):
                with col:
                    if st.button(label, key="dv_"+key, use_container_width=True,
                                 type="primary" if view==key else "secondary"):
                        st.session_state.damage_view = key
                        av["diagrama"] = key
                        _zerar_desenho_avarias(av)
                        st.session_state[f"canvas_version_{key}"] = st.session_state.get(f"canvas_version_{key}", 0) + 1
                        save_current_state()
                        st.rerun()

    if "mark_mode" not in st.session_state:
        st.session_state.mark_mode = "x"

    mc1,mc2 = st.columns(2)
    with mc1:
        if st.button("Arranhão", key="damage_mark_x", use_container_width=True,
                     type="primary" if st.session_state.mark_mode=="x" else "secondary"):
            st.session_state.mark_mode="x"
            st.rerun()
    with mc2:
        if st.button("Amassado", key="damage_mark_o", use_container_width=True,
                     type="primary" if st.session_state.mark_mode=="o" else "secondary"):
            st.session_state.mark_mode="o"
            st.rerun()

    if st.session_state.mark_mode=="x":
        stroke_color = desenho_render.COR_ARRANHAO
        st.caption("Arranhão.")
    else:
        stroke_color = desenho_render.COR_AMASSADO
        st.caption("Amassado.")

    # Vistorias salvas pela versão antiga (imagem 400x460 com os traços embutidos) continuam
    # abrindo: essa imagem vira a base e as ocorrências antigas são mantidas.
    desenho_render.migrar_avarias(av)
    if av.get("imagem_legada"):
        base_url, proporcao = legacy_screen_url(av["imagem_legada"])
        base_image = pil_b64(av["imagem_legada"])
    else:
        base_url, proporcao = diagram_screen_url(view)
        base_image = vehicle_diagram(view)
    if not av.get("imagem"):
        # Como antes, o laudo sempre traz o desenho do veículo (mesmo sem nenhuma marcação).
        av["imagem"] = b64_jpeg(desenho_render.render_avarias(base_image, av["tracos"]))
        av["imagem_ok"] = True

    canvas_version = st.session_state.get(ver_key, 0)
    novos = desenho.area_desenho(
        modo="avarias", versao=f"{view}_{canvas_version}", key=f"canvas_{view}_{canvas_version}",
        tracos=av["tracos"], imagem=base_url, proporcao=proporcao, cor=stroke_color, espessura=0.01,
    )
    if novos is not None and novos != av["tracos"]:
        av["tracos"] = novos
        # Imagem final gerada no servidor: desenho-base + traços (mesmo resultado em qualquer tela).
        av["imagem"] = b64_jpeg(desenho_render.render_avarias(base_image, novos))
        av["imagem_ok"] = True
        # Uma ocorrência por traço, pela cor (vermelho = Arranhão, azul = Amassado) + as antigas.
        av["marcacoes"] = list(av.get("marcacoes_legadas") or []) + desenho_render.marcacoes(novos)

        # Auditoria: registra uma vez por vistoria (não a cada traço).
        _aud_key = f"_audit_desenho_{c.get('_vistoria_id')}"
        if av.get("marcacoes") and not st.session_state.get(_aud_key):
            st.session_state[_aud_key] = True
            audit("avaria_registrada", "marcou avarias no desenho do veículo")

        # Não gravamos o rascunho em disco a cada traço (pesaria o rerun).
        # A navegação e o botão Limpar continuam salvando o estado.

    st.caption("Desfazer, Refazer e Limpar ficam logo abaixo do desenho. Gire o celular para ampliar a área.")
    if st.button("Limpar desenho", key=f"clear_{view}", use_container_width=True,
                 disabled=not (av.get("tracos") or av.get("imagem_legada"))):
        reset_damage_canvas(av, view)

    st.divider()
    damage_photos()
    nav()

@st.fragment
def damage_photos():
    """Fotos das avarias: quantas o usuário quiser. Cada foto = AVARIA 1, 2, 3...
    com miniatura, descrição opcional e botão de remover. É um fragmento: adicionar
    ou remover fotos não recarrega (nem mexe no) desenho do veículo acima."""
    c = st.session_state.inspection
    av = c["avarias"]
    fotos = av.setdefault("fotos", [])
    n = st.session_state.get("avaria_up_n", 0)   # muda a chave do uploader para limpá-lo após cada envio

    st.subheader("Fotos das avarias")
    st.caption("Adicione quantas fotos precisar. No celular, o botão abre a câmera ou a galeria. Todas entram no PDF.")
    if st.session_state.pop("avaria_up_erros", 0):
        st.error("Alguma foto não pôde ser salva. Tente enviar novamente.")

    novas, erros = [], 0
    files = st.file_uploader("Adicionar fotos de avarias", type=["jpg","jpeg","png","webp"],
                             accept_multiple_files=True, key=f"avaria_up_{n}", label_visibility="collapsed")
    for f in (files or []):
        try:
            novas.append({"id": secrets.token_hex(4), "foto": b64_image(f), "descricao": ""})
        except Exception:
            erros += 1

    if st.checkbox("Usar câmera direta do navegador (avançado, requer permissão)", key="usar_cam_avaria"):
        shot = st.camera_input("Tirar foto da avaria", key=f"avaria_cam_{n}", label_visibility="collapsed")
        if shot is not None:
            try:
                novas.append({"id": secrets.token_hex(4), "foto": b64_image(shot), "descricao": ""})
            except Exception:
                erros += 1

    if novas or erros:
        fotos.extend(novas)
        if novas:
            audit("avaria_registrada", f"adicionou {len(novas)} foto(s) de avaria")
        st.session_state["avaria_up_n"] = n + 1
        st.session_state["avaria_up_erros"] = erros
        save_current_state()
        rerun_etapa()

    if fotos:
        st.caption(f"{len(fotos)} foto(s) de avarias.")
    for i, item in enumerate(list(fotos)):
        with st.container(border=True, key=f"ac_card_av_{item['id']}"):
            st.markdown(section_html(f"Avaria {i+1}"), unsafe_allow_html=True)
            try:
                st.image(thumb_bytes(item["foto"]), width=240)
            except Exception:
                st.warning("Não foi possível exibir esta foto.")
            item["descricao"] = st.text_input("Descrição / local (opcional)", item.get("descricao", ""),
                                        key=f"avaria_desc_{item['id']}", placeholder="Ex.: PORTA DIANTEIRA ESQUERDA")
            if st.button("Remover foto", key=f"avaria_rm_{item['id']}", use_container_width=True):
                fotos.remove(item)
                save_current_state()
                rerun_etapa()

@st.fragment
def photos():
    c=st.session_state.inspection
    topbar("08 • Fotos","Envie várias fotos e elas serão mantidas no PDF.")

    st.caption("No celular, use a câmera ou a galeria. Cada posição abaixo guarda sua própria foto.")

    # Mantém as fotos já salvas mesmo depois dos reruns do Streamlit.
    c.setdefault("fotos", {})
    c.setdefault("fotos_acessorios", [])

    usar_camera_navegador = st.checkbox(
        "Usar câmera direta do navegador (avançado, requer permissão)",
        value=st.session_state.get("usar_cam_navegador", False),
        key="usar_cam_navegador"
    )

    with st.container(key="ac_photo_grid"):
        for row in range(0, len(PHOTO_SLOTS), 2):
            cols=st.columns(2)

            for col,(key,label) in zip(cols,PHOTO_SLOTS[row:row+2]):
                with col, st.container(border=True, key="ac_card_photo_"+key):

                    st.markdown(section_html(label), unsafe_allow_html=True)

                    if c["fotos"].get(key):
                        st.image(
                            thumb_bytes(c["fotos"][key], 900),
                            use_container_width=True
                        )
                        if st.button(
                            "Remover foto",
                            key="remove_photo_"+key,
                            use_container_width=True
                        ):
                            c["fotos"].pop(key, None)
                            save_current_state()
                            rerun_etapa()
                    else:
                        src=None
                        if usar_camera_navegador:
                            src=st.camera_input(
                                f"Câmera do navegador — {label}",
                                key="cam_"+key,
                                label_visibility="collapsed"
                            )
                        if src is None:
                            src=st.file_uploader(
                                f"Enviar foto — {label}",
                                type=["jpg","jpeg","png","webp"],
                                key="up_"+key,
                                label_visibility="collapsed"
                            )

                        if src is not None:
                            try:
                                c["fotos"][key]=b64_image(src)
                                audit("foto_adicionada", f"adicionou foto: {label}")
                                save_current_state()
                                rerun_etapa()
                            except Exception:
                                st.error(f"Não foi possível salvar a foto: {label}")

    st.markdown(section_html("Fotos de acessórios", "Selecione uma ou várias fotos de uma vez."), unsafe_allow_html=True)

    # A chave do uploader muda a cada envio: sem isso, o Streamlit continuava devolvendo os
    # arquivos já enviados e a foto removida voltava sozinha na tela seguinte.
    n_extra = st.session_state.get("extras_up_n", 0)
    ex=st.file_uploader(
        "Selecione uma ou várias fotos dos acessórios",
        type=["jpg","jpeg","png","webp"],
        accept_multiple_files=True,
        key=f"extras_up_{n_extra}",
        label_visibility="collapsed"
    )

    if ex:
        # Adiciona novas fotos sem apagar as que já foram enviadas anteriormente.
        erros = 0
        for x in ex:
            try:
                c["fotos_acessorios"].append(b64_image(x))
            except Exception:
                erros += 1
        st.session_state["extras_up_n"] = n_extra + 1
        if len(ex) > erros:
            audit("foto_adicionada", f"adicionou {len(ex) - erros} foto(s) de acessórios")
        save_current_state()
        if erros:
            st.session_state["extras_erros"] = erros
        rerun_etapa()
    if st.session_state.pop("extras_erros", 0):
        st.error("Alguma foto de acessório não pôde ser salva.")

    if c.get("fotos_acessorios"):
        st.caption(f"{len(c['fotos_acessorios'])} foto(s) de acessórios salva(s).")
        for i,b64 in enumerate(c["fotos_acessorios"]):
            st.image(thumb_bytes(b64), width=220)
            if st.button("Remover", key=f"remove_extra_{i}"):
                c["fotos_acessorios"].pop(i)
                save_current_state()
                rerun_etapa()

    

    nav(fragment=True)

def signature(title,key,dono):
    """Área de assinatura responsiva (largura real da tela; maior na horizontal).
    Grava dono["assinatura"] (PNG base64, fundo branco, recortado na assinatura — o formato
    usado pelo PDF) e dono["assinatura_tracos"] (para reabrir a etapa e continuar editando)."""
    st.write(f"**{title}**")
    tracos = dono.get("assinatura_tracos") or []
    novos = desenho.area_desenho(modo="assinatura", versao=key, key=key, tracos=tracos,
                                 cor=desenho_render.COR_ASSINATURA, espessura=0.012)
    if novos is not None and novos != tracos:
        dono["assinatura_tracos"] = novos
        img = desenho_render.render_assinatura(novos)
        dono["assinatura"] = b64_pil(img) if img is not None else None
    if dono.get("assinatura"):
        st.image(pil_b64(dono["assinatura"]),width=360)
    return dono.get("assinatura")

@st.fragment
def owner():
    p=st.session_state.inspection["proprietario"]; topbar("09 • Proprietário","Nome, CPF, telefone e assinatura.")
    
    _antes = p["assinatura"]
    a,b=st.columns(2); p["nome"]=a.text_input("Nome completo",p["nome"]); p["cpf"]=b.text_input("CPF do proprietário",p.get("cpf","")); p["telefone"]=st.text_input("Telefone de contato",p["telefone"]); p["assinatura"]=signature("Assinatura do proprietário / responsável","sig_owner",p)
    _aud_key = f"_audit_sig_owner_{st.session_state.inspection.get('_vistoria_id')}"
    if p["assinatura"] and p["assinatura"] != _antes and not st.session_state.get(_aud_key):
        st.session_state[_aud_key] = True
        audit("assinatura_registrada", "registrou a assinatura do proprietário")
    nav(fragment=True)

@st.fragment
def issuer():
    e=st.session_state.inspection["emitente"]; topbar("10 • Empresa / Emitente","Dados e assinatura que aparecerão no PDF.")

    saved_emitentes=S.listar_emitentes(st.session_state.user)   # emitentes salvos DA EMPRESA do usuário
    if saved_emitentes:
        opcoes=["Preencher manualmente"] + [
            f'{x.get("empresa","")} • {x.get("responsavel","")}'.strip(" •")
            for x in saved_emitentes
        ]
        escolha=st.selectbox("Dados salvos", opcoes, key="issuer_saved_choice")
        if escolha != "Preencher manualmente":
            salvo=saved_emitentes[opcoes.index(escolha)-1]
            for campo in ["empresa","documento","telefone","email","endereco","responsavel"]:
                e[campo]=salvo.get(campo,"")
            st.info("Dados salvos carregados. Você pode alterar antes de finalizar.")
    else:
        st.caption("Depois de preencher, os dados poderão ser salvos para as próximas vistorias.")

    a,b=st.columns(2); e["empresa"]=a.text_input("Empresa / Emitente",e["empresa"]); e["documento"]=b.text_input("CNPJ / Documento",e["documento"])
    a,b=st.columns(2); e["telefone"]=a.text_input("Telefone",e["telefone"]); e["email"]=b.text_input("E-mail",e["email"])
    _antes = e["assinatura"]
    e["endereco"]=st.text_input("Endereço",e["endereco"]); e["responsavel"]=st.text_input("Nome do responsável",e["responsavel"]); e["assinatura"]=signature("Assinatura da empresa / emitente","sig_issuer",e)
    _aud_key = f"_audit_sig_issuer_{st.session_state.inspection.get('_vistoria_id')}"
    if e["assinatura"] and e["assinatura"] != _antes and not st.session_state.get(_aud_key):
        st.session_state[_aud_key] = True
        audit("assinatura_registrada", "registrou a assinatura da empresa / emitente")

    if st.button("Salvar dados da empresa para próximas vistorias", use_container_width=True):
        dados={campo:e.get(campo,"") for campo in ["empresa","documento","telefone","email","endereco","responsavel"]}
        if not dados["empresa"]:
            st.warning("Informe o nome da Empresa / Emitente antes de salvar.")
        else:
            S.salvar_emitente(st.session_state.user, dados)
            st.success("Dados da empresa salvos para as próximas vistorias.")
    nav(fragment=True)

@st.fragment
def review():
    c=st.session_state.inspection; v=c["veiculo"]; f=c["combustivel"]; topbar("11 • Revisão e emissão do PDF","Confira os dados e finalize a inspeção.")
    a,b,d,e=st.columns(4); a.metric("Veículo",f'{v["marca"]} {v["modelo"]}'.strip() or "—"); b.metric("Placa",v["placa"] or "—"); d.metric("Combustível",f'{f["percentual"]}%'); e.metric("Avarias",len(c["avarias"].get("marcacoes", [])))

    summary = st.container(border=True, key="ac_card_summary")
    for name,val in [
        ("Veículo",f'{v.get("marca", "")} {v.get("modelo", "")} • {v.get("cor", "")} • {v.get("km", "")} km'),
        ("Combustível",f'{f["tipo"]} • {f["nivel"]} • {f["percentual"]}%'),
        ("Acessórios",f'{sum(1 for x in c["acessorios"].values() if x["status"])} itens avaliados'),
        ("Pneus",f'{sum(1 for x in c["pneus"].values() if x["estado"])} posições avaliadas'),
        ("Chave e Documentos",f'{len([x for x in (c.get("chave_documentos") or {}).values() if x])} foto(s)'),
        ("Fotos",f'{len([x for x in c["fotos"].values() if x])} padrão + {len(c["fotos_acessorios"])} acessórios'),
        ("Fotos de avarias",f'{len([x for x in c["avarias"].get("fotos", []) if x.get("foto")])} foto(s)'),
        ("Proprietário",f'{c["proprietario"]["nome"] or "Pendente"} • {c["proprietario"].get("cpf","") or "Sem CPF"} • {c["proprietario"]["telefone"] or "Sem telefone"}'),
        ("Emitente",f'{c["emitente"]["empresa"] or "Pendente"} • {c["emitente"]["responsavel"] or "Sem responsável"}')]:
        summary.write(f"**{name}:** {val}")

    if st.button("✓ Finalizar inspeção e preparar PDF",type="primary",use_container_width=True):
        _anterior = (c.get("inspetor"), c.get("status"), c.get("finalizado_em"))
        c["inspetor"]=st.session_state.user.nome; c["status"]="Concluído"; c["finalizado_em"]=now()
        pdf_avisos=[]
        _pdf=pdf_bytes(c, pdf_avisos)
        try:
            # Grava no banco da empresa: vistoria concluída, cliente, veículo, laudo (PDF),
            # consumo do plano e auditoria (antes era gravado em inspections.json).
            if not c.get("_vistoria_id") and not registrar_vistoria_atual():
                raise S.ErroNegocio("Não foi possível registrar a vistoria.")
            S.finalizar_vistoria(st.session_state.user, c["_vistoria_id"], c, _pdf)
        except (rbac.AcessoNegado, S.ErroNegocio) as exc:
            c["inspetor"], c["status"], c["finalizado_em"] = _anterior
            st.error(str(exc))
        else:
            st.session_state.pdf=_pdf
            st.session_state.pdf_numero=c["numero"]
            st.session_state.pdf_fp=inspection_fingerprint(c)
            st.session_state.pdf_avisos=pdf_avisos
            save_current_state()
            st.success("Inspeção finalizada e PDF arquivado no sistema.")
            # Laudo substituído: solicitações de assinatura ainda pendentes do laudo antigo são
            # canceladas no provedor (as que não puderem aparecem como "versão anterior").
            try:
                S.cancelar_assinaturas_substituidas(st.session_state.user, c["_vistoria_id"],
                                                    assinatura.criar_provedor(get_assinatura_config()))
            except (rbac.AcessoNegado, S.ErroNegocio):
                pass
    # O PDF só é oferecido se pertencer a ESTA vistoria e refletir os dados atuais
    # (antes, o botão podia entregar o PDF de outra vistoria ou de antes de novas fotos).
    pdf_atual = (st.session_state.get("pdf") and st.session_state.get("pdf_numero")==c["numero"])
    if pdf_atual and st.session_state.get("pdf_fp")!=inspection_fingerprint(c):
        st.info("A vistoria foi alterada depois de gerar o PDF. Clique em “Finalizar inspeção e preparar PDF” novamente para atualizar.")
        pdf_atual = False
    if pdf_atual and st.session_state.get("pdf_avisos"):
        st.warning("Algumas imagens não puderam ser incluídas no PDF: " + "; ".join(st.session_state.pdf_avisos))
    if pdf_atual:
        st.download_button("⬇ Baixar PDF",data=st.session_state.pdf,file_name=c["numero"]+".pdf",mime="application/pdf",type="primary",use_container_width=True)
        if rbac.pode(st.session_state.user, "laudos.enviar"):
            paineis.links_envio(c["numero"], f'{v.get("marca","")} {v.get("modelo","")}'.strip(), v.get("placa",""),
                                c["proprietario"].get("telefone",""))
    # Assinatura eletrônica à distância: envia o MESMO PDF já arquivado (só com o PDF atual).
    if c.get("_vistoria_id"):
        paineis.painel_assinaturas(contexto_paineis(), c["_vistoria_id"], c["numero"], c.get("proprietario") or {},
                                   permitir_envio=bool(pdf_atual), chave="rev")
    nav(fragment=True)

# ---------------------------------------------------------------------------
# Telas administrativas: agora em paineis.py, com dados do banco isolados por empresa.
# Substituem as antigas inicio() / history() / admin_users() / admin_clear_inspections(),
# que liam e gravavam users.json / inspections.json diretamente:
#   Início  -> Dashboard (admin) / Início (vistoriador)     Histórico / PDFs -> Vistorias e Laudos
#   Gerenciar logins -> Usuários                            Apagar vistorias -> Configurações
# ---------------------------------------------------------------------------
PAGINAS = {
    "dashboard": paineis.pg_dashboard,
    "vistorias": paineis.pg_vistorias,
    "veiculos": paineis.pg_veiculos,
    "clientes": paineis.pg_clientes,
    "laudos": paineis.pg_laudos,
    "usuarios": paineis.pg_usuarios,
    "relatorios": paineis.pg_relatorios,
    "logs": paineis.pg_logs,
    "configuracoes": paineis.pg_configuracoes,
    "inicio_vist": paineis.pg_inicio_vistoriador,
    "conta": paineis.pg_minha_conta,
    "sa_dashboard": paineis.pg_super_dashboard,
    "sa_empresas": paineis.pg_empresas,
    "sa_empresa": paineis.pg_gerenciar_empresa,
    "sa_planos": paineis.pg_planos,
    "sa_usuarios": paineis.pg_usuarios,
    "sa_logs": paineis.pg_logs,
    "sa_integracoes": paineis.pg_integracoes,
}

def paginas_permitidas(user):
    extra = {"conta"} | ({"sa_empresa"} if user.super else set())
    return {p for p, _ in paineis.MENU.get(user.perfil, [])} | extra

def pagina_administrativa(page):
    user = st.session_state.user
    if page not in paginas_permitidas(user):
        page = paineis.PAGINA_INICIAL.get(user.perfil, "conta")
        st.session_state.page = page
    if page == "sa_integracoes":
        st.session_state["_placa_cfg"] = get_placa_config()
    try:
        PAGINAS[page](contexto_paineis())
    except (rbac.AcessoNegado, S.ErroNegocio) as exc:
        st.error(str(exc))

st.markdown(view_marker_html(), unsafe_allow_html=True)
cookie_sync()

def _precisa_trocar_senha():
    """Consulta o banco só até a senha estar em dia (redefinir o acesso revoga a sessão)."""
    if st.session_state.get("_senha_ok"):
        return False
    precisa = S.precisa_trocar_senha(st.session_state.user)
    st.session_state._senha_ok = not precisa
    return precisa

if st.session_state.user is None:
    login_screen()
elif _precisa_trocar_senha():
    def _senha_trocada():
        st.session_state.page = None
        st.rerun()
    paineis.tela_troca_obrigatoria(st.session_state.user, _senha_trocada)
    if st.button("Sair", key="troca_sair"):
        clear_login_session(); st.session_state.user = None; st.rerun()
else:
    sidebar()
    _user = st.session_state.user
    _em_vistoria = (st.session_state.page is None and st.session_state.step >= 1
                    and rbac.pode(_user, "vistorias.criar"))
    if not _em_vistoria:
        pagina_administrativa(st.session_state.page or paineis.PAGINA_INICIAL.get(_user.perfil, "conta"))
    elif registrar_vistoria_atual():
        if st.session_state.step==1: vehicle()
        elif st.session_state.step==2: fuel()
        elif st.session_state.step==3: key_documents()
        elif st.session_state.step==4: accessories()
        elif st.session_state.step==5: tires()
        elif st.session_state.step==6: damage()
        elif st.session_state.step==7: photos()
        elif st.session_state.step==8: owner()
        elif st.session_state.step==9: issuer()
        elif st.session_state.step==10: review()

# Trocou de etapa/tela (Próxima, Voltar, menu lateral, Início, Histórico...)? Volta ao topo.
# Cliques dentro da mesma etapa (SIM/NÃO, fotos etc.) não mudam a assinatura: a tela não pula.
_view_sig = view_signature()
_prev_sig = st.session_state.get("_view_sig")
st.session_state["_view_sig"] = _view_sig
client_helpers(scroll_top=(_prev_sig is not None and _prev_sig != _view_sig))
