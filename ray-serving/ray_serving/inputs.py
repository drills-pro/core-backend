"""How a request body becomes the model's input feeds, one encoder factory per kind of input.

Runs in the Ray Serve replica (the ray_serving package; core-api also embeds this file as text for legacy deployments), so it imports nothing at module level: each
factory imports what it needs, and a deployment needs only the libraries of its own task. A factory takes the
`preprocessing` entry of the model's model.json, the URL of tokenizer.json and the model's first input name, and
returns `encode(body) -> (feeds, width, height)`: the arrays for `session.run` and the size of the image the caller
sent (0, 0 for text).
"""


MAX_IMAGE_PIXELS = 25_000_000  # width * height of the image a caller sends; larger is refused before it is decoded (bad_input)


def image_encoder(pre, tokenizer_url, input_name):
    """Body {"image_base64": ...}. What the trainer did to every training image, read from model.json
    (ray_core.result.load_image)."""
    import base64
    import io

    import numpy as np
    from PIL import Image

    def decode(value):
        """Plain, URL-safe or unpadded base64, with or without line breaks, or a `data:image/...;base64,` URI."""
        if not isinstance(value, str):
            raise ValueError("image_base64 must be a string")
        value = "".join((value.partition(",")[2] if value.startswith("data:") else value).split())
        return base64.b64decode(value.replace("-", "+").replace("_", "/") + "=" * (-len(value) % 4))

    def encode(body):
        opened = Image.open(io.BytesIO(decode(body["image_base64"])))  # reads the header only
        if opened.width * opened.height > MAX_IMAGE_PIXELS:
            raise ValueError(f"image is {opened.width}x{opened.height}, more than {MAX_IMAGE_PIXELS} pixels")
        sent = opened.convert("RGB")
        width, height = pre["resize"]
        img = sent.resize((width, height), getattr(Image, pre["resample"].upper()))
        arr = (np.asarray(img, dtype=np.float32) * pre["rescale"] - np.array(pre["mean"], dtype=np.float32)) / np.array(pre["std"], dtype=np.float32)
        return {input_name: arr.transpose(2, 0, 1)[None, ...]}, sent.width, sent.height

    return encode


def text_encoder(pre, tokenizer_url, input_name):
    """Body {"text": ...}. The tokenizer the model was trained with, padded and truncated to the trained length."""
    import urllib.request

    import numpy as np
    from tokenizers import Tokenizer

    tokenizer = Tokenizer.from_str(urllib.request.urlopen(tokenizer_url, timeout=120).read().decode())
    tokenizer.enable_truncation(pre["max_length"])
    # the base model's own pad token (model.json since the trainer records it; an older model used the BERT one, the library's default)
    tokenizer.enable_padding(length=pre["max_length"], pad_id=pre.get("pad_id") or 0, pad_token=pre.get("pad_token") or "[PAD]")

    def encode(body):
        enc = tokenizer.encode(body["text"])
        return {"input_ids": np.array([enc.ids], dtype=np.int64), "attention_mask": np.array([enc.attention_mask], dtype=np.int64)}, 0, 0

    return encode


def tabular_encoder(pre, tokenizer_url, input_name):
    """Body {feature: value, ...}: one row in the trainer's feature order (model.json preprocessing.features). A missing or null
    value is NaN, as at training; a value that is not a number fails the call (bad_input). A category column (preprocessing.categories)
    takes its text, which becomes the index the trainer gave it."""
    import numpy as np

    features = pre["features"]
    codes = {name: {v: i for i, v in enumerate(values)} for name, values in (pre.get("categories") or {}).items()}

    def cell(name, value):
        if value is None:
            return np.nan
        return codes[name].get(str(value), np.nan) if name in codes else float(value)  # a category the model never saw is missing

    def encode(body):
        return {input_name: np.array([[cell(name, body.get(name)) for name in features]], dtype=np.float32)}, 0, 0

    return encode
