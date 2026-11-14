# The cu128 Ray image plus what ray_core and the task bundles import: the image of the cluster's GPU worker group (the trainer-vision
# image of docs/trainer-plan.md step 6). The base is the same Ray version as k8s apps/ray/app/raycluster.yaml (head and
# workers must run one Ray version); keep the two in step. Built by .github/workflows/ray-training-image.yml (not run yet).
# predictor-gpu (ray_serving, Ray Serve) runs on this group too: ray[serve] and onnxruntime-gpu (it replaces the CPU build
# the pyproject pulls in, which is kept for macOS and CI).
# Only dependencies are baked in: the job code itself arrives as the zip core-api stages (it wins on sys.path).
# Dependency source of truth: pyproject.toml. torch comes from the cu128 wheel index: the default PyPI wheel may target
# a newer CUDA than the driver of the 5090 node supports. Check Blackwell (sm_120) support of the pinned wheel before use.
ARG BASE=rayproject/ray:2.60.0.8f2cbc-py311-cu128@sha256:76e91000ac4eab6b53c5d796bc62ad61a519ba10c6a3bc2411eb651d8f5d067a
FROM ${BASE}

COPY --chown=ray:users pyproject.toml /tmp/ray-training/pyproject.toml
# One resolve, not an install order. The constraints hold the two things that used to depend on the order: Ray stays at the
# base image's version, and protobuf stays below 6 (ray[serve] pins protobuf 5.x, the newest onnx needs 6.31+, so pip settles on an
# onnx that works with 5.x; installed the other way round onnx pulled protobuf 7, Ray downgraded it and `import onnx` failed with a
# gencode/runtime version error, reproduced in compose/ray-head/Dockerfile). The import check at the end covers onnx, so CI fails if
# this regresses. onnxruntime becomes onnxruntime-gpu in the requirements (the pyproject keeps the CPU build for macOS and CI), so
# the CPU wheel is never downloaded and swapped out.
RUN --mount=type=cache,target=/home/ray/.cache/pip,uid=1000,gid=100 \
    python -c "import ray; print('ray==' + ray.__version__)" > /tmp/ray-training/constraints.txt \
    && echo 'protobuf<6' >> /tmp/ray-training/constraints.txt \
    && python -c "import tomllib; print('\\n'.join(tomllib.load(open('/tmp/ray-training/pyproject.toml', 'rb'))['project']['dependencies']))" \
        | sed -E 's/^onnxruntime([^-]|$)/onnxruntime-gpu\1/' > /tmp/ray-training/requirements.txt \
    && pip install -c /tmp/ray-training/constraints.txt --extra-index-url https://download.pytorch.org/whl/cu128 \
        "ray[train,serve]" -r /tmp/ray-training/requirements.txt \
    && python -c "import ray.serve, ray.train.torch, torch, transformers, onnx, onnxruntime, xgboost, onnxmltools; print('torch', torch.__version__, 'cuda', torch.version.cuda, 'onnx', onnx.__version__, 'arch', torch.cuda.get_arch_list() if torch.cuda.is_available() else 'no gpu at build')" \
    && rm -rf /tmp/ray-training
