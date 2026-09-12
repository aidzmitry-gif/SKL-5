"""Additive schema packet after Accounting's unregistered proposal.

Integration must freeze this SQL and table DDL in the final numbered migration.
This helper is used only on disposable evaluation databases.
"""
from pathlib import Path

from modules.wms.invoice_remainder import RemainderRelease, RemainderReleaseLine


def upgrade(connection):
    RemainderRelease.__table__.create(connection)
    RemainderReleaseLine.__table__.create(connection)
    # psycopg supports the complete multi-statement PL/pgSQL packet without parameters.
    cursor = connection.connection.dbapi_connection.cursor()
    cursor.execute(Path(__file__).with_name('invoice_remainder_guards.sql').read_text(encoding='utf-8'))
    cursor.close()
