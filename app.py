import streamlit as st
from pathlib import Path
from datetime import datetime
import json, hashlib, base64, io, re, secrets
from PIL import Image, ImageDraw
import numpy as np
from streamlit_drawable_canvas import st_canvas
from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image as RLImage, PageBreak
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

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
    "Funciona","Segredo","Chave","Painel","Chave reserva","Manual","Quebra sol","Break light","Retrovisor",
    "Macaco","Chave de roda","Estepe","Extintor","Triângulo","Tapetes","Isqueiro","Alto falantes","Aparelho de som",
    "Antena","Faróis auxiliares","Aerofólio","Engate traseiro","Quebra mato","Modulo (carro)","Modulo (som)",
    "Rack","Estribo","Tampão porta malas","Bateria e marca","Documento","Transferência","Nota fiscal",
    "Veículo limpo","Outros acessórios"
]
TIRES = [("Dianteiro esquerdo","DE"),("Dianteiro direito","DD"),("Traseiro esquerdo","TE"),("Traseiro direito","TD"),("Estepe","ESP")]
DAMAGE_TYPES = ["Batida","Arranhão","Amassado","Quebrado","Trincado","Outro"]
SEVERITIES = ["Leve","Média","Grave"]
PHOTO_SLOTS = [("frente","Frente"),("traseira","Traseira"),("lateral_esq","Lateral esquerda"),("lateral_dir","Lateral direita"),("interior","Interior"),("painel","Painel / km")]

VEHICLE_BRANDS = [
    "Chevrolet","Fiat","Ford","Volkswagen","Toyota","Honda","Hyundai","Renault","Nissan","Jeep",
    "Peugeot","Citroën","Mitsubishi","Kia","BMW","Mercedes-Benz","Audi","Volvo","Land Rover",
    "Suzuki","Yamaha","Kawasaki","Triumph","Harley-Davidson","Iveco","Scania","Volvo Trucks",
    "Mercedes-Benz Caminhões","Agrale","Troller","RAM","Chery/Caoa"
]
TIRE_BRANDS = [
    "Pirelli","Goodyear","Michelin","Continental","Bridgestone","Firestone","Dunlop",
    "General Tire","Yokohama","Maxxis","Hankook","Kumho","Cooper","Nexen"
]
TIRE_SIZES = [
    "165/70 R13","175/65 R14","175/70 R13","185/60 R15","185/65 R14","185/65 R15",
    "195/55 R15","195/60 R15","195/60 R16","195/65 R15","205/55 R16","205/60 R16",
    "205/65 R15","215/45 R17","215/50 R17","215/55 R17","215/60 R16","225/45 R17",
    "225/45 R18","225/50 R17","225/55 R18","235/40 R19","235/45 R19","245/40 R20",
    "265/50 R20","275/60 R20"
]
_current_year = datetime.now().year
VEHICLE_YEARS = [str(y) for y in range(_current_year + 1, 1979, -1)]

def pick_or_type(label, options, current, key):
    """Selectbox com lista pré-definida + opção 'Outro' com campo livre."""
    outro = "Outro (digitar)"
    choices = list(options) + [outro]
    if current in choices:
        idx = choices.index(current)
    elif current:
        idx = len(choices) - 1  # valor já digitado que não está na lista -> cai em "Outro"
    else:
        idx = 0
    sel = st.selectbox(label, choices, index=idx, key=key + "_sel")
    if sel == outro:
        return st.text_input(label + " (digite)", current if current not in options else "", key=key + "_free")
    return sel
STEPS = [
    ("veiculo","02","Veículo"),("combustivel","03","Combustível"),("acessorios","04","Acessórios"),
    ("pneus","05","Pneus"),("avarias","06","Avarias"),("fotos","07","Fotos"),
    ("proprietario","08","Proprietário"),("emitente","09","Emitente"),("revisao","10","Revisão / PDF")
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
        "veiculo": {"tipo":"sedan","marca":"","modelo":"","ano":"","placa":"","cor":"","chassi":"","km":"","observacoes":""},
        "combustivel":{"tipo":"Gasolina","nivel":"1/2","percentual":50},
        "acessorios":{a:{"status":"","obs":""} for a in ACCESSORIES},
        "pneus":{k:{"estado":"","marca":"","medida":"","observacao":""} for _,k in TIRES},
        "avarias":{"diagrama":None,"imagem":None,"marcacoes":[]},
        "fotos":{}, "fotos_acessorios":[],
        "proprietario":{"nome":"","cpf":"","telefone":"","assinatura":None},
        "emitente":{"empresa":"","documento":"","telefone":"","email":"","endereco":"","responsavel":"","assinatura":None}
    }

def b64_image(upload):
    if upload is None: return None
    img = Image.open(upload).convert("RGB")
    img.thumbnail((1400,1000))
    out=io.BytesIO(); img.save(out,"JPEG",quality=78,optimize=True)
    return base64.b64encode(out.getvalue()).decode()

def b64_pil(img):
    out=io.BytesIO(); img.convert("RGB").save(out,"PNG")
    return base64.b64encode(out.getvalue()).decode()

def pil_b64(s):
    return Image.open(io.BytesIO(base64.b64decode(s))).convert("RGB")

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
    overlay = Image.fromarray(oa, "RGBA")
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

def pdf_bytes(c):
    buf=io.BytesIO()
    doc=SimpleDocTemplate(buf,pagesize=A4,rightMargin=32,leftMargin=32,topMargin=34,bottomMargin=34)
    ss=getSampleStyleSheet()
    ss.add(ParagraphStyle(name="ACHead",parent=ss["Heading1"],fontSize=20,textColor=colors.HexColor("#111827")))
    ss.add(ParagraphStyle(name="Sec",parent=ss["Heading2"],fontSize=12,textColor=colors.HexColor("#111827"),spaceBefore=12,spaceAfter=6))
    ss.add(ParagraphStyle(name="Sm",parent=ss["BodyText"],fontSize=8.5,leading=11))
    story=[]; v=c["veiculo"]; f=c["combustivel"]
    story += [Paragraph("LAUDO DE VISTORIA",ss["ACHead"]),Paragraph("Relatório profissional de inspeção veicular",ss["Sm"]),Spacer(1,8)]
    t=Table([[c["numero"],f"Status: {c["status"]}",f"Data: {c.get("finalizado_em") or c["criado_em"]}"]],colWidths=[170,170,170])
    t.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,-1),colors.HexColor("#f1f5f9")),("BOX",(0,0),(-1,-1),.5,colors.HexColor("#cbd5e1")),("FONTSIZE",(0,0),(-1,-1),8),("PADDING",(0,0),(-1,-1),7)])); story.append(t)
    story.append(Paragraph("1. Identificação do veículo",ss["Sec"]))
    rows=[[str(x),str(v.get(k,""))] for x,k in [("Tipo","tipo"),("Marca","marca"),("Modelo","modelo"),("Ano","ano"),("Placa","placa"),("Cor","cor"),("Chassi","chassi")]]
    t=Table(rows,colWidths=[110,400]); t.setStyle(TableStyle([("GRID",(0,0),(-1,-1),.3,colors.HexColor("#e2e8f0")),("BACKGROUND",(0,0),(0,-1),colors.HexColor("#f8fafc")),("FONTSIZE",(0,0),(-1,-1),8),("PADDING",(0,0),(-1,-1),5)])); story.append(t)
    story.append(Paragraph("2. Combustível",ss["Sec"])); story.append(Paragraph(f'Tipo: {f["tipo"]} &nbsp; Nível: {f["nivel"]} &nbsp; Percentual: {f["percentual"]}%',ss["Sm"]))
    story.append(Paragraph("3. Acessórios",ss["Sec"]))
    ar=[[n,it["status"],it.get("obs","")] for n,it in c["acessorios"].items() if it.get("status")]
    if ar:
        t=Table([["Item","Situação","Observação"]]+ar,colWidths=[180,90,240]); t.setStyle(TableStyle([("GRID",(0,0),(-1,-1),.3,colors.HexColor("#e2e8f0")),("BACKGROUND",(0,0),(-1,0),colors.HexColor("#111827")),("TEXTCOLOR",(0,0),(-1,0),colors.white),("FONTSIZE",(0,0),(-1,-1),8),("PADDING",(0,0),(-1,-1),5)])); story.append(t)
    story.append(Paragraph("4. Pneus",ss["Sec"]))
    labels={k:n for n,k in TIRES}; tr=[["Posição","Estado","Marca","Medida","Observação"]]+[[labels.get(k,k),it.get("estado",""),it.get("marca",""),it.get("medida",""),it.get("observacao","")] for k,it in c["pneus"].items()]
    t=Table(tr,colWidths=[115,70,100,90,135]); t.setStyle(TableStyle([("GRID",(0,0),(-1,-1),.3,colors.HexColor("#e2e8f0")),("BACKGROUND",(0,0),(-1,0),colors.HexColor("#111827")),("TEXTCOLOR",(0,0),(-1,0),colors.white),("FONTSIZE",(0,0),(-1,-1),7.5),("PADDING",(0,0),(-1,-1),4)])); story.append(t)
    story.append(PageBreak()); story.append(Paragraph("5. Avarias",ss["Sec"]))
    av=c["avarias"]
    story.append(Paragraph(f'Desenho utilizado: {(av.get("diagrama") or "sedan").title()} — {len(av["marcacoes"])} ocorrência(s)',ss["Sm"]))
    if av.get("imagem"):
        try:
            im=pil_b64(av["imagem"]); out=io.BytesIO(); im.save(out,"PNG"); out.seek(0); story.append(RLImage(out,width=360,height=256))
        except Exception: pass
    for m in av["marcacoes"]: story.append(Paragraph(f"• {m['tipo']} — {m['severidade']} — {m['descricao']}",ss["Sm"]))
    story.append(Paragraph("6. Fotos",ss["Sec"]))
    foto_items=[(name,b64) for name,b64 in c.get("fotos",{}).items() if b64]
    for name,b64 in foto_items:
        try:
            im=pil_b64(b64)
            im.thumbnail((900,700))
            out=io.BytesIO()
            im.save(out,"JPEG",quality=90)
            out.seek(0)
            story.append(Paragraph(name.replace("_"," ").title(),ss["Sm"]))
            story.append(RLImage(out,width=230,height=172))
            story.append(Spacer(1,6))
        except Exception:
            pass

    extras=c.get("fotos_acessorios",[])
    if extras:
        story.append(Paragraph("Fotos de acessórios",ss["Sm"]))
        for i,b64 in enumerate(extras,1):
            try:
                im=pil_b64(b64)
                im.thumbnail((900,700))
                out=io.BytesIO()
                im.save(out,"JPEG",quality=90)
                out.seek(0)
                story.append(Paragraph(f"Acessório {i}",ss["Sm"]))
                story.append(RLImage(out,width=230,height=172))
                story.append(Spacer(1,6))
            except Exception:
                pass
    story.append(Paragraph("7. Proprietário",ss["Sec"]))
    p=c["proprietario"]; story.append(Paragraph(f'Nome: {p["nome"]}<br/>CPF: {p.get("cpf","")}<br/>Telefone: {p["telefone"]}',ss["Sm"]))
    if p.get("assinatura"): story.append(RLImage(io.BytesIO(base64.b64decode(p["assinatura"])),width=210,height=75))
    story.append(Paragraph("8. Empresa / Emitente",ss["Sec"]))
    e=c["emitente"]; story.append(Paragraph(f'Empresa: {e["empresa"]}<br/>Documento: {e["documento"]}<br/>Telefone: {e["telefone"]}<br/>E-mail: {e["email"]}<br/>Endereço: {e["endereco"]}<br/>Responsável: {e["responsavel"]}',ss["Sm"]))
    if e.get("assinatura"): story.append(RLImage(io.BytesIO(base64.b64decode(e["assinatura"])),width=210,height=75))
    doc.build(story); return buf.getvalue()

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
        st.session_state.inspection = data["inspection"]
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
if "inspection" not in st.session_state: st.session_state.inspection = new_inspection()
if "damage_view" not in st.session_state: st.session_state.damage_view = None
if "mark_mode" not in st.session_state: st.session_state.mark_mode = "x"
if "admin_users" not in st.session_state: st.session_state.admin_users = False
if "show_history" not in st.session_state: st.session_state.show_history = False
if "clear_inspections" not in st.session_state: st.session_state.clear_inspections = False
if st.session_state.user and not st.session_state.get("_restored_state", False):
    load_current_state(st.session_state.user)
    st.session_state._restored_state = True

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
                    save_current_state()
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
        if st.session_state.step<9 and st.button("Próxima etapa →",type="primary",use_container_width=True):
            st.session_state.step+=1
            save_current_state()
            st.rerun(scope="app") if fragment else st.rerun()
    st.progress((st.session_state.step+1)/10)

@st.fragment
def vehicle():
    c=st.session_state.inspection; v=c["veiculo"]; topbar("02 • Informações do veículo","Tipo, identificação e dados complementares.")
    st.subheader("Informações do Veículo")
    cols=st.columns(6)
    
    a,b,d=st.columns(3)
    with a: v["marca"]=pick_or_type("Marca",VEHICLE_BRANDS,v["marca"],"veic_marca")
    v["modelo"]=b.text_input("Modelo",v["modelo"]); v["placa"]=d.text_input("Placa / Renavam",v["placa"])
    a,b,d=st.columns(3)
    with a: v["ano"]=pick_or_type("Ano",VEHICLE_YEARS,v["ano"],"veic_ano")
    v["cor"]=b.text_input("Cor",v["cor"]).upper(); v["km"]=d.text_input("Km",v["km"])
    v["observacoes"]=st.text_area("Observações gerais",v["observacoes"],height=90)
    nav(fragment=True)

@st.fragment
def fuel():
    c=st.session_state.inspection; f=c["combustivel"]; topbar("03 • Combustível","Tipo, nível e percentual exato.")
    
    f["tipo"]=st.selectbox("Tipo de combustível",FUEL_TYPES,index=FUEL_TYPES.index(f["tipo"]))
    st.subheader("Nível do tanque"); cols=st.columns(5)
    for col,(lab,pct) in zip(cols,FUEL_LEVELS):
        with col:
            if st.button(f"⛽ {lab}\n{pct}%",key="fuel_"+lab,use_container_width=True,type="primary" if f["nivel"]==lab else "secondary"):
                f["nivel"]=lab; f["percentual"]=pct; st.rerun(scope="fragment")
    f["percentual"]=st.slider("Percentual exato",0,100,int(f["percentual"]),5);
    nav(fragment=True)

@st.fragment
def accessories():
    c=st.session_state.inspection; topbar("04 • Acessórios","Botões rápidos para presença, ausência ou não aplicável.")
    for item in ACCESSORIES:
        c["acessorios"].setdefault(item, {"status":"","obs":""})
    
    for i,a in enumerate(ACCESSORIES):
        it=c["acessorios"][a]; st.markdown(f"**{a}**")
        cc=st.columns([1,1,1,3])
        for col,opt,label in zip(cc[:3],["sim","nao","na"],["✓ SIM","✕ NÃO","— N/A"]):
            with col:
                if st.button(label,key=f"acc_{i}_{opt}",use_container_width=True,type="primary" if it["status"]==opt else "secondary"):
                    it["status"]=opt; st.rerun(scope="fragment")
        it["obs"]=cc[3].text_input("Observação",it["obs"],key=f"accobs_{i}",label_visibility="collapsed",placeholder="Observação opcional")
        st.divider()
    nav(fragment=True)

@st.fragment
def tires():
    c=st.session_state.inspection; topbar("05 • Pneus","Avalie os quatro pneus e o estepe.")
    for row in range(0,5,2):
        cols=st.columns(2)
        for col,(label,key) in zip(cols,TIRES[row:row+2]):
            with col:
                it=c["pneus"][key]
                st.markdown(f"### 🛞 {label}")
                oc=st.columns(3)
                for ocol,opt in zip(oc,["Bom","Regular","Ruim"]):
                    with ocol:
                        if st.button(opt,key=f"t_{key}_{opt}",use_container_width=True,type="primary" if it["estado"]==opt else "secondary"):
                            it["estado"]=opt; st.rerun(scope="fragment")
                it["marca"]=pick_or_type("Marca",TIRE_BRANDS,it["marca"],f"tm_{key}")
                it["medida"]=pick_or_type("Medida",TIRE_SIZES,it["medida"],f"td_{key}")
                it["observacao"]=st.text_input("Observação",it["observacao"],key=f"to_{key}")
                
    nav(fragment=True)

@st.fragment
def damage():
    c=st.session_state.inspection; av=c["avarias"]
    topbar("06 • Avarias","Marque arranhões/avarias com vermelho e amassados com azul direto no desenho do veículo.")

    if not st.session_state.get("damage_view"):
        st.session_state.damage_view = av.get("diagrama") or c["veiculo"].get("tipo") or "sedan"
    if not av.get("diagrama"):
        av["diagrama"] = st.session_state.damage_view
    view = st.session_state.damage_view

    # Histórico próprio para os botões de desfazer/refazer/limpar.
    hist_key = f"damage_history_{view}"
    redo_key = f"damage_redo_{view}"
    ver_key = f"canvas_version_{view}"
    if hist_key not in st.session_state:
        st.session_state[hist_key] = []
    if redo_key not in st.session_state:
        st.session_state[redo_key] = []

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
                        st.session_state[f"damage_history_{key}"] = []
                        st.session_state[f"damage_redo_{key}"] = []
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
            st.rerun(scope="fragment")
    with mc2:
        if st.button("Amassado", key="damage_mark_o", use_container_width=True,
                     type="primary" if st.session_state.mark_mode=="o" else "secondary"):
            st.session_state.mark_mode="o"
            st.rerun(scope="fragment")

    if st.session_state.mark_mode=="x":
        stroke_color, draw_mode = "#ef4444", "freedraw"
        st.caption("Arranhão.")
    else:
        stroke_color, draw_mode = "#2563eb", "freedraw"
        st.caption("Amassado.")

    # Tamanho pensado para celular. A resolução do canvas é mantida igual à área
    # exibida para evitar corte e deslocamento das marcações.
    canvas_width = 400
    canvas_height = 460

    if av.get("imagem") and av.get("imagem_ok"):
        try:
            base_image = pil_b64(av["imagem"]).resize(
                (canvas_width, canvas_height), Image.Resampling.LANCZOS
            )
        except Exception:
            base_image = vehicle_diagram(view, size=(canvas_width, canvas_height))
    else:
        if av.get("imagem") and not av.get("imagem_ok"):
            av["imagem"] = None
        base_image = vehicle_diagram(view, size=(canvas_width, canvas_height))

    # IMPORTANTE:
    # O drawable-canvas redimensiona background_image internamente. Em alguns
    # navegadores/Streamlit Cloud isso pode deixar a camada de desenho com
    # coordenadas diferentes da imagem de fundo. Para evitar o deslocamento,
    # a imagem agora faz parte do próprio Fabric.js como um objeto travado.
    # Assim, imagem e marcações usam exatamente o mesmo sistema de coordenadas.
    canvas_base = av.get("imagem") if av.get("imagem") and av.get("imagem_ok") else None
    if canvas_base:
        try:
            canvas_image = pil_b64(canvas_base).resize(
                (canvas_width, canvas_height), Image.Resampling.LANCZOS
            )
        except Exception:
            canvas_image = base_image
    else:
        canvas_image = base_image

    # IMPORTANTE: não recriar o initial_drawing a cada traço.
    # Se ele muda a cada rerun, o drawable-canvas pode reconstruir o Fabric.js
    # enquanto o navegador ainda está mostrando o traço recém-feito. Isso causa
    # o efeito de apagar -> esperar -> aparecer novamente.
    # Mantemos a imagem inicial estável enquanto o usuário desenha.
    canvas_version = st.session_state.get(ver_key, 0)
    init_key = f"canvas_initial_{view}_{canvas_version}"
    if init_key not in st.session_state:
        st.session_state[init_key] = canvas_image_drawing(canvas_image)
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
        display_toolbar=False,
    )

    if can.image_data is not None:
        new_image = canvas_b64(can, base_image)
        old_image = av.get("imagem")

        # Só registra um novo estado quando houve uma alteração real.
        if new_image and new_image != old_image:
            if old_image:
                history = st.session_state[hist_key]
                if not history or history[-1] != old_image:
                    history.append(old_image)
                    if len(history) > 30:
                        del history[0]
            st.session_state[redo_key] = []
            av["imagem"] = new_image
            av["imagem_ok"] = True
            # Não gravamos o rascunho em disco a cada traço. Isso adicionava
            # uma operação pesada ao rerun e aumentava o atraso visual.
            # A navegação, desfazer/refazer e limpeza continuam salvando o estado.

    # Controles próprios e funcionais, abaixo do desenho.
    c1,c2,c3 = st.columns(3)

    with c1:
        if st.button("↶ Desfazer", key=f"undo_{view}", use_container_width=True,
                     disabled=not st.session_state[hist_key]):
            current = av.get("imagem")
            if current:
                st.session_state[redo_key].append(current)
            previous = st.session_state[hist_key].pop()
            av["imagem"] = previous
            av["imagem_ok"] = True
            st.session_state[ver_key] = st.session_state.get(ver_key, 0) + 1
            st.session_state.pop(f"canvas_initial_{view}_{st.session_state[ver_key]}", None)
            save_current_state()
            st.rerun()

    with c2:
        if st.button("↷ Refazer", key=f"redo_{view}", use_container_width=True,
                     disabled=not st.session_state[redo_key]):
            current = av.get("imagem")
            if current:
                st.session_state[hist_key].append(current)
            restored = st.session_state[redo_key].pop()
            av["imagem"] = restored
            av["imagem_ok"] = True
            st.session_state[ver_key] = st.session_state.get(ver_key, 0) + 1
            st.session_state.pop(f"canvas_initial_{view}_{st.session_state[ver_key]}", None)
            save_current_state()
            st.rerun()

    with c3:
        if st.button("🗑️ Limpar", key=f"clear_{view}", use_container_width=True,
                     disabled=not av.get("imagem")):
            current = av.get("imagem")
            if current:
                st.session_state[hist_key].append(current)
                if len(st.session_state[hist_key]) > 30:
                    del st.session_state[hist_key][0]
            av["imagem"] = None
            av["imagem_ok"] = False
            st.session_state[redo_key] = []
            st.session_state[ver_key] = st.session_state.get(ver_key, 0) + 1
            st.session_state.pop(f"canvas_initial_{view}_{st.session_state[ver_key]}", None)
            save_current_state()
            st.rerun()
    nav()

@st.fragment
def photos():
    c=st.session_state.inspection
    topbar("07 • Fotos","Envie várias fotos e elas serão mantidas no PDF.")

    st.caption("No celular, use a câmera ou a galeria. Cada posição abaixo guarda sua própria foto.")

    # Mantém as fotos já salvas mesmo depois dos reruns do Streamlit.
    c.setdefault("fotos", {})
    c.setdefault("fotos_acessorios", [])

    usar_camera_navegador = st.checkbox(
        "Usar câmera direta do navegador (avançado, requer permissão)",
        value=False,
        key="usar_cam_navegador"
    )

    for row in range(0, len(PHOTO_SLOTS), 2):
        cols=st.columns(2)

        for col,(key,label) in zip(cols,PHOTO_SLOTS[row:row+2]):
            with col:
                st.markdown(f"### 📷 {label}")

                up=st.file_uploader(
                    f"Enviar foto — {label}",
                    type=["jpg","jpeg","png","webp"],
                    key="up_"+key
                )

                src=up

                if usar_camera_navegador:
                    cam=st.camera_input(
                        f"Câmera do navegador — {label}",
                        key="cam_"+key
                    )
                    if cam is not None:
                        src=cam

                if src is not None:
                    try:
                        c["fotos"][key]=b64_image(src)
                        save_current_state()
                    except Exception:
                        st.error(f"Não foi possível salvar a foto: {label}")

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

    st.markdown("### 📦 Fotos de acessórios")

    ex=st.file_uploader(
        "Selecione uma ou várias fotos dos acessórios",
        type=["jpg","jpeg","png","webp"],
        accept_multiple_files=True,
        key="extras"
    )

    if ex:
        # Adiciona novas fotos sem apagar as que já foram selecionadas anteriormente.
        existing = c.get("fotos_acessorios", [])
        novos = [b64_image(x) for x in ex]
        c["fotos_acessorios"] = existing + [
            x for x in novos if x not in existing
        ]
        save_current_state()

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
    can=st_canvas(background_color="#ffffff",stroke_width=2.5,stroke_color="#111827",height=180,width=600,drawing_mode="freedraw",key=key)
    if can.image_data is not None:
        arr=can.image_data[:,:,:3]
        if (arr<245).any(): stored=canvas_b64(can)
    if stored: st.image(pil_b64(stored),width=360)
    return stored

@st.fragment
def owner():
    p=st.session_state.inspection["proprietario"]; topbar("08 • Proprietário","Nome, CPF, telefone e assinatura.")
    
    a,b=st.columns(2); p["nome"]=a.text_input("Nome completo",p["nome"]); p["cpf"]=b.text_input("CPF do proprietário",p.get("cpf","")); p["telefone"]=st.text_input("Telefone de contato",p["telefone"]); p["assinatura"]=signature("Assinatura do proprietário / responsável","sig_owner",p["assinatura"])
    nav(fragment=True)

@st.fragment
def issuer():
    e=st.session_state.inspection["emitente"]; topbar("09 • Empresa / Emitente","Dados e assinatura que aparecerão no PDF.")

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
    c=st.session_state.inspection; v=c["veiculo"]; f=c["combustivel"]; topbar("10 • Revisão e emissão do PDF","Confira os dados e finalize a inspeção.")
    a,b,d,e=st.columns(4); a.metric("Veículo",f'{v["marca"]} {v["modelo"]}'.strip() or "—"); b.metric("Placa",v["placa"] or "—"); d.metric("Combustível",f'{f["percentual"]}%'); e.metric("Avarias",len(c["avarias"]["marcacoes"]))
    
    for name,val in [
        ("Veículo",f'{v["tipo"]} • {v["marca"]} {v["modelo"]} • {v["cor"]} • {v["km"]} km'),
        ("Combustível",f'{f["tipo"]} • {f["nivel"]} • {f["percentual"]}%'),
        ("Acessórios",f'{sum(1 for x in c["acessorios"].values() if x["status"])} itens avaliados'),
        ("Pneus",f'{sum(1 for x in c["pneus"].values() if x["estado"])} posições avaliadas'),
        ("Fotos",f'{len([x for x in c["fotos"].values() if x])} padrão + {len(c["fotos_acessorios"])} acessórios'),
        ("Proprietário",f'{c["proprietario"]["nome"] or "Pendente"} • {c["proprietario"].get("cpf","") or "Sem CPF"} • {c["proprietario"]["telefone"] or "Sem telefone"}'),
        ("Emitente",f'{c["emitente"]["empresa"] or "Pendente"} • {c["emitente"]["responsavel"] or "Sem responsável"}')]:
        st.write(f"**{name}:** {val}")
    
    if st.button("✓ Finalizar inspeção e preparar PDF",type="primary",use_container_width=True):
        c["inspetor"]=st.session_state.user["nome"]; c["status"]="Concluído"; c["finalizado_em"]=now()
        items=load_json(INSPECTIONS_FILE,[]); items=[x for x in items if x["numero"]!=c["numero"]]; items.append(c); save_json(INSPECTIONS_FILE,items)
        st.session_state.pdf=pdf_bytes(c)
        save_pdf_file(c["numero"], st.session_state.pdf)
        st.success("Inspeção finalizada e PDF arquivado no sistema.")
    if st.session_state.get("pdf"): st.download_button("⬇ Baixar PDF",data=st.session_state.pdf,file_name=c["numero"]+".pdf",mime="application/pdf",type="primary",use_container_width=True)
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
    elif st.session_state.step==3: accessories()
    elif st.session_state.step==4: tires()
    elif st.session_state.step==5: damage()
    elif st.session_state.step==6: photos()
    elif st.session_state.step==7: owner()
    elif st.session_state.step==8: issuer()
    elif st.session_state.step==9: review()

# Salva automaticamente a etapa e a inspeção para sobreviver ao F5/recarregamento.
if st.session_state.get("user"):
    save_current_state()
