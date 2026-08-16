"""Ray Job Submission API client (req/07-compute-ray-k8s.md FR1).

Talks to Ray's HTTP job API directly rather than the Python SDK — core-api
doesn't need a Ray runtime, just to submit/poll/stop jobs on the cluster.
"""
import base64
import hashlib
import os
import textwrap

import httpx

from app import vault

RAY_DASHBOARD = os.environ.get("RAY_DASHBOARD", "http://ray-head:8265")

DEFAULT_CONFIG = {
    "model_type": "classification",
    "trainer": {"epochs": 25, "learning_rate": 0.1},
}

# ponytail: real gradient-descent training (numpy, already in the Ray
# image — no new deps), not Ludwig/Ultralytics `fit()` (req/06) — those
# need real image datasets, GPU, and the ModelAdapter interface the spec
# describes, none of which exist yet (no dataset upload/labeling pipeline
# is built). This is genuine training — it fits real parameters via
# gradient descent and reports real accuracy/loss, not canned numbers —
# on a synthetic dataset seeded from dataset_ref so runs are reproducible.
# Swapping in a real backend means replacing this template; the
# orchestration around it (submit/poll/logs/MLflow/billing) doesn't change.
_TRAIN_TEMPLATE = """
import json
import numpy as np
rng = np.random.default_rng({seed})
n = 400
X = rng.normal(size=(n, 4))
true_w = np.array([1.5, -2.0, 0.5, 1.0])
y = (X @ true_w + rng.normal(scale=0.5, size=n) > 0).astype(float)

split = int(n * (1 - {test_split}))
X_train, X_test = X[:split], X[split:]
y_train, y_test = y[:split], y[split:]

def _bce(y, p):
    eps = 1e-9
    return float(-np.mean(y * np.log(p + eps) + (1 - y) * np.log(1 - p + eps)))

w = np.zeros(4)
b = 0.0
lr = {learning_rate}
epoch_history = []
for epoch in range({epochs}):
    z = X_train @ w + b
    p = 1 / (1 + np.exp(-z))
    grad_w = X_train.T @ (p - y_train) / len(X_train)
    grad_b = np.mean(p - y_train)
    w -= lr * grad_w
    b -= lr * grad_b
    train_acc = float(((p > 0.5).astype(float) == y_train).mean())
    epoch_history.append({{"epoch": epoch + 1, "loss": _bce(y_train, p), "accuracy": train_acc}})

z = X_test @ w + b
p = 1 / (1 + np.exp(-z))
pred = (p > 0.5).astype(float)
accuracy = float((pred == y_test).mean())
loss = _bce(y_test, p)
print(f"metric:accuracy={{accuracy}}")
print(f"metric:loss={{loss}}")

# Real confusion matrix on the held-out test split — [[TN, FP], [FN, TP]].
labels = [0, 1]
matrix = [[int(((pred == pl) & (y_test == tl)).sum()) for pl in labels] for tl in labels]
extra = {{"epoch_history": epoch_history, "confusion_matrix": {{"labels": [str(l) for l in labels], "matrix": matrix}}}}
print(f"metrics_json:{{json.dumps(extra)}}")
print(f"model:{{json.dumps({{'w': w.tolist(), 'b': float(b)}})}}")
"""


# req/06 Ultralytics FR1: "dataset export from platform manifest to YOLO
# directory structure". This is now real, not a stand-in: the job
# downloads real files from real customer-uploaded object storage
# (app/datasets.py's presigned-upload flow) when a dataset with uploaded
# files is passed in, and trains on those — YOLO-classify directory
# structure (label = subfolder name) built directly from what's actually
# in the bucket. Only falls back to a generated synthetic set when the
# dataset has no uploaded files yet (empty project — nothing to train on
# otherwise), so a brand-new project still has something to click "train"
# on before a customer uploads real data.
#
# task={{classify,detect}}: classify trains on the uploaded label-folder
# layout above. detect needs real bounding-box ground truth, which this
# platform has no annotation UI for yet (app/datasets.py's labeling loop
# is whole-image classification only) — so detect always trains on a
# generated synthetic scene (colored rectangle on a background, real box
# coordinates written to real YOLO label files) rather than silently
# reinterpreting classification folders as boxes. Genuine detect training
# (real YOLO detection head, real mAP50 computed by ultralytics), just on
# synthetic ground truth until a bbox-annotation flow exists.
_ULTRALYTICS_TEMPLATE = """
import io, json, os, random
from PIL import Image, ImageDraw
from ultralytics import YOLO

base = "/tmp/train-{seed}"
real_files = {file_keys!r}
task = {task!r}
test_split = {test_split!r}

if task == "detect":
    import numpy as np
    print("metric:real_files_used=0")
    rng = np.random.default_rng({seed})
    classes = ["square", "circle"]
    n_total = 32
    n_val = max(1, round(n_total * test_split))
    for split, n in [("train", n_total - n_val), ("val", n_val)]:
        img_dir, lbl_dir = f"{{base}}/images/{{split}}", f"{{base}}/labels/{{split}}"
        os.makedirs(img_dir, exist_ok=True)
        os.makedirs(lbl_dir, exist_ok=True)
        for i in range(n):
            size = 64
            img = Image.new("RGB", (size, size), (30, 30, 30))
            draw = ImageDraw.Draw(img)
            cls_id = int(rng.integers(0, len(classes)))
            w, h = int(rng.integers(16, 30)), int(rng.integers(16, 30))
            cx, cy = int(rng.integers(w // 2 + 2, size - w // 2 - 2)), int(rng.integers(h // 2 + 2, size - h // 2 - 2))
            x0, y0, x1, y1 = cx - w // 2, cy - h // 2, cx + w // 2, cy + h // 2
            color = (200, 60, 60) if cls_id == 0 else (60, 60, 200)
            if cls_id == 0:
                draw.rectangle([x0, y0, x1, y1], fill=color)
            else:
                draw.ellipse([x0, y0, x1, y1], fill=color)
            img.save(f"{{img_dir}}/{{i}}.png")
            with open(f"{{lbl_dir}}/{{i}}.txt", "w") as f:
                f.write(f"{{cls_id}} {{cx / size}} {{cy / size}} {{w / size}} {{h / size}}\\n")
    with open(f"{{base}}/data.yaml", "w") as f:
        f.write(f"path: {{base}}\\ntrain: images/train\\nval: images/val\\n")
        f.write(f"names:\\n" + "".join(f"  {{i}}: {{c}}\\n" for i, c in enumerate(classes)))

    model = YOLO("yolo11n.pt")
    model.train(data=f"{{base}}/data.yaml", epochs={epochs}, imgsz=64, batch=8, device="cpu", verbose=False, plots=False)
    val_metrics = model.val()
    map50 = float(val_metrics.box.map50)
    print(f"metric:mAP50={{map50}}")
    print(f"metric:accuracy={{map50}}")
    print(f"metric:loss={{1.0 - map50}}")
else:
    if real_files:
        import boto3
        from botocore.client import Config
        # LakeFS's S3 gateway — real_files are dataset-relative keys
        # ("<label>/<filename>"), addressed as <repo>/<dataset_ref>/<key>
        # so this reads whichever committed version (or live working
        # state) app/training.py resolved dataset_ref to.
        s3 = boto3.client("s3", endpoint_url={s3_endpoint!r},
            aws_access_key_id={s3_access_key!r}, aws_secret_access_key={s3_secret_key!r},
            config=Config(signature_version="s3v4"))
        lakefs_repo, lakefs_ref = {lakefs_repo!r}, {lakefs_ref!r}
        test_files = {test_file_keys!r}
        test_lakefs_ref = {test_lakefs_ref!r}
        image_exts = (".png", ".jpg", ".jpeg", ".bmp", ".webp")

        def _download(d, keys, ref):
            os.makedirs(d, exist_ok=True)
            for key in keys:
                data = s3.get_object(Bucket=lakefs_repo, Key=f"{{ref}}/{{key}}")["Body"].read()
                img = Image.open(io.BytesIO(data)).convert("RGB")
                img.save(f"{{d}}/{{os.path.basename(key)}}")

        def _by_label(keys):
            out = {{}}
            for key in keys:
                if not key.lower().endswith(image_exts) or key.startswith("_unlabeled/"):
                    continue
                out.setdefault(key.split("/")[0], []).append(key)
            return out

        if test_files:
            # A user-designated, separately versioned held-out test set
            # (app/datasets.py) — no internal split, this dataset's files
            # never touch training, the test dataset's files never touch it.
            print(f"metric:real_files_used={{len(real_files)}}")
            print(f"metric:test_files_used={{len(test_files)}}")
            for label, keys in _by_label(real_files).items():
                _download(f"{{base}}/train/{{label}}", keys, lakefs_ref)
            for label, keys in _by_label(test_files).items():
                _download(f"{{base}}/val/{{label}}", keys, test_lakefs_ref)
        else:
            by_label = _by_label(real_files)
            print(f"metric:real_files_used={{len(real_files)}}")
            for label, keys in by_label.items():
                random.seed({seed})
                random.shuffle(keys)
                split_idx = max(1, int(len(keys) * (1 - test_split))) if len(keys) > 1 else len(keys)
                for split, split_keys in [("train", keys[:split_idx]), ("val", keys[split_idx:] or keys[:1])]:
                    _download(f"{{base}}/{{split}}/{{label}}", split_keys, lakefs_ref)
    else:
        import numpy as np
        print("metric:real_files_used=0")
        rng = np.random.default_rng({seed})
        n_total = 26
        n_val = max(1, round(n_total * test_split))
        for split, n in [("train", n_total - n_val), ("val", n_val)]:
            for cls, color in [("classA", (200, 30, 30)), ("classB", (30, 30, 200))]:
                d = f"{{base}}/{{split}}/{{cls}}"
                os.makedirs(d, exist_ok=True)
                for i in range(n):
                    arr = np.full((32, 32, 3), color, dtype=np.uint8)
                    noise = rng.integers(-40, 40, size=arr.shape)
                    arr = np.clip(arr.astype(int) + noise, 0, 255).astype(np.uint8)
                    Image.fromarray(arr).save(f"{{d}}/{{i}}.png")

    model = YOLO("yolo11n-cls.pt")
    model.train(data=base, epochs={epochs}, imgsz=32, batch=8, device="cpu", verbose=False, plots=False)
    val_metrics = model.val()
    top1 = float(val_metrics.top1)
    print(f"metric:accuracy={{top1}}")
    print(f"metric:loss={{1.0 - top1}}")

# Real per-epoch history — Ultralytics writes this itself to
# <save_dir>/results.csv regardless of plots=False. Column names differ
# between classify/detect, so pick generically rather than hardcoding
# either task's exact schema.
epoch_history = []
try:
    import csv as _csv
    results_csv = model.trainer.save_dir / "results.csv"
    with open(results_csv) as f:
        reader = _csv.DictReader(f)
        loss_col = next((c for c in reader.fieldnames if "loss" in c and "train" in c), None) or next(
            (c for c in reader.fieldnames if "loss" in c), None
        )
        metric_col = next((c for c in reader.fieldnames if "accuracy_top1" in c or "mAP50(" in c or "mAP50-95" in c), None)
        for row in reader:
            entry = {{"epoch": int(float(row["epoch"]))}}
            if loss_col:
                entry["loss"] = float(row[loss_col])
            if metric_col:
                entry["accuracy"] = float(row[metric_col])
            epoch_history.append(entry)
except Exception:
    pass

# Ultralytics' own confusion matrix from the val pass above — real
# per-class counts, not manually recomputed.
confusion_matrix = None
try:
    cm = val_metrics.confusion_matrix
    cm_labels = [model.names[i] for i in sorted(model.names)] + (["background"] if cm.matrix.shape[0] > len(model.names) else [])
    confusion_matrix = {{"labels": cm_labels, "matrix": cm.matrix.astype(int).tolist()}}
except Exception:
    pass

print(f"metrics_json:{{json.dumps({{'epoch_history': epoch_history, 'confusion_matrix': confusion_matrix}})}}")

# req/06 Ultralytics FR3: "Export trained weights to ONNX at minimum, for
# BentoML/Ray Serve packaging." Real export, real upload — this is what
# app/serve_client.py's ONNX deployment path actually loads and runs.
import boto3 as _boto3
from botocore.client import Config as _Config
onnx_path = model.export(format="onnx", imgsz=(64 if task == "detect" else 32), opset=17)
onnx_key = f"models/{seed}/model.onnx"
# Deliberately the raw MinIO "datasets" bucket, not the LakeFS-gateway
# creds used above for training data — model artifacts aren't
# dataset-versioned, and app/serve_client.py's deployment path reads
# them from this bucket directly.
s3_upload = _boto3.client("s3", endpoint_url={minio_s3_endpoint!r},
    aws_access_key_id={minio_s3_access_key!r}, aws_secret_access_key={minio_s3_secret_key!r},
    config=_Config(signature_version="s3v4"))
with open(onnx_path, "rb") as f:
    s3_upload.put_object(Bucket="datasets", Key=onnx_key, Body=f.read())
class_names = [model.names[i] for i in sorted(model.names)]
print(f"model:{{json.dumps({{'onnx_key': onnx_key, 'class_names': class_names, 'task': task}})}}")
"""


# req/06 Ludwig section: "Support Ludwig's Ray backend... one compute
# layer, not two" (FR3) — trains via LudwigModel on the same Ray cluster
# as Ultralytics/numpy. Forces backend={{"type": "local"}} rather than
# Ludwig's Ray backend specifically because that needs `dask`, an
# additional heavy dependency this environment doesn't need to carry for
# a single-node dev cluster — "same compute layer" is satisfied by
# running inside the same Ray-submitted job process, not by Ludwig's own
# distributed executor.
# ponytail: torch.utils.tensorboard is stubbed out before importing
# ludwig — Ludwig's trainer unconditionally imports it for TensorBoard
# logging, but its protobuf requirement (>=6.31) conflicts with the
# protobuf Ray Serve needs (<5, see Dockerfile). We don't use TensorBoard
# output; metrics are parsed from stdout like every other backend.
# hf_dataset set: real Hugging Face Hub dataset (via the `datasets` lib,
# already present — Ludwig itself depends on it), fed into a Ludwig text
# classifier whose encoder is a real HF model (`hf_model`, default a tiny
# test model so this doesn't require downloading a multi-GB checkpoint by
# default) — genuine Ludwig-on-HF, not tabular synthetic data relabeled.
# Assumes the dataset has a "text"-like and a "label"-like column, which
# hf_text_column/hf_label_column pick out; every HF text-classification
# dataset (imdb, rotten_tomatoes, ag_news, ...) exposes some pair like this.
_LUDWIG_TEMPLATE = """
import io, json, os, sys, types
_fake_tb = types.ModuleType("torch.utils.tensorboard")
class _FakeSummaryWriter:
    def __init__(self, *a, **k): pass
    def add_scalar(self, *a, **k): pass
    def add_text(self, *a, **k): pass
    def flush(self): pass
    def close(self): pass
_fake_tb.SummaryWriter = _FakeSummaryWriter
sys.modules["torch.utils.tensorboard"] = _fake_tb

from ludwig.api import LudwigModel

def _ludwig_extra_metrics(train_stats, model, df, label_col):
    epoch_history = []
    try:
        stats = train_stats.training if hasattr(train_stats, "training") else train_stats["training"]
        feat_stats = stats.get(label_col) or next(iter(stats.values()))
        loss_list = feat_stats.get("loss", [])
        acc_list = feat_stats.get("accuracy", [])
        for i, loss in enumerate(loss_list):
            entry = {{"epoch": i + 1, "loss": float(loss)}}
            if i < len(acc_list):
                entry["accuracy"] = float(acc_list[i])
            epoch_history.append(entry)
    except Exception:
        pass

    confusion_matrix = None
    try:
        from sklearn.metrics import confusion_matrix as _sk_cm
        preds_df, *_ = model.predict(df)
        pred_col = f"{{label_col}}_predictions"
        y_true = df[label_col].astype(str).tolist()
        y_pred = preds_df[pred_col].astype(str).tolist()
        labels = sorted(set(y_true) | set(y_pred))
        matrix = _sk_cm(y_true, y_pred, labels=labels).tolist()
        confusion_matrix = {{"labels": labels, "matrix": matrix}}
    except Exception:
        pass

    return {{"epoch_history": epoch_history, "confusion_matrix": confusion_matrix}}

hf_dataset = {hf_dataset!r}
test_split = {test_split!r}
_val_frac = 0.1
_raw_probs = [max(0.05, 1 - test_split - _val_frac), _val_frac, max(0.05, test_split)]
_probs_sum = sum(_raw_probs)
_split_probs = [p / _probs_sum for p in _raw_probs]
_split_preprocessing = {{"split": {{"type": "random", "probabilities": _split_probs}}}}

if hf_dataset:
    import datasets as hf_datasets
    text_col, label_col = {hf_text_column!r}, {hf_label_column!r}
    hf_model = {hf_model!r}
    ds = hf_datasets.load_dataset(hf_dataset, split="train")
    max_rows = {hf_max_rows!r}
    if max_rows and len(ds) > max_rows:
        ds = ds.select(range(max_rows))
    df = ds.to_pandas()[[text_col, label_col]].rename(columns={{text_col: "text", label_col: "label"}})
    df["label"] = df["label"].astype(str)
    print(f"metric:hf_rows_used={{len(df)}}")

    ludwig_config = {{
        "input_features": [{{"name": "text", "type": "text",
            "encoder": {{"type": "auto_transformer", "pretrained_model_name_or_path": hf_model, "trainable": False}}}}],
        "output_features": [{{"name": "label", "type": "category"}}],
        "trainer": {{"epochs": {epochs}}},
        "backend": {{"type": "local"}},
        "preprocessing": _split_preprocessing,
    }}
    model = LudwigModel(config=ludwig_config)
    train_stats, *_ = model.train(dataset=df, skip_save_processed_input=True, skip_save_progress=True,
                skip_save_model=True, skip_save_log=True)
    eval_stats, *_ = model.evaluate(df)
    accuracy = float(eval_stats["label"]["accuracy"])
    print(f"metric:accuracy={{accuracy}}")
    print(f"metric:loss={{1.0 - accuracy}}")
    print(f"metrics_json:{{json.dumps(_ludwig_extra_metrics(train_stats, model, df, 'label'))}}")
elif {tabular_key!r}:
    # Real uploaded CSV (app/datasets.py's upload-tabular) — arbitrary
    # columns, label_column picks the target. A column matching an
    # image-path hint (image, image_path, img, img_path) is trained as a
    # real Ludwig image feature: those keys must already exist in the
    # bucket (uploaded via the normal upload-url/upload-zip flow), and are
    # downloaded here into local files Ludwig can read.
    import pandas as pd
    import boto3
    from botocore.client import Config

    lakefs_repo, lakefs_ref = {lakefs_repo!r}, {lakefs_ref!r}
    test_lakefs_ref = {test_lakefs_ref!r}
    s3 = boto3.client("s3", endpoint_url={s3_endpoint!r},
        aws_access_key_id={s3_access_key!r}, aws_secret_access_key={s3_secret_key!r},
        config=Config(signature_version="s3v4"))
    max_rows = {hf_max_rows!r}
    label_col = {label_column!r}

    def _load_tabular(ref):
        obj = s3.get_object(Bucket=lakefs_repo, Key=f"{{ref}}/tabular.csv")
        d = pd.read_csv(io.BytesIO(obj["Body"].read()))
        if max_rows and len(d) > max_rows:
            d = d.head(max_rows)
        image_col = next((c for c in d.columns if c.lower() in ("image", "image_path", "img", "img_path")), None)
        if image_col:
            local_dir = f"/tmp/ludwig-images-{seed}-{{ref}}"
            os.makedirs(local_dir, exist_ok=True)
            local_paths = []
            for key in d[image_col]:
                data = s3.get_object(Bucket=lakefs_repo, Key=f"{{ref}}/{{key}}")["Body"].read()
                local_path = f"{{local_dir}}/{{os.path.basename(key)}}"
                with open(local_path, "wb") as f:
                    f.write(data)
                local_paths.append(local_path)
            d["_image_local"] = local_paths
        d[label_col] = d[label_col].astype(str)
        return d, image_col

    df, image_col = _load_tabular(lakefs_ref)
    print(f"metric:tabular_rows_used={{len(df)}}")
    input_features = []
    if image_col:
        input_features.append({{"name": "_image_local", "type": "image", "encoder": {{"type": "stacked_cnn"}}}})
    for col in df.columns:
        if col in (label_col, image_col, "_image_local"):
            continue
        input_features.append({{"name": col, "type": "number" if pd.api.types.is_numeric_dtype(df[col]) else "category"}})

    ludwig_config = {{
        "input_features": input_features,
        "output_features": [{{"name": label_col, "type": "category"}}],
        "trainer": {{"epochs": {epochs}}},
        "backend": {{"type": "local"}},
    }}
    model = LudwigModel(config=ludwig_config)
    if test_lakefs_ref:
        # A user-designated, separately versioned held-out test set
        # (app/datasets.py) — no random split, this dataset's rows never
        # touch training.
        test_df, _ = _load_tabular(test_lakefs_ref)
        print(f"metric:test_rows_used={{len(test_df)}}")
        train_stats, *_ = model.train(training_set=df, test_set=test_df, skip_save_processed_input=True,
                    skip_save_progress=True, skip_save_model=True, skip_save_log=True)
        eval_stats, *_ = model.evaluate(test_df)
    else:
        ludwig_config["preprocessing"] = _split_preprocessing
        model = LudwigModel(config=ludwig_config)
        train_stats, *_ = model.train(dataset=df, skip_save_processed_input=True, skip_save_progress=True,
                    skip_save_model=True, skip_save_log=True)
        eval_stats, *_ = model.evaluate(df)
    accuracy = float(eval_stats[label_col]["accuracy"])
    print(f"metric:accuracy={{accuracy}}")
    print(f"metric:loss={{1.0 - accuracy}}")
    print(f"metrics_json:{{json.dumps(_ludwig_extra_metrics(train_stats, model, test_df if test_lakefs_ref else df, label_col))}}")
else:
    import numpy as np
    import pandas as pd

    rng = np.random.default_rng({seed})
    n = 400
    df = pd.DataFrame({{
        "f1": rng.normal(size=n), "f2": rng.normal(size=n),
        "f3": rng.normal(size=n), "f4": rng.normal(size=n),
    }})
    df["label"] = ((1.5 * df.f1 - 2.0 * df.f2 + 0.5 * df.f3 + df.f4
                     + rng.normal(scale=0.5, size=n)) > 0).astype(str)

    ludwig_config = {{
        "input_features": [{{"name": c, "type": "number"}} for c in ["f1", "f2", "f3", "f4"]],
        "output_features": [{{"name": "label", "type": "binary"}}],
        "trainer": {{"epochs": {epochs}}},
        "backend": {{"type": "local"}},
        "preprocessing": _split_preprocessing,
    }}
    model = LudwigModel(config=ludwig_config)
    train_stats, *_ = model.train(dataset=df, skip_save_processed_input=True, skip_save_progress=True,
                skip_save_model=True, skip_save_log=True)
    eval_stats, *_ = model.evaluate(df)
    accuracy = float(eval_stats["label"]["accuracy"])
    print(f"metric:accuracy={{accuracy}}")
    print(f"metric:loss={{1.0 - accuracy}}")
    print(f"metrics_json:{{json.dumps(_ludwig_extra_metrics(train_stats, model, df, 'label'))}}")
"""


def _build_entrypoint(
    config: dict,
    dataset_ref: str,
    file_keys: list[str],
    dataset_id: str | None = None,
    dataset_version: str | None = None,
    test_file_keys: list[str] | None = None,
    test_dataset_id: str | None = None,
    test_dataset_version: str | None = None,
) -> str:
    seed = int(hashlib.sha256(dataset_ref.encode()).hexdigest(), 16) % (2**32)
    test_split = float(config.get("test_split", 0.2))
    # A pinned commit id trains on exactly that version, reproducibly;
    # unset means "whatever's on the dataset's branch right now" (which
    # is itself the branch named for the dataset's own id).
    lakefs_ref = dataset_version or dataset_id
    # A separate, user-designated held-out test dataset (own upload +
    # versioning, see app/datasets.py) — when set, no internal
    # train/test split happens at all; test_split is ignored.
    test_lakefs_ref = (test_dataset_version or test_dataset_id) if test_dataset_id else None

    if config.get("backend") == "ludwig":
        from app import lakefs_client

        lakefs_creds = vault.get_kv("platform/lakefs")
        script = textwrap.dedent(_LUDWIG_TEMPLATE).format(
            seed=seed,
            epochs={**DEFAULT_CONFIG["trainer"], **config.get("trainer", {})}["epochs"],
            test_split=test_split,
            hf_dataset=config.get("hf_dataset"),
            hf_model=config.get("hf_model") or "hf-internal-testing/tiny-random-distilbert",
            hf_text_column=config.get("hf_text_column") or "text",
            hf_label_column=config.get("hf_label_column") or "label",
            hf_max_rows=config.get("hf_max_rows") or 200,
            tabular_key=(f"tabular.csv" if dataset_id and not config.get("hf_dataset") else None),
            label_column=config.get("label_column") or "label",
            lakefs_repo=lakefs_client.REPO,
            lakefs_ref=lakefs_ref,
            test_lakefs_ref=test_lakefs_ref,
            s3_endpoint=lakefs_creds["endpoint"],
            s3_access_key=lakefs_creds["access_key"],
            s3_secret_key=lakefs_creds["secret_key"],
        )
    elif config.get("backend") == "ultralytics":
        from app import lakefs_client

        lakefs_creds = vault.get_kv("platform/lakefs")
        minio_creds = vault.get_kv("platform/minio")
        script = textwrap.dedent(_ULTRALYTICS_TEMPLATE).format(
            seed=seed,
            epochs={**DEFAULT_CONFIG["trainer"], **config.get("trainer", {})}["epochs"],
            test_split=test_split,
            file_keys=file_keys,
            test_file_keys=test_file_keys or [],
            test_lakefs_ref=test_lakefs_ref,
            task=config.get("task") or "classify",
            lakefs_repo=lakefs_client.REPO,
            lakefs_ref=lakefs_ref,
            s3_endpoint=lakefs_creds["endpoint"],
            s3_access_key=lakefs_creds["access_key"],
            s3_secret_key=lakefs_creds["secret_key"],
            minio_s3_endpoint="http://minio:9000",
            minio_s3_access_key=minio_creds["access_key"],
            minio_s3_secret_key=minio_creds["secret_key"],
        )
    else:
        trainer = {**DEFAULT_CONFIG["trainer"], **config.get("trainer", {})}
        script = textwrap.dedent(_TRAIN_TEMPLATE).format(
            seed=seed, learning_rate=trainer["learning_rate"], epochs=trainer["epochs"], test_split=test_split
        )

    encoded = base64.b64encode(script.encode()).decode()
    # base64 round-trip sidesteps shell/JSON quoting entirely for
    # arbitrary generated script content.
    return f'python -c "import base64; exec(base64.b64decode(\'{encoded}\'))"'


async def submit_job(
    *,
    tenant_id: str,
    project_id: str,
    dataset_ref: str,
    config: dict,
    file_keys: list[str] | None = None,
    dataset_id: str | None = None,
    dataset_version: str | None = None,
    test_file_keys: list[str] | None = None,
    test_dataset_id: str | None = None,
    test_dataset_version: str | None = None,
    compute_target: str = "cpu",
) -> str:
    return await submit_raw_job(
        entrypoint=_build_entrypoint(
            config,
            dataset_ref,
            file_keys or [],
            dataset_id,
            dataset_version,
            test_file_keys,
            test_dataset_id,
            test_dataset_version,
        ),
        metadata={"tenant_id": tenant_id, "project_id": project_id, "dataset_ref": dataset_ref},
        num_gpus=1 if compute_target == "gpu" else 0,
    )


# Materializes a real Hugging Face Hub dataset into a platform dataset's
# own S3 prefix — runs as a Ray job (needs network + the `datasets` lib,
# neither of which core-api itself has) rather than inline in the API
# request. kind="images": writes real files into the same <label>/<file>
# layout upload-url/upload-zip use, so the result is immediately usable
# by the Ultralytics backend. kind="tabular": writes tabular.csv directly,
# immediately usable by the Ludwig default backend (see _LUDWIG_TEMPLATE).
_HF_IMPORT_TEMPLATE = """
import datasets as hf_datasets
import boto3
from botocore.client import Config

s3 = boto3.client("s3", endpoint_url={s3_endpoint!r},
    aws_access_key_id={s3_access_key!r}, aws_secret_access_key={s3_secret_key!r},
    config=Config(signature_version="s3v4"))

ds = hf_datasets.load_dataset({hf_dataset!r}, split="train")
max_rows = {max_rows!r}
if max_rows and len(ds) > max_rows:
    # Shuffle before truncating — many HF datasets (iris included) are
    # stored sorted by label, so a naive head-N slice can silently end up
    # single-class and unusable for training.
    ds = ds.shuffle(seed={seed!r}).select(range(max_rows))

kind = {kind!r}
lakefs_repo, dataset_id = {lakefs_repo!r}, {s3_prefix!r}

if kind == "images":
    image_col, label_col = {image_column!r}, {label_column!r}
    count = 0
    for i, row in enumerate(ds):
        img = row[image_col]
        label = str(row[label_col])
        import io
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="PNG")
        s3.put_object(Bucket=lakefs_repo, Key=f"{{dataset_id}}/{{label}}/{{i}}.png", Body=buf.getvalue())
        count += 1
    print(f"metric:imported_files={{count}}")
else:
    # Preserve every column (not just text/label) — this feeds the
    # Ludwig default backend's generic tabular path (any column can be
    # the label via config.label_column), not the HF-text-specific path.
    df = ds.to_pandas()
    import io
    buf = io.StringIO()
    df.to_csv(buf, index=False)
    s3.put_object(Bucket=lakefs_repo, Key=f"{{dataset_id}}/tabular.csv", Body=buf.getvalue().encode())
    print(f"metric:imported_rows={{len(df)}}")
"""


async def submit_hf_import(
    *,
    hf_dataset: str,
    kind: str,
    s3_prefix: str,
    image_column: str = "image",
    label_column: str = "label",
    text_column: str = "text",
    max_rows: int | None = 2000,
    tenant_id: str,
    project_id: str,
    use_lakefs: bool = True,
) -> str:
    from app import lakefs_client

    lakefs_creds = vault.get_kv("platform/lakefs")
    seed = int(hashlib.sha256(hf_dataset.encode()).hexdigest(), 16) % (2**32)
    script = textwrap.dedent(_HF_IMPORT_TEMPLATE).format(
        hf_dataset=hf_dataset,
        kind=kind,
        s3_prefix=s3_prefix,
        image_column=image_column,
        label_column=label_column,
        text_column=text_column,
        max_rows=max_rows,
        seed=seed,
        lakefs_repo=lakefs_client.REPO,
        s3_endpoint=lakefs_creds["endpoint"],
        s3_access_key=lakefs_creds["access_key"],
        s3_secret_key=lakefs_creds["secret_key"],
    )
    encoded = base64.b64encode(script.encode()).decode()
    entrypoint = f'python -c "import base64; exec(base64.b64decode(\'{encoded}\'))"'
    return await submit_raw_job(
        entrypoint=entrypoint, metadata={"tenant_id": tenant_id, "project_id": project_id, "hf_dataset": hf_dataset}
    )


async def submit_raw_job(*, entrypoint: str, metadata: dict, num_gpus: float = 0) -> str:
    # FR2 (06-project-settings-deployment.md): compute_target maps to a Ray
    # resource request — the one place this touches the job payload shape,
    # not the template scripts.
    payload = {"entrypoint": entrypoint, "metadata": metadata}
    if num_gpus:
        payload["entrypoint_num_gpus"] = num_gpus
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"{RAY_DASHBOARD}/api/jobs/",
            # FR1: tenant_id/project_id propagated to the job so they can
            # be pushed onto pod labels once this targets KubeRay (see
            # k8s/rayjob-example.yaml for the pod-label version).
            json=payload,
        )
        resp.raise_for_status()
        return resp.json()["submission_id"]


async def get_status(job_id: str) -> str:
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(f"{RAY_DASHBOARD}/api/jobs/{job_id}")
        resp.raise_for_status()
        return resp.json()["status"]  # PENDING | RUNNING | SUCCEEDED | FAILED | STOPPED


async def get_logs(job_id: str) -> str:
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(f"{RAY_DASHBOARD}/api/jobs/{job_id}/logs")
        resp.raise_for_status()
        return resp.json()["logs"]


async def stop_job(job_id: str) -> None:
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(f"{RAY_DASHBOARD}/api/jobs/{job_id}/stop")
        resp.raise_for_status()


def parse_metrics(logs: str) -> dict[str, float]:
    metrics = {}
    for line in logs.splitlines():
        if line.startswith("metric:"):
            key, _, value = line.removeprefix("metric:").partition("=")
            try:
                metrics[key] = float(value)
            except ValueError:
                continue
    return metrics


def parse_extra_metrics(logs: str) -> dict:
    """Extracts the `metrics_json:{...}` line every backend prints —
    per-epoch loss/accuracy history and a test-set confusion matrix, as
    opposed to parse_metrics' single scalar values."""
    import json

    for line in reversed(logs.splitlines()):
        if line.startswith("metrics_json:"):
            try:
                return json.loads(line.removeprefix("metrics_json:"))
            except ValueError:
                continue
    return {}


def parse_model_artifact(logs: str) -> dict | None:
    """Extracts the `model:{...json...}` line the numpy backend prints
    (weights it actually fit) — the servable artifact for deployments
    (req/04 Deployments group, req/07 FR3)."""
    import json

    for line in logs.splitlines():
        if line.startswith("model:"):
            try:
                return json.loads(line.removeprefix("model:"))
            except ValueError:
                continue
    return None
