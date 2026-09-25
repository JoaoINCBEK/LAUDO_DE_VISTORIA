"""Banco de dados (SQLite) e utilidades de data/hora.

Decisão: SQLite, pois já faz parte do Python (sem dependência nova), suporta
relacionamentos, índices, transações e consultas com filtro. Todo acesso passa
por este módulo e por `servicos.py`; para migrar para PostgreSQL no futuro basta
trocar a conexão e o dialeto aqui, sem mexer nas telas.

Local do banco: <LAUDO_DATA_DIR>/laudo.db (padrão: autocheck_data/laudo.db).
"""
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA_VERSION = 1

# Brasil (Fortaleza/Brasília) não tem horário de verão desde 2019: UTC-3 fixo.
# Evita horários errados em servidores configurados em UTC (ex.: Streamlit Cloud).
TZ = timezone(timedelta(hours=-3))

_db_path_override = None


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


@contextmanager
def conectar():
    """Conexão curta por operação. Confirma no fim; desfaz em caso de erro."""
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
"""


def inicializar():
    """Cria as tabelas (idempotente)."""
    with conectar() as con:
        con.execute("PRAGMA journal_mode = WAL")
        con.executescript(SCHEMA)
        con.execute("INSERT OR IGNORE INTO meta(chave, valor) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))


def meta_get(chave, padrao=None):
    with conectar() as con:
        row = con.execute("SELECT valor FROM meta WHERE chave = ?", (chave,)).fetchone()
    return row["valor"] if row else padrao


def meta_set(chave, valor):
    with conectar() as con:
        con.execute("INSERT INTO meta(chave, valor) VALUES (?, ?) "
                    "ON CONFLICT(chave) DO UPDATE SET valor = excluded.valor", (chave, str(valor)))
