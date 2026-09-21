import streamlit as st
import streamlit.components.v1 as components
from pathlib import Path
from datetime import datetime
import json, hashlib, base64, io, re, secrets, time
from PIL import Image, ImageDraw, ImageOps
import numpy as np
from streamlit_drawable_canvas import st_canvas
from streamlit.errors import StreamlitAPIException
import pdf_report, placa_api

APP = "LAUDO DE VISTORIA"
DATA_DIR = Path("autocheck_data")
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
    "Chevrolet","Fiat","Ford","Volkswagen","Toyota","Honda","Hyundai","Renault","Nissan","Jeep",
    "Peugeot","Citroën","Mitsubishi","Kia","BMW","BYD","Geely","GWM","Mercedes-Benz","Audi","Volvo","Land Rover",
    "Suzuki","Yamaha","Kawasaki","Triumph","Harley-Davidson","Iveco","Scania","Volvo Trucks",
    "Mercedes-Benz Caminhões","Agrale","Troller","RAM","Chery/Caoa"
]
TIRE_BRANDS = [
    "Pirelli","Goodyear","Michelin","Continental","Bridgestone","Firestone","Dunlop",
    "General Tire","Yokohama","Maxxis","Hankook","Kumho","Cooper","Nexen"
]

_current_year = datetime.now().year
VEHICLE_YEARS = [str(y) for y in range(_current_year + 1, 1979, -1)]



def is_mobile_client():
    """True quando o navegador é de celular (usado para caber os canvas na tela)."""
    try:
        ua = st.context.headers.get("User-Agent", "") or ""
    except Exception:
        return False
    return bool(re.search(r"Android|iPhone|iPod|Mobile", ua, re.I))

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
def now(): return datetime.now().strftime("%d/%m/%Y %H:%M")

def seed_users():
    if not USERS_FILE.exists():
        save_json(USERS_FILE, [
            {"usuario":"admin","nome":"Administrador","senha":hash_pw("admin123"),"perfil":"Administrador","ativo":True},
            {"usuario":"inspetor","nome":"Inspetor Demo","senha":hash_pw("inspetor123"),"perfil":"Inspetor","ativo":True}
        ])
seed_users()

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
        "numero": next_inspection_number(),
        "criado_em": now(), "finalizado_em": None, "status":"Em andamento", "inspetor":"",
        "veiculo": {"marca":"","modelo":"","ano":"","placa":"","cor":"","chassi":"","km":"","observacoes":""},
        "combustivel":{"tipo":"Gasolina","nivel":"1/2","percentual":50},
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

def vehicle_diagram(tipo, size=(760,460)):
    """Carrega o desenho real enviado pelo usuário, preservando linhas pretas/cinzas.
    O fundo é sempre branco para evitar o retângulo preto no PDF/canvas.
    """
    filename = DIAGRAM_FILES.get(tipo, DIAGRAM_FILES["sedan"])
    path = DIAGRAM_DIR / filename
    if path.exists():
        src = Image.open(path).convert("RGBA")
        bg = Image.new("RGBA", src.size, "white")
        src = Image.alpha_composite(bg, src).convert("RGB")
    else:
        src = Image.new("RGB", size, "white")
        d = ImageDraw.Draw(src)
        d.text((20,20), "Desenho não encontrado", fill="#444444")
    src.thumbnail(size, Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", size, "white")
    x=(size[0]-src.width)//2; y=(size[1]-src.height)//2
    canvas.paste(src,(x,y))
    return canvas

def compose_canvas_image(canvas, background):
    """Reconstrói a imagem final usando o desenho original + somente as marcações coloridas.
    O st_canvas pode devolver a área inteira com fundo preto/opaque em image_data;
    por isso não usamos essa camada inteira como overlay.
    """
    arr = canvas.image_data.astype("uint8")
    h, w = arr.shape[:2]
    bg = background.convert("RGBA").resize((w, h), Image.Resampling.LANCZOS)

    # Extrai somente os traços coloridos: X vermelho e O azul.
    rgb = arr[:, :, :3].astype("int16")
    r, g, b = rgb[:, :, 0], rgb[:, :, 1], rgb[:, :, 2]
    red = (r > 120) & (r > g + 45) & (r > b + 45)
    blue = (b > 100) & (b > r + 35) & (b > g + 20)
    mark = red | blue

    oa = np.zeros((h, w, 4), dtype=np.uint8)
    oa[:, :, :3] = arr[:, :, :3]
    oa[:, :, 3] = np.where(mark, 255, 0).astype(np.uint8)
    overlay = Image.fromarray(oa)
    return Image.alpha_composite(bg, overlay).convert("RGB")

def canvas_b64(canvas, background=None):
    if canvas is None or canvas.image_data is None: return None
    if background is not None:
        return b64_pil(compose_canvas_image(canvas, background))
    return b64_pil(Image.fromarray(canvas.image_data.astype("uint8")).convert("RGB"))

def canvas_image_drawing(image):
    """Coloca a imagem dentro do próprio Fabric.js, em vez de usar background_image.
    Isso mantém desenho e imagem no mesmo sistema de coordenadas e evita o
    deslocamento que pode ocorrer no background_image do drawable-canvas.
    """
    image = image.convert("RGB")
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    src = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    return {
        "version": "4.4.0",
        "objects": [{
            "type": "image",
            "originX": "left",
            "originY": "top",
            "left": 0,
            "top": 0,
            "width": image.width,
            "height": image.height,
            "fill": "rgb(0,0,0)",
            "stroke": None,
            "strokeWidth": 1,
            "strokeDashArray": None,
            "strokeLineCap": "butt",
            "strokeLineJoin": "miter",
            "strokeMiterLimit": 10,
            "scaleX": 1,
            "scaleY": 1,
            "angle": 0,
            "flipX": False,
            "flipY": False,
            "opacity": 1,
            "shadow": None,
            "visible": True,
            "backgroundColor": "",
            "fillRule": "nonzero",
            "globalCompositeOperation": "source-over",
            "selectable": False,
            "evented": False,
            "hasControls": False,
            "hasBorders": False,
            "src": src,
            "filters": [],
            "crossOrigin": ""
        }]
    }

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

def create_login_session(user):
    token = secrets.token_urlsafe(32)
    sessions = load_json(SESSIONS_FILE, {})
    sessions[token] = {"usuario": user["usuario"], "criado_em": now()}
    save_json(SESSIONS_FILE, sessions)
    st.query_params["ac_token"] = token
    return token

def restore_login_session():
    token = st.query_params.get("ac_token")
    if not token:
        return None
    sessions = load_json(SESSIONS_FILE, {})
    session = sessions.get(token)
    if not session:
        return None
    return next((x for x in load_json(USERS_FILE, [])
                 if x["usuario"].lower() == session.get("usuario", "").lower() and x.get("ativo", True)), None)

def save_current_state():
    user = st.session_state.get("user")
    token = st.query_params.get("ac_token")
    if not user or not token:
        return
    inspection = st.session_state.get("inspection")
    if inspection is None:
        inspection = new_inspection()
    data = {
        "step": st.session_state.get("step", 0),
        "damage_view": st.session_state.get("damage_view", "lateral"),
        "admin_users": st.session_state.get("admin_users", False),
        "show_history": st.session_state.get("show_history", False),
        "inspection": inspection
    }
    save_json(draft_path(token), data)

def load_current_state(user):
    token = st.query_params.get("ac_token")
    if not token:
        return False
    data = load_json(draft_path(token), {})
    if not data:
        return False
    st.session_state.step = int(data.get("step", 0))
    st.session_state.damage_view = data.get("damage_view", "lateral")
    st.session_state.admin_users = bool(data.get("admin_users", False))
    st.session_state.show_history = bool(data.get("show_history", False))
    if data.get("inspection"):
        st.session_state.inspection = normalize_tires(data["inspection"])
    return True

def clear_login_session():
    token = st.query_params.get("ac_token")
    if token:
        sessions = load_json(SESSIONS_FILE, {})
        sessions.pop(token, None)
        save_json(SESSIONS_FILE, sessions)
        try:
            draft_path(token).unlink(missing_ok=True)
        except Exception:
            pass
    st.query_params.clear()

st.set_page_config(page_title="LAUDO DE VISTORIA",page_icon="🚗",layout="wide",initial_sidebar_state="collapsed")
from templates import page_styles, hero_html, card_html, card_open, card_close
st.markdown(page_styles(), unsafe_allow_html=True)

if "user" not in st.session_state:
    st.session_state.user = restore_login_session()
    st.session_state._restored_state = False
if "step" not in st.session_state: st.session_state.step = 0
if "inspection" not in st.session_state: st.session_state.inspection = normalize_tires(new_inspection())
if "damage_view" not in st.session_state: st.session_state.damage_view = None
if "mark_mode" not in st.session_state: st.session_state.mark_mode = "x"
if "admin_users" not in st.session_state: st.session_state.admin_users = False
if "show_history" not in st.session_state: st.session_state.show_history = False
if "clear_inspections" not in st.session_state: st.session_state.clear_inspections = False
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
# 3) CANVAS (avarias e assinaturas): se o container for mais estreito que o canvas, a exibição
#    é reduzida (transform: scale) para caber SEM cortar. A resolução interna não muda e o
#    navegador converte o toque para as coordenadas do canvas.
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

  /* ---------- 3) canvas cabe na tela ---------- */
  function fitCanvases() {
    doc.querySelectorAll('iframe').forEach(function (f) {
      var src = f.getAttribute('src') || '';
      if (src.indexOf('streamlit_drawable_canvas') < 0) return;
      var wrap = f.parentElement, cd = null;
      if (!wrap) return;
      try { cd = f.contentDocument; } catch (e) { return; }
      if (!cd) return;
      var cc = cd.querySelector('.canvas-container') || cd.querySelector('canvas');
      if (!cc || !cc.offsetWidth) return;
      var W = cc.offsetWidth;
      wrap.style.overflow = 'hidden'; wrap.style.minWidth = '0';
      var avail = wrap.clientWidth;
      var k = Math.min(1, avail / W);
      if (k > 0.999) {
        if (f.getAttribute('data-ac-fit')) {
          ['width', 'max-width', 'transform', 'transform-origin'].forEach(function (p) { f.style.removeProperty(p); });
          wrap.style.removeProperty('height');
          f.removeAttribute('data-ac-fit');
        }
        return;
      }
      var sig = W + '|' + k.toFixed(3) + '|' + f.offsetHeight;
      if (f.getAttribute('data-ac-fit') === sig) return;
      f.style.setProperty('width', W + 'px', 'important');
      f.style.setProperty('max-width', 'none', 'important');
      f.style.setProperty('transform-origin', '0 0');
      f.style.setProperty('transform', 'scale(' + k + ')');
      wrap.style.height = Math.ceil(f.offsetHeight * k) + 'px';
      f.setAttribute('data-ac-fit', sig);
    });
  }

  var pending = false;
  new MutationObserver(function () {
    if (pending) return; pending = true;
    win.requestAnimationFrame(function () { pending = false; scan(); checkView(); });
  }).observe(doc.body, { childList: true, subtree: true });
  doc.addEventListener('focusin', function (e) {
    var t = e.target;
    if (t && t.matches && t.matches('[data-baseweb="select"] input')) noKeyboard(t);
  }, true);
  win.setInterval(function () { scan(); checkView(); fitCanvases(); }, 350);
  scan(); checkView(); fitCanvases();
})();
"""

def view_signature():
    """Identifica a tela atual (etapa, histórico, admin...). Muda a cada troca de etapa."""
    ss = st.session_state
    insp = ss.get("inspection") or {}
    return (ss.get("user") is not None, ss.get("step", 0), ss.get("admin_users", False),
            ss.get("show_history", False), ss.get("clear_inspections", False), insp.get("numero"))

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
    st.markdown(hero_html("LAUDO DE VISTORIA", "Inspeção veicular digital", "Sistema profissional de inspeção"), unsafe_allow_html=True)
    a,b=st.columns([1,1.1],gap="large")
    with a:
        st.markdown(card_html("Login", "Entre com seu usuário cadastrado."), unsafe_allow_html=True)
        with st.form("login"):
            u=st.text_input("Usuário")
            p=st.text_input("Senha",type="password")
            if st.form_submit_button("Entrar",type="primary",use_container_width=True):
                found=next((x for x in load_json(USERS_FILE,[]) if x["usuario"].lower()==u.lower() and x.get("ativo",True)),None)
                if found and found["senha"]==hash_pw(p):
                    st.session_state.user=found
                    st.session_state.step=0
                    st.session_state.admin_users=False
                    st.session_state.show_history=False
                    st.session_state.inspection=new_inspection()
                    create_login_session(found)
                    save_current_state(); st.rerun()
                else:
                    st.error("Usuário ou senha inválidos.")
    with b:
        st.markdown(card_html("Acesso", "O cadastro de novos logins é controlado pelo administrador."), unsafe_allow_html=True)
        st.info("Peça ao administrador para criar ou liberar seu login.")

def sidebar():
    st.sidebar.markdown("## 🚗 LAUDO DE VISTORIA")
    st.sidebar.caption(f"{st.session_state.user['nome']} • {st.session_state.user['perfil']}")
    st.sidebar.divider()
    if st.sidebar.button("🏠 Inicio",use_container_width=True):
        st.session_state.step=0; st.session_state.admin_users=False; st.session_state.show_history=False; st.session_state.clear_inspections=False; save_current_state(); st.rerun()
    for i,(sid,num,label) in enumerate(STEPS,1):
        if st.sidebar.button(f"{num}  {label}",key="nav_"+sid,use_container_width=True,type="primary" if st.session_state.step==i else "secondary"):
            st.session_state.step=i; st.session_state.admin_users=False; st.session_state.show_history=False; st.session_state.clear_inspections=False; save_current_state(); st.rerun()
    st.sidebar.divider()
    if st.sidebar.button("🕘 Histórico / PDFs",use_container_width=True):
        st.session_state.step=0; st.session_state.admin_users=False; st.session_state.show_history=True; save_current_state(); st.rerun()
    if st.session_state.user.get("perfil")=="Administrador":
        if st.sidebar.button("👥 Gerenciar logins",use_container_width=True):
            st.session_state.admin_users=True; st.session_state.show_history=False; st.session_state.clear_inspections=False; save_current_state(); st.rerun()
        if st.sidebar.button("🗑️ Apagar vistorias",use_container_width=True):
            st.session_state.clear_inspections=True; st.session_state.admin_users=False; st.session_state.show_history=False; save_current_state(); st.rerun()
    if st.sidebar.button("＋ Nova inspeção",use_container_width=True):
        st.session_state.inspection=new_inspection(); st.session_state.step=1; st.session_state.admin_users=False; st.session_state.show_history=False; st.session_state.clear_inspections=False; save_current_state(); st.rerun()
    if st.sidebar.button("🚪 Sair",use_container_width=True):
        st.session_state.user=None; st.session_state.admin_users=False; save_current_state(); st.rerun()

def topbar(title,subtitle):
    c=st.session_state.inspection
    st.markdown(hero_html(title, subtitle, f'📄 {c["numero"]}  •  👤 {st.session_state.user["nome"]}'), unsafe_allow_html=True)

def nav(fragment=False):
    st.divider()
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
    st.progress((st.session_state.step+1)/(len(STEPS)+1))

def get_placa_config():
    """Configuração da consulta de placa (só no servidor). None = desligada."""
    secret, err = {}, ""
    try:
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
    with a: v["marca"]=pick_or_type("Marca",VEHICLE_BRANDS,v["marca"],f"veic_marca_{_ver}",allow_blank=True)
    v["modelo"] = b.text_input("Modelo", v["modelo"])
    v["placa"] = d.text_input("Placa / Renavam", v["placa"])
    plate_lookup_ui(v, placa_antes)
    a,b,d=st.columns(3)
    with a: v["ano"]=pick_or_type("Ano",VEHICLE_YEARS,v["ano"],f"veic_ano_{_ver}",allow_blank=True)
    v["cor"] = b.text_input("Cor", v["cor"])
    v["km"] = d.text_input("Km", v["km"])
    nav(fragment=True)

@st.fragment
def fuel():
    c=st.session_state.inspection; f=c["combustivel"]; topbar("03 • Combustível","Tipo, nível e percentual exato.")
    
    f["tipo"]=st.selectbox("Tipo de combustível",FUEL_TYPES,index=FUEL_TYPES.index(f["tipo"]))
    st.subheader("Nível do tanque"); cols=st.columns(5)
    for col,(lab,pct) in zip(cols,FUEL_LEVELS):
        with col:
            if st.button(f"⛽ {lab}\n{pct}%",key="fuel_"+lab,use_container_width=True,type="primary" if f["nivel"]==lab else "secondary"):
                f["nivel"]=lab; f["percentual"]=pct; save_current_state(); st.rerun()
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

    
    st.markdown('<div class="ac-photo-card-title">📷 Chave principal,Chave reserva,Manual e Documentos</div>', unsafe_allow_html=True)

    if kd.get("foto"):
        st.image(pil_b64(kd["foto"]), use_container_width=True)
        if st.button("🗑️ Remover foto", key="remove_kd_unica", use_container_width=True):
            kd["foto"]=None
            save_current_state()
            st.rerun()
    else:
        src=None
        if usar_cam:
            src=st.camera_input("Tirar foto", key="kdcam_unica", label_visibility="collapsed")
        if src is None:
            src=st.file_uploader("Tirar", type=["jpg","jpeg","png","webp"], key="kd_unica", label_visibility="collapsed")
        if src is not None:
            try:
                kd["foto"]=b64_image(src)
                save_current_state()
                st.rerun()
            except Exception:
                st.error("Não foi possível salvar a foto.")

    # Fotos do formato antigo (chave principal / reserva / manual / documento): continuam
    # visíveis, entram no PDF e podem ser removidas.
    for k, val in [(k, x) for k, x in kd.items() if k != "foto" and x]:
        st.markdown(f'<div class="ac-photo-card-title">{KEY_DOC_ICONS.get(k, "📷")} {KEY_DOC_LABELS.get(k, k)}</div>', unsafe_allow_html=True)
        st.image(pil_b64(val), use_container_width=True)
        if st.button("🗑️ Remover foto", key="remove_kd_"+k, use_container_width=True):
            kd[k] = None
            save_current_state()
            st.rerun()

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
        it=c["acessorios"][a]; st.markdown(f"**{a}**")
        cc=st.columns([1,1,1,3])
        for col,opt,label in zip(cc[:3],["sim","nao","na"],["✓ SIM","✕ NÃO","— N/A"]):
            with col:
                if st.button(label,key=f"acc_{i}_{opt}",use_container_width=True,type="primary" if it["status"]==opt else "secondary"):
                    it["status"]=opt; save_current_state(); st.rerun()
        it["obs"]=cc[3].text_input("Observação",it["obs"],key=f"accobs_{i}",label_visibility="collapsed",placeholder="Observação opcional")
        st.divider()
    nav(fragment=True)

@st.fragment
def tires():
    c=st.session_state.inspection
    topbar("06 • Pneus","Estado e marca dos pneus.")

    # Primeiro pneu: a escolha da marca dele vira a marca padrão.
    first_key = TIRES[0][1]
    first_it = c["pneus"][first_key]

    st.markdown(f"### 🛞 {TIRES[0][0]}")
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
                save_current_state()
                st.rerun()

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

        st.markdown(f"### 🛞 {label}")
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
                    save_current_state()
                    st.rerun()

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


def reset_damage_canvas(av, view):
    """Recomeça o desenho do veículo do zero (novo canvas com o desenho-base limpo)."""
    ver_key = f"canvas_version_{view}"
    av["imagem"] = None
    av["imagem_ok"] = False
    av["marcacoes"] = []
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

    # Versão do canvas: só muda para recomeçar o desenho do zero (Limpar / trocar de veículo).
    # Desfazer/refazer NÃO passa mais por aqui: é a barra nativa do canvas, igual à das assinaturas.
    ver_key = f"canvas_version_{view}"

    with st.expander("🚗🏍️🚐 Desenho dos veículos 🚗🏍️🚐", expanded=False):
        # 3 colunas por linha: melhor encaixe em telas de celular do que 5 colunas.
        for row_start in range(0, len(VEHICLE_DIAGRAMS), 3):
            row_items = VEHICLE_DIAGRAMS[row_start:row_start+3]
            dc = st.columns(3)
            for col,(key,ic,label) in zip(dc, row_items):
                with col:
                    if st.button(f"{ic} {label}", key="dv_"+key, use_container_width=True,
                                 type="primary" if view==key else "secondary"):
                        st.session_state.damage_view = key
                        av["diagrama"] = key
                        av["imagem"] = None
                        av["imagem_ok"] = False
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
        stroke_color, draw_mode = "#ef4444", "freedraw"
        st.caption("Arranhão.")
    else:
        stroke_color, draw_mode = "#2563eb", "freedraw"
        st.caption("Amassado.")

    # Tamanho original do desenho (400x460). Em celular estreito o navegador só reduz a
    # exibição para caber na tela (ver CLIENT_JS): a resolução interna do canvas não muda.
    canvas_width = 400
    canvas_height = 460

    # IMPORTANTE:
    # O drawable-canvas redimensiona background_image internamente. Em alguns
    # navegadores/Streamlit Cloud isso pode deixar a camada de desenho com
    # coordenadas diferentes da imagem de fundo. Para evitar o deslocamento,
    # a imagem faz parte do próprio Fabric.js como um objeto travado.
    #
    # A imagem-base é criada UMA vez por versão do canvas e depois só é lida.
    # (Antes ela era refeita a cada rerun a partir da imagem já com os traços; assim um
    # traço desfeito continuava "embutido" no fundo e reaparecia. Essa era uma das causas
    # de o desfazer não funcionar.)
    canvas_version = st.session_state.get(ver_key, 0)
    init_key = f"canvas_initial_{view}_{canvas_version}_{canvas_width}"
    base_key = init_key + "_base"
    if init_key not in st.session_state:
        base = vehicle_diagram(view, size=(canvas_width, canvas_height))
        if av.get("imagem") and av.get("imagem_ok"):
            try:   # reabrindo a etapa: parte do desenho já salvo
                base = pil_b64(av["imagem"]).resize((canvas_width, canvas_height), Image.Resampling.LANCZOS)
            except Exception:
                pass
        elif av.get("imagem"):
            av["imagem"] = None
        st.session_state[base_key] = base
        st.session_state[init_key] = canvas_image_drawing(base)
    base_image = st.session_state[base_key]
    initial_drawing = st.session_state[init_key]

    can = st_canvas(
        fill_color="rgba(0,0,0,0)",
        stroke_width=4,
        stroke_color=stroke_color,
        background_color="#ffffff",
        background_image=None,
        height=canvas_height,
        width=canvas_width,
        drawing_mode=draw_mode,
        initial_drawing=initial_drawing,
        key=f"canvas_{view}_{canvas_version}",
        display_toolbar=True,   # desfazer / refazer / lixeira nativos: o MESMO recurso das assinaturas
    )

    # A lixeira nativa faz canvas.clear(), o que também apagaria o desenho-base do veículo
    # (ele é um objeto dentro do canvas). Se isso acontecer, recomeçamos com um canvas limpo.
    if isinstance(can.json_data, dict):
        tem_base = any(o.get("type") == "image" for o in can.json_data.get("objects", []))
        seen_key = f"canvas_base_seen_{view}_{canvas_version}"
        if tem_base:
            st.session_state[seen_key] = True
        elif st.session_state.get(seen_key):
            reset_damage_canvas(av, view)

    if can.image_data is not None:
        new_image = canvas_b64(can, base_image)
        old_image = av.get("imagem")

        # Só registra um novo estado quando houve uma alteração real.
        if new_image and new_image != old_image:
            av["imagem"] = new_image
            av["imagem_ok"] = True

            # Sincroniza as ocorrências com os traços existentes no Fabric.js.
            # A imagem final sozinha não informa quantas marcações foram feitas,
            # por isso usamos os objetos do canvas para alimentar o resumo/PDF.
            try:
                drawing = can.json_data or {}
                objects = drawing.get("objects", []) if isinstance(drawing, dict) else []
                marcacoes = []
                for obj in objects:
                    if obj.get("type") != "path":
                        continue
                    stroke = str(obj.get("stroke", "")).lower()
                    tipo = "Arranhão" if stroke in ("#ef4444", "rgb(239, 68, 68)") else "Amassado"
                    marcacoes.append({
                        "tipo": tipo,
                        "severidade": "Marcada",
                        "descricao": "Avaria indicada no desenho do veículo.",
                    })
                # Se o canvas atual não devolver os objetos (por exemplo,
                # ao abrir uma inspeção já salva), conta as marcações pela
                # presença dos traços vermelho/azul na imagem final.
                if not marcacoes and av.get("imagem"):
                    try:
                        # int16 (não uint8): com uint8, "rr + 35" dá a volta e pixels BRANCOS passavam
                        # no filtro de azul, criando avarias fantasmas ("Avaria — Marcada — ...").
                        img = np.array(pil_b64(av["imagem"]).convert("RGB")).astype("int16")
                        rr, gg, bb = img[:, :, 0], img[:, :, 1], img[:, :, 2]
                        red = (rr > 140) & (rr > gg + 45) & (rr > bb + 45)
                        blue = (bb > 120) & (bb > rr + 35) & (bb > gg + 20)
                        mark = red | blue
                        total = 1 if int(mark.sum()) >= 8 else 0

                        av["marcacoes"] = [
                            {
                                "tipo": "Avaria",
                                "severidade": "Marcada",
                                "descricao": "Avaria indicada no desenho do veículo.",
                            }
                            for _ in range(total)
                        ]
                    except Exception:
                        pass

                av["marcacoes"] = marcacoes if marcacoes else av.get("marcacoes", [])
            except Exception:
                pass

            # Não gravamos o rascunho em disco a cada traço. Isso adicionava
            # uma operação pesada ao rerun e aumentava o atraso visual.
            # A navegação e o botão Limpar continuam salvando o estado.

    st.caption("Desfazer e refazer: use os ícones do próprio desenho (o mesmo recurso das assinaturas).")
    if st.button("🗑️ Limpar desenho", key=f"clear_{view}", use_container_width=True, disabled=not av.get("imagem")):
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

    st.subheader("📷 Fotos das avarias")
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
        st.session_state["avaria_up_n"] = n + 1
        st.session_state["avaria_up_erros"] = erros
        save_current_state()
        st.rerun(scope="fragment")

    if fotos:
        st.caption(f"{len(fotos)} foto(s) de avarias.")
    for i, item in enumerate(list(fotos)):
        with st.container(border=True):
            st.markdown(f"**AVARIA {i+1}**")
            try:
                st.image(thumb_bytes(item["foto"]), width=240)
            except Exception:
                st.warning("Não foi possível exibir esta foto.")
            item["descricao"] = st.text_input("Descrição / local (opcional)", item.get("descricao", ""),
                                        key=f"avaria_desc_{item['id']}", placeholder="Ex.: PORTA DIANTEIRA ESQUERDA")
            if st.button("🗑️ Remover foto", key=f"avaria_rm_{item['id']}", use_container_width=True):
                fotos.remove(item)
                save_current_state()
                st.rerun(scope="fragment")

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

    for row in range(0, len(PHOTO_SLOTS), 2):
        cols=st.columns(2)

        for col,(key,label) in zip(cols,PHOTO_SLOTS[row:row+2]):
            with col:
                
                st.markdown(f'<div class="ac-photo-card-title">📷 {label}</div>', unsafe_allow_html=True)

                if c["fotos"].get(key):
                    st.image(
                        pil_b64(c["fotos"][key]),
                        use_container_width=True
                    )
                    if st.button(
                        "🗑️ Remover foto",
                        key="remove_photo_"+key,
                        use_container_width=True
                    ):
                        c["fotos"].pop(key, None)
                        save_current_state()
                        st.rerun()
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
                            save_current_state()
                            st.rerun()
                        except Exception:
                            st.error(f"Não foi possível salvar a foto: {label}")

                

    
    st.markdown('<div class="ac-photo-card-title">📦 Fotos de acessórios</div>', unsafe_allow_html=True)

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
        save_current_state()
        if erros:
            st.session_state["extras_erros"] = erros
        st.rerun(scope="fragment")
    if st.session_state.pop("extras_erros", 0):
        st.error("Alguma foto de acessório não pôde ser salva.")

    if c.get("fotos_acessorios"):
        st.caption(f"{len(c['fotos_acessorios'])} foto(s) de acessórios salva(s).")
        for i,b64 in enumerate(c["fotos_acessorios"]):
            st.image(pil_b64(b64), width=220)
            if st.button("🗑️ Remover", key=f"remove_extra_{i}"):
                c["fotos_acessorios"].pop(i)
                save_current_state()
                st.rerun()

    

    nav(fragment=True)

def signature(title,key,stored):
    st.write(f"**{title}**")
    # Desktop: tamanho original (600x180). Celular: 400x220, área grande para o dedo; se a tela
    # for mais estreita, o navegador reduz só a exibição (ver CLIENT_JS), sem cortar.
    sig_w, sig_h = (400, 220) if is_mobile_client() else (600, 180)
    can=st_canvas(background_color="#ffffff",stroke_width=2.5,stroke_color="#111827",height=sig_h,width=sig_w,drawing_mode="freedraw",key=key)
    if can.image_data is not None:
        arr=can.image_data[:,:,:3]
        if (arr<245).any(): stored=canvas_b64(can)
    if stored: st.image(pil_b64(stored),width=360)
    return stored

@st.fragment
def owner():
    p=st.session_state.inspection["proprietario"]; topbar("09 • Proprietário","Nome, CPF, telefone e assinatura.")
    
    a,b=st.columns(2); p["nome"]=a.text_input("Nome completo",p["nome"]); p["cpf"]=b.text_input("CPF do proprietário",p.get("cpf","")); p["telefone"]=st.text_input("Telefone de contato",p["telefone"]); p["assinatura"]=signature("Assinatura do proprietário / responsável","sig_owner",p["assinatura"])
    nav(fragment=True)

@st.fragment
def issuer():
    e=st.session_state.inspection["emitente"]; topbar("10 • Empresa / Emitente","Dados e assinatura que aparecerão no PDF.")

    saved_emitentes=load_json(ISSUERS_FILE, [])
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
    e["endereco"]=st.text_input("Endereço",e["endereco"]); e["responsavel"]=st.text_input("Nome do responsável",e["responsavel"]); e["assinatura"]=signature("Assinatura da empresa / emitente","sig_issuer",e["assinatura"])

    if st.button("💾 Salvar dados da empresa para próximas vistorias", use_container_width=True):
        dados={campo:e.get(campo,"") for campo in ["empresa","documento","telefone","email","endereco","responsavel"]}
        if not dados["empresa"]:
            st.warning("Informe o nome da Empresa / Emitente antes de salvar.")
        else:
            emitentes=load_json(ISSUERS_FILE, [])
            existente=next((x for x in emitentes if x.get("empresa","").strip().lower()==dados["empresa"].strip().lower()), None)
            if existente: existente.update(dados)
            else: emitentes.append(dados)
            save_json(ISSUERS_FILE, emitentes)
            st.success("Dados da empresa salvos para as próximas vistorias.")
    nav(fragment=True)

@st.fragment
def review():
    c=st.session_state.inspection; v=c["veiculo"]; f=c["combustivel"]; topbar("11 • Revisão e emissão do PDF","Confira os dados e finalize a inspeção.")
    a,b,d,e=st.columns(4); a.metric("Veículo",f'{v["marca"]} {v["modelo"]}'.strip() or "—"); b.metric("Placa",v["placa"] or "—"); d.metric("Combustível",f'{f["percentual"]}%'); e.metric("Avarias",len(c["avarias"].get("marcacoes", [])))
    
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
        st.write(f"**{name}:** {val}")
    
    if st.button("✓ Finalizar inspeção e preparar PDF",type="primary",use_container_width=True):
        c["inspetor"]=st.session_state.user["nome"]; c["status"]="Concluído"; c["finalizado_em"]=now()
        items=load_json(INSPECTIONS_FILE,[]); items=[x for x in items if x["numero"]!=c["numero"]]; items.append(c); save_json(INSPECTIONS_FILE,items)
        pdf_avisos=[]
        st.session_state.pdf=pdf_bytes(c, pdf_avisos)
        st.session_state.pdf_numero=c["numero"]
        st.session_state.pdf_fp=inspection_fingerprint(c)
        st.session_state.pdf_avisos=pdf_avisos
        save_pdf_file(c["numero"], st.session_state.pdf)
        st.success("Inspeção finalizada e PDF arquivado no sistema.")
    # O PDF só é oferecido se pertencer a ESTA vistoria e refletir os dados atuais
    # (antes, o botão podia entregar o PDF de outra vistoria ou de antes de novas fotos).
    pdf_atual = (st.session_state.get("pdf") and st.session_state.get("pdf_numero")==c["numero"])
    if pdf_atual and st.session_state.get("pdf_fp")!=inspection_fingerprint(c):
        st.info("A vistoria foi alterada depois de gerar o PDF. Clique em “Finalizar inspeção e preparar PDF” novamente para atualizar.")
        pdf_atual = False
    if pdf_atual and st.session_state.get("pdf_avisos"):
        st.warning("Algumas imagens não puderam ser incluídas no PDF: " + "; ".join(st.session_state.pdf_avisos))
    if pdf_atual: st.download_button("⬇ Baixar PDF",data=st.session_state.pdf,file_name=c["numero"]+".pdf",mime="application/pdf",type="primary",use_container_width=True)
    nav(fragment=True)

def inicio():
    items=load_json(INSPECTIONS_FILE,[])
    st.markdown(hero_html("PAINEL DO LAUDO DE VISTORIA", "Inspeção veicular profissional.", f"Usuário: {st.session_state.user['nome']}"), unsafe_allow_html=True)
    a,b,d,e=st.columns(4)
    a.metric("Concluídas",len(items))
    b.metric("Hoje",sum(1 for x in items if x.get("finalizado_em","").startswith(datetime.now().strftime("%d/%m/%Y"))))
    d.metric("PDFs arquivados",sum(1 for x in items if pdf_path(x.get("numero","")).exists()))
    e.metric("Usuário",st.session_state.user["nome"])
    if st.button("＋ Começar nova inspeção",type="primary",use_container_width=True):
        st.session_state.inspection=new_inspection(); st.session_state.step=1; st.session_state.show_history=False; save_current_state(); st.rerun()
    st.markdown("### Histórico recente")
    if items:
        for x in reversed(items[-8:]):
            v=x["veiculo"]
            st.write(f"**{x['numero']}** • {v.get('marca','')} {v.get('modelo','')} • {v.get('placa','')} • {x.get('finalizado_em','')}")
    else:
        st.info("Nenhuma inspeção concluída ainda.")

def history():
    items=load_json(INSPECTIONS_FILE,[])
    st.markdown(hero_html("Histórico", "Inspeções concluídas e PDFs arquivados.", "Pesquise pelo número de série do PDF"), unsafe_allow_html=True)
    q=st.text_input("🔎 Pesquisar número do PDF", placeholder="Ex.: CHK-2026-000001").strip().upper()
    filtered=[x for x in reversed(items) if not q or q in x.get("numero","").upper()]
    if not filtered:
        st.info("Nenhum PDF encontrado para essa pesquisa.")
        return
    for x in filtered:
        v=x["veiculo"]
        with st.expander(f'{x["numero"]} • {v.get("marca","")} {v.get("modelo","")} • {v.get("placa","")}'):
            st.write(f'Inspetor: **{x.get("inspetor","")}** • Finalizado: **{x.get("finalizado_em","")}**')
            path=pdf_path(x["numero"])
            if st.button("🔄 Gerar PDF novamente com o layout atual", key="regen_"+x["numero"]):
                save_pdf_file(x["numero"], pdf_bytes(x))
                st.success("PDF atualizado com o layout atual.")
            if not path.exists():
                data=pdf_bytes(x)
                save_pdf_file(x["numero"], data)
            else:
                data=path.read_bytes()
            st.download_button("⬇ Baixar PDF arquivado",data=data,file_name=x["numero"]+".pdf",mime="application/pdf",key="hist_"+x["numero"])

def admin_clear_inspections():
    if st.session_state.user.get("perfil") != "Administrador":
        st.error("Acesso restrito ao administrador.")
        return

    st.subheader("🗑️ Apagar vistorias")
    st.caption("Apaga somente as vistorias concluídas e os PDFs arquivados. Usuários, emitentes e sessões não serão alterados.")

    items = load_json(INSPECTIONS_FILE, [])
    st.write(f"Vistorias cadastradas: **{len(items)}**")

    if not items:
        st.info("Não há vistorias concluídas para apagar.")
        return

    if "confirm_clear_inspections" not in st.session_state:
        st.session_state.confirm_clear_inspections = False

    if not st.session_state.confirm_clear_inspections:
        if st.button("🗑️ Apagar todas as vistorias", type="secondary", use_container_width=True):
            st.session_state.confirm_clear_inspections = True
            st.rerun()
    else:
        st.warning(f"Isso apagará {len(items)} vistoria(s) do histórico e os PDFs correspondentes.")
        a, b = st.columns(2)
        with a:
            if st.button("Cancelar", use_container_width=True):
                st.session_state.confirm_clear_inspections = False
                st.rerun()
        with b:
            if st.button("⚠️ CONFIRMAR APAGAMENTO", type="primary", use_container_width=True):
                save_json(INSPECTIONS_FILE, [])
                for item in items:
                    numero = item.get("numero", "")
                    if numero:
                        try:
                            pdf_path(numero).unlink(missing_ok=True)
                        except Exception:
                            pass
                st.session_state.confirm_clear_inspections = False
                st.session_state.pdf = None
                st.success("Todas as vistorias concluídas e seus PDFs foram apagados.")
                st.rerun()


def admin_users():
    if st.session_state.user.get("perfil") != "Administrador":
        st.error("Acesso restrito ao administrador.")
        return
    st.markdown(hero_html("Gerenciar logins", "Crie e controle os acessos ao LAUDO DE VISTORIA.", "Somente administradores"), unsafe_allow_html=True)
    
    with st.form("admin_create_user"):
        a,b,c=st.columns(3)
        nu=a.text_input("Novo usuário")
        nn=b.text_input("Nome completo")
        perfil=c.selectbox("Perfil", ["Inspetor","Administrador"])
        a,b=st.columns(2)
        np=a.text_input("Senha", type="password")
        np2=b.text_input("Confirmar senha", type="password")
        if st.form_submit_button("＋ Criar login", type="primary", use_container_width=True):
            users=load_json(USERS_FILE,[])
            if not nu or not nn or not np:
                st.warning("Preencha todos os campos.")
            elif np != np2:
                st.error("As senhas não conferem.")
            elif any(x["usuario"].lower()==nu.lower() for x in users):
                st.error("Esse usuário já existe.")
            else:
                users.append({"usuario":nu.strip(),"nome":nn.strip(),"senha":hash_pw(np),"perfil":perfil,"ativo":True})
                save_json(USERS_FILE, users)
                st.success(f"Login '{nu.strip()}' criado com sucesso.")
    
    st.subheader("Usuários cadastrados")
    users=load_json(USERS_FILE,[])
    for i,u in enumerate(users):
        cols=st.columns([2,3,2,1])
        cols[0].write(f"**{u['usuario']}**")
        cols[1].write(f"{u.get('nome','')} • {u.get('perfil','Inspetor')}")
        cols[2].write("Ativo" if u.get("ativo",True) else "Bloqueado")
        if u["usuario"] != st.session_state.user["usuario"]:
            label="Bloquear" if u.get("ativo",True) else "Liberar"
            if cols[3].button(label,key=f"toggle_user_{i}"):
                users[i]["ativo"]=not users[i].get("ativo",True)
                save_json(USERS_FILE, users)
                save_current_state(); st.rerun()
    

st.markdown(view_marker_html(), unsafe_allow_html=True)

if st.session_state.user is None:
    login_screen()
else:
    sidebar()
    if st.session_state.admin_users:
        admin_users()
    elif st.session_state.get("clear_inspections", False):
        admin_clear_inspections()
    elif st.session_state.show_history:
        history()
    elif st.session_state.step==0: inicio()
    elif st.session_state.step==1: vehicle()
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
