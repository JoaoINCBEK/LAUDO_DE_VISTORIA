"""Perfis e permissões (RBAC).

As telas usam `pode()` só para decidir o que mostrar; a proteção real está em
`servicos.py`, que chama `exigir()` antes de qualquer leitura/escrita.

Para criar um perfil novo no futuro (supervisor, gerente, auditor,
recepcionista...), basta acrescentar uma entrada em PERFIS/PERMISSOES.
"""

SUPER_ADMIN = "super_admin"
ADMIN = "admin"
VISTORIADOR = "vistoriador"

PERFIS = {
    SUPER_ADMIN: "Super Administrador",
    ADMIN: "Administrador",
    VISTORIADOR: "Vistoriador",
}

# Perfis que um administrador de empresa pode atribuir aos usuários da própria empresa.
PERFIS_DA_EMPRESA = [ADMIN, VISTORIADOR]

_EMPRESA_ADMIN = {
    "empresa.dashboard",
    "empresa.configurar",
    "usuarios.gerenciar",
    "vistorias.criar",
    "vistorias.ver_todas",
    "vistorias.ver_proprias",
    "vistorias.editar_proprias",
    "vistorias.editar_concluidas",
    "vistorias.cancelar",
    "vistorias.apagar",
    "veiculos.ver",
    "clientes.ver",
    "laudos.ver_todos",
    "laudos.ver_proprios",
    "laudos.enviar",
    "relatorios.ver",
    "logs.ver_empresa",
    "emitentes.gerenciar",
}

_VISTORIADOR = {
    "vistorias.criar",
    "vistorias.ver_proprias",
    "vistorias.editar_proprias",
    "vistorias.cancelar_proprias",
    "laudos.ver_proprios",
    "laudos.enviar",
    "emitentes.gerenciar",
}

_SUPER = {
    "plataforma.gerenciar",      # empresas, planos, limites, integrações
    "usuarios.gerenciar_todos",
    "logs.ver_global",
    # visão de qualquer empresa (somente leitura operacional)
    "empresa.dashboard",
    "vistorias.ver_todas",
    "veiculos.ver",
    "clientes.ver",
    "laudos.ver_todos",
    "relatorios.ver",
    "logs.ver_empresa",
    "usuarios.gerenciar",
}

PERMISSOES = {
    SUPER_ADMIN: _SUPER,
    ADMIN: _EMPRESA_ADMIN,
    VISTORIADOR: _VISTORIADOR,
}


class AcessoNegado(Exception):
    """Operação não permitida para o usuário autenticado."""


def pode(ator, permissao):
    return ator is not None and permissao in PERMISSOES.get(ator.perfil, set())


def exigir(ator, permissao):
    if ator is None:
        raise AcessoNegado("Sessão expirada. Entre novamente.")
    if not pode(ator, permissao):
        raise AcessoNegado("Você não tem permissão para esta ação.")


def nome_perfil(perfil):
    return PERFIS.get(perfil, perfil)
