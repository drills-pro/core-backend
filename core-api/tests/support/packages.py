"""Doubles for the two things the job-package lifecycle stands on: the staging bucket and the job_packages tables.

`PackageDb` answers the queries of app.core.job_packages from memory, so a test drives the lifecycle through its public
functions and reads the outcome from `rows` and `audit`; `FakeS3` is a bucket that can also fail on demand.
"""
import io
from datetime import UTC, datetime
from types import SimpleNamespace

from botocore.exceptions import ClientError

from app.core.enums import PackageKind, PackageStatus, SmokeStatus
from tests.support import TenantCtx

NOT_FOUND = "404"


def client_error(code: str = NOT_FOUND, op: str = "Op") -> ClientError:
    return ClientError({"Error": {"Code": code}}, op)


def package_row(name, sha, key, actor, status=PackageStatus.CANDIDATE):
    return {
        "name": name, "sha256": sha, "s3_key": key, "status": status, "published_by": actor, "updated_by": actor, "updated_at": datetime.now(UTC),
        "smoke_status": SmokeStatus.PENDING, "smoke_job_id": None}


class FakeS3:
    """A staging bucket holding `objects` ({key: bytes}). `head` / `get` are errors every such call raises instead."""

    def __init__(self, objects=None, head=None, get=None):
        self.objects, self.head, self.get = dict(objects or {}), head, get
        self.heads, self.gets, self.puts = [], [], []

    def head_object(self, Bucket, Key):
        self.heads.append(Key)
        if self.head:
            raise self.head
        if Key not in self.objects:
            raise client_error(NOT_FOUND, "HeadObject")
        return {"ContentLength": len(self.objects[Key])}

    def get_object(self, Bucket, Key):
        self.gets.append(Key)
        if self.get:
            raise self.get
        if Key not in self.objects:
            raise client_error("NoSuchKey", "GetObject")
        return {"Body": io.BytesIO(self.objects[Key])}

    def put_object(self, Bucket, Key, Body):
        self.puts.append(Key)
        self.objects[Key] = Body

    def generate_presigned_url(self, op, Params, ExpiresIn):
        return f"http://ray-side/{Params['Key']}"


class PackageDb:
    """The job_packages table and the job_package_audit log, as the queries of app.core.job_packages see them."""

    def __init__(self):
        self.rows: dict[tuple[str, str], dict] = {}
        self.audit: list[tuple] = []  # (name, sha256, action, actor)

    def pool(self):
        async def get_pool():
            return SimpleNamespace(acquire=lambda: TenantCtx(self))

        return get_pool

    def transaction(self):
        return TenantCtx(None)

    async def fetchval(self, sql, name, sha, key, actor, kind, manifest, requirements):  # the INSERT of register
        if (name, sha) in self.rows:
            return None
        self.rows[(name, sha)] = package_row(name, sha, key, actor) | {"kind": kind, "manifest": manifest, "requirements": requirements}
        return True

    async def execute(self, sql, *args):
        if sql.startswith("INSERT INTO job_package_audit"):
            self.audit.append(args)
        elif "smoke_job_id = $3" in sql:  # start_smoke: only the first job id sticks
            name, sha, job_id = args
            row = self.rows[(name, sha)]
            row["smoke_job_id"] = row["smoke_job_id"] or job_id
        else:  # promote: every other stable row of the name is retired
            name, was, sha, now, actor = args
            for row in self.rows.values():
                if row["name"] == name and row["status"] == was and row["sha256"] != sha:
                    row |= {"status": now, "updated_by": actor}

    async def fetchrow(self, sql, *args):
        if sql.startswith("UPDATE") and "SET status = $3" in sql:
            name, sha, status, actor = args
            row = self.rows.get((name, sha))
            if row is not None:
                row |= {"status": status, "updated_by": actor}
            return row
        if sql.startswith("UPDATE") and "smoke_job_id = NULL" in sql:  # reset_smoke: a compare-and-set
            name, sha, status, was, now = args
            row = self.rows.get((name, sha))
            if row is None or row["status"] != status or row["smoke_status"] != was:
                return None
            row |= {"smoke_status": now, "smoke_job_id": None}
            return row
        if sql.startswith("UPDATE"):  # set_smoke
            name, sha, smoke = args
            self.rows[(name, sha)]["smoke_status"] = smoke
            return self.rows[(name, sha)]
        if "ORDER BY updated_at DESC LIMIT 1" in sql:  # latest_stable
            name, status = args
            return next((r for r in reversed(list(self.rows.values())) if r["name"] == name and r["status"] == status), None)
        return self.rows.get(args)

    async def fetch(self, sql, *args):  # list_kind
        kind, status = args
        return [r for r in self.rows.values() if r.get("kind", PackageKind.RUNTIME) == kind and status in (None, r["status"])]
