"""Senhas e tokens.

- Senhas: PBKDF2-HMAC-SHA256 com sal aleatório por usuário (biblioteca padrão).
- Hashes antigos (SHA-256 sem sal, do users.json) continuam aceitos no login e
  são convertidos automaticamente para PBKDF2 na primeira entrada bem-sucedida.
- Tokens de sessão: aleatórios; no banco fica só o SHA-256 do token.
"""
import hashlib
import hmac
import secrets
import string

PBKDF2_ITERACOES = 240_000
SENHA_MIN = 8
MAX_TENTATIVAS = 5          # tentativas de login erradas antes do bloqueio temporário
BLOQUEIO_MINUTOS = 10
SESSAO_DIAS = 7             # validade máxima de uma sessão


def hash_senha(senha):
    sal = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", senha.encode("utf-8"), bytes.fromhex(sal), PBKDF2_ITERACOES)
    return f"pbkdf2_sha256${PBKDF2_ITERACOES}${sal}${dk.hex()}"


def _eh_legado(hash_salvo):
    return len(hash_salvo) == 64 and all(c in string.hexdigits for c in hash_salvo)


def verificar_senha(senha, hash_salvo):
    """Devolve (confere, precisa_atualizar_hash)."""
    senha = senha or ""
    hash_salvo = hash_salvo or ""
    if hash_salvo.startswith("pbkdf2_sha256$"):
        try:
            _, it, sal, esperado = hash_salvo.split("$")
            dk = hashlib.pbkdf2_hmac("sha256", senha.encode("utf-8"), bytes.fromhex(sal), int(it))
        except (ValueError, TypeError):
            return False, False
        ok = hmac.compare_digest(dk.hex(), esperado)
        return ok, ok and int(it) < PBKDF2_ITERACOES
    if _eh_legado(hash_salvo):
        ok = hmac.compare_digest(hashlib.sha256(senha.encode("utf-8")).hexdigest(), hash_salvo.lower())
        return ok, ok
    return False, False


def problema_senha(senha):
    """Regra mínima de senha. Devolve a mensagem do problema ou '' se estiver ok."""
    if len(senha or "") < SENHA_MIN:
        return f"A senha deve ter pelo menos {SENHA_MIN} caracteres."
    if (senha or "").strip() != senha:
        return "A senha não pode começar nem terminar com espaço."
    return ""


def senha_temporaria():
    """Senha provisória legível (sem caracteres ambíguos), para 'redefinir acesso'."""
    alfabeto = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alfabeto) for _ in range(10))


def novo_token():
    return secrets.token_urlsafe(32)


def hash_token(token):
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()
