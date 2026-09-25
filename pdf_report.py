"""Gerador do PDF do LAUDO DE VISTORIA.

Módulo independente do Streamlit (só usa reportlab + Pillow), para poder ser
testado isoladamente. O app.py chama build_pdf() através de pdf_bytes().

Regras de layout:
- seções relacionadas ficam juntas (KeepTogether): título + conteúdo;
- acessórios em duas colunas (metade da lista de cada lado);
- fotos em grade de 2 colunas, mantendo a proporção original;
- 2ª página: "5. AVARIAS" + "7. PROPRIETÁRIO" / "8. EMPRESA / EMITENTE" (lado a lado,
  assinaturas abaixo de cada um) + texto de responsabilidade formam UM bloco. O desenho
  das avarias é dimensionado automaticamente para o bloco inteiro caber numa página;
- nenhuma foto é descartada em silêncio: se uma imagem falhar, o PDF mostra
  um aviso no lugar dela e o motivo é devolvido em `warnings`.
"""
import io
import base64
from html import escape
from pathlib import Path

from PIL import Image
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    Image as RLImage, KeepTogether,
)

MARGIN_X = 32
MARGIN_Y = 34
FRAME_W = A4[0] - 2 * MARGIN_X          # ~531 pt
FRAME_PAD = 6                           # padding interno padrão do Frame do SimpleDocTemplate
GRID_GAP = 8

STATUS_TEXT = {"sim": ("SIM", "#15803d"), "nao": ("NÃO", "#b91c1c"), "na": ("N/A", "#64748b")}

# Limites das imagens (em pontos). A proporção original é sempre preservada.
PHOTO_MAX_W = 250
PHOTO_MAX_H = 205
DIAGRAM_MAX_W = 380
DIAGRAM_MAX_H = 400
DIAGRAM_MIN_H = 250                     # piso: o desenho nunca encolhe abaixo disso para "caber"
SIGNATURE_MAX_W = 200
SIGNATURE_MAX_H = 66
SIGN_GAP = 16                           # espaço entre Proprietário e Empresa/Emitente
FIT_SAFETY = 8                          # folga (pt) ao calcular o que cabe na página

LOGO_FILE = Path(__file__).resolve().parent / "assets" / "ch360_logo_1200.png"
LOGO_H = 30                             # altura da logo CH360 no cabeçalho da 1ª página (pt)
_LOGO = None                            # ImageReader em cache (False = arquivo indisponível)


# ----------------------------------------------------------------------------
# utilidades
# ----------------------------------------------------------------------------
def esc(value):
    """Escapa texto do usuário antes de colocar em Paragraph (evita quebrar o
    PDF com caracteres como & < >)."""
    return escape(str(value if value is not None else ""))


def up(value):
    return str(value if value is not None else "").upper()


def _decode(b64):
    return base64.b64decode(b64)


def fit_image(b64, max_w, max_h):
    """Cria um RLImage proporcional, dentro de max_w x max_h.

    Usa os bytes originais (JPEG segue como JPEG, sem recompressão), então não
    há perda extra de qualidade. Levanta exceção se a imagem for inválida.
    """
    raw = _decode(b64)
    with Image.open(io.BytesIO(raw)) as im:
        w, h = im.size
    if not w or not h:
        raise ValueError("imagem sem dimensões")
    scale = min(max_w / float(w), max_h / float(h))
    return RLImage(io.BytesIO(raw), width=w * scale, height=h * scale)


def _stack_height(flowables, width):
    """Altura total (com espaçamentos) de flowables empilhados, sem desenhá-los.
    É uma estimativa levemente conservadora: soma spaceBefore + altura + spaceAfter de cada um."""
    total = 0
    for fl in flowables:
        _, h = fl.wrap(width, 100000)
        total += h + fl.getSpaceBefore() + fl.getSpaceAfter()
    return total


def _styles():
    ss = getSampleStyleSheet()
    ss.add(ParagraphStyle(name="ACHead", parent=ss["Heading1"], fontSize=20, textColor=colors.HexColor("#111827")))
    ss.add(ParagraphStyle(name="Sec", parent=ss["Heading2"], fontSize=12, leading=14,
                          textColor=colors.HexColor("#111827"),
                          spaceBefore=8, spaceAfter=4, keepWithNext=1))
    ss.add(ParagraphStyle(name="SubSec", parent=ss["BodyText"], fontSize=9.5, leading=12,
                          textColor=colors.HexColor("#111827"), fontName="Helvetica-Bold",
                          spaceBefore=8, spaceAfter=4, keepWithNext=1))
    ss.add(ParagraphStyle(name="Sm", parent=ss["BodyText"], fontSize=8.5, leading=11))
    ss.add(ParagraphStyle(name="SmTight", parent=ss["BodyText"], fontSize=8.5, leading=10.5,
                          spaceBefore=0, spaceAfter=1.5))
    ss.add(ParagraphStyle(name="Resp", parent=ss["BodyText"], fontSize=8.5, leading=10.5,
                          spaceBefore=4, spaceAfter=0))
    ss.add(ParagraphStyle(name="Cell", parent=ss["BodyText"], fontSize=8, leading=9.6))
    ss.add(ParagraphStyle(name="CellSm", parent=ss["BodyText"], fontSize=7, leading=8.4,
                          textColor=colors.HexColor("#475569")))
    ss.add(ParagraphStyle(name="CellHead", parent=ss["BodyText"], fontSize=8, leading=9.6,
                          textColor=colors.white, fontName="Helvetica-Bold"))
    ss.add(ParagraphStyle(name="Cap", parent=ss["BodyText"], fontSize=8, leading=10,
                          fontName="Helvetica-Bold", spaceAfter=2))
    ss.add(ParagraphStyle(name="CapDesc", parent=ss["BodyText"], fontSize=7.5, leading=9,
                          textColor=colors.HexColor("#475569"), spaceBefore=2))
    ss.add(ParagraphStyle(name="Warn", parent=ss["BodyText"], fontSize=7.5, leading=9,
                          textColor=colors.HexColor("#b91c1c")))
    return ss


# ----------------------------------------------------------------------------
# blocos
# ----------------------------------------------------------------------------
def _photo_cell(ss, caption, b64, desc="", warnings=None, max_w=PHOTO_MAX_W, max_h=PHOTO_MAX_H):
    """Conteúdo de uma célula da grade: legenda + imagem (+ descrição)."""
    cell = [Paragraph(esc(caption), ss["Cap"])]
    try:
        cell.append(fit_image(b64, max_w, max_h))
    except Exception as exc:  # nunca descartar a foto em silêncio
        cell.append(Paragraph("(foto não pôde ser incluída no PDF)", ss["Warn"]))
        if warnings is not None:
            warnings.append(f"{caption}: {type(exc).__name__}: {exc}")
    if desc:
        cell.append(Paragraph(esc(desc), ss["CapDesc"]))
    return cell


def _grid_table(rows, col_w, cols):
    t = Table(rows, colWidths=[col_w] * cols)
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 2),
        ("RIGHTPADDING", (0, 0), (-1, -1), GRID_GAP / 2),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]))
    return t


def photo_block(ss, head, items, warnings=None, cols=2):
    """Título(s) + grade de fotos. items = [(legenda, b64, descrição), ...].

    Sem limite de quantidade. O título fica sempre junto da primeira linha de
    fotos (nunca isolado no fim da página); as demais linhas podem seguir para
    a próxima página, evitando páginas quase vazias.
    Devolve uma lista de flowables.
    """
    col_w = (FRAME_W - GRID_GAP * (cols - 1)) / cols
    rows, row = [], []
    for caption, b64, desc in items:
        row.append(_photo_cell(ss, caption, b64, desc, warnings, PHOTO_MAX_W, PHOTO_MAX_H))
        if len(row) == cols:
            rows.append(row)
            row = []
    if row:
        row += [""] * (cols - len(row))
        rows.append(row)
    out = [KeepTogether(list(head) + [_grid_table(rows[:1], col_w, cols)])]
    if len(rows) > 1:
        out.append(_grid_table(rows[1:], col_w, cols))
    return out


def accessories_table(ss, items):
    """items = [(nome, status, obs)]. Duas colunas (metade em cada lado) quando
    houver 4+ itens; adapta-se sozinho à quantidade."""
    dark = colors.HexColor("#111827")
    grid = colors.HexColor("#e2e8f0")

    def status_p(status):
        txt, col = STATUS_TEXT.get(status, (up(status), "#111827"))
        return Paragraph(f'<font color="{col}"><b>{esc(txt)}</b></font>', ss["Cell"])

    def row_cells(it):
        name, status, obs = it
        return [Paragraph(esc(name), ss["Cell"]), status_p(status), Paragraph(esc(obs), ss["CellSm"])]

    head = [Paragraph("Item", ss["CellHead"]), Paragraph("Situação", ss["CellHead"]), Paragraph("Observação", ss["CellHead"])]

    if len(items) < 4:  # poucos itens: coluna única, largura total
        data = [head] + [row_cells(i) for i in items]
        t = Table(data, colWidths=[180, 70, FRAME_W - 250], repeatRows=1)
        t.setStyle(TableStyle([
            ("GRID", (0, 0), (-1, -1), .3, grid), ("BACKGROUND", (0, 0), (-1, 0), dark),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ]))
        return t

    half = (len(items) + 1) // 2
    left, right = items[:half], items[half:]
    gap = 10
    half_w = (FRAME_W - gap) / 2
    cw = [half_w * 0.36, half_w * 0.19, half_w * 0.45]
    data = [head + [""] + head]
    for i in range(half):
        l = row_cells(left[i])
        r = row_cells(right[i]) if i < len(right) else ["", "", ""]
        data.append(l + [""] + r)
    t = Table(data, colWidths=cw + [gap] + cw, repeatRows=1)
    last = len(data) - 1
    style = [
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 4), ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("GRID", (0, 0), (2, last), .3, grid),
        ("BACKGROUND", (0, 0), (2, 0), dark),
        ("BACKGROUND", (4, 0), (6, 0), dark),
    ]
    if right:
        style.append(("GRID", (4, 0), (6, len(right)), .3, grid))
    t.setStyle(TableStyle(style))
    return t


def _key_table(ss, rows, col_widths, header=True, font=8, pad=5):
    t = Table(rows, colWidths=col_widths)
    st = [("GRID", (0, 0), (-1, -1), .3, colors.HexColor("#e2e8f0")),
          ("FONTSIZE", (0, 0), (-1, -1), font), ("PADDING", (0, 0), (-1, -1), pad)]
    if header:
        st += [("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#111827")), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white)]
    else:
        st += [("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f8fafc"))]
    t.setStyle(TableStyle(st))
    return t


def _cell_table_style(rows_n, cols_n, last_row_cells):
    """Estilo comum das grades compactas (identificação e pneus)."""
    st = [("BOX", (0, 0), (-1, -1), .3, colors.HexColor("#e2e8f0")),
          ("INNERGRID", (0, 0), (-1, -1), .3, colors.HexColor("#e2e8f0")),
          ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f8fafc")),
          ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
          ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
          ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6)]
    if rows_n > 1 and 0 < last_row_cells < cols_n:   # última linha incompleta: a última célula ocupa o resto
        st.append(("SPAN", (last_row_cells - 1, rows_n - 1), (cols_n - 1, rows_n - 1)))
    return st


def kv_grid(ss, items, max_cols=5):
    """Pares 'RÓTULO: valor' lado a lado. O nº de colunas se adapta ao tamanho dos textos:
    usa o máximo de colunas em que tudo cabe sem quebrar (sem diminuir a fonte)."""
    def need(label, value):
        return (stringWidth(f"{label}: ", "Helvetica-Bold", 8.5) + stringWidth(str(value), "Helvetica", 8.5) + 14)
    n = 1
    for cand in range(min(max_cols, len(items)), 0, -1):
        if all(need(l, v) <= FRAME_W / cand for l, v in items):
            n = cand
            break
    rows = []
    for i in range(0, len(items), n):
        chunk = items[i:i + n]
        row = [Paragraph(f"<b>{esc(l)}:</b> {esc(v)}", ss["Cell"]) for l, v in chunk]
        rows.append(row + [""] * (n - len(row)))
    t = Table(rows, colWidths=[FRAME_W / n] * n)
    t.setStyle(TableStyle(_cell_table_style(len(rows), n, len(items) % n or n)))
    return t


def tires_grid(ss, tires):
    """Pneus em duas colunas (2x2 + estepe). tires = [(posição, estado, marca)]."""
    cols = 2
    rows = []
    for i in range(0, len(tires), cols):
        chunk = tires[i:i + cols]
        row = [Paragraph(f"<b>{esc(pos)}</b><br/>Estado: {esc(estado)} &nbsp;&nbsp; Marca: {esc(marca)}", ss["Cell"])
               for pos, estado, marca in chunk]
        rows.append(row + [""] * (cols - len(row)))
    t = Table(rows, colWidths=[FRAME_W / cols] * cols)
    t.setStyle(TableStyle(_cell_table_style(len(rows), cols, len(tires) % cols or cols)))
    return t


# Texto automático que o app gravava para cada traço no desenho (não é informação do usuário).
AUTO_MARK_TEXT = "avaria indicada no desenho do veículo"


def real_marks(av):
    """Retorna somente descrições reais do usuário, sem textos automáticos."""
    automatic = {
        "amassado",
        "marcada",
        "avaria indicada no desenho do veículo",
        "amassado — marcada",
        "amassado - marcada",
        "amassado — marcada — avaria indicada no desenho do veículo",
        "amassado - marcada - avaria indicada no desenho do veículo",
    }
    out = []
    for m in av.get("marcacoes", []) or []:
        desc = str(m.get("descricao", "") or "").strip()
        normalized = desc.rstrip(".").strip().lower()
        if normalized and normalized not in automatic:
            out.append(desc)
    return out


def _logo():
    """Logo CH360 (PNG 1200 px, fundo transparente). None se o arquivo não existir."""
    global _LOGO
    if _LOGO is None:
        try:
            with Image.open(LOGO_FILE) as im:
                im = im.convert("RGBA")
                # 600 px para ~88 pt de largura = ~490 dpi: nítida na impressão, PDF mais leve
                im.thumbnail((600, 600), Image.LANCZOS)
                _LOGO = ImageReader(im)
        except Exception:
            _LOGO = False
    return _LOGO or None


def _footer(numero, logo=False):
    def draw(canvas, doc):
        canvas.saveState()
        img = _logo() if logo else None
        if img:  # desenhada na margem superior direita: não ocupa espaço no fluxo (paginação igual)
            w, h = img.getSize()
            lh = LOGO_H
            lw = lh * w / float(h)
            canvas.drawImage(img, A4[0] - MARGIN_X - lw, A4[1] - MARGIN_Y - FRAME_PAD - lh + 2,
                             width=lw, height=lh, mask="auto")
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(colors.HexColor("#64748b"))
        canvas.drawString(MARGIN_X, 18, f"CH360 • Laudo de Vistoria - {numero}")
        canvas.drawRightString(A4[0] - MARGIN_X, 18, f"Página {doc.page}")
        canvas.restoreState()
    return draw


# ----------------------------------------------------------------------------
# PDF completo
# ----------------------------------------------------------------------------
def build_pdf(c, labels=None, warnings=None):
    """Monta o PDF e devolve os bytes.

    c        -> dicionário da vistoria (mesma estrutura salva pelo app);
    labels   -> {"tires": {sigla: nome}, "photos": {chave: nome},
                 "keydoc": {chave: nome}, "diagrams": {chave: nome}};
    warnings -> lista opcional que recebe avisos de fotos que falharam.
    """
    labels = labels or {}
    warnings = warnings if warnings is not None else []
    ss = _styles()
    numero = c.get("numero", "")

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, rightMargin=MARGIN_X, leftMargin=MARGIN_X,
                            topMargin=MARGIN_Y, bottomMargin=MARGIN_Y + 6,
                            title=f"Laudo de Vistoria {numero}", author="CH360")
    story = []
    v = c.get("veiculo", {})
    f = c.get("combustivel", {})

    # ---- cabeçalho
    story += [Paragraph("LAUDO DE VISTORIA", ss["ACHead"]),
              Paragraph("Relatório profissional de inspeção veicular", ss["Sm"]), Spacer(1, 8)]
    t = Table([[numero, f"Status: {c.get('status', '')}", f"Data: {c.get('finalizado_em') or c.get('criado_em', '')}"]],
              colWidths=[FRAME_W / 3.0] * 3)
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f1f5f9")),
                           ("BOX", (0, 0), (-1, -1), .5, colors.HexColor("#cbd5e1")),
                           ("FONTSIZE", (0, 0), (-1, -1), 8), ("PADDING", (0, 0), (-1, -1), 7)]))
    story.append(t)

    # ---- 1. veículo (horizontal; sem "Tipo" e sem "Chassi")
    ident = [("ANO", up(v.get("ano", ""))), ("MARCA", up(v.get("marca", ""))), ("MODELO", up(v.get("modelo", ""))),
             ("COR", up(v.get("cor", ""))), ("PLACA", up(v.get("placa", "")))]
    story.append(KeepTogether([Paragraph("1. IDENTIFICAÇÃO DO VEÍCULO", ss["Sec"]), kv_grid(ss, ident)]))

    # ---- 2. combustível
    story.append(KeepTogether([
        Paragraph("2. Combustível", ss["Sec"]),
        Paragraph(f'Tipo: {esc(f.get("tipo", ""))} &nbsp; Nível: {esc(f.get("nivel", ""))} &nbsp; '
                  f'Percentual: {esc(f.get("percentual", ""))}%', ss["Sm"])]))

    # ---- 3. acessórios (duas colunas)
    acc = [(n, it.get("status", ""), it.get("obs", "")) for n, it in c.get("acessorios", {}).items() if it.get("status")]
    block = [Paragraph("3. Acessórios", ss["Sec"])]
    block.append(accessories_table(ss, acc) if acc else Paragraph("Nenhum acessório avaliado.", ss["Sm"]))
    story.append(KeepTogether(block))

    # ---- 4. pneus (2 por linha, todos na mesma folha quando couber)
    tire_labels = labels.get("tires", {})
    tires = [(tire_labels.get(k, k), it.get("estado", ""), up(it.get("marca", ""))) for k, it in c.get("pneus", {}).items()]
    story.append(KeepTogether([Paragraph("4. PNEUS", ss["Sec"]), tires_grid(ss, tires) if tires else Paragraph("—", ss["Sm"])]))

    # ---- 5. AVARIAS → 7. PROPRIETÁRIO + 8. EMPRESA / EMITENTE → texto de responsabilidade
    # Os três formam UM bloco (KeepTogether) para ficarem juntos na mesma página (a 2ª).
    # O desenho das avarias ocupa o espaço que sobrar: mede-se a altura de todo o resto e o
    # desenho recebe o que couber, entre DIAGRAM_MIN_H e DIAGRAM_MAX_H (proporção preservada).
    av = c.get("avarias", {})
    diag = av.get("diagrama") or "sedan"
    diag_name = labels.get("diagrams", {}).get(diag, diag.replace("_", " ").title())
    avarias_head = [Paragraph("5. AVARIAS", ss["Sec"]),
                    Paragraph(f"Desenho utilizado: {esc(diag_name)}", ss["SmTight"])]
    avarias_marks = [Paragraph(f"• {esc(desc)}", ss["SmTight"]) for desc in real_marks(av)]

    # ---- 7. proprietário + 8. emitente (lado a lado; assinaturas abaixo, na mesma linha)
    p = c.get("proprietario", {})
    e = c.get("emitente", {})

    prop_info = [
        Paragraph("7. PROPRIETÁRIO", ss["Sec"]),
        Paragraph(
            f'Nome: {esc(p.get("nome", ""))}<br/>'
            f'CPF: {esc(p.get("cpf", ""))}<br/>'
            f'Telefone: {esc(p.get("telefone", ""))}',
            ss["SmTight"]
        )
    ]
    emit_info = [
        Paragraph("8. EMPRESA / EMITENTE", ss["Sec"]),
        Paragraph(
            f'Empresa: {esc(e.get("empresa", ""))}<br/>'
            f'Documento: {esc(e.get("documento", ""))}<br/>'
            f'Telefone: {esc(e.get("telefone", ""))}<br/>'
            f'E-mail: {esc(e.get("email", ""))}<br/>'
            f'Endereço: {esc(e.get("endereco", ""))}<br/>'
            f'Responsável: {esc(e.get("responsavel", ""))}',
            ss["SmTight"]
        )
    ]

    def signature_or_blank(data, who):
        if not data.get("assinatura"):
            return ""
        try:
            img = fit_image(data["assinatura"], SIGNATURE_MAX_W, SIGNATURE_MAX_H)
            remota = data.get("assinatura_remota") or {}
            dh = str(remota.get("data_hora") or "")
            if len(dh) >= 16:      # assinada pelo link, no celular do cliente ("AAAA-MM-DD HH:MM:SS")
                quando = f"{dh[8:10]}/{dh[5:7]}/{dh[:4]} às {dh[11:16]}"
                return [img, Paragraph(f"Assinado eletronicamente à distância em {quando}", ss["SmTight"])]
            return img
        except Exception as exc:
            warnings.append(f"Assinatura do {who}: {type(exc).__name__}: {exc}")
            return ""

    sig_prop = signature_or_blank(p, "proprietário")
    sig_emit = signature_or_blank(e, "emitente")

    # Linha 1: dados de cada um. Linha 2: assinaturas (alinhadas entre si, cada uma sob o seu dono).
    # A largura da tabela = largura útil do Frame, para alinhar à esquerda com os títulos das seções.
    inner_w = FRAME_W - 2 * FRAME_PAD
    col_w = (inner_w - SIGN_GAP) / 2
    sign_rows = [[prop_info, "", emit_info]]
    if sig_prop or sig_emit:
        sign_rows.append([sig_prop, "", sig_emit])
    sign_table = Table(sign_rows, colWidths=[col_w, SIGN_GAP, col_w])
    sign_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 1), (-1, -1), 4),      # respiro entre os dados e a assinatura
    ]))

    # ---- Texto de responsabilidade / declaração (logo abaixo das assinaturas)
    responsabilidade_texto = """
    <b>É DE RESPONSABILIDADE DO CLIENTE OBJETOS OU PERTENCES NO INTERIOR DO VEÍCULO E, POSSÍVEIS AVARIAS OCASIONADAS POR ELES.</b>
    <br/><br/>
    A empresa não inspecionará e não se responsabilizará pelos seguintes itens, uma vez que os mesmos não são ocasionados por falha no transporte do auto: Descarga de bateria total ou parcial; mau funcionamento de Vidros e travas elétricas, Luzes e faróis; suspensão, molas, Escapamento, catalisadores, compressores; Mangueiras furadas ou ressecadas; Peças plásticas, Frisos, Aerofólios, Placas, Acessórios, Adesivos que venham a soltar/voar; O não funcionamento em decorrência do travamento do motor, Decodificação de chaves de ignição; Toda e qualquer falha mecânica e/ou elétrica; Objetos ou pertences no interior do auto, bem como possíveis avarias ocasionadas por eles; Veículo poderá chegar interna e externamente sujo.
    <br/><br/>
    <b>DECLARO QUE ACOMPANHEI, CONFERI E ESTOU DE ACORDO COM TODAS AS ANOTAÇÕES CONSTANTES NESTE LAUDO.</b>
    """
    # Mesmo texto, em parágrafos com espaçamento curto (em vez de linhas em branco de <br/><br/>).
    resp_paragraphs = [Paragraph(part.strip(), ss["Resp"])
                       for part in responsabilidade_texto.split("<br/><br/>") if part.strip()]
    after_marks = [Spacer(1, 6), sign_table, Spacer(1, 8)] + resp_paragraphs

    # ---- desenho das avarias: recebe a altura que sobra na página depois do resto do bloco
    usable_h = A4[1] - doc.topMargin - doc.bottomMargin - 2 * FRAME_PAD
    other_h = _stack_height(avarias_head + avarias_marks + after_marks, inner_w) + 4   # +4 = Spacer acima do desenho
    diag_max_h = max(DIAGRAM_MIN_H, min(DIAGRAM_MAX_H, usable_h - other_h - FIT_SAFETY))

    diagram = []
    if av.get("imagem"):
        try:
            diagram = [Spacer(1, 4), fit_image(av["imagem"], DIAGRAM_MAX_W, diag_max_h)]
        except Exception as exc:
            diagram = [Paragraph("(desenho das avarias não pôde ser incluído no PDF)", ss["Warn"])]
            warnings.append(f"Desenho das avarias: {type(exc).__name__}: {exc}")

    story.append(KeepTogether(avarias_head + diagram + avarias_marks + after_marks))

    # ---- 8. TODAS AS FOTOS (parte final do PDF)
    photo_labels = labels.get("photos", {})
    fotos = c.get("fotos", {}) or {}
    ordered = [k for k in photo_labels if fotos.get(k)] + [k for k in fotos if k not in photo_labels and fotos.get(k)]
    std = [(photo_labels.get(k, k.replace("_", " ").title()), fotos[k], "") for k in ordered]

    keydoc_labels = labels.get("keydoc", {})
    kd = c.get("chave_documentos", {}) or {}
    kd_items = []
    for k, val in kd.items():  # todas as fotos da etapa (inclui formato antigo: chave/reserva/manual/documento)
        if val and isinstance(val, str):
            kd_items.append((("Foto" if k == "foto" else keydoc_labels.get(k, k.replace("_", " ").title())), val, ""))
    kd_items.sort(key=lambda x: 0 if x[0] == "Foto" else 1)
    kd_obs = str(c.get("chave_documentos_obs", "") or "").strip()
    kd_extra = [Paragraph(f"<b>Observação:</b> {esc(kd_obs)}", ss["Sm"])] if kd_obs else []

    extras = [(f"Acessório {i}", b64, "") for i, b64 in enumerate(c.get("fotos_acessorios", []) or [], 1) if b64]
    avaria_fotos = [(f"AVARIA {i}", it.get("foto"), it.get("descricao", ""))
                    for i, it in enumerate(av.get("fotos", []) or [], 1) if it and it.get("foto")]

    pending = [Paragraph("TODAS AS FOTOS", ss["Sec"])]
    groups = (("Fotos do veículo", std, []), ("Chave e Documentos", kd_items, kd_extra),
              ("Fotos de acessórios", extras, []), ("Fotos das avarias", avaria_fotos, []))
    for sub, group, extra in groups:
        if not group and not extra:
            continue
        head = pending + [Paragraph(sub, ss["SubSec"])] + extra
        if group:
            story += photo_block(ss, head, group, warnings)
        else:                       # só observação, sem foto
            story.append(KeepTogether(head))
        pending = []
    if pending:  # nenhuma foto: só uma linha, sem espaço vazio
        story.append(KeepTogether(pending + [Paragraph("Nenhuma foto registrada.", ss["Sm"])]))

    doc.build(story, onFirstPage=_footer(numero, logo=True), onLaterPages=_footer(numero))
    return buf.getvalue()