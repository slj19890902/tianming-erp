"""BOM savepoints must remain owned by the caller's real transaction."""

from contextlib import contextmanager


@contextmanager
def atomic_bom(db):
    connection = db.connection()
    # Python sqlite3 legacy transaction control does not BEGIN for a SELECT.
    # A first SAVEPOINT followed by RELEASE would therefore COMMIT despite a
    # later Session.rollback(). Start the real outer transaction explicitly.
    # Do not restart or commit an already-active caller transaction.
    if (connection.dialect.name == "sqlite"
            and not connection.connection.driver_connection.in_transaction):
        connection.exec_driver_sql("BEGIN IMMEDIATE")
    with db.begin_nested():
        yield
