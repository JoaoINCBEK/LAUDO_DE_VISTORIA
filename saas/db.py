"""Banco de dados (SQLite ou PostgreSQL) e utilidades de data/hora.

- Sem configuração: SQLite em <LAUDO_DATA_DIR>/laudo.db (padrão: autocheck_data/laudo.db).
  É o modo usado no computador e nos testes.
- Com endereço de PostgreSQL (Secrets [database] url, ou variável LAUDO_DATABASE_URL):
  os dados ficam num banco permanente (ex.: Neon, Supabase). É o modo de produção:
  nada se perde quando o Streamlit Cloud reinicia.

O resto do sistema escreve SQL no estilo SQLite (parâmetros "?"); aqui ele é
adaptado para o PostgreSQL ("%s", ILIKE, RETURNING id). As telas não mudam.
"""
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA_VERSION = 2          # 2: tabela assinaturas_remotas

# Brasil (Fortaleza/Brasília) não tem horário de verão desde 2019: UTC-3 fixo.
# Evita horários errados em servidores configurados em UTC (ex.: Streamlit Cloud).
TZ = timezone(timedelta(hours=-3))

_db_path_override = None
_pg_url = os.environ.get("LAUDO_DATABASE_URL") or None
_pg_schema = None
_pool = None


def data_dir():
    return Path(os.environ.get("LAUDO_DATA_DIR", "autocheck_data"))


def db_path():
    if _db_path_override:
        return Path(_db_path_override)
    return data_dir() / "laudo.db"


def set_db_path(path):
    """Usado nos testes para apontar para um banco temporário."""
    global _db_path_override
    _db_path_override = str(path) if path else None


def configurar_postgres(url, schema=None):
    """Passa a usar o PostgreSQL do endereço informado (None = volta ao SQLite).
    `schema` separa os dados (usado pelos testes para não tocar nos dados reais)."""
    global _pg_url, _pg_schema, _pool
    if _pool is not None:
        _pool.close()
    _pg_url, _pg_schema, _pool = (url or None), schema, None


def usando_postgres():
    return bool(_pg_url)


def descricao_banco():
    if usando_postgres():
        host = re.sub(r"^.*@", "", _pg_url).split("/")[0].split("?")[0]
        return f"PostgreSQL ({host})"
    return f"SQLite ({db_path()})"


def agora():
    """Data/hora atual (UTC-3) como texto ISO: 'AAAA-MM-DD HH:MM:SS'."""
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")


def hoje():
    return datetime.now(TZ).strftime("%Y-%m-%d")


def agora_dt():
    return datetime.now(TZ).replace(tzinfo=None)


def br(iso, com_hora=True):
    """'2026-09-23 14:32:00' -> '23/09/2026 14:32'."""
    if not iso:
        return ""
    try:
        dt = datetime.strptime(str(iso)[:19], "%Y-%m-%d %H:%M:%S")
        return dt.strftime("%d/%m/%Y %H:%M" if com_hora else "%d/%m/%Y")
    except ValueError:
        try:
            return datetime.strptime(str(iso)[:10], "%Y-%m-%d").strftime("%d/%m/%Y")
        except ValueError:
            return str(iso)


def iso_de_br(texto):
    """'23/09/2026 14:32' -> '2026-09-23 14:32:00' (formato antigo dos JSON)."""
    if not texto:
        return None
    for fmt in ("%d/%m/%Y %H:%M", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y"):
        try:
            return datetime.strptime(str(texto).strip(), fmt).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    return None


def eh_duplicado(exc):
    """True se o erro for violação de UNIQUE (nos dois bancos)."""
    if getattr(exc, "sqlstate", None) == "23505":
        return True
    return isinstance(exc, sqlite3.IntegrityError) and "UNIQUE" in str(exc)


# ---------------------------------------------------------------------------
# PostgreSQL: adaptação mínima para o mesmo uso que o sqlite3
# ---------------------------------------------------------------------------
class Linha:
    """Linha de resultado acessível por nome (r["col"]) e posição (r[0]),
    conversível com dict(r) e desempacotável — igual ao sqlite3.Row."""
    __slots__ = ("_nomes", "_valores")

    def __init__(self, nomes, valores):
        self._nomes, self._valores = nomes, tuple(valores)

    def __getitem__(self, k):
        if isinstance(k, (int, slice)):
            return self._valores[k]
        return self._valores[self._nomes.index(k)]

    def keys(self):
        return list(self._nomes)

    def __iter__(self):
        return iter(self._valores)

    def __len__(self):
        return len(self._valores)


def _fabrica_linhas(cursor):
    nomes = [d.name for d in (cursor.description or [])]
    return lambda valores: Linha(nomes, valores)


_TABELAS_COM_ID = {"planos", "empresas", "usuarios", "clientes", "veiculos", "vistorias", "laudos", "emitentes", "logs",
                   "assinaturas_remotas"}
_RE_INSERT = re.compile(r"^\s*INSERT\s+INTO\s+(\w+)", re.I)


def _sql_pg(sql):
    sql = sql.replace("?", "%s")
    return re.sub(r"\bLIKE\b", "ILIKE", sql)


class _CursorPG:
    def __init__(self, cur, lastrowid=None):
        self._cur, self.lastrowid = cur, lastrowid

    def fetchone(self):
        return self._cur.fetchone() if self._cur.description else None

    def fetchall(self):
        return self._cur.fetchall() if self._cur.description else []

    def __iter__(self):
        return iter(self.fetchall())


class _ConexaoPG:
    def __init__(self, con):
        self._con = con

    def execute(self, sql, params=()):
        cur = self._con.cursor(row_factory=_fabrica_linhas)
        m = _RE_INSERT.match(sql)
        retorna_id = bool(m and m.group(1).lower() in _TABELAS_COM_ID and "RETURNING" not in sql.upper())
        cur.execute(_sql_pg(sql) + (" RETURNING id" if retorna_id else ""), tuple(params))
        lastrowid = cur.fetchone()[0] if retorna_id else None
        return _CursorPG(cur, lastrowid)

    def executescript(self, script):
        for comando in (c.strip() for c in script.split(";")):
            if comando:
                self._con.execute(comando)


def _obter_pool():
    global _pool
    if _pool is None:
        import psycopg                            # só é necessário no modo PostgreSQL
        from psycopg_pool import ConnectionPool

        if _pg_schema:   # schema próprio (testes): criado uma vez, antes das conexões do pool
            with psycopg.connect(_pg_url, prepare_threshold=None, autocommit=True) as c:
                c.execute(f'CREATE SCHEMA IF NOT EXISTS "{_pg_schema}"')

        def _preparar(con):
            if _pg_schema:
                con.execute(f'SET search_path TO "{_pg_schema}"')
            con.commit()

        # prepare_threshold=None: compatível com os "poolers" (PgBouncer) do Neon/Supabase.
        _pool = ConnectionPool(_pg_url, min_size=1, max_size=5, timeout=30, max_idle=300,
                               kwargs={"prepare_threshold": None, "autocommit": False},
                               configure=_preparar, check=ConnectionPool.check_connection, open=True)
    return _pool


@contextmanager
def conectar():
    """Conexão/transação curta por operação. Confirma no fim; desfaz em caso de erro."""
    if usando_postgres():
        with _obter_pool().connection() as con:     # commit/rollback automáticos
            yield _ConexaoPG(con)
        return
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path), timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    chave TEXT PRIMARY KEY,
    valor TEXT
);

CREATE TABLE IF NOT EXISTS planos (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    nome             TEXT NOT NULL UNIQUE COLLATE NOCASE,
    descricao        TEXT NOT NULL DEFAULT '',
    limite_vistorias INTEGER,              -- NULL = ilimitado
    limite_usuarios  INTEGER,              -- NULL = ilimitado
    duracao_meses    INTEGER,
    ativo            INTEGER NOT NULL DEFAULT 1,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS empresas (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    nome                 TEXT NOT NULL,
    cnpj                 TEXT NOT NULL DEFAULT '',
    razao_social         TEXT NOT NULL DEFAULT '',
    responsavel          TEXT NOT NULL DEFAULT '',
    email                TEXT NOT NULL DEFAULT '',
    telefone             TEXT NOT NULL DEFAULT '',
    endereco             TEXT NOT NULL DEFAULT '',
    plano_id             INTEGER REFERENCES planos(id),
    limite_vistorias     INTEGER,          -- NULL = ilimitado
    vistorias_utilizadas INTEGER NOT NULL DEFAULT 0,
    limite_usuarios      INTEGER,          -- NULL = ilimitado
    data_inicio          TEXT,
    data_vencimento      TEXT,             -- NULL = sem vencimento
    status               TEXT NOT NULL DEFAULT 'ativa'
                         CHECK (status IN ('ativa', 'bloqueada', 'inativa')),
    ultimo_acesso        TEXT,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS usuarios (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    empresa_id        INTEGER REFERENCES empresas(id),   -- NULL somente para super_admin
    login             TEXT NOT NULL UNIQUE COLLATE NOCASE,
    nome              TEXT NOT NULL,
    email             TEXT NOT NULL DEFAULT '' COLLATE NOCASE,
    senha_hash        TEXT NOT NULL,
    perfil            TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'ativo' CHECK (status IN ('ativo', 'inativo')),
    trocar_senha      INTEGER NOT NULL DEFAULT 0,
    tentativas_falhas INTEGER NOT NULL DEFAULT 0,
    bloqueado_ate     TEXT,
    ultimo_acesso     TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    CHECK ((perfil = 'super_admin') = (empresa_id IS NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_usuarios_email ON usuarios(email) WHERE email <> '';
CREATE INDEX IF NOT EXISTS ix_usuarios_empresa ON usuarios(empresa_id);

CREATE TABLE IF NOT EXISTS sessoes (
    token_hash TEXT PRIMARY KEY,
    usuario_id INTEGER NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
    empresa_id INTEGER REFERENCES empresas(id),
    created_at TEXT NOT NULL,
    expira_em  TEXT NOT NULL,
    ultimo_uso TEXT,
    ip         TEXT NOT NULL DEFAULT '',
    revogada   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_sessoes_usuario ON sessoes(usuario_id);

CREATE TABLE IF NOT EXISTS clientes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    empresa_id INTEGER NOT NULL REFERENCES empresas(id),
    nome       TEXT NOT NULL DEFAULT '',
    cpf        TEXT NOT NULL DEFAULT '',
    telefone   TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_clientes_empresa ON clientes(empresa_id);

CREATE TABLE IF NOT EXISTS veiculos (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    empresa_id    INTEGER NOT NULL REFERENCES empresas(id),
    placa         TEXT NOT NULL,
    marca         TEXT NOT NULL DEFAULT '',
    modelo        TEXT NOT NULL DEFAULT '',
    ano           TEXT NOT NULL DEFAULT '',
    cor           TEXT NOT NULL DEFAULT '',
    quilometragem TEXT NOT NULL DEFAULT '',
    tipo          TEXT NOT NULL DEFAULT '',
    cliente_id    INTEGER REFERENCES clientes(id),
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    UNIQUE (empresa_id, placa)
);

CREATE TABLE IF NOT EXISTS vistorias (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    empresa_id       INTEGER NOT NULL REFERENCES empresas(id),
    numero           TEXT NOT NULL,
    usuario_id       INTEGER REFERENCES usuarios(id),
    vistoriador_nome TEXT NOT NULL DEFAULT '',
    veiculo_id       INTEGER REFERENCES veiculos(id),
    cliente_id       INTEGER REFERENCES clientes(id),
    placa            TEXT NOT NULL DEFAULT '',
    veiculo_desc     TEXT NOT NULL DEFAULT '',
    tipo_veiculo     TEXT NOT NULL DEFAULT '',
    status           TEXT NOT NULL DEFAULT 'em_andamento'
                     CHECK (status IN ('em_andamento', 'pendente', 'concluida', 'cancelada')),
    consumiu_plano   INTEGER NOT NULL DEFAULT 0,
    data_inicio      TEXT NOT NULL,
    data_conclusao   TEXT,
    dados            TEXT,                 -- vistoria completa (mesmo formato usado pelo PDF)
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    UNIQUE (empresa_id, numero)
);
CREATE INDEX IF NOT EXISTS ix_vistorias_empresa ON vistorias(empresa_id, status);
CREATE INDEX IF NOT EXISTS ix_vistorias_usuario ON vistorias(usuario_id);

CREATE TABLE IF NOT EXISTS laudos (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    empresa_id  INTEGER NOT NULL REFERENCES empresas(id),
    vistoria_id INTEGER NOT NULL REFERENCES vistorias(id) ON DELETE CASCADE,
    veiculo_id  INTEGER REFERENCES veiculos(id),
    cliente_id  INTEGER REFERENCES clientes(id),
    usuario_id  INTEGER REFERENCES usuarios(id),
    numero      TEXT NOT NULL,
    data        TEXT NOT NULL,
    arquivo_pdf TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'emitido' CHECK (status IN ('emitido', 'substituido')),
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_laudos_empresa ON laudos(empresa_id);

CREATE TABLE IF NOT EXISTS emitentes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    empresa_id  INTEGER NOT NULL REFERENCES empresas(id),
    empresa     TEXT NOT NULL,
    documento   TEXT NOT NULL DEFAULT '',
    telefone    TEXT NOT NULL DEFAULT '',
    email       TEXT NOT NULL DEFAULT '',
    endereco    TEXT NOT NULL DEFAULT '',
    responsavel TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_emitentes_empresa ON emitentes(empresa_id);

CREATE TABLE IF NOT EXISTS logs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    empresa_id   INTEGER REFERENCES empresas(id),
    usuario_id   INTEGER REFERENCES usuarios(id),
    usuario_nome TEXT NOT NULL DEFAULT '',
    acao         TEXT NOT NULL,
    descricao    TEXT NOT NULL DEFAULT '',
    vistoria_id  INTEGER,
    data_hora    TEXT NOT NULL,
    ip           TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_logs_empresa ON logs(empresa_id, data_hora);
CREATE INDEX IF NOT EXISTS ix_logs_vistoria ON logs(vistoria_id);

-- Assinatura eletrônica à distância (versão 2 do esquema). Contato do signatário só mascarado:
-- o dado completo fica no provedor. id_externo permite baixar de novo o PDF assinado.
CREATE TABLE IF NOT EXISTS assinaturas_remotas (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    empresa_id          INTEGER NOT NULL REFERENCES empresas(id),
    vistoria_id         INTEGER NOT NULL REFERENCES vistorias(id) ON DELETE CASCADE,
    laudo_id            INTEGER REFERENCES laudos(id),
    usuario_id          INTEGER REFERENCES usuarios(id),
    provedor            TEXT NOT NULL,
    id_externo          TEXT NOT NULL DEFAULT '',
    documento_externo   TEXT NOT NULL DEFAULT '',
    signatario_externo  TEXT NOT NULL DEFAULT '',
    signatario_nome     TEXT NOT NULL DEFAULT '',
    signatario_email    TEXT NOT NULL DEFAULT '',
    signatario_telefone TEXT NOT NULL DEFAULT '',
    canal               TEXT NOT NULL DEFAULT '',
    status              TEXT NOT NULL DEFAULT 'rascunho'
                        CHECK (status IN ('rascunho', 'aguardando', 'assinado', 'recusado', 'cancelado', 'expirado', 'erro')),
    link_assinatura     TEXT NOT NULL DEFAULT '',
    arquivo_assinado    TEXT NOT NULL DEFAULT '',
    mensagem_erro       TEXT NOT NULL DEFAULT '',
    consultado_em       TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_assinaturas_vistoria ON assinaturas_remotas(empresa_id, vistoria_id);
"""


def _schema_pg():
    """O mesmo esquema, no dialeto do PostgreSQL."""
    s = re.sub(r"--[^\n]*", "", SCHEMA)
    s = s.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY")
    s = re.sub(r"INTEGER( NOT NULL)? REFERENCES", r"BIGINT\1 REFERENCES", s)
    s = s.replace(" COLLATE NOCASE", "")
    # usuário/e-mail/plano sem diferenciar maiúsculas (no SQLite isso vem do COLLATE NOCASE)
    s += """
CREATE UNIQUE INDEX IF NOT EXISTS ux_usuarios_login_lower ON usuarios (lower(login));
CREATE UNIQUE INDEX IF NOT EXISTS ux_planos_nome_lower ON planos (lower(nome));
"""
    return s


TABELAS = ["meta", "planos", "empresas", "usuarios", "sessoes", "clientes", "veiculos",
           "vistorias", "laudos", "emitentes", "logs", "assinaturas_remotas"]


def inicializar():
    """Cria as tabelas (idempotente). Bancos já existentes (SQLite ou PostgreSQL) só ganham
    as tabelas novas: CREATE TABLE IF NOT EXISTS não mexe nas que já existem."""
    with conectar() as con:
        if usando_postgres():
            con.executescript(_schema_pg())
        else:
            con.execute("PRAGMA journal_mode = WAL")
            con.executescript(SCHEMA)
        con.execute("INSERT INTO meta(chave, valor) VALUES ('schema_version', ?) ON CONFLICT(chave) DO NOTHING",
                    (str(SCHEMA_VERSION),))
        atual = con.execute("SELECT valor FROM meta WHERE chave = 'schema_version'").fetchone()
        if atual and str(atual[0]).isdigit() and int(atual[0]) < SCHEMA_VERSION:
            con.execute("UPDATE meta SET valor = ? WHERE chave = 'schema_version'", (str(SCHEMA_VERSION),))


def meta_get(chave, padrao=None):
    with conectar() as con:
        row = con.execute("SELECT valor FROM meta WHERE chave = ?", (chave,)).fetchone()
    return row["valor"] if row else padrao


def meta_set(chave, valor):
    with conectar() as con:
        con.execute("INSERT INTO meta(chave, valor) VALUES (?, ?) "
                    "ON CONFLICT(chave) DO UPDATE SET valor = excluded.valor", (chave, str(valor)))
