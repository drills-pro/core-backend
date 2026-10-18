from enum import StrEnum


class ImportShape(StrEnum):
    """How data enters a dataset; TaskType decides which shapes a project accepts."""

    UPLOAD_URL = "upload-url"
    UPLOAD_ZIP = "upload-zip"
    UPLOAD_YOLO = "upload-yolo"
    UPLOAD_COCO = "upload-coco"
    TABULAR = "tabular"
    IMPORT_HF_IMAGES = "import-hf-images"
    IMPORT_HF_TABULAR = "import-hf-tabular"
