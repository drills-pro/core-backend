# Ray head/worker image with real training backends baked in (req/06):
# Ultralytics (detect/segment/classify) and Ludwig (classification/tabular)
# per the doc's backend-split table. Built once here rather than via
# per-job runtime_env pip installs, since that's the shape a real
# deployment would actually use.
#
# py311 base (not the default py39 tag): Ludwig's spacy/thinc/blis chain
# has no linux/arm64 wheels resolving cleanly against Python 3.9 in this
# environment; the py311 tag resolves and installs cleanly. Verified via
# a `pip install --dry-run ludwig` isolation test before committing to
# this switch.
FROM rayproject/ray:2.49.2-py311

USER root
RUN apt-get update && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*
USER ray

RUN pip install --no-cache-dir ultralytics pillow boto3 onnxruntime onnx ludwig \
    # ultralytics pulls numpy 2.x, which breaks the base image's
    # pandas/scipy (compiled against numpy 1.x's ABI) — pin back down.
    # ludwig pulls protobuf 7.x, which breaks Ray Serve's internal proto
    # handling (AttributeError on FieldDescriptor.label) — pin back down
    # to what Ray 2.49 actually ships against. This puts protobuf below
    # what Ludwig's tensorboard dependency wants; that's fine because the
    # Ludwig entrypoint stubs out torch.utils.tensorboard before import
    # (see app/ray_client.py) — we don't use TensorBoard logging, metrics
    # are parsed from stdout like every other backend.
    && pip install --no-cache-dir "numpy<2" "protobuf<5" \
    # Ray's base image ships an ancient fsspec (2023.5.0) that predates the
    # HfFileSystem.glob() API huggingface_hub 1.x calls (KeyError:
    # 'maxdepth') — needed for the Ludwig hf_dataset path (app/ray_client.py)
    # to actually load a real dataset from the Hub instead of failing.
    && pip install --no-cache-dir -U "fsspec>=2024.2.0"

# Pre-fetch the base checkpoints (classify + detect) so job submissions
# don't pay a network fetch on every run.
RUN python -c "from ultralytics import YOLO; YOLO('yolo11n-cls.pt'); YOLO('yolo11n.pt')"
