"""Camada multiempresa (SaaS) do CH360 (Vistoria Veicular).

Módulos:
- db         : conexão SQLite, esquema e utilidades de data/hora
- seguranca  : hash de senha, sessões, bloqueio por tentativas
- rbac       : perfis e permissões
- servicos   : regras de negócio com isolamento por empresa (o "backend")
- migracao   : importação não destrutiva dos arquivos JSON antigos

Nenhum módulo deste pacote importa o Streamlit: tudo pode ser testado isoladamente.
"""
