"""Banco usado pelos testes.

Padrão: SQLite na pasta temporária do teste.
Com a variável LAUDO_TEST_PG_URL: PostgreSQL, num schema temporário próprio de cada
teste (ex.: teste_3f9a...), apagado ao final — os dados reais (schema public) não são tocados.
"""
import os
import uuid

from saas import db

PG_URL = os.environ.get("LAUDO_TEST_PG_URL")
os.environ["LAUDO_TESTE"] = "1"    # o app.py ignora o banco dos Secrets durante os testes


def abrir():
    if PG_URL:
        schema = "teste_" + uuid.uuid4().hex[:12]
        db.configurar_postgres(PG_URL, schema=schema)
        return schema
    db.configurar_postgres(None)
    db.set_db_path(None)
    return None


def fechar(schema):
    if schema:
        with db.conectar() as con:
            con.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
    db.configurar_postgres(None)
