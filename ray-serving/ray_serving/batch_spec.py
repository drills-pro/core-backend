"""Reading the `predict_batch` job spec (core-api app/integrations/job_spec.py BatchPredictSpec). Imports nothing heavy, so core-api's
contract test can import it without numpy or onnxruntime."""
from urllib.parse import urlsplit

from ray_common.spec import parse_envelope
from ray_serving.constants import PREDICT_BATCH_KIND

INDEX_PATH_COLUMN = "path"  # core-api layout.INDEX_COLUMNS[0]


def parse_spec(text: str) -> dict:
    """The envelope's checks (ray_common.spec); returns the payload."""
    return parse_envelope(text, PREDICT_BATCH_KIND, "predict_batch")["payload"]


def allowed_base(model_url: str) -> str:
    """The directory of the model artifact: model.json and tokenizer.json sit beside model.onnx (core-api serve_client.served_model).
    The spec is written by core-api, so this only satisfies model.load's own url check."""
    u = urlsplit(model_url)
    return f"{u.scheme}://{u.netloc}{u.path.rsplit('/', 1)[0]}"
