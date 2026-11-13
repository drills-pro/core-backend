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
# Ray first, then the rest under a protobuf<6 constraint: ray[serve] pins protobuf 5.x, and the newest onnx needs protobuf 6.31+. Installed in
# the other order, onnx pulls protobuf 7, Ray downgrades it, and `import onnx` fails with a gencode/runtime version error, which breaks the
# ONNX export (reproduced building the same dependency set for the local compose Ray head, compose/ray-head/Dockerfile). The build-time import check below
# now includes onnx, so CI fails if this regresses; earlier CI builds passed because that check did not import it.
RUN echo 'protobuf<6' > /tmp/ray-training/constraints.txt \
    && python -c "import tomllib; print('\\n'.join(tomllib.load(open('/tmp/ray-training/pyproject.toml', 'rb'))['project']['dependencies']))" \
        > /tmp/ray-training/requirements.txt \
    && pip install --no-cache-dir "ray[train,serve]==$(python -c 'import ray; print(ray.__version__)')" \
    && pip install --no-cache-dir -c /tmp/ray-training/constraints.txt --extra-index-url https://download.pytorch.org/whl/cu128 -r /tmp/ray-training/requirements.txt \
    && pip uninstall -y onnxruntime && pip install --no-cache-dir -c /tmp/ray-training/constraints.txt onnxruntime-gpu \
    && python -c "import ray.serve, ray.train.torch, torch, transformers, onnx, onnxruntime; print('torch', torch.__version__, 'cuda', torch.version.cuda, 'onnx', onnx.__version__, 'arch', torch.cuda.get_arch_list() if torch.cuda.is_available() else 'no gpu at build')" \
    && rm -rf /tmp/ray-training
