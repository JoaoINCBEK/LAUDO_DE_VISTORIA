"""Ferramenta de linha de comando da plataforma.

Uso (na pasta do projeto):
    python gerenciar.py criar-superadmin          cria o proprietário da plataforma
    python gerenciar.py redefinir-senha LOGIN     define nova senha (qualquer usuário)
    python gerenciar.py migrar                    importa os JSON antigos (também roda sozinho ao abrir o app)

As senhas são digitadas sem aparecer na tela e nunca ficam gravadas em texto puro.
Acrescente --visivel no final para ver o que está digitando (ex.: python gerenciar.py criar-superadmin --visivel).
"""
import getpass
import sys

from saas import db, migracao, seguranca


VISIVEL = "--visivel" in sys.argv


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
        u = con.execute("SELECT id FROM usuarios WHERE login = ?", (login,)).fetchone()
        if not u:
            print("Usuário não encontrado.")
            return 1
        senha = _pedir_senha()
        con.execute("UPDATE usuarios SET senha_hash = ?, trocar_senha = 0, tentativas_falhas = 0, bloqueado_ate = NULL, "
                    "updated_at = ? WHERE id = ?", (seguranca.hash_senha(senha), db.agora(), u["id"]))
        con.execute("UPDATE sessoes SET revogada = 1 WHERE usuario_id = ?", (u["id"],))
    print("Senha alterada. As sessões abertas desse usuário foram encerradas.")
    return 0


def main(argv):
    argv = [a for a in argv if a != "--visivel"]
    if not argv:
        print(__doc__)
        return 1
    cmd = argv[0]
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
    raise SystemExit(main(sys.argv[1:]))
