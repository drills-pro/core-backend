"""A FakeConn whose transactions are visible: `calls` gets a BEGIN, then the queries, then a COMMIT (or a ROLLBACK when the block
raised), so a test can say that a write and its audit row share one transaction."""
from tests.support import FakeConn

BEGIN, COMMIT, ROLLBACK = "BEGIN", "COMMIT", "ROLLBACK"


class _Transaction:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        self.conn.calls.append((BEGIN, ()))

    async def __aexit__(self, exc_type, exc, tb):
        self.conn.calls.append((ROLLBACK if exc_type else COMMIT, ()))
        return False


class TxConn(FakeConn):
    """`fail_on`: a SQL prefix whose execute() raises, to see what a failure inside the transaction does."""

    def __init__(self, *args, fail_on: str | None = None, **kw):
        super().__init__(*args, **kw)
        self.fail_on = fail_on

    def transaction(self):
        return _Transaction(self)

    async def execute(self, sql, *args):
        if self.fail_on and sql.startswith(self.fail_on):
            self.calls.append((sql, args))
            raise RuntimeError("database down")
        return await super().execute(sql, *args)


def statements(conn) -> list[str]:
    """What the connection was asked, as the first words of each statement (transaction markers included)."""
    return [sql.split(" (")[0].split(" SET")[0].split(" WHERE")[0] for sql, _ in conn.calls]
