"""A TenantConn stand-in that counts the connections handed out, so a test can assert that none is held while a
gateway (MLflow, Ray, Serve) is called."""


class Opened:
    def __init__(self):
        self.now = 0  # open right now
        self.total = 0  # ever opened


def patch_counted_conn(monkeypatch, conn, *modules) -> Opened:
    """Make each module's TenantConn hand out `conn` and count the `async with`s on it."""
    opened = Opened()

    class Ctx:
        async def __aenter__(self):
            opened.now += 1
            opened.total += 1
            return conn

        async def __aexit__(self, *exc):
            opened.now -= 1
            return False

    for mod in modules:
        monkeypatch.setattr(mod, "TenantConn", lambda tenant_id: Ctx())
    return opened
