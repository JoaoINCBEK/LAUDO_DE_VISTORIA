"""Migração NÃO destrutiva dos arquivos JSON antigos para o banco.

O que acontece (uma única vez, controlado pela chave 'legado_importado'):
1. Copia users.json, inspections.json, emitentes.json e sessions.json para
   autocheck_data/backup_legado_<data>/ (os originais NÃO são alterados nem apagados).
2. Cria a empresa inicial com os dados do 1º emitente salvo (ou "Empresa principal"),
   sem limites de plano (o Super Admin define depois).
3. Usuários: perfil "Administrador" -> admin da empresa; demais -> vistoriador.
   As senhas continuam as mesmas (o hash antigo é aceito e atualizado no 1º login).
   Quem ainda usa a senha de demonstração (admin123 / inspetor123) é obrigado a trocá-la.
4. Vistorias concluídas -> tabela vistorias (+ clientes, veículos e laudos). Os PDFs
   já arquivados continuam em autocheck_data/pdfs/ e são apenas referenciados.
5. Emitentes salvos -> emitentes da empresa inicial.

Não migrado: sessões antigas (todos entram de novo) e rascunhos de sessões antigas
(arquivos em drafts/ ficam intactos).
"""
import hashlib
import json
import re
import shutil

from . import db, seguranca
from .servicos import _upsert_cliente, _upsert_veiculo, _resumo_dados, registrar

CHAVE = "legado_importado"
SENHAS_DEMO = {hashlib.sha256(s.encode()).hexdigest() for s in ("admin123", "inspetor123")}


def _ler(path, padrao):
    if not path.exists():
        return padrao
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return padrao


def importar_legado():
    """Executa a migração se ainda não foi feita. Devolve um resumo (dict) ou None."""
    db.inicializar()
    if db.meta_get(CHAVE):
        return None
    base = db.data_dir()
    users = _ler(base / "users.json", [])
    inspections = _ler(base / "inspections.json", [])
    emitentes = _ler(base / "emitentes.json", [])
    if not users and not inspections and not emitentes:
        db.meta_set(CHAVE, "sem_dados_" + db.agora())
        return None

    # 1) cópia de segurança
    pasta = base / ("backup_legado_" + db.agora().replace(":", "").replace(" ", "_").replace("-", ""))
    pasta.mkdir(parents=True, exist_ok=True)
    for nome in ("users.json", "inspections.json", "emitentes.json", "sessions.json"):
        if (base / nome).exists():
            shutil.copy2(base / nome, pasta / nome)

    agora = db.agora()
    resumo = {"empresa": "", "usuarios": 0, "vistorias": 0, "emitentes": 0, "backup": str(pasta)}
    with db.conectar() as con:
        # 2) empresa inicial
        e0 = emitentes[0] if emitentes else {}
        nome_emp = (e0.get("empresa") or "").strip() or "Empresa principal"
        emp = con.execute(
            "INSERT INTO empresas(nome, cnpj, responsavel, email, telefone, endereco, limite_vistorias, limite_usuarios, "
            "data_inicio, status, created_at, updated_at) VALUES (?,?,?,?,?,?,NULL,NULL,?,'ativa',?,?)",
            (nome_emp, e0.get("documento", ""), e0.get("responsavel", ""), e0.get("email", ""), e0.get("telefone", ""),
             e0.get("endereco", ""), db.hoje(), agora, agora)).lastrowid
        resumo["empresa"] = nome_emp

        # 3) usuários
        por_nome = {}
        for u in users:
            login = str(u.get("usuario", "")).strip()
            if not login or con.execute("SELECT 1 FROM usuarios WHERE login = ?", (login,)).fetchone():
                continue
            perfil = "admin" if u.get("perfil") == "Administrador" else "vistoriador"
            senha = str(u.get("senha", ""))
            uid = con.execute(
                "INSERT INTO usuarios(empresa_id, login, nome, email, senha_hash, perfil, status, trocar_senha, "
                "created_at, updated_at) VALUES (?,?,?,'',?,?,?,?,?,?)",
                (emp, login, u.get("nome") or login, senha, perfil, "ativo" if u.get("ativo", True) else "inativo",
                 1 if senha.lower() in SENHAS_DEMO else 0, agora, agora)).lastrowid
            por_nome.setdefault((u.get("nome") or "").strip().lower(), []).append(uid)
            resumo["usuarios"] += 1

        # 4) vistorias concluídas + clientes + veículos + laudos
        for it in inspections:
            numero = str(it.get("numero", "")).strip()
            if not numero or con.execute("SELECT 1 FROM vistorias WHERE empresa_id = ? AND numero = ?",
                                         (emp, numero)).fetchone():
                continue
            ini = db.iso_de_br(it.get("criado_em")) or agora
            fim = db.iso_de_br(it.get("finalizado_em")) or ini
            nome_insp = (it.get("inspetor") or "").strip()
            ids = por_nome.get(nome_insp.lower(), [])
            uid = ids[0] if len(ids) == 1 else None
            res = _resumo_dados(it)
            cli = _upsert_cliente(con, emp, it.get("proprietario", {}) or {}, fim)
            vei = _upsert_veiculo(con, emp, it.get("veiculo", {}) or {}, res["tipo_veiculo"], cli, fim)
            vid = con.execute(
                "INSERT INTO vistorias(empresa_id, numero, usuario_id, vistoriador_nome, veiculo_id, cliente_id, placa, "
                "veiculo_desc, tipo_veiculo, status, consumiu_plano, data_inicio, data_conclusao, dados, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,'concluida',1,?,?,?,?,?)",
                (emp, numero, uid, nome_insp, vei, cli, res["placa"], res["veiculo_desc"], res["tipo_veiculo"],
                 ini, fim, json.dumps(it, ensure_ascii=False), ini, fim)).lastrowid
            arq = "pdfs/" + re.sub(r"[^A-Za-z0-9_-]", "_", numero) + ".pdf"
            con.execute("INSERT INTO laudos(empresa_id, vistoria_id, veiculo_id, cliente_id, usuario_id, numero, data, "
                        "arquivo_pdf, status, created_at) VALUES (?,?,?,?,?,?,?,?,'emitido',?)",
                        (emp, vid, vei, cli, uid, numero, fim, arq, fim))
            resumo["vistorias"] += 1
        con.execute("UPDATE empresas SET vistorias_utilizadas = ? WHERE id = ?", (resumo["vistorias"], emp))

        # 5) emitentes salvos
        for e in emitentes:
            if not (e.get("empresa") or "").strip():
                continue
            con.execute("INSERT INTO emitentes(empresa_id, empresa, documento, telefone, email, endereco, responsavel, "
                        "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                        (emp, e.get("empresa", "").strip(), e.get("documento", ""), e.get("telefone", ""),
                         e.get("email", ""), e.get("endereco", ""), e.get("responsavel", ""), agora, agora))
            resumo["emitentes"] += 1

        registrar(None, "migracao", f"Dados antigos importados: {resumo['usuarios']} usuário(s), "
                  f"{resumo['vistorias']} vistoria(s), {resumo['emitentes']} emitente(s). Backup: {pasta.name}",
                  empresa_id=emp, con=con)
        con.execute("INSERT INTO meta(chave, valor) VALUES (?, ?) ON CONFLICT(chave) DO UPDATE SET valor = excluded.valor",
                    (CHAVE, agora))
    return resumo


def garantir_super_admin(login, nome, senha):
    """Cria o Super Admin se ainda não existir (usado pelos Secrets e pelo gerenciar.py).
    Nunca altera a senha de um usuário existente."""
    db.inicializar()
    login = (login or "").strip()
    if not login or not senha:
        return False
    msg = seguranca.problema_senha(senha)
    if msg:
        raise ValueError(msg)
    agora = db.agora()
    with db.conectar() as con:
        if con.execute("SELECT 1 FROM usuarios WHERE login = ?", (login,)).fetchone():
            return False
        con.execute("INSERT INTO usuarios(empresa_id, login, nome, email, senha_hash, perfil, status, trocar_senha, "
                    "created_at, updated_at) VALUES (NULL,?,?,'',?,'super_admin','ativo',0,?,?)",
                    (login, nome or "Super Administrador", seguranca.hash_senha(senha), agora, agora))
        registrar(None, "super_admin_criado", f"Super Admin {login} criado", con=con)
    return True
