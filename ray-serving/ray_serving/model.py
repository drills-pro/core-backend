"""One deployment's model in a replica: fetch the ONNX file and its manifest, then answer requests with it.

No Ray in here, so it runs (and is tested) anywhere onnxruntime does. app.py decides when a model is loaded and kept.
"""
import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Callable
from urllib.parse import urlsplit

from ray_serving import inputs, postprocess

FETCH_TIMEOUT_SECONDS = 120
MAX_FETCH_BYTES = 2 * 1024**3  # the most one model, manifest or tokenizer download may be
DEFAULT_RESAMPLE = "bicubic"  # a model trained before model.json named its filter used this one (core-api task_catalog.IMAGENET_PREPROCESSING)


class ErrorCode(StrEnum):
    """The `error.code` of a failed answer; core-api (deployments/constants.py ServeErrorCode) maps each to a status."""

    MODEL_LOAD_FAILED = "model_load_failed"
    UNKNOWN_FUNCTION = "unknown_function"
    BAD_INPUT = "bad_input"


class ServeError(Exception):
    def __init__(self, code: ErrorCode, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ModelSpec:
    """What core-api sends with every request: where the model is and how to run it (the task_types row, ServingParams)."""

    model_url: str
    manifest_url: str
    tokenizer_url: str
    class_names: list[str]
    params: dict[str, Any]  # ServingParams without providers
    providers: list[str]
    encoder: str
    postprocess: str

    @classmethod
    def from_dict(cls, d: dict) -> "ModelSpec":
        try:
            return cls(**{f: d[f] for f in cls.__dataclass_fields__})
        except (KeyError, TypeError) as e:
            raise ServeError(ErrorCode.BAD_INPUT, f"model spec: {e!r}") from e


@dataclass
class Loaded:
    session: Any
    encode: Callable
    spec: ModelSpec


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # a redirect would leave the allow-listed base: the 3xx surfaces as an HTTPError


_opener = urllib.request.build_opener(_NoRedirect)


def check_url(url: str, base: str) -> None:
    """Refuse a model URL that is not under `base` (the artifact store core-api is configured with): the request body is not
    trusted to say where the replica connects. Same scheme and host, path inside the base's, no `..`."""
    u, b = urlsplit(url), urlsplit(base)
    prefix = b.path.rstrip("/") + "/"
    if (u.scheme, u.netloc) != (b.scheme, b.netloc) or not u.path.startswith(prefix) or ".." in u.path.split("/"):
        raise ServeError(ErrorCode.MODEL_LOAD_FAILED, "model url is outside the allowed artifact store")


def _fetch(url: str) -> bytes:
    with _opener.open(url, timeout=FETCH_TIMEOUT_SECONDS) as resp:  # noqa: S310 - checked against the allowed base by check_url
        return _read_capped(resp)


def _read_capped(resp) -> bytes:
    data = resp.read(MAX_FETCH_BYTES + 1)
    if len(data) > MAX_FETCH_BYTES:
        raise ValueError(f"artifact is larger than {MAX_FETCH_BYTES} bytes")
    return data


def _function(module, name: str) -> Callable:
    found = getattr(module, name, None)
    if not callable(found) or name.startswith("_"):
        raise ServeError(ErrorCode.UNKNOWN_FUNCTION, f"{module.__name__} has no function {name}")
    return found


def load(spec: ModelSpec, allowed_base: str) -> Loaded:
    for url in filter(None, (spec.model_url, spec.manifest_url, spec.tokenizer_url)):  # image models have no tokenizer
        check_url(url, allowed_base)
    make_encoder, _ = _function(inputs, spec.encoder), _function(postprocess, spec.postprocess)
    try:
        import onnxruntime as ort

        session = ort.InferenceSession(_fetch(spec.model_url), providers=spec.providers)
        pre = {"resample": DEFAULT_RESAMPLE, **json.loads(_fetch(spec.manifest_url))["preprocessing"]}
        return Loaded(session, make_encoder(pre, spec.tokenizer_url, session.get_inputs()[0].name), spec)
    except Exception as e:  # noqa: BLE001 - whatever is wrong with the model is the caller's model_load_failed
        raise ServeError(ErrorCode.MODEL_LOAD_FAILED, f"{type(e).__name__}: {e}") from e


def predict(loaded: Loaded, body: dict) -> list:
    try:
        feeds, width, height = loaded.encode(body)
    except Exception as e:  # noqa: BLE001 - a body the encoder cannot read
        raise ServeError(ErrorCode.BAD_INPUT, f"{type(e).__name__}: {e}") from e
    outputs = loaded.session.run(None, feeds)
    return _function(postprocess, loaded.spec.postprocess)(outputs, loaded.spec.class_names, width, height, loaded.spec.params)
