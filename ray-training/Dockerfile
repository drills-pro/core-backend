# The cu128 Ray image plus what ray_training imports: the image of the cluster's GPU worker group (the trainer-vision
# image of docs/trainer-plan.md step 6). The base is the same Ray version as k8s apps/ray/app/raycluster.yaml (head and
# workers must run one Ray version); keep the two in step. NOT BUILT OR TESTED yet.
# Only dependencies are baked in: the job code itself arrives as the zip core-api stages (it wins on sys.path).
# Dependency source of truth: pyproject.toml. torch comes from the cu128 wheel index: the default PyPI wheel may target
# a newer CUDA than the driver of the 5090 node supports. Check Blackwell (sm_120) support of the pinned wheel before use.
ARG BASE=rayproject/ray:2.60.0.8f2cbc-py311-cu128@sha256:76e91000ac4eab6b53c5d796bc62ad61a519ba10c6a3bc2411eb651d8f5d067a
FROM ${BASE}

COPY --chown=ray:users pyproject.toml /tmp/ray-training/pyproject.toml
RUN python -c "import tomllib; print('\n'.join(tomllib.load(open('/tmp/ray-training/pyproject.toml', 'rb'))['project']['dependencies']))" \
        > /tmp/ray-training/requirements.txt \
    && pip install --no-cache-dir --extra-index-url https://download.pytorch.org/whl/cu128 -r /tmp/ray-training/requirements.txt \
    && python -c "import torch, transformers, onnxruntime; print('torch', torch.__version__, 'cuda', torch.version.cuda, 'arch', torch.cuda.get_arch_list() if torch.cuda.is_available() else 'no gpu at build')" \
    && rm -rf /tmp/ray-training
