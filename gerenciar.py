"""Ferramenta de linha de comando da plataforma.

Uso (na pasta do projeto):
    python gerenciar.py criar-superadmin          cria o proprietário da plataforma
    python gerenciar.py redefinir-senha LOGIN     define nova senha (qualquer usuário)
    python gerenciar.py migrar                    importa os JSON antigos (também roda sozinho ao abrir o app)
    python gerenciar.py copiar-para-postgres      copia o banco local (SQLite) para o PostgreSQL
    python gerenciar.py banco                     mostra qual banco está sendo usado

Banco usado: se o arquivo .streamlit/secrets.toml tiver a seção [database] com url = "...",
os comandos trabalham no PostgreSQL (o mesmo do site); senão, no banco local (SQLite).

As senhas são digitadas sem aparecer na tela e nunca ficam gravadas em texto puro.
Acrescente --visivel no final para ver o que está digitando (ex.: python gerenciar.py criar-superadmin --visivel).
"""
import getpass
import sqlite3
import sys
import tomllib
from pathlib import Path

from saas import db, migracao, seguranca


VISIVEL = "--visivel" in sys.argv
SECRETS = Path(__file__).resolve().parent / ".streamlit" / "secrets.toml"


def _url_postgres():
    try:
        url = (tomllib.loads(SECRETS.read_text(encoding="utf-8-sig")).get("database") or {}).get("url") or ""
        return url if url.startswith("postgres") else None     # texto de exemplo = ainda não configurado
    except (FileNotFoundError, tomllib.TOMLDecodeError):
        return None


def _ler_senha(rotulo):
    if VISIVEL:
        return input(rotulo).lstrip("﻿")
    try:
        return getpass.getpass(rotulo)
    except Exception:
        # terminal sem suporte a campo oculto: pede de forma visível
        return input(rotulo + "(visível) ")


def _pedir_senha():
    print(f"Mínimo de {seguranca.SENHA_MIN} caracteres.")
    if not VISIVEL:
        print("Atenção: a senha NÃO aparece enquanto você digita (é normal). Digite e aperte Enter.")
        print("Se preferir ver o que digita, rode o comando com --visivel no final.")
    while True:
        s1 = _ler_senha("Senha: ")
        msg = seguranca.problema_senha(s1)
        if msg:
            print(msg)
            continue
        if _ler_senha("Confirme a senha: ") != s1:
            print("As senhas não conferem. Tente de novo.")
            continue
        return s1


def criar_superadmin():
    login = input("Usuário (login) do Super Admin: ").strip().lstrip("﻿")
    nome = input("Nome: ").strip().lstrip("﻿") or "Super Administrador"
    senha = _pedir_senha()
    if migracao.garantir_super_admin(login, nome, senha):
        print(f"Super Admin '{login}' criado.")
    else:
        print(f"Já existe um usuário com o login '{login}'. Nada foi alterado.")


def redefinir_senha(login):
    db.inicializar()
    with db.conectar() as con:
        u = con.execute("SELECT id FROM usuarios WHERE lower(login) = lower(?)", (login,)).fetchone()
        if not u:
            print("Usuário não encontrado.")
            return 1
        senha = _pedir_senha()
        con.execute("UPDATE usuarios SET senha_hash = ?, trocar_senha = 0, tentativas_falhas = 0, bloqueado_ate = NULL, "
                    "updated_at = ? WHERE id = ?", (seguranca.hash_senha(senha), db.agora(), u["id"]))
        con.execute("UPDATE sessoes SET revogada = 1 WHERE usuario_id = ?", (u["id"],))
    print("Senha alterada. As sessões abertas desse usuário foram encerradas.")
    return 0


def copiar_para_postgres(url):
    """Copia TODOS os dados do banco local (SQLite) para um PostgreSQL VAZIO.
    - Não altera nem apaga o banco local.
    - Não copia sessões (todos entram de novo) nem o Super Admin (ele vem dos Secrets do site).
    - Recusa copiar se o destino já tiver empresas ou usuários (evita duplicar/misturar dados)."""
    origem = db.db_path()
    if not origem.exists():
        print(f"Banco local não encontrado em {origem}. Abra o app uma vez no computador para criá-lo.")
        return 1
    src = sqlite3.connect(str(origem))
    src.row_factory = sqlite3.Row
    db.configurar_postgres(url)
    db.inicializar()
    super_ids = {r["id"] for r in src.execute("SELECT id FROM usuarios WHERE perfil = 'super_admin'")}
    copiados = {}
    with db.conectar() as con:
        ja = con.execute("SELECT (SELECT COUNT(*) FROM empresas) + (SELECT COUNT(*) FROM usuarios "
                         "WHERE perfil <> 'super_admin')").fetchone()[0]
        if ja:
            print("O PostgreSQL de destino já tem empresas/usuários. Nada foi copiado.")
            return 1
        for tabela in db.TABELAS:
            if tabela == "sessoes":
                continue
            n = 0
            for r in src.execute(f"SELECT * FROM {tabela} ORDER BY 1"):
                d = dict(r)
                if tabela == "usuarios" and d["id"] in super_ids:
                    continue
                for campo in ("usuario_id",):
                    if d.get(campo) in super_ids:
                        d[campo] = None
                cols = ", ".join(d)
                marc = ", ".join("?" * len(d))
                fim = " ON CONFLICT(chave) DO UPDATE SET valor = excluded.valor" if tabela == "meta" else ""
                con.execute(f"INSERT INTO {tabela}({cols}) VALUES ({marc}){fim}", tuple(d.values()))
                n += 1
            copiados[tabela] = n
        # próximos ids continuam depois dos copiados
        for tabela in db.TABELAS:
            if tabela in ("meta", "sessoes"):
                continue
            con.execute(f"SELECT setval(pg_get_serial_sequence('{tabela}', 'id'), "
                        f"COALESCE((SELECT MAX(id) FROM {tabela}), 0) + 1, false)")
    src.close()
    print("Cópia concluída:")
    for t, n in copiados.items():
        print(f"  {t}: {n}")
    print("O Super Admin não é copiado: ele é criado pelos Secrets do site ([superadmin]).")
    print("Os PDFs são gerados de novo automaticamente quando forem abertos no site.")
    return 0


def main(argv):
    argv = [a for a in argv if a != "--visivel"]
    if not argv:
        print(__doc__)
        return 1
    cmd = argv[0]
    url = _url_postgres()
    if cmd == "copiar-para-postgres":
        if not url:
            print(f"Coloque o endereço do PostgreSQL em {SECRETS}:\n\n[database]\nurl = \"postgresql://...\"")
            return 1
        return copiar_para_postgres(url)
    if url:
        db.configurar_postgres(url)
    print(f"Banco: {db.descricao_banco()}")
    if cmd == "banco":
        return 0
    if cmd == "criar-superadmin":
        migracao.importar_legado()
        criar_superadmin()
        return 0
    if cmd == "redefinir-senha" and len(argv) == 2:
        return redefinir_senha(argv[1])
    if cmd == "migrar":
        r = migracao.importar_legado()
        print(r or "Nada a migrar (já foi feito ou não há dados antigos).")
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    try:
        codigo = main(sys.argv[1:])
    finally:
        db.configurar_postgres(None)      # fecha as conexões com o PostgreSQL antes de sair
    raise SystemExit(codigo)
