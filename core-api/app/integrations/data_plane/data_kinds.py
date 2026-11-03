"""How a dataset's kind lays its version out in the repo and which ingest job reads an upload of it. A new kind of data is
one entry here plus its ingest job module and a DataKind member."""
from typing import NamedTuple

from app.core.enums import ImportSource
from app.core.task_catalog import DataKind
from app.integrations.data_plane import layout
from app.integrations.data_plane.job_catalog import JobType


class KindLayout(NamedTuple):
    data_key: str  # the file a version's rows are read from
    has_objects: bool  # the rows list files stored under layout.OBJECTS_PREFIX (images), not the data itself (a table)
    upload_job: JobType  # the job that ingests an upload


LAYOUTS: dict[DataKind, KindLayout] = {
    DataKind.IMAGES: KindLayout(layout.INDEX_KEY, True, JobType.UPLOAD_IMAGES),
    DataKind.TABULAR: KindLayout(layout.DATA_KEY, False, JobType.UPLOAD_CSV),
    DataKind.TEXT: KindLayout(layout.DATA_KEY, False, JobType.UPLOAD_CSV),
}


def ingest_job_type(source: str, kind: DataKind) -> JobType:
    """Which catalog entry imports this source into a dataset of this kind."""
    return JobType.HUGGINGFACE if source == ImportSource.HUGGINGFACE else LAYOUTS[kind].upload_job
