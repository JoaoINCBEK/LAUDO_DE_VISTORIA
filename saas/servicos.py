"""Regras de negócio com isolamento por empresa.

REGRA DE OURO: toda função recebe o `ator` (usuário autenticado, montado a partir
da sessão validada no banco) e deriva a empresa DELE. Um `empresa_id` vindo da
tela só é aceito para o Super Admin; para os demais perfis, qualquer valor
diferente da própria empresa gera AcessoNegado. Registros buscados por id são
sempre filtrados também por empresa (e, para o vistoriador, pelo próprio usuário).
"""
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from . import db, seguranca, rbac
from .rbac import AcessoNegado, SUPER_ADMIN, VISTORIADOR, exigir, pode

STATUS_VISTORIA = {
    "em_andamento": "Em andamento",
    "pendente": "Pendente",
    "concluida": "Concluída",
    "cancelada": "Cancelada",
}
STATUS_EMPRESA = {"ativa": "Ativa", "bloqueada": "Bloqueada", "inativa": "Inativa"}
STATUS_USUARIO = {"ativo": "Ativo", "inativo": "Inativo"}
ALERTA_CONSUMO = 0.8        # empresa "próxima do limite" a partir de 80% do plano
ALERTA_VENCIMENTO_DIAS = 15


class ErroNegocio(Exception):
    """Regra de negócio não atendida (mensagem pronta para o usuário)."""


@dataclass(frozen=True)
class Ator:
    id: int
    empresa_id: object      # None para super_admin
    perfil: str
    nome: str
    login: str
    ip: str = ""

    @property
    def super(self):
        return self.perfil == SUPER_ADMIN


def _ator(row, ip=""):
    return Ator(row["id"], row["empresa_id"], row["perfil"], row["nome"], row["login"], ip or "")


def _rows(cur):
    return [dict(r) for r in cur.fetchall()]


def _escopo(ator, empresa_id=None):
    """Empresa efetiva da operação. Super Admin: a informada (None = todas).
    Demais perfis: SEMPRE a própria; pedir outra é tentativa de acesso indevido."""
    if ator is None:
        raise AcessoNegado("Sessão expirada. Entre novamente.")
    if ator.super:
        return int(empresa_id) if empresa_id not in (None, "") else None
    if empresa_id not in (None, "") and int(empresa_id) != int(ator.empresa_id):
        raise AcessoNegado("Acesso negado aos dados de outra empresa.")
    return int(ator.empresa_id)


def _filtro_empresa(alias, emp, params):
    if emp is None:
        return ""
    params.append(emp)
    return f" AND {alias}.empresa_id = ?"


def _so_digitos(txt):
    return re.sub(r"\D", "", str(txt or ""))


def _placa(txt):
    return re.sub(r"[^A-Za-z0-9]", "", str(txt or "")).upper()


# ---------------------------------------------------------------------------
# Logs / auditoria
# ---------------------------------------------------------------------------
def registrar(ator, acao, descricao="", vistoria_id=None, empresa_id=None, con=None):
    """Grava uma ação na auditoria. Nunca interrompe a operação principal."""
    emp = empresa_id if empresa_id is not None else (ator.empresa_id if ator else None)
    vals = (emp, ator.id if ator else None, ator.nome if ator else "", acao, descricao or "",
            vistoria_id, db.agora(), ator.ip if ator else "")
    sql = ("INSERT INTO logs(empresa_id, usuario_id, usuario_nome, acao, descricao, vistoria_id, data_hora, ip) "
           "VALUES (?,?,?,?,?,?,?,?)")
    try:
        if con is not None:
            con.execute(sql, vals)
        else:
            with db.conectar() as c2:
                c2.execute(sql, vals)
    except Exception:
        pass


def listar_logs(ator, empresa_id=None, data_ini=None, data_fim=None, usuario_id=None, acao=None, limite=500):
    if ator.super:
        exigir(ator, "logs.ver_global")
    else:
        exigir(ator, "logs.ver_empresa")
    emp = _escopo(ator, empresa_id)
    params = []
    sql = ("SELECT l.*, e.nome AS empresa_nome FROM logs l LEFT JOIN empresas e ON e.id = l.empresa_id "
           "WHERE 1=1" + _filtro_empresa("l", emp, params))
    if data_ini:
        sql += " AND l.data_hora >= ?"; params.append(f"{data_ini} 00:00:00")
    if data_fim:
        sql += " AND l.data_hora <= ?"; params.append(f"{data_fim} 23:59:59")
    if usuario_id:
        sql += " AND l.usuario_id = ?"; params.append(int(usuario_id))
    if acao:
        sql += " AND l.acao = ?"; params.append(acao)
    sql += " ORDER BY l.data_hora DESC, l.id DESC LIMIT ?"
    params.append(int(limite))
    with db.conectar() as con:
        return _rows(con.execute(sql, params))


# ---------------------------------------------------------------------------
# Autenticação e sessões
# ---------------------------------------------------------------------------
def _empresa_bloqueia_login(con, empresa_id):
    if empresa_id is None:
        return ""
    e = con.execute("SELECT status FROM empresas WHERE id = ?", (empresa_id,)).fetchone()
    if not e:
        return "Empresa não encontrada."
    if e["status"] == "bloqueada":
        return "O acesso da sua empresa está bloqueado. Fale com o suporte da plataforma."
    if e["status"] == "inativa":
        return "A conta da sua empresa está inativa. Fale com o suporte da plataforma."
    return ""


def autenticar(login, senha, ip=""):
    """Login por usuário OU e-mail. Devolve (Ator | None, mensagem_de_erro)."""
    login = (login or "").strip()
    if not login or not senha:
        return None, "Informe usuário e senha."
    erro_padrao = "Usuário ou senha inválidos."
    with db.conectar() as con:
        u = con.execute("SELECT * FROM usuarios WHERE lower(login) = lower(?) OR (email <> '' AND lower(email) = lower(?))",
                        (login, login)).fetchone()
        if not u:
            return None, erro_padrao
        agora = db.agora()
        if u["bloqueado_ate"] and u["bloqueado_ate"] > agora:
            return None, f"Muitas tentativas incorretas. Tente novamente após {db.br(u['bloqueado_ate'])}."
        ok, atualizar = seguranca.verificar_senha(senha, u["senha_hash"])
        if not ok:
            falhas = u["tentativas_falhas"] + 1
            bloqueio = None
            if falhas >= seguranca.MAX_TENTATIVAS:
                bloqueio = (db.agora_dt() + timedelta(minutes=seguranca.BLOQUEIO_MINUTOS)).strftime("%Y-%m-%d %H:%M:%S")
                falhas = 0
            con.execute("UPDATE usuarios SET tentativas_falhas = ?, bloqueado_ate = ? WHERE id = ?",
                        (falhas, bloqueio, u["id"]))
            registrar(Ator(u["id"], u["empresa_id"], u["perfil"], u["nome"], u["login"], ip),
                      "login_falhou", "Senha incorreta" + (" (usuário bloqueado temporariamente)" if bloqueio else ""), con=con)
            return None, erro_padrao
        if u["status"] != "ativo":
            return None, "Seu usuário está desativado. Fale com o administrador."
        msg = _empresa_bloqueia_login(con, u["empresa_id"])
        if msg:
            return None, msg
        novo_hash = seguranca.hash_senha(senha) if atualizar else u["senha_hash"]
        con.execute("UPDATE usuarios SET tentativas_falhas = 0, bloqueado_ate = NULL, ultimo_acesso = ?, "
                    "senha_hash = ? WHERE id = ?", (agora, novo_hash, u["id"]))
        if u["empresa_id"] is not None:
            con.execute("UPDATE empresas SET ultimo_acesso = ? WHERE id = ?", (agora, u["empresa_id"]))
        ator = _ator(u, ip)
        registrar(ator, "login", "Entrou no sistema", con=con)
        return ator, ""


def criar_sessao(ator):
    token = seguranca.novo_token()
    agora = db.agora()
    expira = (db.agora_dt() + timedelta(days=seguranca.SESSAO_DIAS)).strftime("%Y-%m-%d %H:%M:%S")
    with db.conectar() as con:
        con.execute("INSERT INTO sessoes(token_hash, usuario_id, empresa_id, created_at, expira_em, ultimo_uso, ip) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (seguranca.hash_token(token), ator.id, ator.empresa_id, agora, expira, agora, ator.ip))
    return token


def validar_sessao(token, ip=""):
    """Revalida a sessão A CADA acesso: token, validade, usuário ativo e empresa liberada."""
    if not token:
        return None
    th = seguranca.hash_token(token)
    with db.conectar() as con:
        s = con.execute("SELECT * FROM sessoes WHERE token_hash = ?", (th,)).fetchone()
        if not s or s["revogada"] or s["expira_em"] <= db.agora():
            return None
        u = con.execute("SELECT * FROM usuarios WHERE id = ?", (s["usuario_id"],)).fetchone()
        if not u or u["status"] != "ativo":
            return None
        if u["empresa_id"] != s["empresa_id"]:      # usuário mudou de empresa: sessão antiga não vale
            return None
        if _empresa_bloqueia_login(con, u["empresa_id"]):
            return None
        # atualiza "último uso" no máximo a cada 5 minutos (evita escrita a cada clique)
        limite = (db.agora_dt() - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
        if not s["ultimo_uso"] or s["ultimo_uso"] < limite:
            agora = db.agora()
            con.execute("UPDATE sessoes SET ultimo_uso = ? WHERE token_hash = ?", (agora, th))
            con.execute("UPDATE usuarios SET ultimo_acesso = ? WHERE id = ?", (agora, u["id"]))
            if u["empresa_id"] is not None:
                con.execute("UPDATE empresas SET ultimo_acesso = ? WHERE id = ?", (agora, u["empresa_id"]))
        return _ator(u, ip)


def precisa_trocar_senha(ator):
    with db.conectar() as con:
        r = con.execute("SELECT trocar_senha FROM usuarios WHERE id = ?", (ator.id,)).fetchone()
    return bool(r and r["trocar_senha"])


def encerrar_sessao(token, ator=None):
    with db.conectar() as con:
        con.execute("UPDATE sessoes SET revogada = 1 WHERE token_hash = ?", (seguranca.hash_token(token),))
        if ator:
            registrar(ator, "logout", "Saiu do sistema", con=con)


def _revogar_sessoes(con, usuario_id=None, empresa_id=None):
    if usuario_id is not None:
        con.execute("UPDATE sessoes SET revogada = 1 WHERE usuario_id = ?", (usuario_id,))
    if empresa_id is not None:
        con.execute("UPDATE sessoes SET revogada = 1 WHERE empresa_id = ?", (empresa_id,))


def trocar_senha(ator, senha_atual, nova, obrigatoria=False):
    """Troca a própria senha. Na troca obrigatória (senha provisória), a atual já foi
    conferida no login. Revoga as outras sessões do usuário."""
    msg = seguranca.problema_senha(nova)
    if msg:
        raise ErroNegocio(msg)
    with db.conectar() as con:
        u = con.execute("SELECT * FROM usuarios WHERE id = ?", (ator.id,)).fetchone()
        if not u:
            raise AcessoNegado("Usuário não encontrado.")
        if obrigatoria and not u["trocar_senha"]:
            raise AcessoNegado("Troca obrigatória não pendente.")
        if not obrigatoria and not seguranca.verificar_senha(senha_atual, u["senha_hash"])[0]:
            raise ErroNegocio("A senha atual não confere.")
        if seguranca.verificar_senha(nova, u["senha_hash"])[0]:
            raise ErroNegocio("A nova senha deve ser diferente da atual.")
        con.execute("UPDATE usuarios SET senha_hash = ?, trocar_senha = 0, updated_at = ? WHERE id = ?",
                    (seguranca.hash_senha(nova), db.agora(), ator.id))
        registrar(ator, "senha_alterada", "Alterou a própria senha", con=con)


# ---------------------------------------------------------------------------
# Planos
# ---------------------------------------------------------------------------
def listar_planos(ator, somente_ativos=False):
    if ator is None:
        raise AcessoNegado("Sessão expirada.")
    sql = ("SELECT p.*, (SELECT COUNT(*) FROM empresas e WHERE e.plano_id = p.id) AS empresas "
           "FROM planos p" + (" WHERE p.ativo = 1" if somente_ativos else "") + " ORDER BY p.nome")
    with db.conectar() as con:
        return _rows(con.execute(sql))


def _int_ou_none(v):
    if v in (None, "", 0, "0"):
        return None
    v = int(v)
    if v < 0:
        raise ErroNegocio("Limites não podem ser negativos.")
    return v


def salvar_plano(ator, dados, plano_id=None):
    exigir(ator, "plataforma.gerenciar")
    nome = (dados.get("nome") or "").strip()
    if not nome:
        raise ErroNegocio("Informe o nome do plano.")
    vals = (nome, (dados.get("descricao") or "").strip(), _int_ou_none(dados.get("limite_vistorias")),
            _int_ou_none(dados.get("limite_usuarios")), _int_ou_none(dados.get("duracao_meses")),
            1 if dados.get("ativo", True) else 0)
    agora = db.agora()
    with db.conectar() as con:
        try:
            if plano_id:
                con.execute("UPDATE planos SET nome=?, descricao=?, limite_vistorias=?, limite_usuarios=?, "
                            "duracao_meses=?, ativo=?, updated_at=? WHERE id=?", vals + (agora, int(plano_id)))
                registrar(ator, "plano_editado", f"Plano {nome}", con=con)
                return int(plano_id)
            cur = con.execute("INSERT INTO planos(nome, descricao, limite_vistorias, limite_usuarios, duracao_meses, "
                              "ativo, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)", vals + (agora, agora))
            registrar(ator, "plano_criado", f"Plano {nome}", con=con)
            return cur.lastrowid
        except Exception as exc:
            if db.eh_duplicado(exc):
                raise ErroNegocio("Já existe um plano com esse nome.")
            raise


# ---------------------------------------------------------------------------
# Empresas
# ---------------------------------------------------------------------------
_CAMPOS_EMPRESA = ("nome", "cnpj", "razao_social", "responsavel", "email", "telefone", "endereco")


def _consumo(con, emp_row):
    reservadas = con.execute(
        "SELECT COUNT(*) FROM vistorias WHERE empresa_id = ? AND status IN ('em_andamento','pendente') "
        "AND consumiu_plano = 0", (emp_row["id"],)).fetchone()[0]
    usuarios = con.execute("SELECT COUNT(*) FROM usuarios WHERE empresa_id = ? AND status = 'ativo'",
                           (emp_row["id"],)).fetchone()[0]
    lim = emp_row["limite_vistorias"]
    usadas = emp_row["vistorias_utilizadas"]
    venc = emp_row["data_vencimento"]
    vencida = bool(venc and venc < db.hoje())
    dias = None
    if venc:
        try:
            dias = (datetime.strptime(venc[:10], "%Y-%m-%d") - datetime.strptime(db.hoje(), "%Y-%m-%d")).days
        except ValueError:
            dias = None
    return {
        "vistorias_utilizadas": usadas,
        "limite_vistorias": lim,
        "reservadas": reservadas,
        "restantes": None if lim is None else max(0, lim - usadas),
        "percentual": None if not lim else min(1.0, usadas / lim),
        "usuarios_ativos": usuarios,
        "limite_usuarios": emp_row["limite_usuarios"],
        "vencida": vencida,
        "dias_para_vencer": dias,
        "status": emp_row["status"],
    }


def listar_empresas(ator):
    exigir(ator, "plataforma.gerenciar")
    with db.conectar() as con:
        empresas = _rows(con.execute(
            "SELECT e.*, p.nome AS plano_nome FROM empresas e LEFT JOIN planos p ON p.id = e.plano_id ORDER BY e.nome"))
        for e in empresas:
            e.update(_consumo(con, e))
    return empresas


def obter_empresa(ator, empresa_id=None):
    emp = _escopo(ator, empresa_id)
    if emp is None:
        raise ErroNegocio("Selecione uma empresa.")
    with db.conectar() as con:
        e = con.execute("SELECT e.*, p.nome AS plano_nome FROM empresas e LEFT JOIN planos p ON p.id = e.plano_id "
                        "WHERE e.id = ?", (emp,)).fetchone()
        if not e:
            raise ErroNegocio("Empresa não encontrada.")
        e = dict(e)
        e.update(_consumo(con, e))
        return e


def _validar_datas(dados):
    ini, venc = dados.get("data_inicio"), dados.get("data_vencimento")
    if ini and venc and str(venc) < str(ini):
        raise ErroNegocio("A data de vencimento não pode ser anterior à data de início.")


def criar_empresa(ator, dados):
    exigir(ator, "plataforma.gerenciar")
    if not (dados.get("nome") or "").strip():
        raise ErroNegocio("Informe o nome da empresa.")
    _validar_datas(dados)
    agora = db.agora()
    with db.conectar() as con:
        cur = con.execute(
            "INSERT INTO empresas(nome, cnpj, razao_social, responsavel, email, telefone, endereco, plano_id, "
            "limite_vistorias, limite_usuarios, data_inicio, data_vencimento, status, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'ativa',?,?)",
            tuple((dados.get(k) or "").strip() for k in _CAMPOS_EMPRESA)
            + (dados.get("plano_id") or None, _int_ou_none(dados.get("limite_vistorias")),
               _int_ou_none(dados.get("limite_usuarios")), dados.get("data_inicio") or db.hoje(),
               dados.get("data_vencimento") or None, agora, agora))
        eid = cur.lastrowid
        registrar(ator, "empresa_criada", f"Empresa {dados.get('nome').strip()} criada", empresa_id=eid, con=con)
        return eid


def atualizar_empresa(ator, empresa_id, dados):
    """Super Admin: edita tudo (dados, plano, limites, datas). Admin da empresa: só dados cadastrais."""
    emp = _escopo(ator, empresa_id)
    if emp is None:
        raise ErroNegocio("Selecione uma empresa.")
    if not (dados.get("nome") or "").strip():
        raise ErroNegocio("Informe o nome da empresa.")
    campos = {k: (dados.get(k) or "").strip() for k in _CAMPOS_EMPRESA}
    if ator.super:
        _validar_datas(dados)
        campos.update(plano_id=dados.get("plano_id") or None,
                      limite_vistorias=_int_ou_none(dados.get("limite_vistorias")),
                      limite_usuarios=_int_ou_none(dados.get("limite_usuarios")),
                      data_inicio=dados.get("data_inicio") or None,
                      data_vencimento=dados.get("data_vencimento") or None)
    else:
        exigir(ator, "empresa.configurar")
    campos["updated_at"] = db.agora()
    sets = ", ".join(f"{k} = ?" for k in campos)
    with db.conectar() as con:
        con.execute(f"UPDATE empresas SET {sets} WHERE id = ?", tuple(campos.values()) + (emp,))
        registrar(ator, "empresa_editada", "Dados/limites da empresa alterados", empresa_id=emp, con=con)


def definir_status_empresa(ator, empresa_id, status):
    exigir(ator, "plataforma.gerenciar")
    if status not in STATUS_EMPRESA:
        raise ErroNegocio("Status inválido.")
    with db.conectar() as con:
        e = con.execute("SELECT nome FROM empresas WHERE id = ?", (int(empresa_id),)).fetchone()
        if not e:
            raise ErroNegocio("Empresa não encontrada.")
        con.execute("UPDATE empresas SET status = ?, updated_at = ? WHERE id = ?", (status, db.agora(), int(empresa_id)))
        if status != "ativa":
            _revogar_sessoes(con, empresa_id=int(empresa_id))    # derruba quem estiver logado
        registrar(ator, "empresa_status", f"{e['nome']}: {STATUS_EMPRESA[status]}", empresa_id=int(empresa_id), con=con)


def ajustar_consumo(ator, empresa_id, vistorias_utilizadas):
    exigir(ator, "plataforma.gerenciar")
    v = int(vistorias_utilizadas)
    if v < 0:
        raise ErroNegocio("Valor inválido.")
    with db.conectar() as con:
        con.execute("UPDATE empresas SET vistorias_utilizadas = ?, updated_at = ? WHERE id = ?",
                    (v, db.agora(), int(empresa_id)))
        registrar(ator, "consumo_ajustado", f"Vistorias utilizadas ajustadas para {v}", empresa_id=int(empresa_id), con=con)


# ---------------------------------------------------------------------------
# Usuários
# ---------------------------------------------------------------------------
def _usuario_no_escopo(con, ator, usuario_id):
    u = con.execute("SELECT * FROM usuarios WHERE id = ?", (int(usuario_id),)).fetchone()
    if not u:
        raise ErroNegocio("Usuário não encontrado.")
    if ator.super:
        exigir(ator, "usuarios.gerenciar_todos")
        return u
    exigir(ator, "usuarios.gerenciar")
    if u["empresa_id"] != ator.empresa_id or u["perfil"] == SUPER_ADMIN:
        raise AcessoNegado("Acesso negado a usuário de outra empresa.")
    return u


def _checar_limite_usuarios(con, empresa_id):
    e = con.execute("SELECT limite_usuarios FROM empresas WHERE id = ?", (empresa_id,)).fetchone()
    if not e:
        raise ErroNegocio("Empresa não encontrada.")
    if e["limite_usuarios"] is not None:
        ativos = con.execute("SELECT COUNT(*) FROM usuarios WHERE empresa_id = ? AND status = 'ativo'",
                             (empresa_id,)).fetchone()[0]
        if ativos >= e["limite_usuarios"]:
            raise ErroNegocio(f"Limite de usuários do plano atingido ({ativos} / {e['limite_usuarios']}).")


def listar_usuarios(ator, empresa_id=None):
    if ator.super:
        exigir(ator, "usuarios.gerenciar_todos")
    else:
        exigir(ator, "usuarios.gerenciar")
    emp = _escopo(ator, empresa_id)
    params = []
    sql = ("SELECT u.id, u.empresa_id, u.login, u.nome, u.email, u.perfil, u.status, u.trocar_senha, "
           "u.ultimo_acesso, u.created_at, e.nome AS empresa_nome, "
           "(SELECT COUNT(*) FROM vistorias v WHERE v.usuario_id = u.id AND v.status = 'concluida') AS vistorias "
           "FROM usuarios u LEFT JOIN empresas e ON e.id = u.empresa_id WHERE 1=1"
           + _filtro_empresa("u", emp, params))
    if not ator.super:
        sql += " AND u.perfil <> 'super_admin'"
    sql += " ORDER BY e.nome, u.nome"
    with db.conectar() as con:
        return _rows(con.execute(sql, params))


def _login_valido(login):
    if not re.fullmatch(r"[A-Za-z0-9._@-]{3,60}", login or ""):
        raise ErroNegocio("Usuário deve ter 3 a 60 caracteres (letras, números, ponto, hífen, _ ou @), sem espaços.")


def criar_usuario(ator, dados, empresa_id=None):
    """Cria usuário. Devolve (id, senha_provisoria). O usuário troca a senha no 1º acesso."""
    perfil = dados.get("perfil") or VISTORIADOR
    if perfil == SUPER_ADMIN:
        exigir(ator, "plataforma.gerenciar")
        emp = None
    else:
        if ator.super:
            exigir(ator, "usuarios.gerenciar_todos")
        else:
            exigir(ator, "usuarios.gerenciar")
        emp = _escopo(ator, empresa_id)
        if emp is None:
            raise ErroNegocio("Selecione a empresa do usuário.")
        if perfil not in rbac.PERFIS_DA_EMPRESA:
            raise AcessoNegado("Perfil não permitido.")
    nome = (dados.get("nome") or "").strip()
    email = (dados.get("email") or "").strip().lower()
    login = (dados.get("login") or email).strip()
    if not nome:
        raise ErroNegocio("Informe o nome.")
    if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise ErroNegocio("E-mail inválido.")
    _login_valido(login)
    senha = dados.get("senha") or seguranca.senha_temporaria()
    msg = seguranca.problema_senha(senha)
    if msg:
        raise ErroNegocio(msg)
    agora = db.agora()
    with db.conectar() as con:
        if emp is not None:
            _checar_limite_usuarios(con, emp)
        if con.execute("SELECT 1 FROM usuarios WHERE lower(login) = lower(?)", (login,)).fetchone():
            raise ErroNegocio("Esse usuário já existe.")
        if email and con.execute("SELECT 1 FROM usuarios WHERE lower(email) = lower(?)", (email,)).fetchone():
            raise ErroNegocio("Esse e-mail já está em uso.")
        cur = con.execute(
            "INSERT INTO usuarios(empresa_id, login, nome, email, senha_hash, perfil, status, trocar_senha, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,'ativo',1,?,?)",
            (emp, login, nome, email, seguranca.hash_senha(senha), perfil, agora, agora))
        registrar(ator, "usuario_criado", f"{nome} ({login}) — {rbac.nome_perfil(perfil)}", empresa_id=emp, con=con)
        return cur.lastrowid, senha


def atualizar_usuario(ator, usuario_id, dados):
    with db.conectar() as con:
        u = _usuario_no_escopo(con, ator, usuario_id)
        nome = (dados.get("nome") or "").strip()
        email = (dados.get("email") or "").strip().lower()
        perfil = dados.get("perfil") or u["perfil"]
        if not nome:
            raise ErroNegocio("Informe o nome.")
        if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            raise ErroNegocio("E-mail inválido.")
        if (perfil == SUPER_ADMIN) != (u["perfil"] == SUPER_ADMIN):
            raise AcessoNegado("Não é possível mover usuários entre plataforma e empresa.")
        if u["perfil"] != SUPER_ADMIN and perfil not in rbac.PERFIS_DA_EMPRESA:
            raise AcessoNegado("Perfil não permitido.")
        if u["id"] == ator.id and perfil != u["perfil"]:
            raise ErroNegocio("Você não pode alterar o seu próprio perfil.")
        if email and con.execute("SELECT 1 FROM usuarios WHERE lower(email) = lower(?) AND id <> ?", (email, u["id"])).fetchone():
            raise ErroNegocio("Esse e-mail já está em uso.")
        con.execute("UPDATE usuarios SET nome = ?, email = ?, perfil = ?, updated_at = ? WHERE id = ?",
                    (nome, email, perfil, db.agora(), u["id"]))
        if perfil != u["perfil"]:
            _revogar_sessoes(con, usuario_id=u["id"])
        registrar(ator, "usuario_editado", f"{nome} ({u['login']})", empresa_id=u["empresa_id"], con=con)


def definir_status_usuario(ator, usuario_id, status):
    if status not in STATUS_USUARIO:
        raise ErroNegocio("Status inválido.")
    with db.conectar() as con:
        u = _usuario_no_escopo(con, ator, usuario_id)
        if u["id"] == ator.id:
            raise ErroNegocio("Você não pode desativar o seu próprio usuário.")
        if status == "ativo" and u["status"] != "ativo" and u["empresa_id"] is not None:
            _checar_limite_usuarios(con, u["empresa_id"])
        con.execute("UPDATE usuarios SET status = ?, updated_at = ? WHERE id = ?", (status, db.agora(), u["id"]))
        if status != "ativo":
            _revogar_sessoes(con, usuario_id=u["id"])
        registrar(ator, "usuario_status", f"{u['nome']}: {STATUS_USUARIO[status]}", empresa_id=u["empresa_id"], con=con)


def redefinir_acesso(ator, usuario_id):
    """Gera senha provisória (mostrada uma única vez), exige troca no próximo login
    e encerra as sessões abertas do usuário."""
    senha = seguranca.senha_temporaria()
    with db.conectar() as con:
        u = _usuario_no_escopo(con, ator, usuario_id)
        con.execute("UPDATE usuarios SET senha_hash = ?, trocar_senha = 1, tentativas_falhas = 0, "
                    "bloqueado_ate = NULL, updated_at = ? WHERE id = ?",
                    (seguranca.hash_senha(senha), db.agora(), u["id"]))
        _revogar_sessoes(con, usuario_id=u["id"])
        registrar(ator, "acesso_redefinido", f"{u['nome']} ({u['login']})", empresa_id=u["empresa_id"], con=con)
    return senha


def vistoriadores(ator, empresa_id=None):
    """Lista simples (id, nome) para filtros."""
    if not (pode(ator, "vistorias.ver_todas")):
        return [{"id": ator.id, "nome": ator.nome}]
    emp = _escopo(ator, empresa_id)
    params = []
    sql = "SELECT id, nome FROM usuarios u WHERE perfil <> 'super_admin'" + _filtro_empresa("u", emp, params) + " ORDER BY nome"
    with db.conectar() as con:
        return _rows(con.execute(sql, params))


# ---------------------------------------------------------------------------
# Vistorias
# ---------------------------------------------------------------------------
def _resumo_dados(dados):
    v = (dados or {}).get("veiculo", {}) or {}
    av = (dados or {}).get("avarias", {}) or {}
    return {
        "placa": _placa(v.get("placa")),
        "veiculo_desc": f"{v.get('marca', '') or ''} {v.get('modelo', '') or ''}".strip(),
        "tipo_veiculo": av.get("diagrama") or "",
    }


def _vistoria_no_escopo(con, ator, vistoria_id):
    """Busca a vistoria garantindo empresa e (para vistoriador) autoria."""
    vid = int(vistoria_id)
    if ator.super:
        exigir(ator, "vistorias.ver_todas")
        r = con.execute("SELECT * FROM vistorias WHERE id = ?", (vid,)).fetchone()
    else:
        r = con.execute("SELECT * FROM vistorias WHERE id = ? AND empresa_id = ?", (vid, ator.empresa_id)).fetchone()
        if r and not pode(ator, "vistorias.ver_todas"):
            exigir(ator, "vistorias.ver_proprias")
            if r["usuario_id"] != ator.id:
                r = None
    if not r:
        raise AcessoNegado("Vistoria não encontrada ou sem permissão de acesso.")
    return r


def pode_iniciar_vistoria(ator):
    """(ok, mensagem). Verifica permissão, situação da empresa e limite do plano."""
    if not pode(ator, "vistorias.criar"):
        return False, "Seu perfil não realiza vistorias."
    with db.conectar() as con:
        e = con.execute("SELECT * FROM empresas WHERE id = ?", (ator.empresa_id,)).fetchone()
        if not e or e["status"] != "ativa":
            return False, "A conta da empresa não está ativa."
        c = _consumo(con, e)
    if c["vencida"]:
        return False, "O plano da empresa está vencido. Fale com o suporte para renovar."
    if c["limite_vistorias"] is not None and c["vistorias_utilizadas"] + c["reservadas"] >= c["limite_vistorias"]:
        extra = f" ({c['reservadas']} em andamento)" if c["reservadas"] else ""
        return False, (f"Limite de vistorias do plano atingido: {c['vistorias_utilizadas']} / "
                       f"{c['limite_vistorias']}{extra}. Fale com o administrador.")
    return True, ""


def _proximo_numero(con, empresa_id):
    ano = db.hoje()[:4]
    maior = 0
    for (numero,) in con.execute("SELECT numero FROM vistorias WHERE empresa_id = ? AND numero LIKE ?",
                                 (empresa_id, f"CHK-{ano}-%")):
        m = re.fullmatch(rf"CHK-{ano}-(\d+)", numero or "")
        if m:
            maior = max(maior, int(m.group(1)))
    return f"CHK-{ano}-{maior + 1:06d}"


def iniciar_vistoria(ator, dados):
    """Registra uma nova vistoria (status em_andamento) e devolve (id, numero).
    Vistorias em andamento deixadas para trás pelo mesmo usuário passam a 'pendente'."""
    exigir(ator, "vistorias.criar")
    ok, msg = pode_iniciar_vistoria(ator)
    if not ok:
        raise ErroNegocio(msg)
    agora = db.agora()
    res = _resumo_dados(dados)
    for _ in range(5):
        try:
            with db.conectar() as con:
                con.execute("UPDATE vistorias SET status = 'pendente', updated_at = ? WHERE empresa_id = ? "
                            "AND usuario_id = ? AND status = 'em_andamento'", (agora, ator.empresa_id, ator.id))
                numero = _proximo_numero(con, ator.empresa_id)
                d = dict(dados or {})
                d["numero"] = numero
                cur = con.execute(
                    "INSERT INTO vistorias(empresa_id, numero, usuario_id, vistoriador_nome, placa, veiculo_desc, "
                    "tipo_veiculo, status, data_inicio, dados, created_at, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,'em_andamento',?,?,?,?)",
                    (ator.empresa_id, numero, ator.id, ator.nome, res["placa"], res["veiculo_desc"],
                     res["tipo_veiculo"], agora, json.dumps(d, ensure_ascii=False), agora, agora))
                vid = cur.lastrowid
                registrar(ator, "vistoria_iniciada", f"{ator.nome} iniciou a vistoria {numero}", vistoria_id=vid, con=con)
                return vid, numero
        except Exception as exc:
            if not db.eh_duplicado(exc):
                raise
    raise ErroNegocio("Não foi possível numerar a vistoria. Tente novamente.")


def _pode_editar(ator, r):
    """Vistoriador: as próprias vistorias (inclusive finalizar de novo após corrigir algo,
    como já era possível na tela de revisão). Admin: qualquer vistoria da empresa.
    Toda alteração de vistoria concluída fica registrada e o laudo anterior é mantido
    como 'substituído'. Canceladas não são editáveis; o Super Admin não edita vistorias."""
    if ator.super or r["status"] == "cancelada":
        return False
    if r["usuario_id"] == ator.id and pode(ator, "vistorias.editar_proprias"):
        return True
    return pode(ator, "vistorias.editar_concluidas")


def salvar_rascunho(ator, vistoria_id, dados):
    """Guarda o andamento da vistoria (para continuar depois). Vistorias concluídas só
    mudam ao finalizar de novo — o rascunho de uma edição não altera o laudo emitido."""
    with db.conectar() as con:
        r = _vistoria_no_escopo(con, ator, vistoria_id)
        if r["status"] not in ("em_andamento", "pendente") or not _pode_editar(ator, r):
            return False
        res = _resumo_dados(dados)
        con.execute("UPDATE vistorias SET dados = ?, placa = ?, veiculo_desc = ?, tipo_veiculo = ?, "
                    "status = 'em_andamento', updated_at = ? WHERE id = ?",
                    (json.dumps(dados, ensure_ascii=False), res["placa"], res["veiculo_desc"],
                     res["tipo_veiculo"], db.agora(), r["id"]))
        return True


def retomar_vistoria(ator, vistoria_id):
    """Abre uma vistoria para continuar/editar. Devolve o dicionário completo."""
    with db.conectar() as con:
        r = _vistoria_no_escopo(con, ator, vistoria_id)
        if not _pode_editar(ator, r):
            raise AcessoNegado("Esta vistoria não pode ser editada por você.")
        if r["status"] == "pendente":
            con.execute("UPDATE vistorias SET status = 'em_andamento', updated_at = ? WHERE id = ?", (db.agora(), r["id"]))
        registrar(ator, "vistoria_retomada", f"{ator.nome} abriu a vistoria {r['numero']} para edição",
                  vistoria_id=r["id"], empresa_id=r["empresa_id"], con=con)
        dados = json.loads(r["dados"] or "{}")
    dados["numero"] = r["numero"]
    dados["_vistoria_id"] = r["id"]
    return dados


def _upsert_cliente(con, empresa_id, p, agora):
    nome = (p.get("nome") or "").strip()
    cpf = _so_digitos(p.get("cpf"))
    tel = (p.get("telefone") or "").strip()
    if not nome and not cpf:
        return None
    if cpf:
        r = con.execute("SELECT id FROM clientes WHERE empresa_id = ? AND cpf = ?", (empresa_id, cpf)).fetchone()
    else:
        r = con.execute("SELECT id FROM clientes WHERE empresa_id = ? AND cpf = '' AND lower(nome) = lower(?)",
                        (empresa_id, nome)).fetchone()
    if r:
        con.execute("UPDATE clientes SET nome = COALESCE(NULLIF(?, ''), nome), telefone = COALESCE(NULLIF(?, ''), telefone), "
                    "updated_at = ? WHERE id = ?", (nome, tel, agora, r["id"]))
        return r["id"]
    return con.execute("INSERT INTO clientes(empresa_id, nome, cpf, telefone, created_at, updated_at) VALUES (?,?,?,?,?,?)",
                       (empresa_id, nome, cpf, tel, agora, agora)).lastrowid


def _upsert_veiculo(con, empresa_id, v, tipo, cliente_id, agora):
    placa = _placa(v.get("placa"))
    if not placa:
        return None
    campos = (v.get("marca") or "", v.get("modelo") or "", v.get("ano") or "", v.get("cor") or "",
              v.get("km") or "", tipo or "")
    r = con.execute("SELECT id FROM veiculos WHERE empresa_id = ? AND placa = ?", (empresa_id, placa)).fetchone()
    if r:
        con.execute("UPDATE veiculos SET marca=?, modelo=?, ano=?, cor=?, quilometragem=?, tipo=?, "
                    "cliente_id = COALESCE(?, cliente_id), updated_at=? WHERE id=?",
                    campos + (cliente_id, agora, r["id"]))
        return r["id"]
    return con.execute("INSERT INTO veiculos(empresa_id, placa, marca, modelo, ano, cor, quilometragem, tipo, "
                       "cliente_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                       (empresa_id, placa) + campos + (cliente_id, agora, agora)).lastrowid


def caminho_pdf(arquivo_rel):
    """Caminho absoluto a partir do caminho relativo gravado no banco."""
    return db.data_dir() / arquivo_rel


def finalizar_vistoria(ator, vistoria_id, dados, pdf_bytes):
    """Conclui a vistoria: grava dados finais, cliente, veículo, PDF e laudo; consome o
    plano (uma única vez por vistoria) e registra na auditoria. Devolve o id do laudo."""
    agora = db.agora()
    with db.conectar() as con:
        r = _vistoria_no_escopo(con, ator, vistoria_id)
        if not _pode_editar(ator, r):
            raise AcessoNegado("Você não pode finalizar esta vistoria.")
        emp = r["empresa_id"]
        e = con.execute("SELECT * FROM empresas WHERE id = ?", (emp,)).fetchone()
        if not e or e["status"] != "ativa":
            raise ErroNegocio("A conta da empresa não está ativa.")
        primeira = not r["consumiu_plano"]
        if primeira and e["limite_vistorias"] is not None and e["vistorias_utilizadas"] >= e["limite_vistorias"]:
            raise ErroNegocio(f"Limite de vistorias do plano atingido ({e['vistorias_utilizadas']} / "
                              f"{e['limite_vistorias']}). Fale com o administrador.")
        res = _resumo_dados(dados)
        cliente_id = _upsert_cliente(con, emp, (dados or {}).get("proprietario", {}) or {}, agora)
        veiculo_id = _upsert_veiculo(con, emp, (dados or {}).get("veiculo", {}) or {}, res["tipo_veiculo"], cliente_id, agora)

        pasta = Path("pdfs") / f"emp_{emp}"
        (db.data_dir() / pasta).mkdir(parents=True, exist_ok=True)
        nome_arq = re.sub(r"[^A-Za-z0-9_-]", "_", r["numero"]) + ".pdf"
        rel = (pasta / nome_arq).as_posix()
        caminho_pdf(rel).write_bytes(pdf_bytes)

        d = dict(dados or {})
        d["numero"] = r["numero"]
        d.pop("_vistoria_id", None)
        con.execute("UPDATE vistorias SET status = 'concluida', dados = ?, placa = ?, veiculo_desc = ?, tipo_veiculo = ?, "
                    "veiculo_id = ?, cliente_id = ?, data_conclusao = COALESCE(data_conclusao, ?), consumiu_plano = 1, "
                    "updated_at = ? WHERE id = ?",
                    (json.dumps(d, ensure_ascii=False), res["placa"], res["veiculo_desc"], res["tipo_veiculo"],
                     veiculo_id, cliente_id, agora, agora, r["id"]))
        con.execute("UPDATE laudos SET status = 'substituido' WHERE vistoria_id = ? AND status = 'emitido'", (r["id"],))
        lid = con.execute("INSERT INTO laudos(empresa_id, vistoria_id, veiculo_id, cliente_id, usuario_id, numero, data, "
                          "arquivo_pdf, status, created_at) VALUES (?,?,?,?,?,?,?,?,'emitido',?)",
                          (emp, r["id"], veiculo_id, cliente_id, ator.id, r["numero"], agora, rel, agora)).lastrowid
        if primeira:
            con.execute("UPDATE empresas SET vistorias_utilizadas = vistorias_utilizadas + 1, updated_at = ? WHERE id = ?",
                        (agora, emp))
            registrar(ator, "vistoria_finalizada", f"{ator.nome} finalizou a vistoria {r['numero']}",
                      vistoria_id=r["id"], empresa_id=emp, con=con)
        else:
            registrar(ator, "vistoria_alterada", f"{ator.nome} alterou e finalizou novamente a vistoria {r['numero']}",
                      vistoria_id=r["id"], empresa_id=emp, con=con)
        registrar(ator, "laudo_gerado", f"Laudo PDF {r['numero']} gerado", vistoria_id=r["id"], empresa_id=emp, con=con)
        return lid


def cancelar_vistoria(ator, vistoria_id, motivo=""):
    with db.conectar() as con:
        r = _vistoria_no_escopo(con, ator, vistoria_id)
        if r["status"] in ("concluida", "cancelada"):
            raise ErroNegocio("Somente vistorias em andamento ou pendentes podem ser canceladas.")
        if not (pode(ator, "vistorias.cancelar") or (r["usuario_id"] == ator.id and pode(ator, "vistorias.cancelar_proprias"))):
            raise AcessoNegado("Você não pode cancelar esta vistoria.")
        con.execute("UPDATE vistorias SET status = 'cancelada', updated_at = ? WHERE id = ?", (db.agora(), r["id"]))
        registrar(ator, "vistoria_cancelada", f"{ator.nome} cancelou a vistoria {r['numero']}"
                  + (f" — {motivo}" if motivo else ""), vistoria_id=r["id"], empresa_id=r["empresa_id"], con=con)


def status_vistoria(ator, vistoria_id):
    with db.conectar() as con:
        return _vistoria_no_escopo(con, ator, vistoria_id)["status"]


def listar_vistorias(ator, empresa_id=None, data_ini=None, data_fim=None, usuario_id=None, placa=None,
                     modelo=None, status=None, tipo_veiculo=None, limite=1000):
    if pode(ator, "vistorias.ver_todas"):
        proprias = False
    else:
        exigir(ator, "vistorias.ver_proprias")
        proprias = True
    emp = _escopo(ator, empresa_id)
    params = []
    sql = ("SELECT v.id, v.empresa_id, v.numero, v.usuario_id, v.vistoriador_nome, v.placa, v.veiculo_desc, "
           "v.tipo_veiculo, v.status, v.data_inicio, v.data_conclusao, v.updated_at, v.veiculo_id, v.cliente_id, "
           "c.nome AS cliente_nome, e.nome AS empresa_nome, "
           "(SELECT l.id FROM laudos l WHERE l.vistoria_id = v.id AND l.status = 'emitido' ORDER BY l.id DESC LIMIT 1) AS laudo_id "
           "FROM vistorias v LEFT JOIN clientes c ON c.id = v.cliente_id LEFT JOIN empresas e ON e.id = v.empresa_id "
           "WHERE 1=1" + _filtro_empresa("v", emp, params))
    if proprias:
        sql += " AND v.usuario_id = ?"; params.append(ator.id)
    if data_ini:
        sql += " AND COALESCE(v.data_conclusao, v.data_inicio) >= ?"; params.append(f"{data_ini} 00:00:00")
    if data_fim:
        sql += " AND COALESCE(v.data_conclusao, v.data_inicio) <= ?"; params.append(f"{data_fim} 23:59:59")
    if usuario_id:
        sql += " AND v.usuario_id = ?"; params.append(int(usuario_id))
    if placa:
        sql += " AND v.placa LIKE ?"; params.append(f"%{_placa(placa)}%")
    if modelo:
        sql += " AND v.veiculo_desc LIKE ?"; params.append(f"%{modelo.strip()}%")
    if status:
        if isinstance(status, (list, tuple)):
            sql += f" AND v.status IN ({','.join('?' * len(status))})"; params.extend(status)
        else:
            sql += " AND v.status = ?"; params.append(status)
    if tipo_veiculo:
        sql += " AND v.tipo_veiculo = ?"; params.append(tipo_veiculo)
    sql += " ORDER BY COALESCE(v.data_conclusao, v.data_inicio) DESC, v.id DESC LIMIT ?"
    params.append(int(limite))
    with db.conectar() as con:
        return _rows(con.execute(sql, params))


def obter_vistoria(ator, vistoria_id):
    """Vistoria completa (com dados/fotos), laudos e histórico de alterações."""
    with db.conectar() as con:
        r = dict(_vistoria_no_escopo(con, ator, vistoria_id))
        r["dados"] = json.loads(r["dados"] or "{}")
        r["laudos"] = _rows(con.execute("SELECT * FROM laudos WHERE vistoria_id = ? ORDER BY id DESC", (r["id"],)))
        r["historico"] = _rows(con.execute("SELECT data_hora, usuario_nome, acao, descricao FROM logs "
                                           "WHERE vistoria_id = ? ORDER BY data_hora, id", (r["id"],)))
        r["cliente"] = dict(con.execute("SELECT * FROM clientes WHERE id = ?", (r["cliente_id"],)).fetchone() or {}) if r["cliente_id"] else {}
        r["pode_editar"] = _pode_editar(ator, r)
        return r


def apagar_vistorias_empresa(ator):
    """Recurso existente "Apagar vistorias", agora restrito à PRÓPRIA empresa.
    Não devolve consumo do plano (o contador não diminui)."""
    exigir(ator, "vistorias.apagar")
    emp = _escopo(ator)
    with db.conectar() as con:
        arquivos = [r["arquivo_pdf"] for r in con.execute("SELECT arquivo_pdf FROM laudos WHERE empresa_id = ?", (emp,))]
        n = con.execute("SELECT COUNT(*) FROM vistorias WHERE empresa_id = ?", (emp,)).fetchone()[0]
        con.execute("DELETE FROM laudos WHERE empresa_id = ?", (emp,))
        con.execute("UPDATE logs SET vistoria_id = NULL WHERE empresa_id = ?", (emp,))
        con.execute("DELETE FROM vistorias WHERE empresa_id = ?", (emp,))
        registrar(ator, "vistorias_apagadas", f"{n} vistoria(s) apagada(s) com seus laudos", con=con)
    for rel in arquivos:
        try:
            caminho_pdf(rel).unlink(missing_ok=True)
        except Exception:
            pass
    return n


# ---------------------------------------------------------------------------
# Laudos
# ---------------------------------------------------------------------------
def listar_laudos(ator, empresa_id=None, data_ini=None, data_fim=None, usuario_id=None, placa=None, incluir_substituidos=False):
    if pode(ator, "laudos.ver_todos"):
        proprios = False
    else:
        exigir(ator, "laudos.ver_proprios")
        proprios = True
    emp = _escopo(ator, empresa_id)
    params = []
    sql = ("SELECT l.id, l.empresa_id, l.vistoria_id, l.numero, l.data, l.status, l.arquivo_pdf, "
           "v.placa, v.veiculo_desc, v.vistoriador_nome, v.usuario_id, c.nome AS cliente_nome, c.telefone AS cliente_telefone, "
           "e.nome AS empresa_nome FROM laudos l JOIN vistorias v ON v.id = l.vistoria_id "
           "LEFT JOIN clientes c ON c.id = l.cliente_id LEFT JOIN empresas e ON e.id = l.empresa_id WHERE 1=1"
           + _filtro_empresa("l", emp, params))
    if proprios:
        sql += " AND v.usuario_id = ?"; params.append(ator.id)
    if not incluir_substituidos:
        sql += " AND l.status = 'emitido'"
    if data_ini:
        sql += " AND l.data >= ?"; params.append(f"{data_ini} 00:00:00")
    if data_fim:
        sql += " AND l.data <= ?"; params.append(f"{data_fim} 23:59:59")
    if usuario_id:
        sql += " AND v.usuario_id = ?"; params.append(int(usuario_id))
    if placa:
        sql += " AND v.placa LIKE ?"; params.append(f"%{_placa(placa)}%")
    sql += " ORDER BY l.data DESC, l.id DESC"
    with db.conectar() as con:
        return _rows(con.execute(sql, params))


def _laudo_no_escopo(con, ator, laudo_id):
    l = con.execute("SELECT * FROM laudos WHERE id = ?", (int(laudo_id),)).fetchone()
    if not l:
        raise AcessoNegado("Laudo não encontrado.")
    _vistoria_no_escopo(con, ator, l["vistoria_id"])       # reaproveita a checagem de empresa/autoria
    return l


def pdf_do_laudo(ator, laudo_id, gerar_pdf=None):
    """Bytes do PDF. Se o arquivo sumiu do disco, gera de novo a partir dos dados
    gravados (mesmo comportamento do histórico antigo). Devolve (bytes, nome_arquivo)."""
    with db.conectar() as con:
        l = _laudo_no_escopo(con, ator, laudo_id)
        path = caminho_pdf(l["arquivo_pdf"])
        if not path.exists():
            if gerar_pdf is None:
                raise ErroNegocio("Arquivo do laudo não encontrado.")
            v = con.execute("SELECT dados FROM vistorias WHERE id = ?", (l["vistoria_id"],)).fetchone()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(gerar_pdf(json.loads(v["dados"] or "{}")))
        return path.read_bytes(), f"{l['numero']}.pdf"


def regenerar_laudo(ator, laudo_id, gerar_pdf):
    """'Gerar PDF novamente com o layout atual' (recurso já existente no histórico)."""
    exigir(ator, "laudos.ver_todos")
    with db.conectar() as con:
        l = _laudo_no_escopo(con, ator, laudo_id)
        if ator.super:
            raise AcessoNegado("O Super Admin não altera laudos das empresas.")
        v = con.execute("SELECT dados FROM vistorias WHERE id = ?", (l["vistoria_id"],)).fetchone()
        caminho_pdf(l["arquivo_pdf"]).parent.mkdir(parents=True, exist_ok=True)
        caminho_pdf(l["arquivo_pdf"]).write_bytes(gerar_pdf(json.loads(v["dados"] or "{}")))
        registrar(ator, "laudo_regenerado", f"Laudo {l['numero']} gerado novamente", vistoria_id=l["vistoria_id"],
                  empresa_id=l["empresa_id"], con=con)


# ---------------------------------------------------------------------------
# Veículos e clientes
# ---------------------------------------------------------------------------
def listar_veiculos(ator, empresa_id=None, busca=None):
    exigir(ator, "veiculos.ver")
    emp = _escopo(ator, empresa_id)
    params = []
    sql = ("SELECT ve.*, c.nome AS cliente_nome, e.nome AS empresa_nome, "
           "(SELECT COALESCE(v.data_conclusao, v.data_inicio) FROM vistorias v WHERE v.veiculo_id = ve.id "
           " ORDER BY COALESCE(v.data_conclusao, v.data_inicio) DESC LIMIT 1) AS ultima_vistoria, "
           "(SELECT v.vistoriador_nome FROM vistorias v WHERE v.veiculo_id = ve.id "
           " ORDER BY COALESCE(v.data_conclusao, v.data_inicio) DESC LIMIT 1) AS ultimo_vistoriador, "
           "(SELECT v.status FROM vistorias v WHERE v.veiculo_id = ve.id "
           " ORDER BY COALESCE(v.data_conclusao, v.data_inicio) DESC LIMIT 1) AS ultimo_status, "
           "(SELECT COUNT(*) FROM vistorias v WHERE v.veiculo_id = ve.id) AS vistorias "
           "FROM veiculos ve LEFT JOIN clientes c ON c.id = ve.cliente_id LEFT JOIN empresas e ON e.id = ve.empresa_id "
           "WHERE 1=1" + _filtro_empresa("ve", emp, params))
    if busca:
        sql += " AND (ve.placa LIKE ? OR ve.marca LIKE ? OR ve.modelo LIKE ? OR c.nome LIKE ?)"
        params += [f"%{_placa(busca) or busca}%", f"%{busca}%", f"%{busca}%", f"%{busca}%"]
    sql += " ORDER BY ultima_vistoria DESC NULLS LAST, ve.placa"
    with db.conectar() as con:
        return _rows(con.execute(sql, params))


def historico_veiculo(ator, veiculo_id):
    exigir(ator, "veiculos.ver")
    with db.conectar() as con:
        if ator.super:
            ve = con.execute("SELECT * FROM veiculos WHERE id = ?", (int(veiculo_id),)).fetchone()
        else:
            ve = con.execute("SELECT * FROM veiculos WHERE id = ? AND empresa_id = ?",
                             (int(veiculo_id), ator.empresa_id)).fetchone()
        if not ve:
            raise AcessoNegado("Veículo não encontrado.")
        vistorias = _rows(con.execute(
            "SELECT id, numero, vistoriador_nome, status, data_inicio, data_conclusao FROM vistorias "
            "WHERE veiculo_id = ? AND empresa_id = ? ORDER BY COALESCE(data_conclusao, data_inicio) DESC",
            (ve["id"], ve["empresa_id"])))
        return dict(ve), vistorias


def listar_clientes(ator, empresa_id=None, busca=None):
    exigir(ator, "clientes.ver")
    emp = _escopo(ator, empresa_id)
    params = []
    sql = ("SELECT c.*, e.nome AS empresa_nome, "
           "(SELECT COUNT(*) FROM vistorias v WHERE v.cliente_id = c.id) AS vistorias, "
           "(SELECT COUNT(*) FROM veiculos ve WHERE ve.cliente_id = c.id) AS veiculos, "
           "(SELECT MAX(COALESCE(v.data_conclusao, v.data_inicio)) FROM vistorias v WHERE v.cliente_id = c.id) AS ultima_vistoria "
           "FROM clientes c LEFT JOIN empresas e ON e.id = c.empresa_id WHERE 1=1" + _filtro_empresa("c", emp, params))
    if busca:
        sql += " AND (c.nome LIKE ? OR c.cpf LIKE ? OR c.telefone LIKE ?)"
        params += [f"%{busca}%", f"%{_so_digitos(busca) or busca}%", f"%{busca}%"]
    sql += " ORDER BY c.nome"
    with db.conectar() as con:
        return _rows(con.execute(sql, params))


def historico_cliente(ator, cliente_id):
    exigir(ator, "clientes.ver")
    with db.conectar() as con:
        if ator.super:
            c = con.execute("SELECT * FROM clientes WHERE id = ?", (int(cliente_id),)).fetchone()
        else:
            c = con.execute("SELECT * FROM clientes WHERE id = ? AND empresa_id = ?",
                            (int(cliente_id), ator.empresa_id)).fetchone()
        if not c:
            raise AcessoNegado("Cliente não encontrado.")
        vistorias = _rows(con.execute(
            "SELECT id, numero, placa, veiculo_desc, vistoriador_nome, status, data_inicio, data_conclusao FROM vistorias "
            "WHERE cliente_id = ? AND empresa_id = ? ORDER BY COALESCE(data_conclusao, data_inicio) DESC",
            (c["id"], c["empresa_id"])))
        return dict(c), vistorias


# ---------------------------------------------------------------------------
# Emitentes salvos (etapa "Emitente") — agora por empresa
# ---------------------------------------------------------------------------
_CAMPOS_EMITENTE = ("empresa", "documento", "telefone", "email", "endereco", "responsavel")


def listar_emitentes(ator):
    exigir(ator, "emitentes.gerenciar")
    emp = _escopo(ator)
    with db.conectar() as con:
        return _rows(con.execute("SELECT * FROM emitentes WHERE empresa_id = ? ORDER BY empresa", (emp,)))


def salvar_emitente(ator, dados):
    exigir(ator, "emitentes.gerenciar")
    emp = _escopo(ator)
    d = {k: (dados.get(k) or "").strip() for k in _CAMPOS_EMITENTE}
    if not d["empresa"]:
        raise ErroNegocio("Informe o nome da Empresa / Emitente antes de salvar.")
    agora = db.agora()
    with db.conectar() as con:
        r = con.execute("SELECT id FROM emitentes WHERE empresa_id = ? AND lower(empresa) = lower(?)",
                        (emp, d["empresa"])).fetchone()
        if r:
            con.execute("UPDATE emitentes SET documento=?, telefone=?, email=?, endereco=?, responsavel=?, updated_at=? "
                        "WHERE id=?", (d["documento"], d["telefone"], d["email"], d["endereco"], d["responsavel"], agora, r["id"]))
        else:
            con.execute("INSERT INTO emitentes(empresa_id, empresa, documento, telefone, email, endereco, responsavel, "
                        "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                        (emp,) + tuple(d.values()) + (agora, agora))


def excluir_emitente(ator, emitente_id):
    exigir(ator, "empresa.configurar")
    emp = _escopo(ator)
    with db.conectar() as con:
        con.execute("DELETE FROM emitentes WHERE id = ? AND empresa_id = ?", (int(emitente_id), emp))


# ---------------------------------------------------------------------------
# Indicadores e séries (dashboards) — sempre calculados dos dados reais
# ---------------------------------------------------------------------------
def indicadores_empresa(ator, empresa_id=None):
    exigir(ator, "empresa.dashboard")
    emp = _escopo(ator, empresa_id)
    if emp is None:
        raise ErroNegocio("Selecione uma empresa.")
    hoje = db.hoje()
    mes = hoje[:7]
    with db.conectar() as con:
        q = lambda sql, *p: con.execute(sql, (emp,) + p).fetchone()[0]
        ind = {
            "total_vistorias": q("SELECT COUNT(*) FROM vistorias WHERE empresa_id = ? AND status <> 'cancelada'"),
            "vistorias_hoje": q("SELECT COUNT(*) FROM vistorias WHERE empresa_id = ? AND status <> 'cancelada' "
                                "AND substr(COALESCE(data_conclusao, data_inicio), 1, 10) = ?", hoje),
            "vistorias_mes": q("SELECT COUNT(*) FROM vistorias WHERE empresa_id = ? AND status <> 'cancelada' "
                               "AND substr(COALESCE(data_conclusao, data_inicio), 1, 7) = ?", mes),
            "pendentes": q("SELECT COUNT(*) FROM vistorias WHERE empresa_id = ? AND status IN ('em_andamento','pendente')"),
            "concluidas": q("SELECT COUNT(*) FROM vistorias WHERE empresa_id = ? AND status = 'concluida'"),
            "total_veiculos": q("SELECT COUNT(*) FROM veiculos WHERE empresa_id = ?"),
            "total_clientes": q("SELECT COUNT(*) FROM clientes WHERE empresa_id = ?"),
            "total_usuarios": q("SELECT COUNT(*) FROM usuarios WHERE empresa_id = ? AND status = 'ativo'"),
            "laudos_emitidos": q("SELECT COUNT(*) FROM laudos WHERE empresa_id = ? AND status = 'emitido'"),
        }
        e = con.execute("SELECT * FROM empresas WHERE id = ?", (emp,)).fetchone()
        ind["consumo"] = _consumo(con, e)
        ind["empresa_nome"] = e["nome"]
    return ind


def serie_vistorias(ator, data_ini, data_fim, empresa_id=None):
    """Linhas (dia, status, total) no período, pela data de conclusão (ou de início)."""
    if not (pode(ator, "empresa.dashboard") or pode(ator, "relatorios.ver")):
        exigir(ator, "empresa.dashboard")
    emp = _escopo(ator, empresa_id)
    params = [f"{data_ini} 00:00:00", f"{data_fim} 23:59:59"]
    sql = ("SELECT substr(COALESCE(v.data_conclusao, v.data_inicio), 1, 10) AS dia, v.status, COUNT(*) AS total "
           "FROM vistorias v WHERE COALESCE(v.data_conclusao, v.data_inicio) BETWEEN ? AND ?"
           + _filtro_empresa("v", emp, params) + " GROUP BY dia, v.status ORDER BY dia")
    with db.conectar() as con:
        return _rows(con.execute(sql, params))


def produtividade(ator, data_ini, data_fim, empresa_id=None):
    exigir(ator, "empresa.dashboard")
    emp = _escopo(ator, empresa_id)
    params = [f"{data_ini} 00:00:00", f"{data_fim} 23:59:59"]
    sql = ("SELECT COALESCE(NULLIF(v.vistoriador_nome, ''), 'Sem nome') AS vistoriador, COUNT(*) AS total "
           "FROM vistorias v WHERE v.status = 'concluida' AND v.data_conclusao BETWEEN ? AND ?"
           + _filtro_empresa("v", emp, params) + " GROUP BY vistoriador ORDER BY total DESC")
    with db.conectar() as con:
        return _rows(con.execute(sql, params))


def indicadores_plataforma(ator):
    exigir(ator, "plataforma.gerenciar")
    mes = db.hoje()[:7]
    with db.conectar() as con:
        one = lambda sql, *p: con.execute(sql, p).fetchone()[0]
        empresas = _rows(con.execute("SELECT * FROM empresas"))
        proximas = 0
        for e in empresas:
            c = _consumo(con, e)
            perto_limite = c["percentual"] is not None and c["percentual"] >= ALERTA_CONSUMO
            perto_venc = c["dias_para_vencer"] is not None and c["dias_para_vencer"] <= ALERTA_VENCIMENTO_DIAS
            if e["status"] == "ativa" and (perto_limite or perto_venc):
                proximas += 1
        return {
            "total_empresas": len(empresas),
            "empresas_ativas": sum(1 for e in empresas if e["status"] == "ativa"),
            "empresas_inativas": sum(1 for e in empresas if e["status"] != "ativa"),
            "total_usuarios": one("SELECT COUNT(*) FROM usuarios WHERE perfil <> 'super_admin' AND status = 'ativo'"),
            "total_vistorias": one("SELECT COUNT(*) FROM vistorias WHERE status <> 'cancelada'"),
            "vistorias_mes": one("SELECT COUNT(*) FROM vistorias WHERE status <> 'cancelada' "
                                 "AND substr(COALESCE(data_conclusao, data_inicio), 1, 7) = ?", mes),
            "empresas_proximas_limite": proximas,
        }


def series_plataforma(ator, data_ini, data_fim):
    exigir(ator, "plataforma.gerenciar")
    ini, fim = f"{data_ini} 00:00:00", f"{data_fim} 23:59:59"
    with db.conectar() as con:
        return {
            "empresas_por_mes": _rows(con.execute(
                "SELECT substr(created_at, 1, 7) AS mes, COUNT(*) AS total FROM empresas GROUP BY mes ORDER BY mes")),
            "vistorias_por_dia": _rows(con.execute(
                "SELECT substr(COALESCE(data_conclusao, data_inicio), 1, 10) AS dia, COUNT(*) AS total FROM vistorias "
                "WHERE status <> 'cancelada' AND COALESCE(data_conclusao, data_inicio) BETWEEN ? AND ? "
                "GROUP BY dia ORDER BY dia", (ini, fim))),
            "consumo_por_empresa": _rows(con.execute(
                "SELECT nome AS empresa, vistorias_utilizadas AS utilizadas, limite_vistorias AS limite FROM empresas "
                "ORDER BY vistorias_utilizadas DESC")),
            "empresas_por_plano": _rows(con.execute(
                "SELECT COALESCE(p.nome, 'Sem plano') AS plano, COUNT(*) AS total FROM empresas e "
                "LEFT JOIN planos p ON p.id = e.plano_id GROUP BY plano ORDER BY total DESC")),
            "usuarios_por_empresa": _rows(con.execute(
                "SELECT e.nome AS empresa, COUNT(u.id) AS total FROM empresas e "
                "LEFT JOIN usuarios u ON u.empresa_id = e.id AND u.status = 'ativo' GROUP BY e.id ORDER BY total DESC")),
            "atividade_por_dia": _rows(con.execute(
                "SELECT substr(data_hora, 1, 10) AS dia, COUNT(*) AS total FROM logs WHERE data_hora BETWEEN ? AND ? "
                "GROUP BY dia ORDER BY dia", (ini, fim))),
        }
