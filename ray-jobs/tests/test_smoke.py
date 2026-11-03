"""The smoke entrypoint against an in-memory staging bucket."""
import json

from ray_jobs.common.constants import REPORT_KEY, SMOKE_KEY
from ray_jobs.entrypoints import smoke


class Bucket:
    def __init__(self, bucket, prefix, *_):
        self.prefix = prefix

    puts: dict = {}

    def put(self, key, data):
        self.puts[self.prefix + key] = data

    def size(self, key):
        return len(self.puts[self.prefix + key])

    def download(self, key, path):
        path.write_bytes(self.puts[self.prefix + key])


def test_the_smoke_import_succeeds_and_says_so(monkeypatch, fake_parquet):
    Bucket.puts = {}
    for name in ("ray_jobs.runner.S3Store", "ray_jobs.entrypoints.smoke.S3Store"):
        monkeypatch.setattr(name, Bucket)
    for var in ("STAGING_ENDPOINT", "STAGING_ACCESS_KEY", "STAGING_SECRET_KEY"):
        monkeypatch.setenv(var, "x")
    assert smoke.main(["", json.dumps({"job_id": "j", "bucket": "b", "prefix": "p/"})]) == 0
    assert json.loads(Bucket.puts["p/" + SMOKE_KEY]) == {"ok": True}
    assert json.loads(Bucket.puts["p/" + REPORT_KEY])["outcome"] == "ok"
