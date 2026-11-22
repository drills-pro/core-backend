"""The staging bucket (S3, write only) and presigned HTTP reads: ray_common.store, re-exported here, plus the Ray Train storage root."""
import os

from ray_common.constants import CHECKSUM_ENV, CHECKSUM_WHEN_REQUIRED, LOCAL_SCHEME  # noqa: F401 - re-exported for callers and drift tests
from ray_common.store import DirStore, S3Store, data_url, http_get, store_from_env  # noqa: F401 - the names callers and tests import from here
from ray_core.constants import TRAIN_STATE_DIR


def train_state_storage(bucket: str, prefix: str):
    """(pyarrow filesystem, path) of Ray Train's storage root: under a top-level train-state/ prefix, so a bucket lifecycle rule can
    expire checkpoints without touching a run's manifest and outputs. The run's prefix keeps two runs apart; Ray adds the job id."""
    from urllib.parse import urlsplit

    from pyarrow import fs

    endpoint = os.environ["STAGING_ENDPOINT"]
    if endpoint.startswith(LOCAL_SCHEME):
        return fs.LocalFileSystem(), f"{endpoint.removeprefix(LOCAL_SCHEME)}/{TRAIN_STATE_DIR}/{prefix}"
    url = urlsplit(endpoint)
    s3 = fs.S3FileSystem(
        access_key=os.environ["STAGING_ACCESS_KEY"], secret_key=os.environ["STAGING_SECRET_KEY"],
        endpoint_override=url.netloc, scheme=url.scheme)
    return s3, f"{bucket}/{TRAIN_STATE_DIR}/{prefix}"
