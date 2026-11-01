"""An in-memory table behind job_lifecycle's SQL, for tests of anything that claims, settles or advances rows.

The fake answers the SQL job_lifecycle generates (claim, advance, release, the tenant listing) by equality with the
generator's output, and really enforces what the SQL does: a claim skips rows leased to someone else, and an advance
only updates a row that is still at the status it was read at. So a double settle or a lost race behaves as it would
in Postgres. Writes apply at once (there is no rollback), and `open` counts the connections handed out right now, so
a test can assert that none is held across a gateway call.
"""
import re
import uuid
from datetime import UTC, datetime, timedelta

from app.integrations import job_lifecycle
from app.integrations.job_lifecycle import JobTable

_UPDATE = re.compile(r"^UPDATE (\w+) SET (.+) WHERE id = \$1 AND status = \$2 RETURNING \*$")


def apply_advance(job: JobTable, rows: dict, sql: str, args) -> dict | None:
    """Run job_lifecycle.advance's UPDATE against `rows` (id -> row): the row moves only if it is still at the status
    it was read at. Also for a fake that holds the rows itself."""
    match = _UPDATE.match(sql)
    assert match and match.group(1) == job.table, sql
    assigned = [a.split(" = ") for a in match.group(2).split(", ")]
    extra = [name for name, _ in assigned if name not in ("status", "claimed_until", job.completed_col)]
    stamped = any(name == job.completed_col for name, _ in assigned)
    assert sql == job_lifecycle.advance_sql(job, extra, stamped), sql  # the generator is the only author of this SQL
    row = rows[args[0]]
    if row["status"] != args[1]:  # the compare-and-set
        return None
    row.update({name: None if v == "NULL" else args[int(v[1:]) - 1] for name, v in assigned})
    return dict(row)


class FakeJobTable:
    def __init__(self, job: JobTable, tenant_id=None):
        self.job, self.tenant_id = job, tenant_id or uuid.uuid4()
        self.rows: dict = {}
        self.now = datetime.now(UTC)  # what the SQL's now() is; move it to expire a lease
        self.open = 0
        self.seq = 0
        self.conn_cls = FakeJobConn  # a test that must answer other SQL too swaps in a subclass

    def seed(self, status, **cols) -> dict:
        """A live row: advance mutates it, so a test reads the outcome from it."""
        self.seq += 1
        row = {"id": uuid.uuid4(), "tenant_id": self.tenant_id, "status": status, "ray_job_id": "job", "claimed_until": None,
               "created_at": self.now + timedelta(microseconds=self.seq), "completed_at": None, **cols}
        self.rows[row["id"]] = row
        return row

    def __call__(self, tenant_id):
        return _Open(self)

    async def fetch_cross(self, sql, *args):
        assert sql == job_lifecycle.tenants_sql(self.job), sql
        tenants = {r["tenant_id"] for r in self.rows.values() if r["status"] in self.job.inflight}
        return [(t,) for t in sorted(tenants, key=str)]


class _Open:
    def __init__(self, table: FakeJobTable):
        self.table = table

    async def __aenter__(self):
        self.table.open += 1
        return self.table.conn_cls(self.table)

    async def __aexit__(self, *exc):
        self.table.open -= 1
        return False


class FakeJobConn:
    def __init__(self, table: FakeJobTable):
        self.table = table

    async def fetch(self, sql, *args):
        t = self.table
        assert sql == job_lifecycle.claim_sql(t.job), sql
        tenant_id, statuses, lease_seconds, limit = args
        due = sorted(
            (r for r in t.rows.values()
             if str(r["tenant_id"]) == str(tenant_id) and r["status"] in statuses
             and (r["claimed_until"] is None or r["claimed_until"] < t.now)),
            key=lambda r: r["created_at"],
        )[:limit]
        for r in due:
            r["claimed_until"] = t.now + timedelta(seconds=lease_seconds)
        return [dict(r) for r in due]

    async def fetchrow(self, sql, *args):
        return apply_advance(self.table.job, self.table.rows, sql, args)

    async def execute(self, sql, *args):
        t = self.table
        assert sql == job_lifecycle.release_sql(t.job), sql
        row = t.rows.get(args[0])
        if row is not None and row["claimed_until"] == args[1]:  # only the lease this claim took
            row["claimed_until"] = None


def patch(monkeypatch, table: FakeJobTable, *modules):
    """job_lifecycle (and each module that opens a TenantConn of its own) reads and writes `table`."""
    for mod in (job_lifecycle, *modules):
        monkeypatch.setattr(mod, "TenantConn", table)
    monkeypatch.setattr(job_lifecycle, "fetch", table.fetch_cross)
