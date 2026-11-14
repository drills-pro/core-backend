# The CPU Ray image plus what the ray_jobs entrypoints import: the image of the cluster's CPU worker group, which runs
# the ingest jobs. The base is the same Ray version as k8s apps/ray/app/raycluster.yaml (head and workers must run one
# Ray version); keep the two in step.
# Only dependencies are baked in: the job code itself still arrives as the zip core-api stages (it wins on sys.path),
# so a job-code change needs no image rebuild. Dependency source of truth: pyproject.toml, except the two lines below it:
# the training driver runs on this group too and starts Ray Train, whose TorchTrainer imports torch (CPU wheel is enough);
# predictor-cpu (ray_serving, Ray Serve) runs here as well, hence ray[serve] and onnxruntime.
# BASE is the amd64 image of the cluster; docker-compose.yml overrides it with the arm64 build for Apple Silicon.
ARG BASE=rayproject/ray:2.60.0.8f2cbc-py311-cpu@sha256:64f1f57a8c547f7582fe985437f083bfb72f63d6c54987ecad98f9505a380ceb
FROM ${BASE}

COPY --chown=ray:users pyproject.toml /tmp/ray-jobs/pyproject.toml
# torch first, from the CPU wheel index only (the PyPI wheel on Linux carries the CUDA libraries, gigabytes this group never uses).
# Then one resolve for the rest: the constraints keep Ray at the base image's version and protobuf below 6 (ray[serve] pins protobuf 5.x,
# the newest onnx needs 6.31+, so pip settles on an onnx that works with 5.x; the install-order version of this fix is in git history).
RUN --mount=type=cache,target=/home/ray/.cache/pip,uid=1000,gid=100 \
    python -c "import ray; print('ray==' + ray.__version__)" > /tmp/ray-jobs/constraints.txt \
    && echo 'protobuf<6' >> /tmp/ray-jobs/constraints.txt \
    && python -c "import tomllib; print('\\n'.join(tomllib.load(open('/tmp/ray-jobs/pyproject.toml', 'rb'))['project']['dependencies']))" \
        > /tmp/ray-jobs/requirements.txt \
    && pip install -c /tmp/ray-jobs/constraints.txt --index-url https://download.pytorch.org/whl/cpu torch \
    && pip install -c /tmp/ray-jobs/constraints.txt "ray[train,serve]" -r /tmp/ray-jobs/requirements.txt \
    && python -c "import ray.serve, ray.train.torch, torch, boto3, pyarrow, PIL, onnx, onnxruntime, tokenizers; print('boto3', boto3.__version__, 'pyarrow', pyarrow.__version__, 'Pillow', PIL.__version__, 'onnx', onnx.__version__)" \
    && rm -rf /tmp/ray-jobs
